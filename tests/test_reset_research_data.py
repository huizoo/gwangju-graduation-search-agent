"""격리 DB에서 초기화의 보존·백업·실패 복구를 검사한다."""

from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.records import reset_data
from app.core.config import ROOT
from app.launcher import InstanceLock
from app.records.storage import Service, Store
from test_storage import event, registry_payload


@pytest.fixture
def prepared(tmp_path):
    """수기·공식 명부·작업·원문을 가진 검사 전용 저장소를 만든다.

    Args:
        tmp_path: pytest 격리 경로.
    Returns:
        데이터 폴더와 서비스 및 초기 테이블 상태.
    """
    directory = tmp_path / "live"
    service = Service(Store(directory / "graduation.sqlite3"))
    current, season, institution = event(service)
    preview = service.registry_preview(registry_payload())
    service.registry_commit(preview["id"], {
        "expected_preview_hash": preview["preview_hash"],
        "expected_content_revision": preview["content_revision"], "allow_partial": False,
        "mappings": [{"row_index": 0, "decision": "update", "institution_id": institution["id"]}],
    }, "registry-key")
    with service.store.transaction(True) as conn:
        conn.execute("INSERT INTO app_meta VALUES('selected_season_id',?)", (season["id"],))
        conn.execute("INSERT INTO app_meta VALUES('preserved_setting','unchanged')")
        conn.execute("""INSERT INTO jobs(id,kind,season_id,status,request_json,scope_snapshot_json,
                     limits_json,usage_json,target_count,idempotency_key,request_hash,created_at,updated_at)
                     VALUES('job','schedule_scan',?,'paused','{}','{}','{}','{}',1,'job-key','hash','now','now')""",
                     (season["id"],))
        conn.execute("""INSERT INTO job_items(id,job_id,target_ref,institution_id,status,stage,usage_json)
                     VALUES('item','job','target',?,'failed','read','{}')""", (institution["id"],))
        conn.execute("""INSERT INTO natural_records(id,season_id,turns_json,state_json,created_at,updated_at)
                     VALUES('record',?,'["검사 전용 수기"]','{}','now','now')""", (season["id"],))
        conn.execute("""INSERT INTO documents(id,institution_id,source_key,display_url,title,authority,
                     source_kind,status,retrieved_at,segments_json)
                     VALUES('document',?,'source','https://example.test/notice','원문','official',
                     'html','ok','now','[{"text":"보존할 원문"}]')""", (institution["id"],))
        conn.execute("""INSERT INTO observations(id,institution_id,season_id,event_id,document_id,
                     candidate_json,fingerprint,created_at) VALUES('observation',?,?,?,'document','{}','unique','now')""",
                     (institution["id"], season["id"], current["id"]))
        conn.execute("""INSERT INTO count_observations VALUES('count',?,?,?,'document','{}','{}','count-hash','now')""",
                     (institution["id"], season["id"], current["id"]))
        conn.execute("""INSERT INTO collection_routes(id,institution_id,url,purpose,reading_method,document_id)
                     VALUES('route',?,'https://example.test/notice','time','html','document')""", (institution["id"],))
        conn.execute("INSERT INTO route_attempts VALUES('attempt','route',?,'{}','now')", (institution["id"],))
        conn.execute("INSERT INTO investigation_traces VALUES('trace',?,?,'time',?,'{}','now')",
                     (institution["id"], season["id"], current["id"]))
        before = reset_data._table_state(conn)
    for folder in ("exports/versions/previous", "backups", "archives"):
        (directory / folder).mkdir(parents=True, exist_ok=True)
    (directory / "exports/old.html").write_text("이전 일정표", encoding="utf-8")
    (directory / "exports/versions/previous/old.html").write_text("이전 판본", encoding="utf-8")
    (directory / "backups/existing.sqlite3").write_bytes(b"untouched existing backup")
    (directory / "archives/development.json").write_text("{}", encoding="utf-8")
    return directory, service, before


