"""전체 날짜·인원 조사와 단일 행사 시간 조사의 큐 경계를 검사한다."""

import asyncio
import json
from uuid import uuid4

import pytest

from app.api import create_app
from app.domain import ApiError
from test_jobs_api import settings_for, institution_payload, job_payload, manual_event


def test_default_request_freezes_all_target_schools_independent_of_display(tmp_path):
    app = create_app(settings_for(tmp_path))
    runtime = app.state.runtime
    for i, kind in enumerate(("elementary", "middle", "high", "university", "junior_college", "kindergarten")):
        runtime.service.create_institution({**institution_payload(f"학교{i}"), "type": kind,
                                           "district": "동구" if i % 2 else "북구"})
    season = runtime.service.list_seasons()["items"][0]
    payload = {**job_payload(season["id"]), "purpose": "date_count", "target_event_id": None}
    wrong = {**payload, "selection": {"scope": "filter", "types": [], "districts": ["북구"], "query": ""}}
    with pytest.raises(ApiError, match="전체 학교"):
        runtime.jobs.create(wrong, str(uuid4()))
    job = runtime.jobs.create(payload, str(uuid4()))
    items = runtime.jobs.items(job["id"])
    assert len(items) == 5
    assert job["scope_snapshot"]["purpose"] == "date_count"
    assert job["scope_snapshot"]["research_version"] == "2.0"


@pytest.mark.parametrize("days", [None, ["2027-01-07", "2027-01-08"]])
def test_time_job_passes_exact_event_and_keeps_manual_values(tmp_path, days):
    captured = []
    class Runner:
        """큐가 넘긴 조사 목적·행사를 기록하는 격리 실행기."""
        def __init__(self, settings, meter=None):
            self.meter = meter

        async def run(self, institution, season, *, identifiers=None, purpose=None,
                      target_event=None, routes=None, existing_evidence=None):
            captured.append((institution, season, purpose, target_event))
            return {"documents": [], "candidates": [], "counts": [], "errors": [],
                    "agent_trace": [{"kind": "stop", "reason": "검사"}]}

    app = create_app(settings_for(tmp_path), runner_factory=Runner)
    runtime = app.state.runtime
    school = runtime.service.create_institution(institution_payload("시간학교"))
    other = runtime.service.create_institution(institution_payload("다른학교"))
    season = runtime.service.list_seasons()["items"][0]
    body = manual_event(school["id"], season["id"])
    if days:
        next(op["value"] for op in body["operations"] if op["field"] == "event_date")["dates"] = days
    event = runtime.service.create_event(body)
    payload = {**job_payload(season["id"], [other["id"]]), "purpose": "time", "target_event_id": event["id"]}
    with pytest.raises(ApiError, match="학교와"):
        runtime.jobs.create(payload, str(uuid4()))
    payload["selection"]["institution_ids"] = [school["id"]]
    job = runtime.jobs.create(payload, str(uuid4()))
    asyncio.run(runtime.jobs.run(job["id"]))
    assert len(captured) == 1 and captured[0][2] == "time"
    assert captured[0][3]["id"] == event["id"]
    assert captured[0][3]["event_date"]["date"] == event["publication"]["calendar_date"]
    assert captured[0][3]["event_date"].get("dates") == days
    assert len(runtime.service.events(season["id"])["items"]) == 1
    current = runtime.service.event_detail(event["id"])["event"]
    assert current["fields"]["event_date"]["manual"] == event["fields"]["event_date"]["manual"]
    with runtime.store.transaction() as conn:
        histories = [json.loads(r[0]) for r in conn.execute("SELECT history_json FROM job_items")]
        assert any("agent_trace" in h for history in histories for h in history)


def test_time_job_requires_current_allowed_date(tmp_path):
    """날짜가 보류된 행사에 시간 조사를 연결해 과거 시각을 되살리지 않는다."""
    runtime = create_app(settings_for(tmp_path)).state.runtime
    school = runtime.service.create_institution(institution_payload("날짜없는학교"))
    season = runtime.service.list_seasons()["items"][0]
    body = manual_event(school["id"], season["id"])
    body["operations"] = [op for op in body["operations"] if op["field"] == "ceremony_status"]
    event = runtime.service.create_event(body)
    payload = {**job_payload(season["id"], [school["id"]]), "purpose": "time", "target_event_id": event["id"]}
    with pytest.raises(ApiError, match="날짜를 먼저"):
        runtime.jobs.create(payload, str(uuid4()))
