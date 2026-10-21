"""개발 자료 분리의 원본 보존·일반 자료 보호·재수집 경계를 검사한다."""

import importlib.util
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from app import development_archive as archive
from app.domain import ApiError, dumps, fingerprint, utcnow
from app.storage import Service, Store
from test_grounded_claims import candidate, _save, _count
from test_jobs_api import institution_payload, settings_for
from test_rules import notice
from test_storage import operation

RULES = {"auto_rule_ids": ["explicit_notice", "grounded_notice"]}
TEXT = "광주과학기술원\n2025학년도 학위수여식\n일시 : 2026. 2. 20.(금) 14:00\n졸업생 총 308명"


def _claim(text=TEXT):
    """명시된 과거 학년도와 실제 행사일 후보를 만든다."""
    return candidate(text, academic_year=2025, event_title="2025학년도 학위수여식",
                     date="2026-02-20", date_quote="2026. 2. 20.(금)",
                     time_quote="일시 : 2026. 2. 20.(금) 14:00")


def _document(school, text=TEXT):
    """지정한 공식 URL을 읽은 합성 원문을 만든다."""
    doc = notice(school, text)
    doc["source_key"] = doc["display_url"] = doc["acquisition"]["final_url"] = archive.NOTICE
    channel = school["official_channels"][0]
    doc["authority_evidence"] = {"channel_url": channel["url"], "verified_by_url": channel["verified_by_url"],
                                 "verified_at": channel["verified_at"]}
    return doc