def test_reset_keeps_roster_and_full_backup_but_clears_user_research(prepared):
    """학교 정보는 보존하고 원문·수기·작업을 검증된 백업으로 분리한다."""
    directory, service, before = prepared
    result = reset_data.reset_research_data(directory, confirmed=True)
    backup = Path(result["backup_database"])
    assert result["status"] == "completed"
    assert result["export_files_archived"] == 2
    with closing(reset_data._connect(backup, readonly=True)) as saved:
        assert reset_data._table_state(saved) == before
        assert saved.execute("SELECT segments_json FROM documents").fetchone()[0] == '[{"text":"보존할 원문"}]'
        assert saved.execute("SELECT count(*) FROM manual_overrides").fetchone()[0] == 3
    with service.store.transaction() as conn:
        after = reset_data._table_state(conn)
        assert all(after[table] == before[table] for table in reset_data.PRESERVED_TABLES)
        assert all(after[table]["count"] == 0 for table in reset_data.RESET_TABLES)
        assert conn.execute("SELECT value FROM app_meta WHERE key='preserved_setting'").fetchone()[0] == "unchanged"
        assert not conn.execute("SELECT 1 FROM app_meta WHERE key='selected_season_id'").fetchone()
        reset_data._integrity(conn)
    assert not (directory / "exports").exists()
    assert (backup.parent / "exports/old.html").read_text() == "이전 일정표"
    assert (directory / "backups/existing.sqlite3").read_bytes() == b"untouched existing backup"
    assert (directory / "archives/development.json").read_text() == "{}"
    assert json.loads((backup.parent / "reset-receipt.json").read_text())["after_tables"] == after
    reopened = Service(Store(directory / "graduation.sqlite3"))
    assert reopened.list_seasons()["total"] == 0
    assert reopened.list_institutions()["total"] == 1
    period = reopened.create_season({"search_start": "2025-12-01", "search_end": "2026-03-31"})
    snapshot = reopened.snapshot(period["id"])
    assert not snapshot["events"]
    assert snapshot["request_job_id"] is None
    assert snapshot["research_summary"]["state"] == "not_started"


def test_confirmation_and_running_instance_block_any_reset(prepared):
    """명시적 확인이 없거나 서버 잠금이 있으면 백업도 만들지 않는다."""
    directory, service, before = prepared
    with pytest.raises(reset_data.ResetError, match="confirm-reset"):
        reset_data.reset_research_data(directory)
    lock = InstanceLock(directory / "instance.lock")
    assert lock.acquire()
    try:
        with pytest.raises(reset_data.ResetError, match="실행 중"):
            reset_data.reset_research_data(directory, confirmed=True)
    finally:
        lock.close()
    with service.store.transaction() as conn:
        assert reset_data._table_state(conn) == before
    assert not list((directory / "backups").glob("research-reset-*"))


def test_failed_backup_leaves_database_and_exports_unchanged(prepared, monkeypatch):
    """백업 저장 실패 시 원본 DB를 쓰기 연결로 열지 않는다."""
    directory, service, before = prepared
    original_hash = hashlib.sha256(service.store.path.read_bytes()).hexdigest()

    def fail_backup(source, destination):
        raise OSError("검사 전용 디스크 실패")

    monkeypatch.setattr(reset_data, "_backup", fail_backup)
    with pytest.raises(OSError):
        reset_data.reset_research_data(directory, confirmed=True)
    assert hashlib.sha256(service.store.path.read_bytes()).hexdigest() == original_hash
    with service.store.transaction() as conn:
        assert reset_data._table_state(conn) == before
    assert (directory / "exports/old.html").read_text() == "이전 일정표"


@pytest.mark.parametrize("keep_seasons", [False, True])
def test_failed_artifact_validation_restores_db_and_exports(prepared, monkeypatch, keep_seasons):
    """HTML 이동 후 검증 실패도 DB 삭제와 파일 이동을 함께 되돌린다."""
    directory, service, before = prepared
    real_state = reset_data._artifact_state

    def changed_artifact(path):
        result = real_state(path)
        if path.parent.name.startswith("research-reset-"):
            return {"unexpected": "changed"}
        return result

    monkeypatch.setattr(reset_data, "_artifact_state", changed_artifact)
    with pytest.raises(reset_data.ResetError, match="일정표 파일 백업"):
        reset_data.reset_research_data(directory, confirmed=True, keep_seasons=keep_seasons)
    with service.store.transaction() as conn:
        assert reset_data._table_state(conn) == before
    assert (directory / "exports/old.html").read_text() == "이전 일정표"
    assert not list((directory / "backups").glob("research-reset-*/reset-receipt.json"))


def test_unknown_schema_is_not_silently_deleted(prepared):
    """향후 추가된 테이블의 의미를 모르면 자동 삭제하지 않는다."""
    directory, service, _ = prepared
    with service.store.transaction(True) as conn:
        conn.execute("CREATE TABLE unknown_future_settings(value TEXT)")
        conn.execute("INSERT INTO unknown_future_settings VALUES('보존')")
        before = reset_data._table_state(conn)
    with pytest.raises(reset_data.ResetError, match="테이블 구성"):
        reset_data.reset_research_data(directory, confirmed=True)
    with service.store.transaction() as conn:
        assert reset_data._table_state(conn) == before


