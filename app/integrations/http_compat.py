"""폐기된 P3P 응답 헤더의 공백 오류만 제한된 libcurl 경로로 읽는다."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import shutil

import certifi
import httpx


class CompatibilityError(httpx.HTTPError):
    """원문·쿠키·실행 인자를 노출하지 않는 호환 읽기 실패."""

    def __init__(self, code: str = "access_failed"):
        self.code = code
        super().__init__("공개 자료의 호환 읽기를 완료하지 못했습니다.")


def is_legacy_p3p_error(error: Exception) -> bool:
    """실제로 확인한 P3P 헤더 오류만 다른 HTTP 오류와 구분한다.

    Args:
        error: 기본 HTTPX 전송에서 발생한 예외.
    Returns:
        헤더 이름 뒤 공백이 있는 P3P 오류이면 True.
    """
    return isinstance(error, httpx.RemoteProtocolError) and str(error).startswith(
        "illegal header line: bytearray(b'P3P :"
    )


async def _bounded_read(stream: asyncio.StreamReader, limit: int, code: str) -> bytes:
    """본문과 메타데이터의 메모리 사용을 수신 중 제한한다."""
    chunks, length = [], 0
    while chunk := await stream.read(min(65536, limit + 1)):
        length += len(chunk)
        if length > limit:
            raise CompatibilityError(code)
        chunks.append(chunk)
    return b"".join(chunks)


def _parsed_headers(values: dict, body_size: int) -> list[tuple[str, str]]:
    """libcurl이 파싱한 헤더에서 폐기된 P3P만 버리고 응답 모호성을 거부한다.

    Args:
        values: curl의 header_json 결과.
        body_size: 압축하지 않은 실제 수신 본문 크기.
    Returns:
        HTTPX에 전달할 헤더 목록.
    Raises:
        CompatibilityError: 이름·값·메시지 길이·인코딩이 모호한 응답.
    """
    if not isinstance(values, dict):
        raise CompatibilityError()
    headers = []
    for name, items in values.items():
        # P3P는 본문·리다이렉트·프레이밍에 쓰이지 않는 폐기된 정책 헤더다.
        if name.lower() in {"p3p", "p3p "}:
            continue
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            raise CompatibilityError()
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise CompatibilityError()
        if any("\r" in item or "\n" in item or "\x00" in item for item in items):
            raise CompatibilityError()
        headers.extend((name, item) for item in items)
    normalized = httpx.Headers(headers)
    lengths = normalized.get_list("content-length", split_commas=True)
    transfer = normalized.get_list("transfer-encoding", split_commas=True)
    if (lengths and transfer) or transfer not in ([], ["chunked"]):
        raise CompatibilityError()
    if lengths and (any(not value.isdecimal() for value in lengths)
                    or any(int(value) != body_size for value in lengths)):
        raise CompatibilityError()
    # 별도 압축 해제로 제한을 우회하지 않도록 비압축 응답만 받는다.
    if normalized.get("content-encoding", "identity").lower() != "identity":
        raise CompatibilityError()
    return headers


async def read_legacy_p3p(url: str, address: str, timeout: float, max_bytes: int) -> httpx.Response:
    """검증한 공개 IP에 고정하여 GET 한 번을 libcurl로 수행한다.

    Args:
        url: 공개 주소 검사를 이미 통과한 원래 URL.
        address: DNS 검사에서 고정한 공개 IP.
        timeout: 전체 요청 제한 시간(초).
        max_bytes: 허용할 본문 바이트 수.
    Returns:
        자동 주소 이동 없이 받은 제한된 응답.
    Raises:
        CompatibilityError: curl 부재, TLS·크기·응답 검사 실패.
        asyncio.CancelledError: 호출 작업이 취소됨. 자식 프로세스도 종료한다.
    """
    executable = shutil.which("curl")
    target = httpx.URL(url).copy_with(fragment=None)
    pinned = ipaddress.ip_address(address)
    if not executable or not pinned.is_global or target.scheme not in {"http", "https"}:
        raise CompatibilityError()
    if target.username or target.password or target.port not in {None, 80, 443}:
        raise CompatibilityError()
    port = target.port or (443 if target.scheme == "https" else 80)
    ip_text = f"[{pinned}]" if pinned.version == 6 else str(pinned)
    args = [
        executable, "-q", "--silent", "--globoff", "--path-as-is", "--http1.1",
        "--noproxy", "*", "--proto", "=http,https", "--proto-redir", "=http,https",
        "--max-redirs", "0", "--retry", "0", "--max-time", str(timeout),
        "--connect-timeout", str(timeout), "--max-filesize", str(max_bytes),
        "--cacert", certifi.where(), "--resolve", f"{target.host}:{port}:{ip_text}",
        "--header", "Accept-Encoding: identity",
        "--user-agent", "GwangjuGraduationAgent/0.1 (+public-research)",
        "--write-out", "%{stderr}%{json}\n%{header_json}", "--url", str(target),
    ]
    # -q와 최소 환경으로 사용자 curl 설정·프록시·인증·TLS 로그를 상속하지 않는다.
    process = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env={"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"},
    )
    readers = [
        asyncio.create_task(_bounded_read(process.stdout, max_bytes, "document_partial")),
        asyncio.create_task(_bounded_read(process.stderr, 65536, "access_failed")),
        asyncio.create_task(process.wait()),
    ]
    try:
        body, metadata, returncode = await asyncio.wait_for(
            asyncio.gather(*readers), timeout=timeout + 1,
        )
    except TimeoutError:
        raise CompatibilityError() from None
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        for reader in readers:
            if not reader.done():
                reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        await process.wait()
    if returncode:
        code = "document_partial" if returncode == 63 else (
            "tls_error" if returncode in {51, 58, 60, 77, 83, 90, 91} else "access_failed"
        )
        raise CompatibilityError(code)
    try:
        decoder = json.JSONDecoder()
        info, offset = decoder.raw_decode(metadata.decode("utf-8"))
        headers = decoder.decode(metadata.decode("utf-8")[offset:].strip())
        if (ipaddress.ip_address(info["remote_ip"]) != pinned
                or httpx.URL(info["url_effective"]) != target
                or info["ssl_verify_result"] != 0
                or not 200 <= info["response_code"] <= 599):
            raise ValueError
        parsed = _parsed_headers(headers, len(body))
        return httpx.Response(info["response_code"], headers=parsed, content=body,
                              request=httpx.Request("GET", target),
                              extensions={"legacy_p3p_compatibility": True})
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise CompatibilityError() from None