@pytest.fixture
def prepared(tmp_path):
    """실제 사용자 DB 없이 확인된 검증 ID의 구조만 재현한다."""
    service = Service(Store(tmp_path / "source.sqlite3"), RULES)
    with service.store.transaction(True) as conn:
        school_id = service._insert_institution(conn, {
            **institution_payload("광주과학기술원"), "type": "university",
            "homepage_url": "https://www.gist.ac.kr"}, archive.INSTITUTION_ID)
        channels = [{"url": "https://www.gist.ac.kr", "verified_by_url": "https://directory.example/gist",
                     "verified_at": "2026-10-20T01:00:00Z"}]
        conn.execute("UPDATE institutions SET official_channels_json=?,source_checked_at=? WHERE id=?",
                     (dumps(channels), utcnow(), school_id))
        conn.execute("INSERT INTO seasons VALUES(?,?,?,?,?,?,?,?,?)", (
            archive.SEASON_ID, "과거 공지 검증", 2025, "2025-12-01", "2026-03-31",
            "2025-winter-source-check", "Asia/Seoul", "gwangju_5_gu", 1))
        conn.execute("""INSERT INTO events(id,institution_id,season_id,event_key,title,event_type,
                     audience,campus_label,created_at,updated_at,academic_year) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                     (archive.EVENT_ID, school_id, archive.SEASON_ID, "development-event", "2025학년도 학위수여식",
                      "degree_award_ceremony", "졸업 대상 학생", None, utcnow(), utcnow(), 2025))
        conn.execute("INSERT INTO event_seasons VALUES(?,?)", (archive.SEASON_ID, archive.EVENT_ID))
    school = service.get_institution(school_id)
    with service.store.transaction(True) as conn:
        for index, (observation_id, document_id) in enumerate(archive.OBSERVATIONS.items()):
            text = TEXT + "\n" + ("첫 취득" if index == 0 else "다시 취득")
            saved_id = service._save_document(conn, school_id, _document(school, text))
            conn.execute("UPDATE documents SET id=? WHERE id=?", (document_id, saved_id))
            claim = _claim(text)
            claim["event_key_hint"] = "development-event"
            conn.execute("INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?,?)", (
                observation_id, school_id, archive.SEASON_ID, archive.EVENT_ID, document_id,
                dumps(claim), fingerprint(observation_id), "accepted", "[]", f"2026-10-20T10:{41+index*4}:00Z"))
        service._reassess(conn, archive.EVENT_ID)
        service.store.touch(conn)
    return service, school


def _clone(prepared, tmp_path):
    """사용자 DB 대신 검사 전용 원본의 일관된 사본을 연다."""
    service, school = prepared
    path = Path(service.store.backup(tmp_path / "clone.sqlite3"))
    return Service(Store(path), RULES), school


def test_archive_removes_only_verified_development_bundle_and_research_restarts(prepared, tmp_path):
    """순수 검증 자료를 분리하고 같은 기간의 새 수집만 정상 결과로 제공한다."""
    original, school = prepared
    assert original.event_detail(archive.EVENT_ID)["event"]["publication"]["calendar_time"] == "14:00"
    export_path = tmp_path / "old-development.html"
    export_path.write_text("원본 개발 검증 HTML", encoding="utf-8")
    with original.store.transaction(True) as conn:
        conn.execute("UPDATE exports SET file_ref=?,status='current' WHERE season_id=?",
                     (str(export_path), archive.SEASON_ID))
        before = {table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
                  for table in ("seasons", "events", "documents", "observations", "assessments", "audit_log")}
    service, _ = _clone(prepared, tmp_path)
    assert service.list_seasons()["items"] == []
    with service.store.transaction() as conn:
        receipt = json.loads(conn.execute("SELECT value FROM app_meta WHERE key=?", (archive.ARCHIVE_KEY,)).fetchone()[0])
        for table in ("seasons", "events", "observations", "documents", "assessments", "exports", "event_seasons"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    stored = json.loads(Path(receipt["archive"]).read_text())
    for table in ("observations", "documents"):
        assert sorted(stored["tables"][table], key=lambda row: row["id"]) == sorted(before[table], key=lambda row: row["id"])
    with sqlite3.connect(f"file:{receipt['backup']}?mode=ro", uri=True) as saved:
        saved.row_factory = sqlite3.Row
        for table, rows in before.items():
            assert [dict(row) for row in saved.execute(f"SELECT * FROM {table}")] == rows
    assert export_path.read_text() == "원본 개발 검증 HTML"
    with pytest.raises(ApiError):
        service.event_detail(archive.EVENT_ID)
    season = service.create_season({"search_start": "2025-12-01", "search_end": "2026-03-31"})
    assert season["id"] != archive.SEASON_ID
    assert not service.snapshot(season["id"])["events"]
    assert service.overview(season["id"])["date_confirmed_institutions"] == 0
    context = service, school, season
    result = _save(context, TEXT, _claim(), document=_document(school))
    assert len(result["event_ids"]) == 1 and result["event_ids"][0] != archive.EVENT_ID
    assert service.snapshot(season["id"])["events"][0]["publication"]["calendar_time"] == "14:00"
    with service.store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        assert not set(archive.OBSERVATIONS.values()) & set(service._documents(conn, school["id"]))
    files = sorted(tmp_path.glob("archives/*"))
    restarted = Service(Store(service.store.path), RULES)
    assert sorted(tmp_path.glob("archives/*")) == files
    assert restarted.events(season["id"])["total"] == 1
    assert original.event_detail(archive.EVENT_ID)["event"]["publication"]["calendar_time"] == "14:00"


@pytest.mark.parametrize("mixed", ["manual", "ordinary_observation"])
def test_mixed_user_records_keep_ids_and_only_independent_support(prepared, tmp_path, mixed):
    """수기 또는 일반 조사가 섞인 행사는 지우지 않고 개발 원문만 분리한다."""
    original, school = prepared
    if mixed == "manual":
        event = original.event_detail(archive.EVENT_ID)["event"]
        original.apply_overrides(archive.EVENT_ID, {"expected_revision": event["revision"], "operations": [
            operation("ceremony_status", "scheduled"),
            operation("event_date", {"precision": "day", "date": "2026-02-20", "month": None}),
            operation("start_time", "11:00", bound_date="2026-02-20", note="실제 사용자 전화 메모")]})
    else:
        with original.store.transaction() as conn:
            season = original._season(conn, archive.SEASON_ID)
        independent = _document(school)
        independent["display_url"] = independent["source_key"] = independent["acquisition"]["final_url"] = "https://www.gist.ac.kr/ordinary"
        _save((original, school, season), TEXT, _claim(), document=independent)
    with original.store.transaction() as conn:
        manual = [dict(row) for row in conn.execute("SELECT * FROM manual_overrides")]
        ordinary = [dict(row) for row in conn.execute("SELECT * FROM observations WHERE id NOT IN (?,?)",
                                                     tuple(archive.OBSERVATIONS))]
    service, _ = _clone(prepared, tmp_path)
    assert service.list_seasons()["items"][0]["id"] == archive.SEASON_ID
    event = service.event_detail(archive.EVENT_ID)["event"]
    assert event["publication"]["calendar_time"] == ("11:00" if mixed == "manual" else "14:00")
    with service.store.transaction() as conn:
        assert [dict(row) for row in conn.execute("SELECT * FROM manual_overrides")] == manual
        assert [row["id"] for row in conn.execute("SELECT * FROM observations")] == [row["id"] for row in ordinary]
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    assert set(service.event_detail(archive.EVENT_ID)["event"]["fields"]["event_date"]["automatic"]["evidence_ids"]).isdisjoint(archive.OBSERVATIONS)


@pytest.mark.parametrize("failure", ["write", "identity", "transaction"])
def test_archive_failure_leaves_operational_rows_unchanged(prepared, tmp_path, monkeypatch, failure):
    """보관 실패나 원본 식별 불일치에서는 삭제를 시작하지 않는다."""
    original, _ = prepared
    path = Path(original.store.backup(tmp_path / "failed-clone.sqlite3"))
    if failure == "write":
        def fail(*args):
            raise OSError("검사에서 강제로 만든 파일 보관 실패")
        monkeypatch.setattr(archive, "_write_archive", fail)
    elif failure == "transaction":
        def fail(*args):
            raise ApiError("migration_required", "검사에서 강제로 만든 거래 종료 실패", 409)
        monkeypatch.setattr(Store, "touch", fail)
    else:
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE observations SET candidate_json=json_set(candidate_json,'$.start_time','15:00') WHERE id=?",
                         (next(iter(archive.OBSERVATIONS)),))
    with sqlite3.connect(path) as conn:
        before = conn.execute("SELECT * FROM observations").fetchall()
    with pytest.raises((OSError, ApiError)):
        Store(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM observations").fetchall() == before
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        assert not conn.execute("SELECT 1 FROM app_meta WHERE key=?", (archive.ARCHIVE_KEY,)).fetchone()


def test_unrelated_user_season_manual_and_audit_are_unchanged(prepared, tmp_path):
    """검증 분리가 다른 시즌의 직접 입력·UUID·감사 이력을 건드리지 않는다."""
    original, _ = prepared
    season = original.create_season({"search_start": "2026-12-01", "search_end": "2027-03-31"})
    school = original.create_institution(institution_payload("사용자학교"))
    event = original.create_event({
        "institution_id": school["id"], "season_id": season["id"], "title": "졸업식",
        "event_type": "graduation_ceremony", "audience": None, "campus_label": None,
        "allow_separate_event": False, "operations": [
            operation("ceremony_status", "scheduled"),
            operation("event_date", {"precision": "day", "date": "2027-01-07", "month": None}),
            operation("start_time", "10:30", bound_date="2027-01-07", note="원래 사용자 메모")]})
    queries = {
        "season": ("SELECT * FROM seasons WHERE id=?", season["id"]),
        "event": ("SELECT * FROM events WHERE id=?", event["id"]),
        "manual": ("SELECT * FROM manual_overrides WHERE event_id=? ORDER BY id", event["id"]),
        "history": ("SELECT * FROM audit_log WHERE entity_id=? ORDER BY seq", event["id"])}
    with original.store.transaction() as conn:
        before = {key: [dict(row) for row in conn.execute(sql, (identity,))]
                  for key, (sql, identity) in queries.items()}
    service, _ = _clone(prepared, tmp_path)
    with service.store.transaction() as conn:
        for key, (sql, identity) in queries.items():
            assert [dict(row) for row in conn.execute(sql, (identity,))] == before[key]
    assert service.event_detail(event["id"])["event"]["publication"]["calendar_time"] == "10:30"


def test_development_counts_and_route_are_not_reused(prepared, tmp_path):
    """개발 원문을 가리키는 파생 인원·경로는 원본 보관 후 재사용에서 제외한다."""
    original, school = prepared
    document_id = next(iter(archive.OBSERVATIONS.values()))
    with original.store.transaction(True) as conn:
        count_id, route_id = str(uuid4()), str(uuid4())
        conn.execute("INSERT INTO count_observations VALUES(?,?,?,?,?,?,?,?,?)",
                     (count_id, school["id"], archive.SEASON_ID, archive.EVENT_ID, document_id,
                      dumps({"academic_year": 2025, "value": 308}), dumps({"allowed": True}), "development-count", utcnow()))
        conn.execute("INSERT INTO collection_routes VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (route_id, school["id"], archive.NOTICE, "date_count", "html_parser", 1, utcnow(), utcnow(), None, document_id))
    service, _ = _clone(prepared, tmp_path)
    with service.store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM count_observations").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM collection_routes").fetchone()[0] == 0
        receipt = json.loads(conn.execute("SELECT value FROM app_meta WHERE key=?", (archive.ARCHIVE_KEY,)).fetchone()[0])
    saved = json.loads(Path(receipt["archive"]).read_text())
    assert saved["tables"]["count_observations"][0]["id"] == count_id
    assert saved["tables"]["collection_routes"][0]["id"] == route_id


def test_same_document_recollected_by_general_research_keeps_its_counts_and_route(prepared, tmp_path):
    """같은 본문을 일반 조사에서 다시 읽은 경우 개발 관측만 빼고 새 근거를 유지한다."""
    original, school = prepared
    with original.store.transaction() as conn:
        season = original._season(conn, archive.SEASON_ID)
    text = TEXT + "\n다시 취득"
    document = _document(school, text)
    result = _save((original, school, season), text, _claim(text), document=document,
                   counts=[_count(document, value=308, academic_year=2025, event_date="2026-02-20")])
    with original.store.transaction(True) as conn:
        document_id = conn.execute("SELECT document_id FROM observations WHERE id=?",
                                   (result["observation_ids"][0],)).fetchone()[0]
        assert document_id in archive.OBSERVATIONS.values()
        count_id, route_id = conn.execute("SELECT id FROM count_observations").fetchone()[0], str(uuid4())
        conn.execute("INSERT INTO collection_routes VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (route_id, school["id"], archive.NOTICE, "date_count", "html_parser", 1, utcnow(), utcnow(), None, document_id))
    service, _ = _clone(prepared, tmp_path)
    event = service.event_detail(archive.EVENT_ID)["event"]
    assert event["publication"]["calendar_time"] == "14:00"
    assert event["fields"]["event_date"]["automatic"]["evidence_ids"] == result["observation_ids"]
    assert service.snapshot(archive.SEASON_ID)["counts"] == {archive.EVENT_ID: 308}
    with service.store.transaction() as conn:
        assert conn.execute("SELECT id FROM count_observations").fetchone()[0] == count_id
        assert conn.execute("SELECT id FROM collection_routes").fetchone()[0] == route_id
        assert conn.execute("SELECT id FROM documents").fetchone()[0] == document_id
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()


def test_validation_script_rejects_operational_paths_and_existing_folders(tmp_path, monkeypatch):
    """과거 URL 검증 도구가 운영 DB나 기존 폴더를 검사 대상으로 선택하지 못한다."""
    spec = importlib.util.spec_from_file_location("historical_check", Path("scripts/check_historical_publication.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "validation"
    root.mkdir()
    monkeypatch.setattr(module, "VALIDATION_ROOT", root)
    source = settings_for(tmp_path / "operating")
    for folder in (source.data_dir, root, tmp_path / "elsewhere"):
        with pytest.raises(ValueError):
            module.isolated_settings(folder, source)
    occupied = root / "existing"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("보존")
    with pytest.raises(ValueError):
        module.isolated_settings(occupied, source)
    isolated = module.isolated_settings(root / "new", source)
    assert isolated.data_dir != source.data_dir
    assert isolated.data_dir.is_relative_to(root)
    assert not isolated.data_dir.exists()
    assert source.data["app"]["data_dir"] == str(tmp_path / "operating")


def test_validation_script_uses_only_its_dedicated_app(prepared, tmp_path, monkeypatch):
    """외부 수집을 합성 읽기로 대체하고 전용 앱·DB·HTML 저장 경로를 실제로 검사한다."""
    _, school = prepared
    spec = importlib.util.spec_from_file_location("historical_check_isolated", Path("scripts/check_historical_publication.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "validation"
    record = tmp_path / "institution.json"
    record.write_text(dumps(school))
    source = settings_for(tmp_path / "operating")
    monkeypatch.setattr(module, "VALIDATION_ROOT", root)
    monkeypatch.setattr(module, "load_settings", lambda: source)
    async def synthetic_read(institution, season, settings):
        assert settings.data_dir.is_relative_to(root)
        document = _document(institution)
        return {"documents": [document], "candidates": [{"document_id": document["id"], "candidate": _claim()}],
                "trace": [], "discovery_mode": "seeded_url"}
    monkeypatch.setattr(module, "read", synthetic_read)
    monkeypatch.setattr(module.sys, "argv", ["check", "--validation-dir", str(root / "new"),
                                           "--institution-record", str(record)])
    module.main()
    result = json.loads((root / "new" / "historical-gist-publication.json").read_text())
    assert result["evidence_level"] == "isolated_app_mcp_storage_api_html"
    assert result["assertions"]["same_api_html_values"]
    assert not source.data_dir.exists()
    assert (root / "new" / "data" / "demo" / "graduation.sqlite3").exists()
