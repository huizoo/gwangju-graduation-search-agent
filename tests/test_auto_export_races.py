"""격리 SQLite·스레드 경합으로 자동 HTML 저장의 복구와 응답성을 검사한다."""

import asyncio
from pathlib import Path
from threading import Event

import httpx
import pytest

from app.web.api import create_app
from app.web.exporting import current_export, schedule_filename
from tests.test_jobs_api import seed_season, institution_payload, settings_for


async def until(predicate, timeout=2):
    """실제 비동기 상태가 바뀔 때까지 한정된 시간 동안 기다린다."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.005)
    assert predicate(), "저장 작업이 제한 시간 안에 기대 상태에 도달하지 못했습니다."


def payload(season):
    """내보내기 계약의 필수 사용자 입력만 구성한다."""
    return {"season_id": season["id"], "include_notes": False, "publication_policy_version": "4.0"}


def latest_path(runtime, season):
    """프로그램 없이 열 수 있는 기간별 최신 파일의 경로를 얻는다."""
    return runtime.data_dir / "exports" / schedule_filename(season)


@pytest.mark.asyncio
async def test_sqlite_write_lock_preserves_last_file_and_worker_retries(tmp_path, monkeypatch):
    """실제 DB 쓰기 잠금 실패 후 같은 worker가 최신 파일을 다시 저장한다."""
    runtime = create_app(settings_for(tmp_path)).state.runtime
    season = seed_season(runtime)
    runtime.service.create_institution(institution_payload("잠금 전 학교"))
    assert runtime._refresh_exports()
    path = latest_path(runtime, season)
    before = path.read_bytes()
    runtime.service.create_institution(institution_payload("잠금 후 학교"))
    connect = runtime.store.connect

    def short_connect():
        connection = connect()
        connection.execute("PRAGMA busy_timeout=20")
        return connection

    monkeypatch.setattr(runtime.store, "connect", short_connect)
    wait_for = asyncio.wait_for

    async def short_backoff(awaitable, timeout):
        # 실패 재시도 흐름은 그대로 실행하고 검사에서만 30초 대기를 줄인다.
        return await wait_for(awaitable, timeout=0.02 if timeout == 30 else timeout)

    monkeypatch.setattr(asyncio, "wait_for", short_backoff)
    blocker = connect()
    blocker.execute("BEGIN IMMEDIATE")
    task = asyncio.create_task(runtime.watch_exports())
    try:
        await until(lambda: runtime.export_worker_failed)
        assert not task.done()
        assert runtime.html_save_status(season["id"])["status"] == "failed"
        assert path.read_bytes() == before
        blocker.rollback()
        await until(lambda: "잠금 후 학교" in path.read_text())
        await until(lambda: not runtime.export_worker_failed)
        assert not task.done()
        assert runtime.export_result(season["id"])["status"] == "current"
        assert current_export(path)
    finally:
        blocker.rollback()
        blocker.close()
        runtime.export_stop.set()
        await wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_explicit_export_wait_does_not_block_health_or_event_loop(tmp_path, monkeypatch):
    """다른 스레드의 저장 잠금을 기다리는 중에도 같은 앱의 조회가 처리된다."""
    app = create_app(settings_for(tmp_path))
    runtime = app.state.runtime
    season = seed_season(runtime)
    held, release, attempted = Event(), Event(), Event()
    released_by_async = []

    def hold_export_lock():
        with runtime.export_lock:
            held.set()
            released_by_async.append(release.wait(2))

    original_export = runtime.export

    def observed_export(body):
        attempted.set()
        return original_export(body)

    monkeypatch.setattr(runtime, "export", observed_export)
    holder = asyncio.create_task(asyncio.to_thread(hold_export_lock))
    saving = None
    try:
        await until(held.is_set)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1") as client:
            saving = asyncio.create_task(client.post(
                "/api/exports/html", json=payload(season), headers={"X-App-Token": runtime.token}))
            await until(attempted.is_set)
            response = await client.get("/health")
            assert response.status_code == 200
            assert not holder.done(), "내보내기 잠금 대기가 이벤트 루프를 막았습니다."
            release.set()
            result = await asyncio.wait_for(saving, timeout=2)
            assert result.status_code == 202 and result.json()["status"] == "current"
        await holder
        assert released_by_async == [True]
    finally:
        release.set()
        await asyncio.gather(holder, *([saving] if saving else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_automatic_and_manual_exports_keep_newest_revision_after_concurrent_edit(tmp_path, monkeypatch):
    """이전 snapshot의 자동 저장이 새 기록의 수동 저장을 역순으로 덮어쓰지 않는다."""
    import app.web.exporting as exporting

    app = create_app(settings_for(tmp_path))
    runtime = app.state.runtime
    season = seed_season(runtime)
    runtime.service.create_institution(institution_payload("보존할 이전 학교"))
    assert runtime._refresh_exports()
    with runtime.store.transaction() as conn:
        old_path = Path(conn.execute("SELECT file_ref FROM exports WHERE season_id=?",
                                    (season["id"],)).fetchone()[0])
    old_bytes = old_path.read_bytes()
    runtime.service.create_institution(institution_payload("자동 저장 학교"))
    entered, release = Event(), Event()
    render = exporting.render_export
    snapshots = []

    def paused_first_render(snapshot, *, include_notes=False):
        snapshots.append(snapshot["content_revision"])
        if len(snapshots) == 1:
            entered.set()
            assert release.wait(2), "검사에서 자동 저장 스레드를 해제하지 못했습니다."
        return render(snapshot, include_notes=include_notes)

    monkeypatch.setattr(exporting, "render_export", paused_first_render)
    automatic = asyncio.create_task(runtime.flush_exports(force=True))
    explicit = None
    try:
        await until(entered.is_set)
        runtime.service.create_institution(institution_payload("동시 입력 새 학교"))
        expected_revision = runtime.store.revision()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1") as client:
            explicit = asyncio.create_task(client.post(
                "/api/exports/html", json=payload(season), headers={"X-App-Token": runtime.token}))
            await asyncio.sleep(0)
            release.set()
            saved = await asyncio.wait_for(explicit, timeout=2)
            assert saved.status_code == 202
            assert saved.json()["status"] == "current"
            assert saved.json()["exported_revision"] == expected_revision
        assert not await automatic  # 이전 snapshot의 자동 저장은 최신 저장 완료라고 주장하지 않는다.
        result = runtime.export_result(season["id"])
        assert result["status"] == "current" and result["exported_revision"] == expected_revision
        path = latest_path(runtime, season)
        text = path.read_text()
        assert all(name in text for name in ("보존할 이전 학교", "자동 저장 학교", "동시 입력 새 학교"))
        with runtime.store.transaction() as conn:
            current = Path(conn.execute("SELECT file_ref FROM exports WHERE season_id=?",
                                       (season["id"],)).fetchone()[0])
        assert current.read_bytes() == path.read_bytes()
        assert old_path.read_bytes() == old_bytes
        assert snapshots == sorted(set(snapshots)) and len(snapshots) == 2
    finally:
        release.set()
        await asyncio.gather(automatic, *([explicit] if explicit else []), return_exceptions=True)
