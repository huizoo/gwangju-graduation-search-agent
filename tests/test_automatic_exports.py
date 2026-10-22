"""자동 HTML 저장의 지속성·실패 보존·종료 경로를 격리된 DB로 확인한다."""

import asyncio
from pathlib import Path
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.web.api import create_app
from app.web.exporting import schedule_filename
from tests.test_jobs_api import institution_payload, job_payload, manual_event, settings_for


def latest_file(runtime, season):
    """기간별 고정 HTML 경로를 반환한다."""
    return runtime.data_dir / "exports" / schedule_filename(season)


def wait_for(predicate, timeout=8):
    """실제 자동 저장 루프의 조건 충족을 제한된 시간만 기다린다."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.05)
    assert predicate(), "자동 저장이 제한 시간 안에 완료되지 않았습니다."


def test_manual_change_exports_without_open_or_download_and_restart(tmp_path):
    """저장 버튼 없이 직접 기록을 HTML에 반영하고 재시작 누락분도 갱신한다."""
    settings = settings_for(tmp_path)
    app = create_app(settings)
    runtime = app.state.runtime
    with TestClient(app, base_url="http://127.0.0.1") as client:
        boot = client.get("/api/bootstrap").json()
        season = boot["seasons"][0]
        school = runtime.service.create_institution(institution_payload("자동저장 검사학교"))
        response = client.post("/api/events", json=manual_event(school["id"], season["id"]),
                               headers={"X-App-Token": boot["token"], "Idempotency-Key": str(uuid4())})
        assert response.status_code == 201, response.text
        path = latest_file(runtime, season)
        wait_for(lambda: path.exists() and "자동저장 검사학교" in path.read_text())
        assert "2027-01-07" in path.read_text()
        before = sorted(path.parent.glob("versions/*/*.html"))
        assert runtime._refresh_exports()
        assert sorted(path.parent.glob("versions/*/*.html")) == before
    # 앱이 꺼진 사이의 저장 변경도 다음 시작에서 복구한다.
    runtime.service.create_institution(institution_payload("재시작 검사학교"))
    with TestClient(create_app(settings), base_url="http://127.0.0.1"):
        wait_for(lambda: "재시작 검사학교" in path.read_text())
    assert path.exists()
    assert "fetch(" not in path.read_text()


def test_file_failure_preserves_last_good_export_and_retries(tmp_path, monkeypatch):
    """고정 파일 교체 실패는 마지막 정상 파일을 유지하고 재시도로 복구한다."""
    runtime = create_app(settings_for(tmp_path)).state.runtime
    season = runtime.service.list_seasons()["items"][0]
    runtime.service.create_institution(institution_payload("기존 검사학교"))
    assert runtime._refresh_exports()
    path = latest_file(runtime, season)
    old_content = path.read_bytes()
    old_url = runtime.export_result(season["id"])["url"]
    runtime.service.create_institution(institution_payload("추가 검사학교"))
    import app.web.api as api_module
    replace = api_module.os.replace

    def reject_latest(source, target):
        if Path(target) == path:
            raise OSError("검사용 디스크 쓰기 실패")
        return replace(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(api_module.os, "replace", reject_latest)
        unlink = Path.unlink

        def reject_cleanup(target, *args, **kwargs):
            if target.suffix == ".tmp":
                raise PermissionError("임시 파일도 지울 수 없는 디스크")
            return unlink(target, *args, **kwargs)

        patch.setattr(Path, "unlink", reject_cleanup)
        assert not runtime._refresh_exports(force=True)
        assert path.read_bytes() == old_content
        result = runtime.export_result(season["id"])
        assert result["status"] == "failed" and result["url"] == old_url
        assert runtime.html_save_status(season["id"])["message"]
        assert runtime.service.list_institutions()["total"] == 2
    assert runtime._refresh_exports(force=True)
    assert "추가 검사학교" in path.read_text()
    assert runtime.html_save_status(season["id"]) == {"status": "current", "message": None}


def test_shutdown_pauses_work_and_flushes_html(tmp_path):
    """조사 중 종료는 재개 가능한 작업과 최신 HTML을 남긴다."""
    class WaitingRunner:
        def __init__(self, settings, meter=None):
            pass

        async def run(self, institution, season, identifiers):
            await asyncio.sleep(60)
            return {"documents": [], "candidates": [], "errors": []}

    app = create_app(settings_for(tmp_path), WaitingRunner)
    runtime = app.state.runtime
    with TestClient(app, base_url="http://127.0.0.1") as client:
        boot = client.get("/api/bootstrap").json()
        season = boot["seasons"][0]
        runtime.service.create_institution(institution_payload())
        headers = {"X-App-Token": boot["token"], "Idempotency-Key": str(uuid4())}
        job = client.post("/api/jobs", headers=headers, json=job_payload(season["id"])).json()
        response = client.post("/api/app/shutdown", headers=headers, json={"pause_active_job": True})
        assert response.status_code == 202, response.text
        assert response.json() == {"accepted": True, "paused_active_job": True}
        assert runtime.jobs.get(job["id"])["status"] == "paused"
        assert latest_file(runtime, season).exists()
        assert runtime.export_result(season["id"])["status"] == "current"


@pytest.mark.parametrize("raised", [False, True])
def test_shutdown_failure_keeps_app_available_and_does_not_claim_success(tmp_path, monkeypatch, raised):
    """HTML 저장 실패 시 종료 성공을 반환하거나 이미 저장한 DB를 잃지 않는다."""
    app = create_app(settings_for(tmp_path))
    runtime = app.state.runtime
    stopped = []
    runtime.shutdown_callback = lambda: stopped.append(True)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        token = client.get("/api/bootstrap").json()["token"]

        async def failed_flush(*, force=False):
            if raised:
                raise PermissionError("검사용 파일 시스템 오류")
            return False

        with monkeypatch.context() as patch:
            patch.setattr(runtime, "flush_exports", failed_flush)
            response = client.post("/api/app/shutdown", headers={"X-App-Token": token},
                                   json={"pause_active_job": True})
            assert response.status_code == 503
            assert response.json()["code"] == "export_failed"
            assert not stopped and not runtime.jobs.stopping
            assert client.get("/health").status_code == 200


@pytest.mark.asyncio
async def test_worker_survives_unexpected_flush_failure(tmp_path, monkeypatch):
    """저장 실패가 자동 저장 루프를 종료시키지 않고 사용자 안내로 남는다."""
    runtime = create_app(settings_for(tmp_path)).state.runtime
    entered = asyncio.Event()

    async def failed_flush(*, force=False):
        entered.set()
        raise PermissionError("검사용 저장 오류")

    monkeypatch.setattr(runtime, "flush_exports", failed_flush)
    task = asyncio.create_task(runtime.watch_exports())
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.sleep(0)
    assert not task.done()
    season = runtime.service.list_seasons()["items"][0]
    assert runtime.html_save_status(season["id"])["status"] == "failed"
    runtime.export_stop.set()
    await asyncio.wait_for(task, 1)
