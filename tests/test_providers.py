"""실제 Adapter 구현의 정상·실패 분기를 격리한 HTTP 응답으로 검사한다."""

import asyncio
import json
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pypdf import PdfWriter

from app.domain import validate
from app.providers import (
    DocumentReader, HttpGateway, ModelProvider, NeisProvider, ProviderError,
    TavilyProvider, official_channel, public_addresses, safe_url,
)


def settings():
    data = json.loads(Path("config/defaults.json").read_text())
    data["execution"].update(per_host_min_interval_ms=0, http_max_retries=0)
    return SimpleNamespace(data=data, secrets={})


def institution():
    return {"id": str(uuid4()), "name": "검증초등학교", "type": "elementary",
            "homepage_url": "https://school.example", "official_channels": [
                {"url": "https://school.example", "verified_by_url": "https://registry.example",
                 "verified_at": "2026-10-01T00:00:00Z"},
                {"url": "https://open.neis.go.kr/hub",
                 "verified_by_url": "https://open.neis.go.kr/hub/schoolInfo?ATPT_OFCDC_SC_CODE=F10&Type=json",
                 "verified_at": "2026-10-01T00:00:00Z"}]}


async def resolver(_):
    return ["93.184.216.34"]


def gateway(config, handler, meter=None):
    return HttpGateway(config, meter, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolver=resolver)


def page(rows, total):
    return {"SchoolSchedule": [{"head": [{"list_total_count": total}, {"RESULT": {"CODE": "INFO-000"}}]}, {"row": rows}]}


def test_neis_requires_key_does_not_use_sample():
    calls = []
    g = gateway(settings(), lambda request: calls.append(request))
    with pytest.raises(ProviderError) as caught:
        asyncio.run(NeisProvider(g.settings, gateway=g).rows("SchoolSchedule", {}))
    assert caught.value.code == "configuration_required"
    assert not calls


def test_neis_authenticates_and_reads_all_pages_without_known_day():
    config, calls = settings(), []
    config.secrets["neis_api_key"] = "test-only-secret"

    def handler(request):
        calls.append(dict(request.url.params))
        rows = [{"id": "1"}, {"id": "2"}] if request.url.params["pIndex"] == "1" else [{"id": "3"}]
        return httpx.Response(200, json=page(rows, 3))

    g = gateway(config, handler)
    result = asyncio.run(NeisProvider(config, gateway=g).rows("SchoolSchedule",
        {"ATPT_OFCDC_SC_CODE": "F10", "SD_SCHUL_CODE": "1", "AA_FROM_YMD": "20261201", "AA_TO_YMD": "20270331"}, page_size=2))
    assert len(result) == 3 and [item["pIndex"] for item in calls] == ["1", "2"]
    assert all(item["KEY"] == "test-only-secret" and "AA_YMD" not in item for item in calls)
    assert all(item["AA_FROM_YMD"] == "20261201" and item["AA_TO_YMD"] == "20270331" for item in calls)


