"""실제 API·SQLite·작업 수명에서 정상과 실패 경로를 검사한다."""

import asyncio
from copy import deepcopy
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.web.api import CONTRACT, create_app
from app.core.config import ROOT, load_settings
from app.core.domain import ApiError, validate
from app.agent.jobs import BudgetStop, JobManager, Meter, dump, empty_usage, now


def settings_for(tmp_path):
    """실제 키와 저장소를 사용하지 않는 검사 설정을 만든다."""
    return load_settings(
        process_values={"APP_MODE": "demo", "LLM_BACKEND": "fake", "APP_DATA_DIR": str(tmp_path), "AUTO_OPEN_BROWSER": "false"},
        dotenv_path=tmp_path / "absent.env",
    )


def institution_payload(name="검사학교"):
    """기관 등록 계약의 최소 입력을 반환한다."""
    return {"name": name, "type": "high", "district": "북구", "address": "광주광역시 북구 검사로 1", "campus_label": None, "phone": "062-000-0000", "homepage_url": None}


def manual_event(institution_id, season_id):
    """확인일을 가진 가상 전화 확인 양성 예제를 만든다."""
    body = json.loads((ROOT / "tests/fixtures/contracts/event-create.example.json").read_text())
    body.update(institution_id=institution_id, season_id=season_id)
    for op in body["operations"]:
        op.update(basis="phone_confirmed", confirmed_at="2026-10-19T03:00:00Z", note="")
    return body


def job_payload(season_id, ids=None):
    """전체 또는 선택 기관 조사 요청을 만든다."""
    return {"kind": "schedule_scan", "season_id": season_id, "mode": "recheck", "selection": {"scope": "all"} if ids is None else {"scope": "ids", "institution_ids": ids}, "limits": None}


def seed_season(runtime):
    """시즌이 필요한 검사만 명시적으로 조사 기간을 준비한다.

    Args:
        runtime: 임시 저장소를 사용하는 앱 실행 객체.
    Returns:
        해당 검사 기간의 저장 시즌. 재시작 자체는 시즌을 만들지 않는다.
    """
    return runtime.service.create_season({"search_start": "2026-12-01", "search_end": "2027-03-31"})


@pytest.fixture
def runtime(tmp_path):
    """앱의 실제 도메인 서비스와 영속 저장소를 준비한다."""
    return create_app(settings_for(tmp_path)).state.runtime


def test_api_security_idempotency_revision_restart_and_export(tmp_path):
    """직접 입력·중복 요청·경합·파일 저장·재시작 보존을 실제 API로 검사한다."""
    settings = settings_for(tmp_path)
    app = create_app(settings)
    seed_season(app.state.runtime)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        bootstrap = client.get("/api/bootstrap").json()
        season = bootstrap["seasons"][0]
        headers = {"X-App-Token": bootstrap["token"], "Idempotency-Key": str(uuid4())}
        assert client.post("/api/institutions", json=institution_payload()).status_code == 403
        assert client.post("/api/institutions", headers={**headers, "Origin": "https://evil.example"}, json=institution_payload()).status_code == 403
        first = client.post("/api/institutions", headers=headers, json=institution_payload())
        assert first.status_code == 201, first.text
        institution = first.json()
        assert client.post("/api/institutions", headers=headers, json=institution_payload()).json() == institution
        assert client.post("/api/institutions", headers=headers, json=institution_payload("다른학교")).status_code == 409
        headers["Idempotency-Key"] = str(uuid4())
        response = client.post("/api/events", headers=headers, json=manual_event(institution["id"], season["id"]))
        assert response.status_code == 201, response.text
        event = response.json()
        assert event["publication"]["state"] == "date_only"
        assert event["publication"]["current_date_allowed"]
        assert not event["publication"]["sales_ready"]
        patch = json.loads((ROOT / "tests/fixtures/contracts/manual-patch.example.json").read_text())
        patch["expected_revision"] = event["revision"]
        response = client.patch(f"/api/events/{event['id']}/overrides", headers=headers, json=patch)
        assert response.status_code == 200, response.text
        assert response.json()["publication"]["sales_ready"]
        assert client.patch(f"/api/events/{event['id']}/overrides", headers=headers, json=patch).status_code == 409
        assert client.get("/api/events", params={"season_id": season["id"], "view": "sales"}).json()["total"] == 1
        assert client.get("/api/overview", params={"season_id": season["id"]}).json()["time_confirmed_institutions"] == 1
        headers["Idempotency-Key"] = str(uuid4())
        exported = client.post("/api/exports/html", headers=headers, json={"season_id": season["id"], "include_notes": False, "publication_policy_version": "4.0"})
        assert exported.status_code == 202, exported.text
        assert exported.json()["status"] == "current"
        html = client.get(exported.json()["url"])
        assert html.status_code == 200
        assert "2027-01-07" in html.text and "10:00" in html.text
        headers["Idempotency-Key"] = str(uuid4())
        backup = client.post("/api/backups", headers=headers)
        assert backup.status_code == 201, backup.text
        assert client.get(backup.json()["download_url"]).content.startswith(b"SQLite format 3")
        assert client.get("/api/review-queue", params={"season_id": season["id"]}).status_code == 200
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        assert client.get(f"/api/events/{event['id']}").json()["event"]["publication"]["sales_ready"]
        assert client.get("/api/seasons").json()["items"][0] == season


