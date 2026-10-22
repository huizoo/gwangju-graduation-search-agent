"""외부 요청 없이 재시도 분류·대기·취소·영속 사용량을 검사한다."""

import asyncio
import errno
import json
import socket
import ssl
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.integrations import retry
from app.agent.jobs import BudgetStop, Meter, dump, empty_usage, now
from app.integrations.providers import HttpGateway, ModelProvider, ProviderError


SCHEMA = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
MODEL_BODY = {"message": {"content": '{"ok": true}'}, "prompt_eval_count": 4, "eval_count": 3}


def settings():
    """운영 설정·키를 읽지 않는 독립 호출 설정을 만든다."""
    data = json.loads(Path("config/defaults.json").read_text())
    data["execution"].update(per_host_min_interval_ms=0, http_max_retries=2, llm_max_retries=2)
    return SimpleNamespace(data=data, secrets={})


@pytest.fixture
def clock(monkeypatch):
    """재시도 대기만 가상 시계로 진행하며 실제 이벤트 루프는 유지한다."""
    state = SimpleNamespace(seconds=0.0, sleeps=[])

    async def sleep(seconds):
        state.sleeps.append(seconds)
        state.seconds += seconds

    monkeypatch.setattr(retry, "sleep", sleep)
    monkeypatch.setattr(retry, "monotonic", lambda: state.seconds)
    return state


def gateway(handler, *, config=None, meter=None):
    """MockTransport로만 응답하는 게이트웨이를 준비한다."""
    return HttpGateway(config or settings(), meter=meter,
                       client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def runtime_job(tmp_path):
    """임시 DB의 실제 JobManager와 대기 중인 학교 작업을 준비한다."""
    from app.web.api import create_app
    from app.core.config import load_settings
    from test_jobs_api import seed_season

    config = load_settings(process_values={
        "APP_MODE": "demo", "LLM_BACKEND": "fake", "APP_DATA_DIR": str(tmp_path), "AUTO_OPEN_BROWSER": "false",
    }, dotenv_path=tmp_path / "missing.env")
    runtime = create_app(config).state.runtime
    school = runtime.service.create_institution({
        "name": "검사학교", "type": "high", "district": "북구", "address": "광주광역시 북구 검사로 1",
        "campus_label": None, "phone": "062-000-0000", "homepage_url": None,
    })
    season = seed_season(runtime)
    job = runtime.jobs.create({
        "kind": "schedule_scan", "season_id": season["id"], "mode": "recheck",
        "selection": {"scope": "ids", "institution_ids": [school["id"]]}, "limits": None,
    }, str(uuid4()))
    return runtime, job


async def invoke(layer, transport):
    """HTTP와 모델의 별도 재시도 경계를 같은 응답으로 검사한다."""
    if layer == "http":
        return await transport.request("GET", "https://school.example/notice")
    return await ModelProvider(transport.settings, gateway=transport)._structured("공개 지침", "공개 자료", SCHEMA)


@pytest.mark.parametrize("layer", ["http", "model"])
@pytest.mark.parametrize("error", [
    httpx.ConnectTimeout("timeout"), httpx.ReadTimeout("timeout"), httpx.ConnectError("connection reset"),
    httpx.ReadError("connection lost"), ConnectionResetError("reset"), OSError(errno.EHOSTUNREACH, "unreachable"),
])
@pytest.mark.asyncio
async def test_transient_error_waits_then_succeeds(layer, error, clock):
    calls = []

    def handler(_):
        calls.append(clock.seconds)
        if len(calls) == 1:
            raise error
        return httpx.Response(200, json=MODEL_BODY)

    await invoke(layer, gateway(handler))
    assert calls == [0, 0.25]


@pytest.mark.parametrize("layer", ["http", "model"])
@pytest.mark.parametrize("error", [
    httpx.RemoteProtocolError("invalid header"), httpx.UnsupportedProtocol("invalid scheme"),
    httpx.DecodingError("invalid encoding"), ValueError("invalid input"),
    OSError(errno.EACCES, "permission denied"), socket.gaierror(socket.EAI_NONAME, "no such host"),
    httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]"), ssl.SSLError("handshake failed"),
])
@pytest.mark.asyncio
async def test_persistent_transport_errors_are_called_once(layer, error, clock):
    calls = []

    def handler(_):
        calls.append(True)
        raise error

    with pytest.raises(ProviderError):
        await invoke(layer, gateway(handler))
    assert len(calls) == 1 and not clock.sleeps


