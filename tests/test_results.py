"""조사 대상·현재 필드·취소·공식 링크의 단일 결과 계약을 검사한다."""

from uuid import uuid4

from fastapi.testclient import TestClient

from app.api import create_app
from app.domain import validate
from app.results import result_rows
from test_jobs_api import settings_for, institution_payload, job_payload
from test_review_regressions import source_context, save_notice


def test_all_target_schools_include_unattempted_missing_rows(tmp_path):
    """등록된 대상 학교는 최근 조사 선택과 무관하게 결과에 남긴다."""
    app = create_app(settings_for(tmp_path))
    runtime = app.state.runtime
    a = runtime.service.create_institution(institution_payload("요청 학교"))
    runtime.service.create_institution(institution_payload("요청하지 않은 학교"))
    season = runtime.service.list_seasons()["items"][0]
    runtime.jobs.create(job_payload(season["id"], [a["id"]]), str(uuid4()))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get("/api/results", params={"season_id": season["id"]})
        assert response.status_code == 200, response.text
        result = validate("ScheduleResult", response.json())
        assert len(result["rows"]) == 2
        row = next(r for r in result["rows"] if r["name"] == "요청 학교")
        assert row["name"] == "요청 학교"
        assert row["date"] is row["start_time"] is row["address"] is None


def test_cancelled_request_does_not_remove_any_target_school(tmp_path):
    """취소 여부나 마지막 요청의 대상이 전체 학교 표를 줄이지 않는다."""
    app = create_app(settings_for(tmp_path))
    runtime = app.state.runtime
    a = runtime.service.create_institution(institution_payload("이전 요청"))
    b = runtime.service.create_institution(institution_payload("취소 요청"))
    season = runtime.service.list_seasons()["items"][0]
    first = runtime.jobs.create(job_payload(season["id"], [a["id"]]), str(uuid4()))
    runtime.jobs.pause(first["id"])
    second = runtime.jobs.create(job_payload(season["id"], [b["id"]]), str(uuid4()))
    runtime.jobs.cancel(second["id"])
    assert [r["name"] for r in result_rows(runtime.service.snapshot(season["id"]))] == ["이전 요청", "취소 요청"]


def test_verified_cancellation_masks_date_time_but_retains_internal_evidence(source_context):
    """확인 취소를 간단히 표시하며 과거 날짜·시간은 내부 이력에만 보관한다."""
    service, _, season = source_context
    save_notice(source_context, "2026학년도 검증초등학교 졸업식은 2027년 1월 7일 오전 11시에 시작합니다.")
    save_notice(source_context, "2026학년도 검증초등학교 졸업식은 취소합니다.", "cancel")
    snapshot = service.snapshot(season["id"])
    row = result_rows(snapshot)[0]
    assert row["status"] == "cancelled"
    assert row["date"] is row["start_time"] is None
    assert snapshot["events"][0]["fields"]["event_date"]["automatic"]["value"]["date"] == "2027-01-07"
    assert len(snapshot["documents"][snapshot["events"][0]["id"]]) == 2


def test_failed_recheck_does_not_publish_old_cancellation_as_current(source_context):
    """현재 확인할 수 없는 과거 취소 표시도 경고 없이 확정 정보로 내보내지 않는다."""
    from app.domain import dumps, utcnow
    service, _, season = source_context
    save_notice(source_context, "2026학년도 검증초등학교 졸업식은 취소 안내.", "cancel")
    assert result_rows(service.snapshot(season["id"]))[0]["status"] == "cancelled"
    with service.store.transaction(True) as conn:
        conn.execute("UPDATE documents SET recheck_json=?", (dumps({"status":"failed","attempted_at":utcnow(),"reason_codes":["recheck_failed"]}),))
    row = result_rows(service.snapshot(season["id"]))[0]
    assert row["status"] is row["date"] is row["start_time"] is None