@pytest.mark.parametrize("payload, code", [
    ({"RESULT": {"CODE": "ERROR-290", "MESSAGE": "test-only-secret"}}, "provider_error"),
    ({"wrong": []}, "provider_error"),
    (page([{"id": 1}], 3), "coverage_incomplete"),
])
def test_neis_rejects_auth_contract_and_truncated_pages(payload, code):
    config = settings()
    config.secrets["neis_api_key"] = "test-only-secret"
    g = gateway(config, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(ProviderError) as caught:
        asyncio.run(NeisProvider(config, gateway=g).rows("SchoolSchedule", {}, page_size=2))
    assert caught.value.code == code and "test-only-secret" not in str(caught.value)


def test_neis_distinguishes_valid_zero_rows():
    config = settings()
    config.secrets["neis_api_key"] = "test"
    g = gateway(config, lambda request: httpx.Response(200, json={"RESULT": {"CODE": "INFO-200"}}))
    assert asyncio.run(NeisProvider(config, gateway=g).rows("SchoolSchedule", {})) == []


def test_calendar_rejects_wrong_institution_even_with_http_success():
    config = settings()
    config.secrets["neis_api_key"] = "test"
    row = {"ATPT_OFCDC_SC_CODE": "F10", "SD_SCHUL_CODE": "2", "AY": "2026", "AA_YMD": "20270107", "EVENT_NM": "졸업식"}
    g = gateway(config, lambda request: httpx.Response(200, json=page([row], 1)))
    season = {"id": str(uuid4()), "academic_year": 2026, "search_start": "2026-12-01", "search_end": "2027-03-31"}
    result = asyncio.run(NeisProvider(config, gateway=g).calendar(institution(), season, [{"provider": "neis", "external_key": "F10:1"}]))
    validate("CalendarResult", result)
    assert result["reason_codes"] == ["wrong_source"] and not result["coverage_complete"]


def test_reader_pins_public_ip_but_preserves_host_and_tls_name():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text="<html><body>검증초등학교 졸업식 안내</body></html>",
                              headers={"content-type": "text/html"})
    g = gateway(settings(), handler)
    document = asyncio.run(DocumentReader(g).read(institution(), "https://school.example/notice"))
    validate("DocumentRead", document)
    assert calls[0].url.host == "93.184.216.34"
    assert calls[0].headers["Host"] == "school.example" and calls[0].extensions["sni_hostname"] == "school.example"
    assert document["acquisition"]["final_url"] == "https://school.example/notice"
    assert document["authority"] == "official" and document["reading"]["institution_match"] == "matched"


