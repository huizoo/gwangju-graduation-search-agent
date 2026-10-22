"""운영 조사 없이 구간 교체와 미전송 판단 비용을 실제 저장 경로에서 검사한다."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.agent.jobs import BudgetStop, dump
from app.agent.research import ResearchRunner
from app.integrations.providers import HttpGateway, ModelProvider
from app.integrations.providers_student import StudentProvider
from app.web.api import create_app
from test_agent_loop import decision, no_calendar, no_students, resolve, setup_case
from test_budget_diagnostics import setup_meter
from test_jobs_api import seed_job, settings_for


def runtime_job(tmp_path, count=4, **limits):
    """외부 연결 없는 작업의 고정 한도만 검사 경계로 설정한다."""
    runtime = create_app(settings_for(tmp_path)).state.runtime
    job = seed_job(runtime, count)
    with runtime.store.transaction(True) as conn:
        conn.execute("UPDATE jobs SET limits_json=? WHERE id=?", (
            dump({**job["limits"], "concurrency": 1, "segment_max_tokens": 20000, **limits}), job["id"]))
    return runtime, job


class TokenRunner:
    """기관마다 알려진 사용량을 정산해 실제 구간 경계를 넘긴다."""

    def __init__(self, settings, meter=None):
        self.meter = meter

    async def run(self, institution, season, identifiers):
        reservation = await self.meter.reserve("tokens", 14000)
        await self.meter.settle(reservation, 10000)
        return {"documents": [], "candidates": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 2])
async def test_segments_continue_same_queue_and_keep_cumulative_cost(tmp_path, concurrency):
    """구간 교체는 누적 비용·학교 ID를 보존하고 남은 기관을 모두 처리한다."""
    runtime, job = runtime_job(tmp_path, concurrency=concurrency)
    before = {item["id"] for item in runtime.jobs.items(job["id"])}
    runtime.jobs.runner_factory = TokenRunner
    await asyncio.wait_for(runtime.jobs.run(job["id"]), 5)
    result = runtime.jobs.get(job["id"])
    assert result["status"] == "completed"
    assert result["counts"]["done"] == 4
    assert result["usage"]["tokens"] == 40000
    assert {item["id"] for item in runtime.jobs.items(job["id"])} == before
    with runtime.store.transaction() as conn:
        segments = conn.execute("SELECT ended_at FROM job_segments WHERE job_id=?", (job["id"],)).fetchall()
        assert len(segments) == 4 and all(row[0] for row in segments)
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM call_reservations WHERE status='reserved'").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("limits, reason, segments", [
    ({"job_max_tokens": 25000}, "job_budget", 2),
    ({"segment_max_tokens": 13000}, "segment_budget", 1),
])
async def test_total_budget_and_impossible_request_do_not_loop(tmp_path, limits, reason, segments):
    """전체 한도와 빈 구간에도 불가능한 예약은 자동 재개하지 않는다."""
    runtime, job = runtime_job(tmp_path, **limits)
    runtime.jobs.runner_factory = TokenRunner
    await asyncio.wait_for(runtime.jobs.run(job["id"]), 5)
    result = runtime.jobs.get(job["id"])
    assert result["status"] == "paused" and result["paused_reason"] == reason
    with runtime.store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM job_segments").fetchone()[0] == segments


@pytest.mark.asyncio
@pytest.mark.parametrize("action, expected", [("pause", "paused"), ("cancel", "cancelled")])
async def test_user_intent_wins_over_segment_stop(tmp_path, action, expected):
    """구간 정리 도중 사용자가 멈추거나 취소하면 뒤늦은 한도가 덮어쓰지 않는다."""
    runtime, job = runtime_job(tmp_path)

    class Runner(TokenRunner):
        async def run(self, institution, season, identifiers):
            try:
                return await super().run(institution, season, identifiers)
            except BudgetStop:
                with runtime.store.transaction(True) as conn:
                    conn.execute("UPDATE jobs SET status='pausing',paused_reason='segment_budget' WHERE id=?", (job["id"],))
                getattr(runtime.jobs, action)(job["id"])
                raise

    runtime.jobs.runner_factory = Runner
    runtime.jobs.start(job["id"])
    await asyncio.wait_for(runtime.jobs.tasks[job["id"]], 5)
    result = runtime.jobs.get(job["id"])
    assert result["status"] == expected
    if action == "pause":
        assert result["paused_reason"] == "user_pause"
    with runtime.store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM job_segments").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_shutdown_never_starts_another_segment(tmp_path):
    """종료 요청이 있으면 구간 한도를 이유로 다시 시작하지 않는다."""
    runtime, job = runtime_job(tmp_path)
    runtime.jobs.runner_factory = TokenRunner
    runtime.jobs.stopping = True
    await runtime.jobs.run(job["id"])
    assert runtime.jobs.get(job["id"])["status"] == "paused"
    with runtime.store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM job_segments").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_elapsed_segment_continues_without_resetting_item_time(tmp_path):
    """벽시계 구간 만료도 같은 기관을 이어가되 이전 실행 비용을 남긴다."""
    runtime, job = runtime_job(tmp_path, count=1)

    class Runner(TokenRunner):
        expired = False

        async def run(self, institution, season, identifiers):
            if not Runner.expired:
                Runner.expired = True
                with runtime.store.transaction(True) as conn:
                    conn.execute("UPDATE job_segments SET started_at=? WHERE id=?", (
                        (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), self.meter.segment_id))
            return await super().run(institution, season, identifiers)

    runtime.jobs.runner_factory = Runner
    await asyncio.wait_for(runtime.jobs.run(job["id"]), 5)
    result = runtime.jobs.get(job["id"])
    assert result["status"] == "completed"
    assert result["usage"]["active_seconds"] >= 2
    with runtime.store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM job_segments").fetchone()[0] == 2
        history = json.loads(conn.execute("SELECT history_json FROM job_items").fetchone()[0])
        assert history[0]["details"]["limit_key"] == "segment_timeout_seconds"


@pytest.mark.asyncio
async def test_changed_conditions_block_automatic_continuation(tmp_path, monkeypatch):
    """구간 사이 조건 변경도 수동 재개와 같은 해시 검사로 차단한다."""
    runtime, job = runtime_job(tmp_path)
    finish = runtime.jobs._finish

    def changed_finish(job_id):
        finish(job_id)
        runtime.settings.data["publication"]["policy_version"] = "changed"

    monkeypatch.setattr(runtime.jobs, "_finish", changed_finish)
    runtime.jobs.runner_factory = TokenRunner
    await runtime.jobs.run(job["id"])
    result = runtime.jobs.get(job["id"])
    assert result["status"] == "paused"
    assert result["paused_reason"] == "configuration_required"
    with runtime.store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM job_segments").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_request", [False, True])
async def test_decision_charge_refunds_only_when_no_model_request_was_sent(tmp_path, monkeypatch, prior_request):
    """토큰 예약 거절은 판단을 환불하지만 전송 후 재시도 거절은 비용을 유지한다."""
    runtime, job, meter = setup_meter(tmp_path, segment_max_tokens=13000 if not prior_request else 20000)
    settings, institution, season = setup_case()
    settings.data["execution"]["llm_max_retries"] = 1
    calls = []
    monkeypatch.setattr(StudentProvider, "lookup", no_students)

    def respond(request):
        if request.url.host == "open.neis.go.kr":
            return no_calendar()
        calls.append(request)
        return httpx.Response(503, json={})

    async def decide(self, *args):
        await self._structured("공개 검사 지침", "합성 문맥", {"type": "object"})
        return decision()

    monkeypatch.setattr(ModelProvider, "decide", decide)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        gateway = HttpGateway(settings, meter=meter, client=client, resolver=resolve)
        with pytest.raises(BudgetStop) as stopped:
            await ResearchRunner(settings, meter=meter, gateway=gateway).run(
                institution, season, identifiers=[{"provider": "neis", "external_key": "F10:123"}])
    assert stopped.value.reason == "segment_budget"
    usage = runtime.jobs.get(job["id"])["usage"]
    assert len(calls) == usage["replans"] == int(prior_request)
    assert usage["llm_calls"] == int(prior_request)
    assert usage["tokens"] == (14000 if prior_request else 0)