@pytest.mark.asyncio
async def test_wrapped_tls_error_is_not_retried(clock):
    calls = []

    def handler(_):
        calls.append(True)
        try:
            raise ssl.SSLCertVerificationError("certificate failure")
        except ssl.SSLError as cause:
            raise httpx.ConnectError("connection failed") from cause

    with pytest.raises(ProviderError) as caught:
        await invoke("model", gateway(handler))
    assert caught.value.code == "tls_error"
    assert len(calls) == 1 and not clock.sleeps


@pytest.mark.parametrize("cause", [
    socket.gaierror(socket.EAI_NONAME, "no such host"), OSError(errno.EACCES, "permission denied"),
])
@pytest.mark.asyncio
async def test_wrapped_persistent_connection_cause_is_not_retried(cause, clock):
    calls = []

    def handler(_):
        calls.append(True)
        raise httpx.ConnectError("connection failed") from cause

    with pytest.raises(ProviderError):
        await invoke("model", gateway(handler))
    assert len(calls) == 1 and not clock.sleeps


@pytest.mark.parametrize("layer", ["http", "model"])
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 501])
@pytest.mark.asyncio
async def test_persistent_http_status_does_not_retry(layer, status, clock):
    calls = []

    def handler(_):
        calls.append(True)
        return httpx.Response(status, json={"error": "rejected"})

    if layer == "model":
        with pytest.raises(ProviderError):
            await invoke(layer, gateway(handler))
    else:
        assert (await invoke(layer, gateway(handler))).status_code == status
    assert len(calls) == 1 and not clock.sleeps


@pytest.mark.parametrize("layer", ["http", "model"])
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
@pytest.mark.asyncio
async def test_existing_transient_statuses_wait_then_succeed(layer, status, clock):
    calls = []

    def handler(_):
        calls.append(clock.seconds)
        return httpx.Response(status if len(calls) == 1 else 200, json=MODEL_BODY)

    await invoke(layer, gateway(handler))
    assert calls == [0, 0.25]


@pytest.mark.parametrize("layer", ["http", "model"])
@pytest.mark.parametrize("limit", [0, 1, 2])
@pytest.mark.parametrize("failure", ["timeout", "status"])
@pytest.mark.asyncio
async def test_configured_attempt_limit_is_not_multiplied(layer, limit, failure, clock):
    config, calls = settings(), []
    config.data["execution"][f"{'http' if layer == 'http' else 'llm'}_max_retries"] = limit

    def handler(_):
        calls.append(clock.seconds)
        if failure == "timeout":
            raise httpx.ReadTimeout("temporary")
        return httpx.Response(503)

    if layer == "model" or failure == "timeout":
        with pytest.raises(ProviderError):
            await invoke(layer, gateway(handler, config=config))
    else:
        assert (await invoke(layer, gateway(handler, config=config))).status_code == 503
    assert calls == [0, 0.25, 0.75][:limit + 1]


@pytest.mark.parametrize("layer", ["http", "model"])
@pytest.mark.parametrize("headers", [{"content-length": "invalid"}, {"content-length": "999999999"}])
@pytest.mark.asyncio
async def test_document_format_and_size_errors_are_not_model_retries(layer, headers, clock):
    calls = []

    def handler(_):
        calls.append(True)
        return httpx.Response(200, content=b"body", headers=headers)

    with pytest.raises(ProviderError):
        await invoke(layer, gateway(handler))
    assert len(calls) == 1 and not clock.sleeps


@pytest.mark.parametrize("body", [
    {"message": {"content": "not json"}}, {"message": {"content": '{"ok": 123}'}}, {"wrong": []},
])
@pytest.mark.asyncio
async def test_model_json_error_returns_to_agent_without_transport_retry(body, clock):
    calls = []

    def handler(_):
        calls.append(True)
        return httpx.Response(200, json=body)

    with pytest.raises(ProviderError) as caught:
        await invoke("model", gateway(handler))
    assert caught.value.code == "parse_failed"
    assert len(calls) == 1 and not clock.sleeps


