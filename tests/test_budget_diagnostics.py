"""임시 DB와 모의 전송으로 한도 판정·진단 이력·재개 보존을 검사한다."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.web.api import create_app
from app.agent.jobs import BudgetStop, Meter, dump, empty_usage, now
from app.integrations.providers import HttpGateway, ModelProvider
from test_jobs_api import seed_job, settings_for


def setup_meter(tmp_path, **limits):
    """운영 설정을 읽지 않는 임시 학교 작업과 실행 중 계량기를 만든다."""
    runtime = create_app(settings_for(tmp_path)).state.runtime
    job = seed_job(runtime, 1)
    item, segment_id = runtime.jobs.items(job["id"])[0], str(uuid4())
    with runtime.store.transaction(True) as conn:
        conn.execute("UPDATE jobs SET status='running',limits_json=? WHERE id=?",
                     (dump({**job["limits"], **limits}), job["id"]))
        conn.execute("INSERT INTO job_segments VALUES(?,?,?,?,?,?,?)",
                     (segment_id, job["id"], 1, now(), None, now(), dump(empty_usage())))
    return runtime, job, Meter(runtime.jobs, job["id"], item["id"], segment_id)


def stored_usage(runtime, meter):
    """기관·작업·구간 사용량과 예약 수를 같은 DB 읽기에서 반환한다."""
    with runtime.store.transaction() as conn:
        return {
            **{table: json.loads(conn.execute(f"SELECT usage_json FROM {table} WHERE id=?", (identity,)).fetchone()[0])
               for table, identity in (("jobs", meter.job_id), ("job_items", meter.item_id), ("job_segments", meter.segment_id))},
            "reservations": conn.execute("SELECT count(*) FROM call_reservations").fetchone()[0],
        }


def test_existing_job_keeps_its_limit_when_new_default_is_raised(tmp_path):
    """새 기본값은 새 조사에만 적용하고 기존 작업의 고정 한도를 덮어쓰지 않는다."""
    settings = settings_for(tmp_path)
    settings.data["execution"]["item_max_tokens"] = 24000
    runtime = create_app(settings).state.runtime
    existing = seed_job(runtime, 1)
    settings.data["execution"]["item_max_tokens"] = 48000
    restarted = create_app(settings).state.runtime
    assert restarted.jobs.get(existing["id"])["limits"]["item_max_tokens"] == 24000
    restarted.jobs.cancel(existing["id"])
    new_job = seed_job(restarted, 1)
    assert new_job["limits"]["item_max_tokens"] == 48000
    assert restarted.jobs.get(existing["id"])["limits"]["item_max_tokens"] == 24000


@pytest.mark.parametrize("limit, successful_calls", [(24000, 3), (48000, 4)])
@pytest.mark.asyncio
async def test_fourth_model_request_reserves_headroom_before_actual_http(tmp_path, limit, successful_calls):
    """24,000과 48,000을 같은 실측량 모의 응답으로 비교하고 미전송 호출 환불도 확인한다."""
    runtime, job, meter = setup_meter(tmp_path, item_max_tokens=limit)
    config = SimpleNamespace(data=deepcopy(runtime.settings.data), secrets={})
    config.data["app"]["llm_backend"] = "ollama"
    config.data["execution"]["per_host_min_interval_ms"] = 0
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={
            "message": {"content": '{"ok":true}'}, "prompt_eval_count": 3400, "eval_count": 164,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = ModelProvider(config, gateway=HttpGateway(config, meter=meter, client=client))
        schema = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
        for _ in range(3):
            assert await model._structured("검사 지침", "합성 공개 자료", schema) == {"ok": True}
        if limit == 24000:
            before = stored_usage(runtime, meter)
            with pytest.raises(BudgetStop) as stopped:
                await model._structured("검사 지침", "합성 공개 자료", schema)
            assert stopped.value.reason == "item_budget"
            assert stopped.value.details == {
                "scope": "item", "metric": "tokens", "limit_key": "item_max_tokens",
                "used": 10692, "requested": 14000, "limit": 24000,
                "reserved_tokens": 0, "unknown_reserved_tokens": 0,
            }
            after = stored_usage(runtime, meter)
            assert all(after[table] == before[table] for table in ("jobs", "job_items", "job_segments"))
        else:
            assert await model._structured("검사 지침", "합성 공개 자료", schema) == {"ok": True}
    usage = runtime.jobs.get(job["id"])["usage"]
    assert len(calls) == usage["http_requests"] == usage["llm_calls"] == successful_calls
    assert usage["tokens"] == successful_calls * 3564 and usage["reserved_tokens"] == 0


@pytest.mark.parametrize("scope, metric, maximum", [
    ("item", "search_calls", 1), ("job", "http_requests", 1),
    ("segment", "tokens", 14000), ("item", "tokens", 24000),
])
@pytest.mark.asyncio
async def test_exact_limit_is_allowed_and_failed_reservation_has_no_charge(tmp_path, scope, metric, maximum):
    """합계가 한도와 같으면 허용하고 하나라도 넘으면 숫자 근거를 남기며 차감하지 않는다."""
    limit_key = f"{scope}_max_{metric}"
    runtime, _, meter = setup_meter(tmp_path, **{limit_key: maximum})
    reserved = await meter.reserve(metric, maximum)
    await meter.settle(reserved, maximum)
    before = stored_usage(runtime, meter)
    with pytest.raises(BudgetStop) as stopped:
        await meter.reserve(metric, 1)
    assert stopped.value.reason == f"{scope}_budget"
    assert stopped.value.details.items() >= {
        "scope": scope, "metric": metric, "limit_key": limit_key,
        "used": maximum, "requested": 1, "limit": maximum,
    }.items()
    assert stored_usage(runtime, meter) == before


@pytest.mark.parametrize("scope", ["item", "job", "segment"])
@pytest.mark.asyncio
async def test_unknown_tokens_remain_charged_and_identifiable_in_diagnostics(tmp_path, scope):
    """응답 유실 14,000은 실측 7,053과 구분하며 거절된 다음 예약으로 증가하지 않는다."""
    limits = {"item_max_tokens": 48000, f"{scope}_max_tokens": 24000}
    runtime, _, meter = setup_meter(tmp_path, **limits)
    measured = await meter.reserve("tokens", 14000)
    await meter.settle(measured, 7053)
    unknown = await meter.reserve("tokens", 14000)
    await meter.settle(unknown)
    before = stored_usage(runtime, meter)
    with pytest.raises(BudgetStop) as stopped:
        await meter.reserve("tokens", 14000)
    assert stopped.value.details == {
        "scope": scope, "metric": "tokens", "limit_key": f"{scope}_max_tokens",
        "used": 21053, "requested": 14000, "limit": 24000,
        "reserved_tokens": 14000, "unknown_reserved_tokens": 14000,
    }
    assert stored_usage(runtime, meter) == before
    with runtime.store.transaction() as conn:
        row = conn.execute("SELECT status,actual_json FROM call_reservations WHERE id=?", (unknown,)).fetchone()
        assert tuple(row) == ("charged_unknown", None)


@pytest.mark.parametrize("scope, limit_key, reason", [
    ("item", "item_timeout_seconds", "item_timeout"),
    ("job", "job_max_active_seconds", "job_budget"),
    ("segment", "segment_timeout_seconds", "segment_budget"),
])
@pytest.mark.asyncio
async def test_time_limits_report_used_seconds_without_extra_charge(tmp_path, scope, limit_key, reason):
    """재시도 대기 검사와 호출 전 시간 검사 모두 실제 적용 범위를 설명한다."""
    runtime, _, meter = setup_meter(tmp_path, **{limit_key: 1})
    if scope == "segment":
        with runtime.store.transaction(True) as conn:
            conn.execute("UPDATE job_segments SET started_at=? WHERE id=?",
                         ((datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat(), meter.segment_id))
    else:
        meter.started -= 2
    before = stored_usage(runtime, meter)
    for operation in (meter.check_active, lambda: meter.reserve("http_requests")):
        with pytest.raises(BudgetStop) as stopped:
            await operation()
        details = stopped.value.details
        assert stopped.value.reason == reason
        assert details["scope"] == scope and details["limit_key"] == limit_key
        assert details["metric"] == "active_seconds" and details["used"] >= 2
        assert details["requested"] == 0 and details["limit"] == 1
    assert stored_usage(runtime, meter) == before


@pytest.mark.parametrize("scope", ["item", "job", "segment"])
@pytest.mark.asyncio
async def test_worker_persists_budget_details_and_resume_retains_history(tmp_path, scope):
    """실행기에서 발생한 한도 이력을 저장하고 재시작·재개해도 이전 근거를 유지한다."""
    settings = settings_for(tmp_path)
    settings.data["execution"][f"{scope}_max_tokens"] = 24000
    runtime = create_app(settings).state.runtime
    job = seed_job(runtime, 1)

    class LimitedRunner:
        def __init__(self, _settings, meter=None):
            self.meter = meter

        async def run(self, _institution, _season, identifiers):
            first = await self.meter.reserve("tokens", 14000)
            await self.meter.settle(first, 10692)
            second = await self.meter.reserve("tokens", 14000)
            await self.meter.settle(second, 10692)
            return {"documents": [], "candidates": [], "errors": []}

    runtime.jobs.runner_factory = LimitedRunner
    await runtime.jobs.run(job["id"])
    with runtime.store.transaction() as conn:
        history = json.loads(conn.execute("SELECT history_json FROM job_items WHERE job_id=?", (job["id"],)).fetchone()[0])
    assert len(history) == 1 and history[0]["stage"] == "budget_stop"
    assert history[0]["reason"] == f"{scope}_budget"
    assert history[0]["details"]["used"] == 10692
    assert history[0]["details"]["requested"] == 14000
    item = runtime.jobs.items(job["id"])[0]
    assert item["outcome"] == "limit_reached"
    assert item["reason_codes"] == [f"{scope}_budget"] and item["error"] is None
    runtime = create_app(settings).state.runtime
    assert runtime.jobs.get(job["id"])["limits"][f"{scope}_max_tokens"] == 24000
    if scope != "item":
        runtime.jobs.resume(job["id"], {
            "expected_revision": runtime.jobs.get(job["id"])["revision"],
            "increase_limits": {"job_max_tokens": 48000} if scope == "job" else None,
        })
        runtime.jobs.runner_factory = LimitedRunner
        await runtime.jobs.run(job["id"])
    with runtime.store.transaction() as conn:
        saved = json.loads(conn.execute("SELECT history_json FROM job_items WHERE job_id=?", (job["id"],)).fetchone()[0])
    assert saved[:len(history)] == history


@pytest.mark.parametrize("reason", ["user_cancel", "user_pause", "interrupted"])
@pytest.mark.asyncio
async def test_user_control_is_not_recorded_as_budget_exhaustion(tmp_path, reason):
    """사용자 중단과 취소에는 예산 숫자를 만들어 저장하지 않는다."""
    runtime = create_app(settings_for(tmp_path)).state.runtime
    job = seed_job(runtime, 1)

    class StoppedRunner:
        def __init__(self, _settings, meter=None):
            pass

        async def run(self, _institution, _season, identifiers):
            if reason == "interrupted":
                raise asyncio.CancelledError
            raise BudgetStop(reason)

    runtime.jobs.runner_factory = StoppedRunner
    await runtime.jobs.run(job["id"])
    with runtime.store.transaction() as conn:
        history = json.loads(conn.execute("SELECT history_json FROM job_items WHERE job_id=?", (job["id"],)).fetchone()[0])
    assert not any(entry.get("stage") == "budget_stop" for entry in history)


@pytest.mark.asyncio
async def test_worker_deadline_records_the_original_time_limit(tmp_path):
    """실행 대기 기한이 끝나도 고정한 시간 상한과 실제 경과시간을 기록한다."""
    settings = settings_for(tmp_path)
    runtime = create_app(settings).state.runtime
    job = seed_job(runtime, 1)
    with runtime.store.transaction(True) as conn:
        conn.execute("UPDATE jobs SET limits_json=? WHERE id=?",
                     (dump({**job["limits"], "item_timeout_seconds": 1}), job["id"]))

    class SlowRunner:
        def __init__(self, _settings, meter=None):
            pass

        async def run(self, _institution, _season, identifiers):
            await asyncio.sleep(2)

    runtime.jobs.runner_factory = SlowRunner
    await runtime.jobs.run(job["id"])
    with runtime.store.transaction() as conn:
        history = json.loads(conn.execute("SELECT history_json FROM job_items WHERE job_id=?", (job["id"],)).fetchone()[0])
    stop = history[-1]
    assert stop["stage"] == "budget_stop" and stop["reason"] == "item_timeout"
    assert stop["details"]["limit"] == 1 and stop["details"]["requested"] == 0
    assert stop["details"]["used"] >= 1
