"""격리 SQLite에서 학교 목록 부재와 조건에 맞는 대상 부재를 구분한다."""

from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app.web.api import create_app
from app.core.domain import ApiError, validate
from app.agent.job_feedback import season_research_summary
from test_jobs_api import seed_season, institution_payload, job_payload, settings_for


@pytest.fixture
def runtime(tmp_path):
    """실제 사용자 데이터와 연결하지 않은 앱 저장소를 준비한다."""
    return create_app(settings_for(tmp_path)).state.runtime


def _schedule_request(runtime):
    """첫 사용 화면과 같은 전체 학교 조사 요청을 만든다."""
    season = seed_season(runtime)
    return {**job_payload(season["id"]), "purpose": "date_count", "target_event_id": None}


def _assert_no_queue(runtime):
    """거절한 요청이 작업과 기관 큐를 남기지 않았는지 검사한다."""
    with runtime.store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM job_items").fetchone()[0] == 0


def test_no_roster_returns_actionable_api_error_without_a_job(tmp_path):
    """HTTP 성공·빈 작업 완료 대신 명부를 불러올 수 있는 오류를 반환한다."""
    app = create_app(settings_for(tmp_path))
    runtime = app.state.runtime
    with TestClient(app, base_url="http://127.0.0.1") as client:
        token = client.get("/api/bootstrap").json()["token"]
        response = client.post("/api/jobs", json=_schedule_request(runtime),
                               headers={"X-App-Token": token, "Idempotency-Key": str(uuid4())})
        assert response.status_code == 422
        error = response.json()
        validate("Error", error)
        assert error["code"] == "roster_required"
        assert "학교 목록을 먼저 불러와 주세요" in error["message"]
        assert not runtime.jobs.tasks
        _assert_no_queue(runtime)


def test_non_target_kindergarten_roster_does_not_start_empty_research(runtime):
    """명부가 있어도 조사 대상 학교가 없으면 명부 부재와 다른 코드로 거절한다."""
    runtime.service.create_institution({**institution_payload("검사용유치원"), "type": "kindergarten"})
    with pytest.raises(ApiError) as error:
        runtime.jobs.create(_schedule_request(runtime), str(uuid4()))
    assert error.value.code == "no_eligible_schools"
    assert "학교 목록을 다시 불러와 주세요" in error.value.message
    _assert_no_queue(runtime)


def test_registry_sync_remains_available_before_any_school_is_loaded(runtime):
    """학교 목록이 비어 있어도 목록 자체를 불러오는 작업은 시작할 수 있다."""
    job = runtime.jobs.create(
        {"kind": "registry_sync", "providers": ["neis", "higher_education"], "limits": None}, str(uuid4()))
    assert job["status"] == "queued" and job["target_count"] == 2
    assert {item["target_ref"] for item in runtime.jobs.items(job["id"])} == {"neis", "higher_education"}


def test_empty_filter_does_not_claim_school_roster_is_missing(runtime):
    """호환용 선택 조건이 0개인 경우에는 명부를 다시 불러오라고 안내하지 않는다."""
    runtime.service.create_institution(institution_payload())
    season_id = seed_season(runtime)["id"]
    payload = {**job_payload(season_id), "selection": {
        "scope": "filter", "types": [], "districts": [], "query": "없는이름"}}
    with pytest.raises(ApiError) as error:
        runtime.jobs.create(payload, str(uuid4()))
    assert error.value.code == "no_eligible_schools"
    assert "조사 조건을 확인해 주세요" in error.value.message
    assert "목록" not in error.value.message
    _assert_no_queue(runtime)


def test_loaded_target_school_enables_research_and_ignores_non_target(runtime):
    """목록 확보 뒤에는 유효한 학교만 같은 요청 경로의 고정 큐에 들어간다."""
    request = _schedule_request(runtime)
    with pytest.raises(ApiError):
        runtime.jobs.create(request, str(uuid4()))
    school = runtime.service.create_institution(institution_payload())
    runtime.service.create_institution({**institution_payload("검사용유치원"), "type": "kindergarten"})
    job = runtime.jobs.create(request, str(uuid4()))
    assert job["status"] == "queued" and job["target_count"] == 1
    assert [item["institution_id"] for item in runtime.jobs.items(job["id"])] == [school["id"]]


@pytest.mark.parametrize("has_history", [False, True])
def test_missing_current_roster_guidance_precedes_old_research_history(runtime, has_history):
    """현재 목록이 없을 때는 과거 작업과의 불일치가 아닌 목록 확보를 안내한다."""
    request = _schedule_request(runtime)
    if has_history:
        runtime.service.create_institution(institution_payload())
        job = runtime.jobs.create(request, str(uuid4()))
        with runtime.store.transaction(write=True) as conn:
            conn.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (job["id"],))
    with runtime.store.transaction() as conn:
        summary = season_research_summary(conn, request["season_id"], [])
    assert summary == {"state": "not_started", "message": "학교 목록을 먼저 불러와 주세요."}