@pytest.mark.parametrize("layer", ["http", "model"])
@pytest.mark.parametrize("header", ["2", "Thu, 22 Oct 2026 00:00:02 GMT"])
@pytest.mark.asyncio
async def test_retry_after_seconds_and_http_date_are_honored(layer, header, clock, monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, _tz=None):
            return datetime(2026, 10, 22, tzinfo=timezone.utc)

    monkeypatch.setattr(retry, "datetime", FixedDatetime)
    calls = []

    def handler(_):
        calls.append(clock.seconds)
        return httpx.Response(503 if len(calls) == 1 else 200,
                              headers={"Retry-After": header}, json=MODEL_BODY)

    await invoke(layer, gateway(handler))
    assert calls == [0, 2.0]


@pytest.mark.parametrize("header", ["", "bad date", "-1", "0.5", "NaN", "infinity"])
def test_broken_retry_after_uses_short_backoff(header):
    assert retry.retry_delay(0, httpx.Response(503, headers={"Retry-After": header})) == 0.25


def test_expired_date_and_zero_do_not_remove_backoff():
    current = datetime(2026, 10, 22, tzinfo=timezone.utc)
    for header in ("0", format_datetime(current - timedelta(days=1), usegmt=True)):
        assert retry.retry_delay(2, httpx.Response(429, headers={"Retry-After": header}), now=current) == 1.0
    assert retry.retry_delay(1000) == 2.0


@pytest.mark.parametrize("layer", ["http", "model"])
@pytest.mark.parametrize("header", ["31", "9" * 400, "Thu, 22 Oct 2099 00:00:00 GMT"])
@pytest.mark.asyncio
async def test_long_retry_after_is_not_shortened_to_make_another_call(layer, header, clock):
    calls = []

    def handler(_):
        calls.append(True)
        return httpx.Response(429, headers={"Retry-After": header})

    if layer == "model":
        with pytest.raises(ProviderError):
            await invoke(layer, gateway(handler))
    else:
        assert (await invoke(layer, gateway(handler))).status_code == 429
    assert len(calls) == 1 and not clock.sleeps


@pytest.mark.parametrize("layer", ["http", "model"])
@pytest.mark.asyncio
async def test_retry_after_exceeding_remaining_budget_stops_before_wait(layer, clock):
    class ShortMeter:
        async def reserve(self, *_):
            return None

        async def check_active(self):
            return 0.5

    calls = []

    def handler(_):
        calls.append(True)
        return httpx.Response(503, headers={"Retry-After": "1"})

    if layer == "model":
        with pytest.raises(ProviderError):
            await invoke(layer, gateway(handler, meter=ShortMeter()))
    else:
        assert (await invoke(layer, gateway(handler, meter=ShortMeter()))).status_code == 503
    assert len(calls) == 1 and not clock.sleeps


@pytest.mark.parametrize("layer", ["http", "model"])
@pytest.mark.parametrize("reason", ["user_cancel", "user_pause", "item_timeout"])
@pytest.mark.asyncio
async def test_wait_propagates_cancellation_and_budget_stop_without_another_call(layer, reason, clock):
    stopped, calls = BudgetStop(reason), []

    class StoppingMeter:
        async def reserve(self, *_):
            return None

        async def check_active(self):
            if clock.seconds > 0:
                raise stopped
            return 10.0

    def handler(_):
        calls.append(True)
        return httpx.Response(503, headers={"Retry-After": "2"})

    with pytest.raises(BudgetStop) as caught:
        await invoke(layer, gateway(handler, meter=StoppingMeter()))
    assert caught.value is stopped
    assert len(calls) == 1 and clock.seconds == 0.1


