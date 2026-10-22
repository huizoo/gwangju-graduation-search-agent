"""허용한 진단 정보만 제한된 대기열로 보내는 선택적 LangSmith 연결."""

from __future__ import annotations

from contextvars import ContextVar
from datetime import date, datetime, timezone
from queue import Empty, Full, Queue
from threading import Event, Thread
import time
from uuid import uuid4

from .domain import SCHEMA


CURRENT = ContextVar("graduation_trace", default=None)
NAMES = {"job": "research_job", "school": "school_research", "model": "agent_decision",
         "get_school_calendar": "get_school_calendar", "search_public_web": "search_public_web",
         "read_public_document": "read_public_document", "get_graduating_students": "get_graduating_students"}
CODES = set(SCHEMA["$defs"]["ReasonCode"]["enum"]) | {
    "internal_error", "structured_evidence_sufficient", "verified_route_sufficient", "evidence_sufficient",
    "decision_limit", "model_unavailable", "invalid_decision", "agent_stop", "document_limit",
    "unverified_source", "search_limit", "duplicate_request", "purpose_mismatch", "repeated_action",
}
STATES = {"ok", "no_data", "not_found", "unsupported", "failed", "partial", "completed",
          "running", "paused", "cancelled", "error", "verified", "needs_review", "blocked",
          "limit_reached", "provider_error", "access_failed"}
FIELDS = {"event_date", "start_time", "graduation_count"}


def sdk_client(key: str):
    """숨은 환경·런타임 정보를 붙이지 않는 전용 SDK 클라이언트를 만든다.

    Args:
        key: 메모리에서만 사용하는 LangSmith 인증키.
    Returns:
        자동 배치·추적과 재시도를 끈 SDK 클라이언트.
    Raises:
        RuntimeError: 별도 자동 전송 엔진이 환경에서 강제된 경우.
    """
    import os
    import requests
    from urllib3.util.retry import Retry
    from langsmith import Client

    if any(name in os.environ for name in ("LANGSMITH_USE_PYO3_CLIENT", "LANGCHAIN_USE_PYO3_CLIENT")):
        raise RuntimeError("unsupported_tracing_transport")
    session = requests.Session()
    session.trust_env = False
    session.max_redirects = 0
    return Client(
        api_url="https://api.smith.langchain.com", api_key=key, session=session,
        timeout_ms=(3000, 10000), retry_config=Retry(total=0, connect=0, read=0, redirect=0, status=0),
        auto_batch_tracing=False, omit_traced_runtime_info=True, tracing_mode="langsmith",
        tracing_sampling_rate=1.0, workspace_id="", disable_prompt_cache=True,
        hide_inputs=False, hide_outputs=False, hide_metadata=False,
    )


class TraceRecorder:
    """진단 전송 실패를 조사 작업과 격리하고 종료 대기를 제한한다."""

    def __init__(self, settings, *, client_factory=sdk_client):
        """현재 설정만 읽고 SDK·스레드·외부 연결은 지연 생성한다.

        Args:
            settings: 키와 비밀 아닌 추적 설정을 가진 실행 설정.
            client_factory: 격리 검사에서 주입할 SDK 생성자.
        """
        self.key = settings.secrets.get("langsmith_api_key", "")
        self.project = settings.data["tracing"]["project"]
        self.enabled = bool(settings.data["tracing"]["enabled"] and self.key)
        self.reason = ("disabled" if not settings.data["tracing"]["enabled"]
                       else "ready" if self.key else "key_missing")
        self._secrets = tuple(value for value in settings.secrets.values() if isinstance(value, str) and value)
        self._factory = client_factory
        self._queue = Queue(maxsize=256)
        self._thread = None
        self._stop = Event()
        self.sent = self.dropped = 0

    def clean(self, values: dict) -> dict:
        """알려진 진단 필드만 새 사전에 복사하고 비밀과 원문을 배제한다.

        Args:
            values: 내부 코드가 제출한 진단 후보.
        Returns:
            외부 전송이 허용된 형식·값의 사본.
        """
        result = {}
        for key, value in values.items():
            if isinstance(value, str) and any(secret in value for secret in self._secrets):
                continue
            if key in {"school_name", "institution_type"}:
                if isinstance(value, str) and 0 < len(value) <= 120 and not any(x in value for x in ("\n", "\r", "://", "/Users/", "\\")):
                    result[key] = value
            elif key in {"search_start", "search_end"} and isinstance(value, str):
                try:
                    if date.fromisoformat(value).isoformat() == value:
                        result[key] = value
                except ValueError:
                    pass
            elif key == "purpose" and isinstance(value, str) and value in {"date_count", "time", "registry_sync"}:
                result[key] = value
            elif key == "backend" and isinstance(value, str) and value in {"ollama", "openai", "fake"}:
                result[key] = value
            elif key == "status" and isinstance(value, str) and value in STATES:
                result[key] = value
            elif key in {"error_code", "stop_reason"} and isinstance(value, str) and value in CODES:
                result[key] = value
            elif key == "action" and isinstance(value, str) and value in {"search_web", "fetch_document", "stop"}:
                result[key] = value
            elif key == "missing_fields" and isinstance(value, list):
                result[key] = [field for field in value if isinstance(field, str) and field in FIELDS]
            elif key == "reason_codes" and isinstance(value, list):
                result[key] = [code for code in value if isinstance(code, str) and code in CODES]
            elif key in {"http_requests", "llm_calls", "input_tokens", "output_tokens", "total_tokens",
                         "completed_count", "failed_count", "target_count", "candidate_count",
                         "document_count", "count_candidate_count"} and type(value) is int and value >= 0:
                result[key] = value
        return result

    def span(self, kind: str, **inputs):
        """이름과 전송 입력이 제한된 실행 구간을 만든다.

        Args:
            kind: 코드에 등록한 작업·학교·모델·도구 이름.
            **inputs: 허용 목록으로 재검사할 진단 입력.
        Returns:
            원래 예외를 그대로 전달하는 실행 구간.
        """
        return TraceSpan(self, kind, self.clean(inputs))

    def _submit(self, operation: str, payload: dict) -> None:
        """조사를 기다리게 하지 않고 전송 대기열에 진단 사본을 넣는다."""
        if not self.enabled or self._stop.is_set():
            return
        try:
            self._queue.put_nowait((operation, payload))
            if self._thread is None:
                self._thread = Thread(target=self._deliver, name="graduation-trace", daemon=True)
                self._thread.start()
        except Full:
            self.dropped += 1
        except Exception:
            self.reason, self.enabled = "delivery_failed", False

    def _deliver(self) -> None:
        """SDK와 네트워크 오류를 원문 출력 없이 내부 상태로만 남긴다."""
        client = None
        try:
            client = self._factory(self.key)
            while not self._stop.is_set():
                try:
                    operation, payload = self._queue.get(timeout=0.1)
                except Empty:
                    continue
                try:
                    if operation == "create":
                        client.create_run(**payload)
                    else:
                        client.update_run(**payload)
                    self.sent += 1
                finally:
                    self._queue.task_done()
        except Exception:
            # SDK 예외에는 키 일부·주소가 포함될 수 있어 문자열도 로그에 남기지 않는다.
            self.reason, self.enabled = "delivery_failed", False
        finally:
            while True:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                    self.dropped += 1
                except Empty:
                    break
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def flush(self, timeout: float = 1.0) -> bool:
        """완료된 진단을 제한 시간까지만 기다린다.

        Args:
            timeout: 최대 대기 초. 조사 코드에서는 호출하지 않는다.
        Returns:
            제한 시간 안에 대기열이 비었는지 여부.
        """
        deadline = time.monotonic() + max(0, timeout)
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return not self._queue.unfinished_tasks

    def close(self) -> None:
        """종료 시 최대 1.2초만 기다리고 남은 진단은 버린다."""
        self.flush(1.0)
        self._stop.set()
        self.enabled = False
        if self._thread:
            self._thread.join(timeout=0.2)


