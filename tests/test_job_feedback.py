"""합성 작업 이력으로 사용자 안내를 검사하며 실제 수집 성공과 구분한다."""

import json
from uuid import uuid4

import pytest

from app.job_feedback import season_research_summary, time_feedback
from app.storage import Service, Store
from test_jobs_api import institution_payload


@pytest.fixture
def context(tmp_path):
    """사용자 자료와 분리한 실제 SQLite 스키마와 기관 두 곳을 준비한다."""
    store = Store(tmp_path / "feedback.sqlite3")
    service = Service(store)
    season = service.create_season({"search_start": "2026-12-01", "search_end": "2027-03-15"})
    schools = [service.create_institution(institution_payload(f"안내검사{i}"))["id"] for i in range(2)]
    with store.transaction(True) as conn:
        yield conn, season["id"], schools


def _job(context, *, purpose="time", scope="ids", status="completed", targets=None,
         outcome="no_data", reasons=None, history=None, error=None, item_status="done", kind="schedule_scan"):
    """현재 저장 형태 그대로 작업과 마지막 항목 이력을 만든다."""
    conn, season, schools = context
    targets = schools[:1] if targets is None else targets
    identity = str(uuid4())
    request = {"selection": {"scope": scope}, "purpose": purpose}
    conn.execute(
        "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (identity, kind, season, status, None, json.dumps(request), json.dumps({"purpose": purpose}),
         "{}", "{}", len(targets), str(uuid4()), "hash", 1, "2026-10-21T00:00:00Z", "2026-10-21T00:00:00Z"))
    for target in targets:
        conn.execute(
            "INSERT INTO job_items(id,job_id,target_ref,institution_id,status,stage,outcome,"
            "reason_codes_json,usage_json,history_json,error_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid4()), identity, target, target, item_status, "save", outcome, json.dumps(reasons or []),
             "{}", json.dumps(history or []), json.dumps(error) if error else None))
    return identity


@pytest.mark.parametrize("code", ["access_failed", "tls_error", "unsupported_format", "document_partial", "read_failed"])
def test_completed_review_uses_source_failure_in_actual_history_shape(context, code):
    job = _job(context, outcome="needs_review", reasons=["provider_error"], history=[{
        "errors": [{"provider": "reader", "code": code, "message": "공식 원문 읽기 실패"}],
        "stop_reason": "agent_stop", "trace": [{"tool": "read_public_document", "status": code}]}])
    assert time_feedback(context[0], job) == "source_unavailable"


@pytest.mark.parametrize("provider", ["ollama", "openai", "tavily"])
def test_model_search_or_configuration_errors_are_not_not_found(context, provider):
    job = _job(context, outcome="needs_review", reasons=["provider_error"],
               history=[{"errors": [{"provider": provider, "code": "provider_error"}]}])
    assert time_feedback(context[0], job) == "service_error"


@pytest.mark.parametrize("reason", ["item_budget", "item_timeout", "document_limit", "decision_limit"])
def test_limits_are_incomplete_even_when_worker_is_completed(context, reason):
    job = _job(context, outcome="limit_reached", reasons=[reason])
    assert time_feedback(context[0], job) == "incomplete"


def test_no_data_success_and_old_attempt_failure_are_distinguished(context):
    missing = _job(context, history=[{"errors": [], "stop_reason": "agent_stop"}])
    assert time_feedback(context[0], missing) == "not_found"
    verified = _job(context, outcome="verified", reasons=["provider_error"],
                    history=[{"errors": [{"provider": "reader", "code": "access_failed"}]}])
    assert time_feedback(context[0], verified) is None
    retried = _job(context, history=[{"errors": [{"provider": "reader", "code": "access_failed"}]},
                                   {"errors": [], "stop_reason": "agent_stop"}])
    assert time_feedback(context[0], retried) == "not_found"


@pytest.mark.parametrize("status", ["queued", "running", "pausing", "paused", "cancelling", "cancelled"])
def test_job_control_states_stay_with_existing_status(context, status):
    job = _job(context, status=status, reasons=["provider_error"])
    assert time_feedback(context[0], job) is None