@pytest.mark.asyncio
async def test_direct_task_cancellation_interrupts_retry_wait():
    calls, waiting = [], asyncio.Event()

    def handler(_):
        calls.append(True)
        waiting.set()
        return httpx.Response(503, headers={"Retry-After": "2"})

    task = asyncio.create_task(invoke("model", gateway(handler)))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_response_lost_reservations_remain_charged_in_real_meter(tmp_path, clock):
    """응답 없는 시도와 성공한 시도를 실제 SQLite 예약으로 따로 정산한다."""
    runtime, job = runtime_job(tmp_path)
    item, segment_id = runtime.jobs.items(job["id"])[0], str(uuid4())
    with runtime.store.transaction(True) as conn:
        conn.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
        conn.execute("INSERT INTO job_segments VALUES(?,?,?,?,?,?,?)",
                     (segment_id, job["id"], 1, now(), None, now(), dump(empty_usage())))
    meter = Meter(runtime.jobs, job["id"], item["id"], segment_id)
    calls = []

    def handler(_):
        calls.append(True)
        if len(calls) == 1:
            raise httpx.ReadTimeout("response lost")
        return httpx.Response(200, json=MODEL_BODY)

    request_settings = settings()
    request_settings.data["execution"].update(llm_max_input_tokens=128, llm_max_output_tokens=32)
    transport = gateway(handler, config=request_settings, meter=meter)
    model = ModelProvider(transport.settings, gateway=transport)
    assert await model._structured("공개 지침", "공개 자료", SCHEMA) == {"ok": True}
    amount = sum(transport.execution[key] for key in ("llm_max_input_tokens", "llm_max_output_tokens"))
    usage = runtime.jobs.get(job["id"])["usage"]
    assert usage["http_requests"] == usage["llm_calls"] == 2
    assert usage["tokens"] == amount + 7 and usage["reserved_tokens"] == amount
    assert model.last_usage == {"input_tokens": 4, "output_tokens": 3, "total_tokens": 7}
    with runtime.store.transaction() as conn:
        reservations = conn.execute("SELECT status,reserved_json,actual_json FROM call_reservations").fetchall()
    assert len(reservations) == 6
    tokens = [row for row in reservations if "tokens" in json.loads(row["reserved_json"])]
    assert [(row["status"], row["actual_json"]) for row in tokens] == [
        ("charged_unknown", None), ("settled", dump({"tokens": 7})),
    ]
    assert all(row["status"] == "settled" for row in reservations if row not in tokens)


@pytest.mark.parametrize("layer", ["http", "model"])
@pytest.mark.parametrize("action, status, item_status, reason", [
    ("cancel", "cancelled", "cancelled", "user_cancel"),
    ("pause", "paused", "pending", "user_pause"),
])
@pytest.mark.asyncio
async def test_real_job_control_interrupts_retry_wait_and_settles_active_time(
    tmp_path, monkeypatch, layer, action, status, item_status, reason,
):
    """실제 작업 제어가 대기 중 추가 호출을 막고 활성시간 선예약을 정산한다."""
    runtime, job = runtime_job(tmp_path)
    waiting, release, calls = asyncio.Event(), asyncio.Event(), []
    request_settings = settings()
    request_settings.data["execution"].update(llm_max_input_tokens=128, llm_max_output_tokens=32)

    async def controlled_sleep(_seconds):
        waiting.set()
        await release.wait()

    def handler(_):
        calls.append(True)
        return httpx.Response(503, headers={"Retry-After": "2"})

    class RetryingRunner:
        def __init__(self, _settings, meter=None):
            self.meter = meter

        async def run(self, _institution, _season, identifiers):
            transport = gateway(handler, config=request_settings, meter=self.meter)
            try:
                await invoke(layer, transport)
            finally:
                await transport.client.aclose()
            raise AssertionError("중단된 재시도는 정상 결과를 반환하지 않는다")

    monkeypatch.setattr(retry, "sleep", controlled_sleep)
    runtime.jobs.runner_factory = RetryingRunner
    runtime.jobs.start(job["id"])
    task = runtime.jobs.tasks[job["id"]]
    try:
        await asyncio.wait_for(waiting.wait(), 5)
        before = runtime.jobs.get(job["id"])
        assert before["usage"]["active_seconds"] > 1
        getattr(runtime.jobs, action)(job["id"])
        release.set()
        await asyncio.wait_for(task, 5)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    saved, item = runtime.jobs.get(job["id"]), runtime.jobs.items(job["id"])[0]
    assert saved["status"] == status and item["status"] == item_status
    assert reason in item["reason_codes"]
    assert len(calls) == 1 and saved["usage"]["http_requests"] == 1
    assert 1 <= saved["usage"]["active_seconds"] < before["usage"]["active_seconds"]
    if layer == "model":
        assert saved["usage"]["llm_calls"] == 1
        assert saved["usage"]["tokens"] == saved["usage"]["reserved_tokens"] == 160
    with runtime.store.transaction() as conn:
        reservations = conn.execute("SELECT status,reserved_json,actual_json FROM call_reservations").fetchall()
    assert all(row["status"] != "reserved" for row in reservations)
    active = next(row for row in reservations if "active_seconds" in json.loads(row["reserved_json"]))
    assert active["status"] == "settled"
    assert json.loads(active["actual_json"]) == {"active_seconds": saved["usage"]["active_seconds"]}
    if layer == "model":
        tokens = next(row for row in reservations if "tokens" in json.loads(row["reserved_json"]))
        assert tokens["status"] == "charged_unknown" and tokens["actual_json"] is None


