"""첫 사용 안내가 빈 일정 칸이나 화면 필터 대신 실제 조사 명부를 기준으로 하는지 검사한다."""

from fastapi.testclient import TestClient

from app.api import create_app
from app.domain import validate
from tests.test_jobs_api import institution_payload, settings_for


def test_results_report_school_readiness_before_any_schedule_is_collected(tmp_path):
    """일정이 없어도 대상 학교가 있으면 조사 준비가 된 것으로 응답한다."""
    app = create_app(settings_for(tmp_path))
    service = app.state.runtime.service
    season = service.list_seasons()["items"][0]
    with TestClient(app, base_url="http://127.0.0.1") as client:
        empty = client.get("/api/results", params={"season_id": season["id"]}).json()
        validate("ScheduleResult", empty)
        assert empty["eligible_institution_count"] == 0
        assert empty["rows"] == []
        assert empty["research_summary"]["state"] == "not_started"
        service.create_institution({**institution_payload("명부검사용유치원"), "type": "kindergarten"})
        only_kindergarten = client.get("/api/results", params={"season_id": season["id"]}).json()
        assert only_kindergarten["eligible_institution_count"] == 0
        service.create_institution(institution_payload("명부검사용고등학교"))
        ready = client.get("/api/results", params={"season_id": season["id"]}).json()
        validate("ScheduleResult", ready)
        assert ready["eligible_institution_count"] == 1
        assert len(ready["rows"]) == 1
        assert ready["rows"][0]["date"] is None
        assert ready["research_summary"]["state"] == "not_started"
        assert ready["request_job_id"] is None
