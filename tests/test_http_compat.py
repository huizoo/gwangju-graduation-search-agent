"""P3P 호환 읽기의 좁은 적용 범위와 기존 전송 보호를 검사한다."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app import http_compat, providers
from app.http_compat import CompatibilityError, is_legacy_p3p_error, read_legacy_p3p
from app.providers import DocumentReader, HttpGateway, ProviderError


P3P_ERROR = "illegal header line: bytearray(b'P3P : CP=\"ALL\"')"
PUBLIC_IP = "93.184.216.34"
URL = "https://school.example/notice"
BODY = b"<html><body>school notice</body></html>"


def settings():
    """실제 API를 호출하지 않는 최소 전송 설정을 반환한다."""
    data = json.loads(Path("config/defaults.json").read_text())
    data["execution"].update(per_host_min_interval_ms=0, http_max_retries=0)
    return SimpleNamespace(data=data, secrets={})


async def resolver(_):
    """공개 주소 검증 결과를 결정적으로 대체한다."""
    return [PUBLIC_IP]


class Process:
    """출력 제한·취소 처리를 검사하는 비동기 프로세스 대역."""

    def __init__(self, body=BODY, headers=None, info=None, returncode=0, stalled=False):
        self.returncode = None if stalled else returncode
        self.killed = False
        self.body, self.stalled = body, stalled
        meta = {"remote_ip": PUBLIC_IP, "url_effective": URL, "ssl_verify_result": 0, "response_code": 200}
        meta.update(info or {})
        headers = headers if headers is not None else {"content-type": ["text/html"], "p3p ": ['CP="ALL"']}
        self.metadata = (json.dumps(meta) + "\n" + json.dumps(headers)).encode()

    def start(self):
        """실제 호출의 이벤트 루프 안에서 스트림을 만든다."""
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.stdout.feed_data(self.body)
        self.stderr.feed_data(self.metadata)
        if not self.stalled:
            self.stdout.feed_eof()
            self.stderr.feed_eof()

    async def wait(self):
        """종료 또는 kill까지 기다린다."""
        while self.returncode is None:
            await asyncio.sleep(0.001)
        return self.returncode

    def kill(self):
        """제한 초과 시 자식 회수 여부를 남긴다."""
        self.killed, self.returncode = True, -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()


def install_process(monkeypatch, process):
    """셸을 실행하지 않고 전달 인자와 프로세스 수명을 관찰한다."""
    calls = []

    async def create(*args, **kwargs):
        calls.append((args, kwargs))
        process.start()
        return process

    monkeypatch.setattr(http_compat.shutil, "which", lambda _: "/usr/bin/curl")
    monkeypatch.setattr(http_compat.asyncio, "create_subprocess_exec", create)
    return calls


@pytest.mark.parametrize("error, expected", [
    (httpx.RemoteProtocolError(P3P_ERROR), True),
    (httpx.RemoteProtocolError("illegal header line: bytearray(b'Content-Length : 12')"), False),
    (httpx.RemoteProtocolError("server disconnected"), False),
    (httpx.ConnectError(P3P_ERROR), False),
])
def test_only_observed_p3p_protocol_error_matches(error, expected):
    assert is_legacy_p3p_error(error) is expected


def test_curl_keeps_dns_tls_and_environment_boundaries(monkeypatch):
    calls = install_process(monkeypatch, Process())
    response = asyncio.run(read_legacy_p3p(URL, PUBLIC_IP, 5, 4096))
    args, kwargs = calls[0]
    assert response.content == BODY and response.status_code == 200
    assert "p3p " not in response.headers
    assert response.extensions["legacy_p3p_compatibility"] is True
    assert args[1] == "-q" and args[args.index("--noproxy") + 1] == "*"
    assert args[args.index("--resolve") + 1] == f"school.example:443:{PUBLIC_IP}"
    assert Path(args[args.index("--cacert") + 1]).is_file()
    assert "--insecure" not in args and "-k" not in args and "--location" not in args
    assert args[args.index("--max-redirs") + 1] == "0"
    assert args[args.index("--retry") + 1] == "0"
    assert args[args.index("--url") + 1] == URL
    assert set(kwargs["env"]) == {"PATH", "LANG", "LC_ALL"}


@pytest.mark.parametrize("headers", [
    {"content-length ": ["1"]},
    {"x-test": ["injected\r\nheader: value"]},
    {"content-length": ["1", "2"]},
    {"content-length": [str(len(BODY))], "transfer-encoding": ["chunked"]},
    {"content-encoding": ["gzip"]},
    {"transfer-encoding": ["unknown"]},
])
def test_ambiguous_non_p3p_headers_are_not_accepted(monkeypatch, headers):
    install_process(monkeypatch, Process(headers=headers))
    with pytest.raises(CompatibilityError):
        asyncio.run(read_legacy_p3p(URL, PUBLIC_IP, 5, 4096))


@pytest.mark.parametrize("headers", [
    {"content-length": [str(len(BODY))], "content-type": ["text/html"]},
    {"transfer-encoding": ["chunked"], "content-type": ["text/html"]},
])
def test_standard_content_length_and_chunked_responses_succeed(monkeypatch, headers):
    install_process(monkeypatch, Process(headers=headers))
    assert asyncio.run(read_legacy_p3p(URL, PUBLIC_IP, 5, 4096)).content == BODY


@pytest.mark.parametrize("info", [
    {"remote_ip": "127.0.0.1"},
    {"url_effective": "http://127.0.0.1/private"},
    {"ssl_verify_result": 1},
    {"response_code": 0},
])
def test_unexpected_peer_redirect_or_tls_result_is_rejected(monkeypatch, info):
    install_process(monkeypatch, Process(info=info))
    with pytest.raises(CompatibilityError):
        asyncio.run(read_legacy_p3p(URL, PUBLIC_IP, 5, 4096))


@pytest.mark.parametrize("returncode, expected", [(60, "tls_error"), (63, "document_partial"), (28, "access_failed")])
def test_curl_errors_have_safe_categories(monkeypatch, returncode, expected):
    install_process(monkeypatch, Process(returncode=returncode))
    with pytest.raises(CompatibilityError) as caught:
        asyncio.run(read_legacy_p3p(URL, PUBLIC_IP, 5, 4096))
    assert caught.value.code == expected and URL not in str(caught.value)


def test_missing_curl_fails_without_changing_transport_guards(monkeypatch):
    monkeypatch.setattr(http_compat.shutil, "which", lambda _: None)
    with pytest.raises(CompatibilityError):
        asyncio.run(read_legacy_p3p(URL, PUBLIC_IP, 5, 4096))


def test_oversized_stream_kills_process_and_discards_partial_body(monkeypatch):
    process = Process(body=b"x" * 51, stalled=True)
    install_process(monkeypatch, process)
    with pytest.raises(CompatibilityError) as caught:
        asyncio.run(read_legacy_p3p(URL, PUBLIC_IP, 5, 50))
    assert caught.value.code == "document_partial" and process.killed


def test_oversized_metadata_kills_process(monkeypatch):
    process = Process(headers={"x-data": ["x" * 70000]}, stalled=True)
    install_process(monkeypatch, process)
    with pytest.raises(CompatibilityError):
        asyncio.run(read_legacy_p3p(URL, PUBLIC_IP, 5, 4096))
    assert process.killed


def test_task_cancellation_kills_child_process(monkeypatch):
    process = Process(stalled=True)
    install_process(monkeypatch, process)

    async def cancel():
        task = asyncio.create_task(read_legacy_p3p(URL, PUBLIC_IP, 5, 4096))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())
    assert process.killed


def test_timeout_kills_child_process(monkeypatch):
    process = Process(stalled=True)
    install_process(monkeypatch, process)
    with pytest.raises(CompatibilityError):
        asyncio.run(read_legacy_p3p(URL, PUBLIC_IP, 0.01, 4096))
    assert process.killed


def test_gateway_reserves_additional_request_and_retains_host_guard(monkeypatch):
    reservations, fallbacks, requests = [], [], []

    class Meter:
        def reserve(self, kind, amount=1):
            reservations.append(kind)

    def handler(request):
        requests.append(request)
        raise httpx.RemoteProtocolError(P3P_ERROR)

    async def compatibility(url, address, timeout, max_bytes):
        fallbacks.append((url, address))
        return httpx.Response(200, text="<html><body>검증초등학교 졸업식 안내</body></html>",
                              headers={"content-type": "text/html"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(providers, "read_legacy_p3p", compatibility)
    gateway = HttpGateway(settings(), meter=Meter(),
                          client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolver=resolver)
    response = asyncio.run(gateway.request("GET", URL, public=True))
    assert response.is_success and fallbacks == [(URL, PUBLIC_IP)]
    assert requests[0].url.host == PUBLIC_IP
    assert requests[0].headers["Host"] == "school.example"
    assert requests[0].extensions["sni_hostname"] == "school.example"
    assert reservations == ["http_requests", "http_requests"]


@pytest.mark.parametrize("public, method, kwargs, message", [
    (False, "GET", {}, P3P_ERROR),
    (True, "POST", {}, P3P_ERROR),
    (True, "GET", {"headers": {"Authorization": "test"}}, P3P_ERROR),
    (True, "GET", {}, "illegal header line: bytearray(b'Content-Length : 2')"),
])
def test_no_fallback_for_api_post_authenticated_or_other_protocol_errors(monkeypatch, public, method, kwargs, message):
    calls = []

    async def unexpected(*_):
        calls.append(True)

    def handler(_):
        raise httpx.RemoteProtocolError(message)

    monkeypatch.setattr(providers, "read_legacy_p3p", unexpected)
    gateway = HttpGateway(settings(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolver=resolver)
    with pytest.raises(ProviderError):
        asyncio.run(gateway.request(method, URL, public=public, **kwargs))
    assert not calls


def test_fallback_redirect_rechecks_public_address(monkeypatch):
    destinations = []

    async def guarded(url):
        destinations.append(url)
        if url != URL:
            raise ProviderError("access_failed", "공개 주소가 아닙니다.", "reader")
        return [PUBLIC_IP]

    def handler(_):
        raise httpx.RemoteProtocolError(P3P_ERROR)

    async def compatibility(url, *_):
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(providers, "read_legacy_p3p", compatibility)
    gateway = HttpGateway(settings(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolver=guarded)
    with pytest.raises(ProviderError):
        asyncio.run(gateway.request("GET", URL, public=True))
    assert destinations == [URL, "http://127.0.0.1/private"]


def test_successful_relocated_page_does_not_gain_official_authority(monkeypatch):
    old_url, new_url = "https://old.example/", "https://new.example/"

    def handler(request):
        if request.headers["Host"] == "old.example":
            return httpx.Response(302, headers={"location": new_url})
        raise httpx.RemoteProtocolError(P3P_ERROR)

    async def compatibility(url, *_):
        return httpx.Response(200, headers={"content-type": "text/html"},
                              text="<html><body>검증초등학교 졸업식 10시</body></html>",
                              request=httpx.Request("GET", url))

    monkeypatch.setattr(providers, "read_legacy_p3p", compatibility)
    gateway = HttpGateway(settings(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolver=resolver)
    institution = {"id": "school", "name": "검증초등학교", "official_channels": [
        {"url": old_url, "verified_by_url": "https://registry.example/", "verified_at": "2026-10-21T00:00:00Z"}]}
    document = asyncio.run(DocumentReader(gateway).read(institution, old_url))
    assert document["status"] == "ok" and document["authority"] == "unverified"
    assert document["acquisition"]["final_url"] == new_url


def test_failed_compatibility_is_not_retried_indefinitely(monkeypatch):
    calls = []

    def handler(_):
        raise httpx.RemoteProtocolError(P3P_ERROR)

    async def compatibility(*_):
        calls.append(True)
        raise CompatibilityError("tls_error")

    monkeypatch.setattr(providers, "read_legacy_p3p", compatibility)
    config = settings()
    config.data["execution"]["http_max_retries"] = 3
    gateway = HttpGateway(config, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolver=resolver)
    with pytest.raises(ProviderError) as caught:
        asyncio.run(gateway.request("GET", URL, public=True))
    assert caught.value.code == "tls_error" and len(calls) == 1