@pytest.mark.parametrize("reason, job_limit, segment_limit, segment_age, remaining, elapsed", [
    ("item_timeout", 100, 100, 0, 13, 15.1),
    ("job_budget", 45, 100, 0, 8, 10.1),
    ("segment_budget", 100, 10, 3, 7, 2),
])
@pytest.mark.asyncio
async def test_real_meter_remaining_time_matches_existing_active_reservations(
    tmp_path, clock, reason, job_limit, segment_limit, segment_age, remaining, elapsed,
):
    """이전 사용량과 본인 선예약을 구분하며 읽기 검사와 예약의 중단 사유를 맞춘다."""
    import time

    runtime, job = runtime_job(tmp_path)
    item, segment_id = runtime.jobs.items(job["id"])[0], str(uuid4())
    limits = {**job["limits"], "item_timeout_seconds": 20,
              "job_max_active_seconds": job_limit, "segment_timeout_seconds": segment_limit}
    segment_started = (datetime.now(timezone.utc) - timedelta(seconds=segment_age)).isoformat()
    job_usage, item_usage = {**empty_usage(), "active_seconds": 35}, {**empty_usage(), "active_seconds": 5}
    with runtime.store.transaction(True) as conn:
        conn.execute("UPDATE jobs SET status='running',limits_json=?,usage_json=? WHERE id=?",
                     (dump(limits), dump(job_usage), job["id"]))
        conn.execute("UPDATE job_items SET usage_json=? WHERE id=?", (dump(item_usage), item["id"]))
        conn.execute("INSERT INTO job_segments VALUES(?,?,?,?,?,?,?)",
                     (segment_id, job["id"], 1, segment_started, None, now(), dump(job_usage)))
    claimed = await runtime.jobs._claim_active_time(job["id"], item["id"], segment_id)
    assert claimed[-1] is not None
    meter = Meter(runtime.jobs, job["id"], item["id"], segment_id)
    meter.started -= 2
    reported = await meter.check_active()
    assert remaining - 0.5 < reported <= remaining
    assert not await retry.wait_for_retry(0, response=httpx.Response(503, headers={"Retry-After": "30"}), meter=meter)
    assert not clock.sleeps

    if reason == "segment_budget":
        with runtime.store.transaction(True) as conn:
            conn.execute("UPDATE job_segments SET started_at=? WHERE id=?",
                         ((datetime.now(timezone.utc) - timedelta(seconds=11)).isoformat(), segment_id))
    else:
        meter.started = time.monotonic() - elapsed
    with pytest.raises(BudgetStop) as checked:
        await meter.check_active()
    with pytest.raises(BudgetStop) as reserved:
        await meter.reserve("http_requests")
    assert checked.value.reason == reserved.value.reason == reason
    with runtime.store.transaction() as conn:
        reservations = conn.execute("SELECT reserved_json FROM call_reservations").fetchall()
    assert len(reservations) == 1 and "active_seconds" in json.loads(reservations[0][0])
    assert runtime.jobs.get(job["id"])["usage"]["http_requests"] == 0


@pytest.mark.parametrize("input_tokens, output_tokens", [(-1, 5), (True, 3), (4, None), (4, -1)])
@pytest.mark.asyncio
async def test_unknown_usage_does_not_become_actual_trace_tokens(input_tokens, output_tokens):
    body = {**MODEL_BODY, "prompt_eval_count": input_tokens, "eval_count": output_tokens}
    transport = gateway(lambda _: httpx.Response(200, json=body))
    model = ModelProvider(transport.settings, gateway=transport)
    await model._structured("공개 지침", "공개 자료", SCHEMA)
    assert model.last_usage is None


@pytest.mark.asyncio
async def test_last_usage_does_not_reuse_a_previous_attempt(clock):
    calls = []

    def handler(_):
        calls.append(True)
        return (httpx.Response(503, json=MODEL_BODY) if len(calls) == 1
                else httpx.Response(200, json={"message": MODEL_BODY["message"]}))

    transport = gateway(handler)
    model = ModelProvider(transport.settings, gateway=transport)
    await model._structured("공개 지침", "공개 자료", SCHEMA)
    assert len(calls) == 2 and model.last_usage is None