def test_runtime_openapi_has_contract_paths_and_models(runtime):
    """런타임 Pydantic route의 모든 P0 경로와 요청·응답 타입을 대조한다."""
    app = create_app(runtime.settings)
    schema = app.openapi()
    for path, methods in CONTRACT["paths"].items():
        assert path in schema["paths"]
        for method, operation in methods.items():
            assert method in schema["paths"][path]
            request_ref = operation.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {}).get("$ref")
            if request_ref:
                runtime_ref = schema["paths"][path][method]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
                assert request_ref.rsplit("/", 1)[-1] == runtime_ref.rsplit("/", 1)[-1]


def test_api_cursor_and_invalid_queries(tmp_path):
    """같은 목록 cursor 재사용과 변경 후 거절·페이지 총건수를 검사한다."""
    with TestClient(create_app(settings_for(tmp_path)), base_url="http://127.0.0.1") as client:
        token = client.get("/api/bootstrap").json()["token"]
        for n in range(3):
            client.post("/api/institutions", headers={"X-App-Token": token, "Idempotency-Key": str(uuid4())}, json=institution_payload(f"검사{n}"))
        page = client.get("/api/institutions", params={"limit": 2}).json()
        assert page["total"] == 3 and len(page["items"]) == 2
        next_page = client.get("/api/institutions", params={"limit": 2, "cursor": page["next_cursor"]})
        assert next_page.status_code == 200 and len(next_page.json()["items"]) == 1
        assert client.get("/api/institutions", params={"limit": 2, "cursor": page["next_cursor"], "query": "다른"}).status_code == 409
        assert client.get("/api/institutions", params={"limit": 501}).status_code == 422
        assert client.get("/api/institutions", params={"unexpected": "value"}).status_code == 422
        for invalid in (None, 1, {"season_id": ["bad"]}, {"season_id": "not-a-uuid"}):
            response = client.post("/api/selected-season", headers={"X-App-Token": token}, json=invalid)
            assert response.status_code == 422
            assert response.json()["code"] == "validation_error"


def seed_job(runtime, count=2):
    """고정 큐 검사용 기관과 대기 작업을 만든다."""
    for n in range(count):
        runtime.service.create_institution(institution_payload(f"검사{n}"))
    season = seed_season(runtime)
    return runtime.jobs.create(job_payload(season["id"]), str(uuid4()))


