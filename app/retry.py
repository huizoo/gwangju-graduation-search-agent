"""일시 전송 오류만 분류하고 취소·시간 한도 안에서 재시도를 기다린다."""

from __future__ import annotations

import errno
import inspect
import socket
import ssl
from asyncio import sleep
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from time import monotonic

import httpx


RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
DIRECT_WAIT_LIMIT_SECONDS = 30.0
POLL_SECONDS = 0.1
_TRANSIENT_ERRNOS = {
    errno.ECONNABORTED, errno.ECONNREFUSED, errno.ECONNRESET,
    errno.EHOSTUNREACH, errno.ENETDOWN, errno.ENETUNREACH,
    errno.EPIPE, errno.ETIMEDOUT,
}


def is_tls_error(error: BaseException) -> bool:
    """HTTPX가 감싼 인증서·TLS 오류도 재시도 대상에서 제외한다."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, ssl.SSLError):
            return True
        message = str(error).upper()
        if any(marker in message for marker in ("CERTIFICATE_VERIFY", "[SSL", "SSL:", "TLS", "CERTIFICATE VERIFY")):
            return True
        error = error.__cause__ or error.__context__
    return False


def is_transient_error(error: BaseException) -> bool:
    """시간초과와 일시 연결 오류만 허용하고 입력·프로토콜 오류는 제외한다."""
    if (not isinstance(error, (httpx.TimeoutException, httpx.NetworkError, OSError))
            or is_tls_error(error)):
        return False
    cause, seen = error, set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, socket.gaierror):
            return cause.errno == socket.EAI_AGAIN
        if isinstance(cause, OSError) and cause.errno is not None and cause.errno not in _TRANSIENT_ERRNOS:
            return False
        cause = cause.__cause__ or cause.__context__
    if isinstance(error, (httpx.TimeoutException, httpx.NetworkError, TimeoutError, ConnectionError)):
        return True
    return isinstance(error, OSError) and error.errno in _TRANSIENT_ERRNOS


def retry_delay(attempt: int, response: httpx.Response | None = None, *, now: datetime | None = None) -> float:
    """짧은 지수 대기와 유효한 Retry-After 중 더 긴 시간을 반환한다.

    Args:
        attempt: 실패한 시도의 0부터 시작하는 순번.
        response: Retry-After를 읽을 HTTP 응답.
        now: HTTP 날짜 검사에 사용할 현재 UTC 시각.
    Returns:
        기다려야 하는 초. 서버 대기는 상한으로 줄이지 않는다.
    """
    delay = min(0.25 * 2 ** min(attempt, 4), 2.0)
    value = response.headers.get("retry-after", "").strip() if response is not None else ""
    if not value:
        return delay
    try:
        if value.isascii() and value.isdigit():
            requested = float(value)
        else:
            requested_at = parsedate_to_datetime(value)
            if requested_at.tzinfo is None:
                requested_at = requested_at.replace(tzinfo=timezone.utc)
            requested = (requested_at - (now or datetime.now(timezone.utc))).total_seconds()
        return max(delay, requested)
    except (ValueError, TypeError, OverflowError):
        return delay


async def wait_for_retry(attempt: int, *, response: httpx.Response | None = None, meter=None) -> bool:
    """취소를 확인하며 기다리고 허용시간보다 긴 대기는 재시도 없이 끝낸다.

    Args:
        attempt: 실패한 시도의 순번.
        response: 서버가 요청한 최소 대기시간을 담은 응답.
        meter: check_active()로 남은 초를 반환하거나 중단 예외를 내는 계량기.
    Returns:
        대기를 마쳐 재시도할 수 있으면 True, 대기 한도를 넘으면 False.
    Raises:
        Exception: 계량기의 취소·시간 예산 중단 예외를 그대로 전달한다.
    """
    check = getattr(meter, "check_active", None)

    async def remaining_time():
        """구형 계량기에는 직접 실행과 같은 대기 상한을 적용한다."""
        remaining = check() if check else None
        if inspect.isawaitable(remaining):
            remaining = await remaining
        return DIRECT_WAIT_LIMIT_SECONDS if remaining is None else remaining

    delay = retry_delay(attempt, response)
    if delay >= await remaining_time():
        return False
    deadline = monotonic() + delay
    while (remaining := deadline - monotonic()) > 0:
        await sleep(min(POLL_SECONDS, remaining))
        if deadline - monotonic() >= await remaining_time():
            return False
    return True
