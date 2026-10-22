"""실제 계정·원문·네트워크 없이 선택적 추적의 격리 경계를 검사한다."""

import asyncio
import copy
import json
import os
import socket
import time
from threading import Event
from types import SimpleNamespace

import pytest

from app import tracing


KEY = "synthetic-langsmith-key"
PRIVATE = "private-content-sentinel"


def settings(*, enabled=True, key=KEY):
    """운영 설정을 읽지 않는 합성 추적 설정을 만든다."""
    return SimpleNamespace(
        data={"tracing": {"enabled": enabled, "project": "synthetic-tracing-project"}},
        secrets={"langsmith_api_key": key, "openai_api_key": "synthetic-model-key"},
    )


class CaptureClient:
    """SDK 호출 경계에 도착한 사본만 메모리에 보관한다."""

    def __init__(self):
        self.calls = []
        self.closed = Event()

    def create_run(self, **payload):
        self.calls.append(("create", copy.deepcopy(payload)))

    def update_run(self, **payload):
        self.calls.append(("update", copy.deepcopy(payload)))

    def close(self):
        self.closed.set()


@pytest.fixture(autouse=True)
def isolated_transport(monkeypatch):
    """계정 프로필·환경 인증·실제 소켓 연결을 검사에서 격리한다."""
    for name in tuple(os.environ):
        if name.startswith(("LANGSMITH_", "LANGCHAIN_", "OTEL_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("LANGSMITH_CONFIG_FILE", os.devnull)

    def forbidden(*args, **kwargs):
        raise AssertionError("external_network_forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def recorder():
    """검사 뒤 daemon 작업자를 정리하는 메모리 전송기를 제공한다."""
    client = CaptureClient()
    result = tracing.TraceRecorder(settings(), client_factory=lambda key: client)
    yield result, client
    result.close()
    assert client.closed.wait(0.5)


@pytest.mark.parametrize(
    ("enabled", "key", "reason"),
    [(False, KEY, "disabled"), (True, "", "key_missing")],
)
def test_inactive_tracing_never_constructs_sdk(enabled, key, reason):
    calls = []
    recorder = tracing.TraceRecorder(
        settings(enabled=enabled, key=key), client_factory=lambda value: calls.append(value)
    )
    with recorder.span("job", target_count=1) as span:
        span.update(status="completed", completed_count=1)
    recorder.close()
    assert calls == []
    assert recorder._thread is None
    assert recorder.reason == reason
    assert recorder.sent == recorder.dropped == 0
    assert tracing.CURRENT.get() is None


def test_nested_spans_preserve_parent_order_and_shared_trace(recorder):
    recorder, client = recorder
    with recorder.span("job", target_count=1) as job:
        with recorder.span("school", school_name="합성초등학교") as school:
            with recorder.span("model", backend="fake") as model:
                model.update(input_tokens=7, output_tokens=3, total_tokens=10)
            school.update(status="completed", llm_calls=1)
        job.update(status="completed", completed_count=1)
    assert recorder.flush()
    creates = {payload["id"]: payload for operation, payload in client.calls if operation == "create"}
    updates = {payload["run_id"]: payload for operation, payload in client.calls if operation == "update"}
    assert set(creates) == set(updates) == {job.id, school.id, model.id}
    assert creates[job.id]["parent_run_id"] is None
    assert creates[school.id]["parent_run_id"] == job.id
    assert creates[model.id]["parent_run_id"] == school.id
    assert all(payload["trace_id"] == job.id for payload in creates.values())
    assert creates[model.id]["dotted_order"].startswith(creates[school.id]["dotted_order"] + ".")
    assert creates[school.id]["dotted_order"].startswith(creates[job.id]["dotted_order"] + ".")
    assert [creates[span.id]["run_type"] for span in (job, school, model)] == ["chain", "chain", "llm"]
    assert updates[model.id]["outputs"] == {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}
    assert all(updates[run_id]["end_time"] >= payload["start_time"] for run_id, payload in creates.items())
    assert tracing.CURRENT.get() is None


@pytest.mark.asyncio
async def test_concurrent_async_spans_keep_their_own_parent(recorder):
    recorder, client = recorder
    entered = [asyncio.Event(), asyncio.Event()]

    async def branch(index):
        with recorder.span("school", school_name=f"합성{index}초등학교") as school:
            entered[index].set()
            await entered[1 - index].wait()
            with recorder.span("search_public_web", purpose="date_count") as tool:
                await asyncio.sleep(0)
            assert tracing.CURRENT.get() is school
        return school, tool

    with recorder.span("job") as job:
        branches = await asyncio.gather(branch(0), branch(1))
        assert tracing.CURRENT.get() is job
    assert recorder.flush()
    creates = {payload["id"]: payload for operation, payload in client.calls if operation == "create"}
    for school, tool in branches:
        assert creates[school.id]["parent_run_id"] == job.id
        assert creates[tool.id]["parent_run_id"] == school.id
        assert creates[tool.id]["trace_id"] == job.id
    assert tracing.CURRENT.get() is None


def test_payload_contains_only_public_identifiers_and_typed_counts(recorder, monkeypatch):
    recorder, client = recorder
    monkeypatch.setenv("LANGSMITH_PRIVATE_NOTE", PRIVATE)
    discarded = {
        "prompt": PRIVATE, "response": PRIVATE, "raw_text": PRIVATE,
        "manual_records": [{"memo": PRIVATE}], "messages": [{"content": PRIVATE}],
        "url": f"https://school.example/{PRIVATE}", "headers": {"Authorization": KEY},
        "api_key": KEY, "environment": {"LANGSMITH_PRIVATE_NOTE": PRIVATE},
        "extra": {"metadata": {"private": PRIVATE}}, "serialized": {"private": PRIVATE},
    }
    with recorder.span(
        "school", school_name="합성초등학교", institution_type="초등학교",
        search_start="2026-01-01", search_end="2026-12-31", purpose="date_count",
        backend="fake", target_count=1, **discarded,
    ) as span:
        span.update(
            status="completed", candidate_count=2, llm_calls=1,
            reason_codes=["internal_error", PRIVATE],
            missing_fields=["event_date", PRIVATE], **discarded,
        )
    assert recorder.flush()
    wire = json.dumps(client.calls, default=str, ensure_ascii=False)
    assert PRIVATE not in wire and KEY not in wire and "synthetic-model-key" not in wire
    assert "https://" not in wire and "LANGSMITH_PRIVATE_NOTE" not in wire
    create, update = [payload for _, payload in client.calls]
    assert create["inputs"] == {
        "school_name": "합성초등학교", "institution_type": "초등학교",
        "search_start": "2026-01-01", "search_end": "2026-12-31",
        "purpose": "date_count", "backend": "fake", "target_count": 1,
    }
    assert update["outputs"] == {
        "status": "completed", "candidate_count": 2, "llm_calls": 1,
        "reason_codes": ["internal_error"], "missing_fields": ["event_date"],
    }
    assert recorder.clean({
        "school_name": f"학교 {KEY}", "institution_type": "https://private.example",
        "search_start": "2026-02-30", "search_end": "20260101",
        "target_count": True, "llm_calls": -1, "total_tokens": 2.5,
        "purpose": PRIVATE, "status": PRIVATE, "error_code": PRIVATE,
        "action": PRIVATE,
    }) == {}


@pytest.mark.parametrize(
    ("kind", "usage", "expected"),
    [
        ("model", {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}, (7, 3, 10)),
        ("model", {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}, (0, 0, 0)),
        ("model", {"input_tokens": 7, "output_tokens": 3, "total_tokens": 99}, (7, 3, 10)),
        ("model", {}, None),
        ("model", {"input_tokens": 7}, None),
        ("model", {"input_tokens": 7, "output_tokens": True}, None),
        ("model", {"input_tokens": 7, "output_tokens": -1}, None),
        ("model", {"input_tokens": 7, "output_tokens": "3"}, None),
        ("job", {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}, None),
        ("school", {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}, None),
    ],
)
def test_model_token_metadata_reports_only_observed_last_response(recorder, kind, usage, expected):
    recorder, client = recorder
    with recorder.span(kind) as span:
        span.update(**usage, usage_metadata={"input_tokens": PRIVATE}, usage_scope=PRIVATE)
    assert recorder.flush()
    update = next(payload for operation, payload in client.calls if operation == "update")
    metadata = update.get("extra", {}).get("metadata", {})
    if expected is None:
        assert "usage_metadata" not in metadata
    else:
        input_tokens, output_tokens, total_tokens = expected
        assert metadata == {
            "usage_metadata": {
                "input_tokens": input_tokens, "output_tokens": output_tokens,
                "total_tokens": total_tokens,
            },
            "usage_scope": "last_response_only",
            "usage_includes_retry_attempts": False,
        }
    assert PRIVATE not in json.dumps(client.calls, default=str)


@pytest.mark.parametrize("known_code", [True, False])
def test_exception_identity_is_preserved_and_only_safe_code_is_sent(recorder, known_code):
    recorder, client = recorder
    error = RuntimeError(f"{PRIVATE}: {KEY}")
    error.code = "model_unavailable" if known_code else PRIVATE
    expected_code = "model_unavailable" if known_code else "internal_error"
    with pytest.raises(RuntimeError) as caught:
        with recorder.span("model", backend="fake"):
            raise error
    assert caught.value is error
    assert recorder.flush()
    update = next(payload for operation, payload in client.calls if operation == "update")
    assert update["error"] == expected_code
    assert update["outputs"] == {"status": "error", "error_code": expected_code}
    assert PRIVATE not in json.dumps(client.calls, default=str)
    assert tracing.CURRENT.get() is None


@pytest.mark.parametrize("stage", ["factory", "create", "update"])
def test_sdk_failure_never_replaces_application_result_or_exception(stage, caplog, capsys):
    failed = Event()
    client = CaptureClient()

    def fail(*args, **kwargs):
        failed.set()
        raise RuntimeError(f"{PRIVATE}: {KEY}")

    factory = fail if stage == "factory" else lambda key: client
    if stage != "factory":
        setattr(client, f"{stage}_run", fail)
    recorder = tracing.TraceRecorder(settings(), client_factory=factory)
    error = ValueError("application-error")
    try:
        with pytest.raises(ValueError) as caught:
            with recorder.span("job"):
                raise error
        assert caught.value is error
        assert failed.wait(0.5)
        assert recorder.flush()
        recorder._thread.join(timeout=0.5)
        assert recorder.reason == "delivery_failed" and not recorder.enabled
        with recorder.span("school"):
            result = "original-result"
        assert result == "original-result"
    finally:
        recorder.close()
    captured = capsys.readouterr()
    assert PRIVATE not in captured.out + captured.err + caplog.text
    assert KEY not in captured.out + captured.err + caplog.text
    assert tracing.CURRENT.get() is None


@pytest.mark.parametrize("stage", ["factory", "send"])
def test_slow_sdk_does_not_block_work_or_exceed_shutdown_budget(stage):
    entered, release = Event(), Event()
    client = CaptureClient()

    def blocked_create(**payload):
        entered.set()
        release.wait(5)
        client.calls.append(("create", payload))

    def factory(key):
        if stage == "factory":
            entered.set()
            release.wait(5)
        return client

    if stage == "send":
        client.create_run = blocked_create
    recorder = tracing.TraceRecorder(settings(), client_factory=factory)
    try:
        started = time.monotonic()
        with recorder.span("job"):
            result = "original-result"
        assert time.monotonic() - started < 0.5
        assert result == "original-result" and entered.wait(0.5)
        started = time.monotonic()
        recorder.close()
        assert time.monotonic() - started < 1.6
        assert recorder._thread.daemon
        assert not recorder.enabled
    finally:
        release.set()
        if recorder._thread is not None:
            recorder._thread.join(timeout=1)
    assert client.closed.is_set()


def test_saturated_queue_drops_traces_without_waiting():
    entered, release = Event(), Event()
    client = CaptureClient()

    def factory(key):
        entered.set()
        release.wait(5)
        return client

    recorder = tracing.TraceRecorder(settings(), client_factory=factory)
    try:
        with recorder.span("job"):
            pass
        assert entered.wait(0.5)
        started = time.monotonic()
        for _ in range(200):
            with recorder.span("school"):
                pass
        assert time.monotonic() - started < 0.5
        assert 0 < recorder._queue.maxsize <= 256
        assert recorder._queue.qsize() == recorder._queue.maxsize
        assert recorder.dropped == 402 - recorder._queue.maxsize
        assert not recorder.flush(timeout=0)
    finally:
        release.set()
        recorder.close()
    assert client.closed.wait(0.5)


@pytest.mark.parametrize("name", ["LANGSMITH_USE_PYO3_CLIENT", "LANGCHAIN_USE_PYO3_CLIENT"])
@pytest.mark.parametrize("value", ["true", "false", "0", ""])
def test_alternate_sdk_transport_is_rejected_for_any_present_value(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match="unsupported_tracing_transport"):
        tracing.sdk_client(KEY)


def test_real_sdk_serialization_excludes_runtime_environment_and_private_payload(monkeypatch):
    import langsmith
    import requests
    from langsmith import utils as sdk_utils

    calls, sessions = [], []
    session_type = requests.Session

    class CaptureSession(session_type):
        """실제 통신 없이 SDK가 직렬화한 본문을 검사한다."""

        def __init__(self):
            super().__init__()
            sessions.append(self)

        def request(self, method, url, **kwargs):
            calls.append((method, url, json.loads(kwargs["data"])))
            response = requests.Response()
            response.status_code = 200
            response._content = b"{}"
            return response

    monkeypatch.setattr(requests, "Session", CaptureSession)
    monkeypatch.setenv("LANGSMITH_PRIVATE_NOTE", PRIVATE)
    monkeypatch.setenv("LANGSMITH_REVISION_ID", PRIVATE)
    monkeypatch.setenv("LANGSMITH_WORKSPACE_ID", PRIVATE)
    monkeypatch.setenv("GITHUB_SHA", PRIVATE)
    sdk_utils.get_env_var.cache_clear()
    client = tracing.sdk_client(KEY)
    recorder = tracing.TraceRecorder(settings(), client_factory=lambda key: client)
    try:
        with recorder.span("job") as job:
            with recorder.span("school", school_name="합성초등학교") as school:
                with recorder.span("model", backend="fake", prompt=PRIVATE) as span:
                    span.update(input_tokens=7, output_tokens=3, total_tokens=10, response=PRIVATE)
        assert recorder.flush()
        assert isinstance(client, langsmith.Client)
        assert [method for method, _, _ in calls] == ["POST", "POST", "POST", "PATCH", "PATCH", "PATCH"]
        creates = {body["id"]: body for method, _, body in calls if method == "POST"}
        updates = {body["id"]: body for method, _, body in calls if method == "PATCH"}
        assert set(creates) == set(updates) == {job.id, school.id, span.id}
        for method, url, body in calls:
            expected_url = "https://api.smith.langchain.com/runs"
            if method == "PATCH":
                expected_url += f"/{body['id']}"
            assert url == expected_url
        for run_id, created in creates.items():
            updated = updates[run_id]
            for field in ("parent_run_id", "trace_id", "dotted_order", "session_name", "start_time", "name", "run_type"):
                assert updated[field] == created[field]
            parts = updated["dotted_order"].split(".")
            assert parts[-1].split("Z")[1] == run_id
            assert parts[0].split("Z")[1] == updated["trace_id"] == job.id
            assert updated["parent_run_id"] == (parts[-2].split("Z")[1] if len(parts) > 1 else None)
            assert updated["session_name"] == "synthetic-tracing-project"
        wire = json.dumps(calls)
        assert PRIVATE not in wire and KEY not in wire
        assert "runtime" not in wire and "revision_id" not in wire
        assert creates[span.id]["inputs"] == {"backend": "fake"}
        assert updates[span.id]["outputs"] == {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}
        assert updates[span.id]["extra"]["metadata"] == {
            "usage_metadata": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
            "usage_scope": "last_response_only", "usage_includes_retry_attempts": False,
        }
        assert client.tracing_queue is None
        assert client._workspace_id is None and client._cache is None
        assert client._timeout == (3.0, 10.0)
        assert client.retry_config.total == 0
        assert len(sessions) == 1
        assert not sessions[0].trust_env and sessions[0].max_redirects == 0
    finally:
        recorder.close()
        sdk_utils.get_env_var.cache_clear()