def test_fixed_queue_idempotency_and_recovery(runtime):
    """재시작 때 취소 우선·고정 큐·사용량 보존을 검사한다."""
    job = seed_job(runtime)
    runtime.service.create_institution(institution_payload("나중에 추가"))
    assert len(runtime.jobs.items(job["id"])) == 2
    with runtime.store.transaction(True) as conn:
        conn.execute("UPDATE jobs SET status='cancelling' WHERE id=?", (job["id"],))
        conn.execute("UPDATE job_items SET status='running' WHERE job_id=?", (job["id"],))
    JobManager(runtime.service, runtime.settings).recover()
    assert runtime.jobs.get(job["id"])["status"] == "cancelled"
    assert all(i["status"] == "cancelled" for i in runtime.jobs.items(job["id"]))
    with pytest.raises(ApiError):
        runtime.jobs.resume(job["id"], {"expected_revision": runtime.jobs.get(job["id"])["revision"], "increase_limits": None})


def test_resume_settings_hash_precedes_provider_initialization(runtime):
    """중단 작업의 backend 변경을 provider 생성 전에 거절한다."""
    job = seed_job(runtime, 1)
    runtime.jobs.pause(job["id"])
    before = runtime.jobs.get(job["id"])
    runtime.settings.data["models"]["image_candidates_enabled"] = True
    with pytest.raises(ApiError) as error:
        runtime.jobs.resume(job["id"], {"expected_revision": before["revision"], "increase_limits": None})
    assert error.value.code == "settings_changed"
    assert runtime.jobs.get(job["id"])["scope_snapshot"] == before["scope_snapshot"]


@pytest.mark.asyncio
async def test_worker_completion_and_late_cancel(runtime):
    """실행된 가상 수집기는 작업 제어 검사에만 사용하고 실제 조사로 세지 않는다."""
    entered, release = asyncio.Event(), asyncio.Event()

    class ControlledRunner:
        def __init__(self, settings, meter=None):
            self.meter = meter

        async def run(self, institution, season, identifiers):
            reservation = await self.meter.reserve("http_requests")
            entered.set()
            await release.wait()
            await self.meter.settle(reservation)
            return {"documents": [], "candidates": [], "errors": []}

    runtime.jobs.runner_factory = ControlledRunner
    job = seed_job(runtime, 1)
    task = asyncio.create_task(runtime.jobs.run(job["id"]))
    await asyncio.wait_for(entered.wait(), 2)
    runtime.jobs.cancel(job["id"])
    release.set()
    await task
    saved = runtime.jobs.get(job["id"])
    assert saved["status"] == "cancelled"
    assert saved["usage"]["http_requests"] == 1
    assert runtime.jobs.items(job["id"])[0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_budget_is_reserved_and_kept_across_crash(runtime):
    """응답 유실 예약을 취소하거나 0으로 재설정하지 않는다."""
    job = seed_job(runtime, 1)
    item = runtime.jobs.items(job["id"])[0]
    segment_id = str(uuid4())
    with runtime.store.transaction(True) as conn:
        limits = job["limits"]
        limits["job_max_http_requests"] = 1
        conn.execute("UPDATE jobs SET status='running',limits_json=? WHERE id=?", (dump(limits), job["id"]))
        conn.execute("INSERT INTO job_segments VALUES(?,?,?,?,?,?,?)", (segment_id, job["id"], 1, now(), None, now(), dump(empty_usage())))
    meter = Meter(runtime.jobs, job["id"], item["id"], segment_id)
    reservation = await meter.reserve("http_requests")
    with pytest.raises(BudgetStop) as stop:
        await meter.reserve("http_requests")
    assert stop.value.reason == "job_budget"
    runtime.jobs.recover()
    assert runtime.jobs.get(job["id"])["usage"]["http_requests"] == 1
    with runtime.store.transaction() as conn:
        assert conn.execute("SELECT status FROM call_reservations WHERE id=?", (reservation,)).fetchone()[0] == "charged_unknown"
