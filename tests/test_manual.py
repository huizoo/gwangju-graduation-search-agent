"""수기 확인·불확실성·날짜 연결의 실제 저장 동작을 검사한다."""
import pytest
from app.domain import ApiError
from test_storage import service, event, operation


def test_missing_confirmation_remains_management_record(service):
    e, s, _ = event(service, confirmed=False)
    assert not e["publication"]["calendar_eligible"]
    assert service.events(s["id"], "all")["total"] == 1
    assert service.events(s["id"], "calendar")["total"] == 0
    assert service.event_detail(e["id"])["event"]["fields"]["start_time"]["manual"]["value"] == "10:00"


def test_date_change_does_not_rebind_old_time(service):
    e, s, _ = event(service)
    service.apply_overrides(e["id"], {"expected_revision": e["revision"], "operations": [operation("event_date", {"precision": "day", "date": "2027-01-08", "month": None})]})
    publication = service.event_detail(e["id"])["event"]["publication"]
    assert publication["calendar_date"] == "2027-01-08"
    assert publication["calendar_time"] is None
    assert publication["current_date_allowed"] and not publication["sales_ready"]
    assert service.overview(s["id"])["time_confirmed_institutions"] == 0


def test_clear_release_preserves_history(service):
    e, _, _ = event(service)
    for index, action in enumerate(("clear", "release")):
        service.apply_overrides(e["id"], {"expected_revision": e["revision"] + index, "operations": [{"field": "start_time", "action": action, "note": ""}]})
    detail = service.event_detail(e["id"])
    assert detail["event"]["fields"]["start_time"]["manual"] is None
    with service.store.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM manual_overrides WHERE event_id=? AND field='start_time'", (e["id"],)).fetchone()[0] == 2
    assert {row["action"] for row in detail["history"]} >= {"set", "clear", "release"}


def test_future_confirmation_and_duplicate_fields_atomic(service):
    e, _, _ = event(service)
    revision = service.store.revision()
    with pytest.raises(ApiError):
        service.apply_overrides(e["id"], {"expected_revision": e["revision"], "operations": [operation("venue", "강당", bound_date=None, confirmed_at="2099-01-01T00:00:00+00:00")]})
    with pytest.raises(ApiError):
        service.apply_overrides(e["id"], {"expected_revision": e["revision"], "operations": [{"field": "venue", "action": "clear", "note": ""}, {"field": "venue", "action": "release", "note": ""}]})
    assert service.store.revision() == revision


def test_month_claim_preserved_without_invented_day(service):
    e, s, _ = event(service)
    service.apply_overrides(e["id"], {"expected_revision": e["revision"], "operations": [operation("event_date", {"precision": "month", "date": None, "month": "2027-01"})]})
    current = service.event_detail(e["id"])["event"]
    assert current["publication"]["calendar_date"] is None
    assert current["fields"]["event_date"]["manual"]["value"]["precision"] == "month"
    assert service.overview(s["id"])["month_only_institutions"] == 1