def test_no_database_is_not_created_and_backup_never_overwrites(prepared, tmp_path):
    """없는 DB와 기존 백업 이름을 안전하게 거부한다."""
    directory, service, _ = prepared
    missing = tmp_path / "missing"
    with pytest.raises(reset_data.ResetError, match="DB 파일"):
        reset_data.reset_research_data(missing, confirmed=True)
    assert not missing.exists()
    existing = directory / "backups/existing.sqlite3"
    with pytest.raises(FileExistsError):
        reset_data._backup(service.store.path, existing)
    assert existing.read_bytes() == b"untouched existing backup"


def test_repeated_resets_keep_distinct_complete_backups(prepared):
    """빈 기록을 다시 초기화해도 이전 백업을 덮어쓰지 않는다."""
    directory, _, _ = prepared
    first = reset_data.reset_research_data(directory, confirmed=True)
    first_bytes = Path(first["backup_database"]).read_bytes()
    second = reset_data.reset_research_data(directory, confirmed=True)
    assert first["backup_database"] != second["backup_database"]
    assert Path(first["backup_database"]).read_bytes() == first_bytes
    assert all(count == 0 for count in second["cleared_counts"].values())
    assert second["preserved_counts"]["institutions"] == 1


def test_preservation_failure_rolls_back_deletes(prepared):
    """예상하지 못한 트리거가 명부를 바꾸면 보존 검사로 전체 삭제를 취소한다."""
    directory, service, _ = prepared
    with service.store.transaction(True) as conn:
        conn.execute("""CREATE TRIGGER unexpected_change AFTER DELETE ON events
                     BEGIN UPDATE institutions SET address='의도하지 않은 변경'; END""")
        before = reset_data._table_state(conn)
    with pytest.raises(reset_data.ResetError, match="학교 목록 보존"):
        reset_data.reset_research_data(directory, confirmed=True)
    with service.store.transaction() as conn:
        assert reset_data._table_state(conn) == before
    assert (directory / "exports/old.html").read_text() == "이전 일정표"


def test_invalid_reference_blocks_backup_before_deletion(prepared):
    """기존 참조 무결성 문제를 초기화로 숨기지 않는다."""
    directory, service, _ = prepared
    with sqlite3.connect(service.store.path) as conn:
        conn.execute("UPDATE manual_overrides SET event_id='missing-event'")
    with service.store.transaction() as conn:
        before = reset_data._table_state(conn)
    with pytest.raises(reset_data.ResetError, match="참조 검사"):
        reset_data.reset_research_data(directory, confirmed=True)
    with service.store.transaction() as conn:
        assert reset_data._table_state(conn) == before
    assert (directory / "exports/old.html").read_text() == "이전 일정표"


def test_export_symlink_blocks_without_touching_target(prepared, tmp_path):
    """앱 외부로 연결된 파일은 이동하거나 읽지 않는다."""
    directory, service, before = prepared
    outside = tmp_path / "outside.txt"
    outside.write_text("외부 파일", encoding="utf-8")
    (directory / "exports/outside.txt").symlink_to(outside)
    with pytest.raises(reset_data.ResetError, match="특수 파일"):
        reset_data.reset_research_data(directory, confirmed=True)
    with service.store.transaction() as conn:
        assert reset_data._table_state(conn) == before
    assert outside.read_text() == "외부 파일"
    assert not list((directory / "backups").glob("research-reset-*"))