class TraceSpan:
    """원문을 캡처하지 않는 명시적 구간과 부모·자식 연결."""

    def __init__(self, recorder, kind, inputs):
        """정해진 이름과 허용 입력으로 구간을 준비한다."""
        self.recorder, self.kind, self.inputs = recorder, kind, inputs
        self.run_type = "llm" if kind == "model" else "tool" if kind not in {"job", "school"} else "chain"
        self.outputs = {}
        self.id = str(uuid4())
        self.started = datetime.now(timezone.utc)
        parent = CURRENT.get()
        parent = parent if parent and parent.recorder is recorder else None
        self.parent = parent.id if parent else None
        self.trace_id = parent.trace_id if parent else self.id
        part = self.started.strftime("%Y%m%dT%H%M%S%fZ") + self.id
        self.order = parent.order + "." + part if parent else part
        self.token = None

    def __enter__(self):
        """시작 사실만 제출하고 현재 비동기 문맥의 부모 구간을 지정한다."""
        self.token = CURRENT.set(self)
        if self.kind in NAMES:
            self.recorder._submit("create", {
                "id": self.id, "name": NAMES[self.kind], "inputs": self.inputs,
                "run_type": self.run_type,
                "project_name": self.recorder.project, "start_time": self.started,
                "parent_run_id": self.parent, "trace_id": self.trace_id, "dotted_order": self.order,
            })
        return self

    def update(self, **outputs):
        """결과 요약 중 허용된 값만 저장한다."""
        self.outputs.update(self.recorder.clean(outputs))

    def __exit__(self, kind, error, traceback):
        """예외 원문 대신 공개 오류 코드만 보내고 원래 예외는 전파한다."""
        try:
            code = None
            if error is not None:
                candidate = getattr(error, "reason", None) or getattr(error, "code", None)
                code = candidate if isinstance(candidate, str) and candidate in CODES else "internal_error"
                self.update(status="cancelled" if code in {"user_cancel", "interrupted"} else "error", error_code=code)
            if self.kind in NAMES:
                # SDK는 생략한 부모를 null로 전송하므로 종료에도 같은 연결·프로젝트를 명시한다.
                payload = {"run_id": self.id, "outputs": dict(self.outputs),
                           "end_time": datetime.now(timezone.utc), "trace_id": self.trace_id,
                           "dotted_order": self.order, "parent_run_id": self.parent,
                           "start_time": self.started, "session_name": self.recorder.project,
                           "name": NAMES[self.kind], "run_type": self.run_type}
                if self.kind == "model" and all(key in self.outputs for key in ("input_tokens", "output_tokens")):
                    usage = {key: self.outputs[key] for key in ("input_tokens", "output_tokens")}
                    usage["total_tokens"] = sum(usage.values())
                    # 응답을 받지 못한 재시도의 토큰까지 합산한 총량으로 표시하지 않는다.
                    payload["extra"] = {"metadata": {
                        "usage_metadata": usage, "usage_scope": "last_response_only",
                        "usage_includes_retry_attempts": False,
                    }}
                if code:
                    payload["error"] = code
                self.recorder._submit("update", payload)
        finally:
            CURRENT.reset(self.token)
        return False