def test_non_time_and_unknown_job_have_no_time_feedback(context):
    assert time_feedback(context[0], str(uuid4())) is None
    assert time_feedback(context[0], _job(context, purpose="date_count", scope="all")) is None


def test_no_full_research_does_not_claim_no_saved_information(context):
    conn, season, schools = context
    initial = season_research_summary(conn, season, schools)
    assert initial["state"] == "not_started" and "전체 학교 조사" in initial["message"]
    _job(context)
    _job(context, kind="registry_sync", purpose="date_count", scope="all", targets=schools)
    assert season_research_summary(conn, season, schools) == initial


def test_selected_subset_never_becomes_full_research_completion(context):
    conn, season, schools = context
    _job(context, purpose="date_count", scope="ids", targets=schools)
    assert season_research_summary(conn, season, schools)["state"] == "partial"


@pytest.mark.parametrize("status,expected", [
    ("queued", "running"), ("running", "running"), ("pausing", "running"),
    ("cancelling", "running"), ("paused", "paused"), ("cancelled", "cancelled"),
    ("failed", "failed"), ("completed", "completed"),
])
def test_full_job_preserves_control_status_and_completion_does_not_confirm_all_dates(context, status, expected):
    conn, season, schools = context
    _job(context, purpose="date_count", scope="all", targets=schools, status=status)
    result = season_research_summary(conn, season, schools)
    assert result["state"] == expected
    if expected == "completed":
        assert "모든 일정이 확인된 것은 아니" in result["message"]


def test_actual_target_identity_matters_more_than_equal_count(context):
    conn, season, schools = context
    _job(context, purpose="date_count", scope="all", targets=schools[:1])
    assert season_research_summary(conn, season, schools[1:])["state"] == "partial"
    assert season_research_summary(conn, season, schools)["state"] == "partial"


def test_new_time_job_does_not_replace_latest_full_research(context):
    conn, season, schools = context
    _job(context, purpose="date_count", scope="all", targets=schools)
    _job(context, status="running")
    assert season_research_summary(conn, season, schools)["state"] == "completed"
    _job(context, purpose="date_count", scope="all", targets=schools, status="paused")
    paused = season_research_summary(conn, season, schools)
    assert paused["state"] == "paused" and "이어서 조사할 수" in paused["message"]


@pytest.mark.parametrize("reason", ["job_budget", "configuration_required"])
def test_non_resumable_pause_guides_to_new_research_of_same_season(context, reason):
    """기존 조건으로 재개할 수 없는 작업은 실제 화면의 다시 조사 동선을 안내한다."""
    conn, season, schools = context
    job = _job(context, purpose="date_count", scope="all", targets=schools, status="paused")
    conn.execute("UPDATE jobs SET paused_reason=? WHERE id=?", (reason, job))
    result = season_research_summary(conn, season, schools)
    assert result["state"] == "paused"
    assert "'이 시즌 다시 조사'" in result["message"]
    assert "이어서 조사할 수" not in result["message"]


def test_source_failure_or_unprocessed_targets_leave_partial_full_research(context):
    conn, season, schools = context
    _job(context, purpose="date_count", scope="all", targets=schools, outcome="needs_review",
         reasons=["provider_error"], history=[{"errors": [{"provider": "reader", "code": "tls_error"}]}])
    assert season_research_summary(conn, season, schools)["state"] == "partial"
    _job(context, purpose="date_count", scope="all", targets=schools, item_status="failed")
    assert season_research_summary(conn, season, schools)["state"] == "failed"


def test_legacy_all_request_without_purpose_is_still_checked_against_queue(context):
    conn, season, schools = context
    job = _job(context, purpose="date_count", scope="all", targets=schools)
    conn.execute("UPDATE jobs SET request_json=?,scope_snapshot_json='{}' WHERE id=?",
                 (json.dumps({"selection": {"scope": "all"}, "mode": "initial"}), job))
    assert season_research_summary(conn, season, schools)["state"] == "completed"
    conn.execute("DELETE FROM job_items WHERE job_id=? AND institution_id=?", (job, schools[0]))
    assert season_research_summary(conn, season, schools)["state"] == "partial"