def test_cli_requires_confirmation_without_loading_live_settings():
    """사용자가 확인 옵션을 빠뜨리면 설정·DB를 읽기 전에 종료한다."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/reset_research_data.py")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert "--confirm-reset" in result.stderr


def test_reset_can_keep_periods_and_selection_without_retaining_research(prepared):
    """겹치는 기간도 같은 ID로 보존하고 재시작 결과에 옛 조사와 수기를 남기지 않는다."""
    directory, service, _ = prepared
    with service.store.transaction(True) as conn:
        conn.execute("""INSERT INTO seasons(id,label,academic_year,search_start,search_end,
                     export_key,timezone,region_profile,revision)
                     VALUES('00000000-0000-4000-8000-000000000001','추가 기간',NULL,
                     '2025-12-01','2026-03-26','additional-period','Asia/Seoul','gwangju_5_gu',1)""")
        before = reset_data._table_state(conn)
        selected = conn.execute("SELECT value FROM app_meta WHERE key='selected_season_id'").fetchone()[0]
    result = reset_data.reset_research_data(directory, confirmed=True, keep_seasons=True)
    assert result["kept_seasons"] is True
    assert "seasons" not in result["cleared_counts"]
    assert result["preserved_counts"]["seasons"] == 2
    with service.store.transaction() as conn:
        after = reset_data._table_state(conn)
        assert after["seasons"] == before["seasons"]
        assert all(after[table] == before[table] for table in reset_data.PRESERVED_TABLES)
        assert conn.execute("SELECT value FROM app_meta WHERE key='selected_season_id'").fetchone()[0] == selected
        assert all(after[table]["count"] == 0 for table in reset_data.RESET_TABLES if table != "seasons")
        reset_data._integrity(conn)
    with closing(reset_data._connect(Path(result["backup_database"]), readonly=True)) as conn:
        assert reset_data._table_state(conn) == before
    reopened = Service(Store(directory / "graduation.sqlite3"))
    for season in reopened.list_seasons()["items"]:
        snapshot = reopened.snapshot(season["id"])
        assert snapshot["events"] == []
        assert snapshot["request_job_id"] is None
        assert snapshot["research_summary"]["state"] == "not_started"


def reset_cli():
    """실제 설정 로드를 격리할 수 있도록 초기화 CLI 모듈을 읽는다.

    Returns:
        초기화 명령 모듈.
    """
    spec = importlib.util.spec_from_file_location("reset_cli", ROOT / "scripts/reset_research_data.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("answer", ["", "취소", "초기화 아님", None])
def test_interactive_cancel_never_loads_settings(answer, monkeypatch, capsys):
    """취소와 입력 종료는 사용자 DB나 설정에 접근하기 전에 끝난다."""
    cli = reset_cli()
    monkeypatch.setattr(sys, "argv", ["reset_research_data.py", "--interactive"])

    def respond(_):
        if answer is None:
            raise EOFError
        return answer

    def unexpected_settings():
        pytest.fail("취소 중 설정을 읽으면 안 됩니다.")

    monkeypatch.setattr("builtins.input", respond)
    monkeypatch.setattr(cli, "load_settings", unexpected_settings)
    assert cli.main() == 0
    assert "기록을 변경하지 않았습니다" in capsys.readouterr().out


@pytest.mark.parametrize("keep_seasons", [False, True])
def test_interactive_cli_resets_isolated_data_and_reports_backup(prepared, monkeypatch, capsys, keep_seasons):
    """기본 실행은 시즌도 지우고 명시적 보존 옵션에서만 기간과 선택을 유지한다."""
    directory, service, _ = prepared
    cli = reset_cli()
    arguments = ["reset_research_data.py", "--interactive"]
    if keep_seasons:
        arguments.append("--keep-seasons")
    monkeypatch.setattr(sys, "argv", arguments)
    monkeypatch.setattr("builtins.input", lambda _: "초기화")
    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace(data_dir=directory))
    assert cli.main() == 0
    output = capsys.readouterr().out
    assert "모든 시즌" in output and "직접 기록" in output
    assert "초기화를 완료했습니다" in output
    if keep_seasons:
        assert "저장된 기간과 현재 선택은 유지" in output
        assert "저장된 시즌을 골라 다시 조사" in output
    else:
        assert "저장된 시즌·기간과 현재 선택도 지웁니다" in output
        assert "기간을 선택해 새 시즌 조사를 시작" in output
    assert str(directory / "backups") in output
    with service.store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM seasons").fetchone()[0] == int(keep_seasons)
        selected = conn.execute("SELECT value FROM app_meta WHERE key='selected_season_id'").fetchone()
        assert bool(selected) is keep_seasons
    assert len(list((directory / "backups").glob("research-reset-*/reset-receipt.json"))) == 1


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS 더블 클릭 실행파일")
def test_command_runs_from_another_directory_and_cancel_keeps_data(tmp_path):
    """실행 위치가 달라도 프로젝트를 찾고 취소 시 운영 설정을 읽지 않는다."""
    command = ROOT / "reset-research.command"
    assert os.access(command, os.X_OK)
    result = subprocess.run([str(command)], cwd=tmp_path, input="취소\n\n",
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert "저장된 시즌·기간과 현재 선택도 지웁니다" in result.stdout
    assert "기록을 변경하지 않았습니다" in result.stdout