def test_reader_blocks_redirect_to_private_network():
    calls = []

    async def guarded(url):
        if "127.0.0.1" in url:
            raise ProviderError("access_failed", "사설 주소 거부", "reader")
        return ["93.184.216.34"]

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    g = HttpGateway(settings(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolver=guarded)
    document = asyncio.run(DocumentReader(g).read(institution(), "https://school.example/notice"))
    assert document["status"] == "access_failed" and len(calls) == 1


def test_empty_meta_refresh_follows_only_validated_public_url():
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path == "/":
            return httpx.Response(200, text='<html><head><meta http-equiv="refresh" content="0;url=/notice"></head><body></body></html>',
                                  headers={"content-type": "text/html"})
        return httpx.Response(200, text="<html><body>검증초등학교 졸업식 안내</body></html>", headers={"content-type": "text/html"})
    g = gateway(settings(), handler)
    document = asyncio.run(DocumentReader(g).read(institution(), "https://school.example/"))
    assert len(calls) == 2 and document["status"] == "ok"
    assert document["acquisition"]["final_url"] == "https://school.example/notice"


def test_form_wrapper_does_not_erase_public_notice_body():
    g = gateway(settings(), lambda request: httpx.Response(200,
        text='<html><body><form>검증초등학교 졸업식 안내<input value="not evidence"></form></body></html>',
        headers={"content-type": "text/html"}))
    document = asyncio.run(DocumentReader(g).read(institution(), "https://school.example/notice"))
    assert document["status"] == "ok" and "졸업식 안내" in document["segments"][0]["text"]


def test_meta_refresh_revalidates_private_address_before_request():
    calls = []
    async def guarded(url):
        if "127.0.0.1" in url:
            raise ProviderError("access_failed", "사설 주소 거부", "reader")
        return ["93.184.216.34"]
    def handler(request):
        calls.append(request)
        return httpx.Response(200, text='<html><meta http-equiv="refresh" content="0;url=http://127.0.0.1/admin"><body></body></html>',
                              headers={"content-type": "text/html"})
    g = HttpGateway(settings(), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolver=guarded)
    document = asyncio.run(DocumentReader(g).read(institution(), "https://school.example/"))
    assert len(calls) == 1 and document["status"] == "access_failed"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1", "http://[::1]", "http://192.168.0.1", "https://user:pass@example.com"])
def test_public_reader_rejects_non_public_urls(url):
    with pytest.raises(ProviderError):
        asyncio.run(public_addresses(url))


def test_http_200_missing_notice_is_not_valid_evidence():
    g = gateway(settings(), lambda request: httpx.Response(200,
        text="<html><body>검증초등학교 존재하지 않는 게시물입니다.</body></html>", headers={"content-type": "text/html"}))
    document = asyncio.run(DocumentReader(g).read(institution(), "https://school.example/notice"))
    validate("DocumentRead", document)
    assert document["status"] == "content_mismatch" and document["reading"]["content_match"] == "missing_notice"


def test_wrong_signature_and_empty_pdf_are_unsupported():
    stream = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(stream)
    for content, mime in [(b"not html", "text/html"), (stream.getvalue(), "application/pdf")]:
        g = gateway(settings(), lambda request: httpx.Response(200, content=content, headers={"content-type": mime}))
        document = asyncio.run(DocumentReader(g).read(institution(), "https://school.example/file"))
        validate("DocumentRead", document)
        assert document["status"] == "unsupported_format"


def test_document_size_limit_does_not_adopt_prefix_as_full_text():
    config = settings()
    config.data["execution"]["document_max_bytes"] = 30
    g = gateway(config, lambda request: httpx.Response(200, text="<html><body>" + "x" * 40, headers={"content-type": "text/html"}))
    document = asyncio.run(DocumentReader(g).read(institution(), "https://school.example/notice"))
    assert document["status"] == "too_large" and not document["segments"]


def test_tavily_failure_is_not_not_found_and_secrets_are_not_returned():
    config = settings()
    config.secrets["tavily_api_key"] = "test-secret"
    g = gateway(config, lambda request: httpx.Response(400, json={"error": "test-secret"}))
    result = asyncio.run(TavilyProvider(config, gateway=g).search("기관 졸업식"))
    validate("SearchResult", result)
    assert result["status"] == "request_error" and "test-secret" not in json.dumps(result)


def test_external_transfer_blocked_before_any_model_request():
    config, calls = settings(), []
    config.data["models"]["ollama_base_url"] = "https://remote.example"
    g = gateway(config, lambda request: calls.append(request))
    with pytest.raises(ProviderError) as caught:
        asyncio.run(ModelProvider(config, gateway=g).candidates({}, {}, {}))
    assert caught.value.code == "external_transfer_blocked" and calls == []


def test_selected_backend_alone_determines_required_credentials():
    config = settings()
    assert ModelProvider(config)._endpoint() == "http://127.0.0.1:11434/api/chat"
    config.data["app"]["llm_backend"] = "openai"
    with pytest.raises(ProviderError) as caught:
        ModelProvider(config)._endpoint()
    assert caught.value.code == "configuration_required"
    config.secrets["openai_api_key"] = "test"
    config.data["external_transfer"]["document_text_allowed"] = True
    config.data["models"]["ollama_base_url"] = "invalid-ignored"
    assert ModelProvider(config)._endpoint() == "https://api.openai.com/v1/responses"


def test_loopback_ollama_cloud_model_still_requires_external_transfer_permission():
    config = settings()
    config.data["models"]["ollama_text_model"] = "example:cloud"
    with pytest.raises(ProviderError) as caught:
        ModelProvider(config)._endpoint()
    assert caught.value.code == "external_transfer_blocked"


def test_officiality_requires_verified_channel_and_not_name_or_homepage():
    school = institution()
    assert official_channel(school, "https://school.example/notice")
    assert not official_channel(school, "https://school.example.evil.test/notice")
    school["official_channels"] = []
    assert not official_channel(school, school["homepage_url"])
    assert "test-secret" not in safe_url("https://user:test-secret@host.example/a?KEY=test-secret&x=1")


@pytest.mark.parametrize("day, expected_precision", [("2027-01-07", "day"), ("2027-01", "month"), (None, "unknown")])
def test_compact_model_mapping_preserves_only_claimed_precision_and_literal_quotes(day, expected_precision):
    source = "2026학년도 검증초등학교 졸업식: 2027년 1월 7일 오전 11시에 시작."
    claim = {
        "event_title": "2026학년도 졸업식", "event_type": "graduation_ceremony", "academic_year": 2026,
        "ceremony_status": "scheduled", "event_date": day, "start_time": None, "time_role": "unknown",
        "venue": None, "is_change_notice": False, "segment_id": "s1", "context_quote": source,
        "date_quote": "2027년 1월 7일" if day else None, "time_quote": None, "venue_quote": None,
    }
    class Meter:
        def __init__(self):
            self.reserved, self.settled = {}, {}
        async def reserve(self, kind, amount=1):
            key = str(len(self.reserved))
            self.reserved[key] = (kind, amount)
            return key
        async def settle(self, reservation_id, actual_amount=None):
            self.settled[reservation_id] = actual_amount
    meter = Meter()
    g = gateway(settings(), lambda request: httpx.Response(200, json={
        "message": {"content": json.dumps({"candidates": [claim]})}, "prompt_eval_count": 111, "eval_count": 33,
    }), meter=meter)
    document = {"status": "ok", "segments": [{"id": "s1", "text": source}]}
    season = {"academic_year": 2026, "search_start": "2026-12-01", "search_end": "2027-03-31"}
    candidates = asyncio.run(ModelProvider(g.settings, gateway=g).candidates(institution(), season, document))
    candidate = candidates[0]
    validate("Candidate", candidate)
    assert candidate["event_date"]["precision"] == expected_precision
    assert candidate["event_key_hint"] is None and candidate["time_role"] is None
    assert candidate["context_evidence"][0]["quote"] == source
    token_reservation = next(key for key, value in meter.reserved.items() if value[0] == "tokens")
    assert meter.settled[token_reservation] == 144


def test_model_invented_quote_is_rejected_without_promoting_claim():
    source = "2026학년도 검증초등학교 졸업식은 미정입니다."
    claim = {
        "event_title": "2026학년도 졸업식", "event_type": "graduation_ceremony", "academic_year": 2026,
        "ceremony_status": "scheduled", "event_date": "2027-01-07", "start_time": None, "time_role": None,
        "venue": None, "is_change_notice": False, "segment_id": "s1", "context_quote": source,
        "date_quote": "2027년 1월 7일", "time_quote": None, "venue_quote": None,
    }
    g = gateway(settings(), lambda request: httpx.Response(200, json={
        "message": {"content": json.dumps({"candidates": [claim]})},
    }))
    with pytest.raises(ProviderError) as caught:
        asyncio.run(ModelProvider(g.settings, gateway=g).candidates(institution(),
            {"academic_year": 2026, "search_start": "2026-12-01", "search_end": "2027-03-31"},
            {"status": "ok", "segments": [{"id": "s1", "text": source}]}))
    assert caught.value.code == "parse_failed"


def test_token_budget_stop_refunds_unissued_model_call_and_preserves_exception():
    """토큰 예약 실패 전의 호출 예약은 비용 없이 정산하고 중단 사유를 유지한다."""
    class BudgetStop(Exception):
        pass
    stopped = BudgetStop("token budget exhausted")
    class Meter:
        def __init__(self):
            self.reservations, self.settlements = [], []
        async def reserve(self, kind, amount=1):
            self.reservations.append((kind, amount))
            if kind == "tokens":
                raise stopped
            return "unissued-model-call"
        async def settle(self, reservation_id, actual_amount=None):
            self.settlements.append((reservation_id, actual_amount))
    meter, network_calls = Meter(), []
    g = gateway(settings(), lambda request: network_calls.append(request), meter=meter)
    with pytest.raises(BudgetStop) as caught:
        asyncio.run(ModelProvider(g.settings, gateway=g).candidates(institution(),
            {"academic_year": 2026, "search_start": "2026-12-01", "search_end": "2027-03-31"},
            {"status": "ok", "segments": [{"id": "s1", "text": "2026학년도 검증초등학교 졸업식 안내"}]}))
    assert caught.value is stopped
    assert [kind for kind, _ in meter.reservations] == ["llm_calls", "tokens"]
    assert meter.settlements == [("unissued-model-call", 0)]
    assert network_calls == []
