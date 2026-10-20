"""실제 LangGraph·MCP를 통과하는 연구 흐름을 격리 transport로 검사한다."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.domain import validate
from app.providers import HttpGateway, ModelProvider, ProviderError
from app.research import ResearchRunner


def fixture():
    data = json.loads(Path("config/defaults.json").read_text())
    data["execution"].update(per_host_min_interval_ms=0, http_max_retries=0, llm_max_retries=0)
    settings = SimpleNamespace(data=data, secrets={"neis_api_key": "fixture", "tavily_api_key": "fixture"})
    institution = {"id": str(uuid4()), "name": "검증초등학교", "type": "elementary", "homepage_url": None,
        "official_channels": [{"url": "https://open.neis.go.kr/hub",
            "verified_by_url": "https://open.neis.go.kr/hub/schoolInfo", "verified_at": "2026-10-01T00:00:00Z"},
            {"url": "https://school.example", "verified_by_url": "https://open.neis.go.kr/hub/schoolInfo", "verified_at": "2026-10-01T00:00:00Z"}]}
    season = {"id": str(uuid4()), "academic_year": 2026, "search_start": "2026-12-01", "search_end": "2027-03-31"}
    return settings, institution, season


class Meter:
    def __init__(self):
        self.calls, self.checkpoints = [], []

    async def reserve(self, kind, amount=1):
        self.calls.append((kind, amount))
        return str(len(self.calls))

    async def settle(self, reservation_id):
        pass

    async def checkpoint(self, result, stage):
        self.checkpoints.append((stage, len(result["documents"]), len(result["candidates"])))


async def resolver(_):
    return ["93.184.216.34"]


def test_langgraph_neis_then_missing_time_search_through_actual_mcp():
    settings, institution, season = fixture()
    calls, meter = [], Meter()

    def handler(request):
        calls.append((request.url.host, request.url.path))
        if request.url.host == "open.neis.go.kr":
            row = {"ATPT_OFCDC_SC_CODE": "F10", "SD_SCHUL_CODE": "123", "SCHUL_NM": institution["name"],
                   "AY": "2026", "AA_YMD": "20270107", "EVENT_NM": "졸업식", "EVENT_CNTNT": "",
                   "SCHUL_CRSE_SC_NM": "초등학교", "SIX_GRADE_EVENT_YN": "Y"}
            return httpx.Response(200, json={"SchoolSchedule": [
                {"head": [{"list_total_count": 1}, {"RESULT": {"CODE": "INFO-000"}}]}, {"row": [row]}]})
        if request.url.host == "api.tavily.com":
            return httpx.Response(200, json={"results": [{"url": "https://school.example/notice", "title": "안내", "content": "검색 요약은 근거 아님"}]})
        if request.url.host == "93.184.216.34":
            return httpx.Response(200, text="<html><body>2026학년도 검증초등학교 졸업식은 2027년 1월 7일 오전 11시에 시작합니다.</body></html>",
                                  headers={"content-type": "text/html"})
        raise AssertionError("정형 근거가 충분한데 모델을 호출했습니다.")

    g = HttpGateway(settings, meter, httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolver)
    result = asyncio.run(ResearchRunner(settings, meter, g).run(institution, season,
        identifiers=[{"provider": "neis", "external_key": "F10:123"}]))
    assert len(result["documents"]) == 2 and len(result["candidates"]) == 2
    assert result["coverage"]["calendar"]["coverage_complete"]
    tool_calls = [item["tool"] for item in result["trace"] if "tool" in item]
    assert tool_calls == ["read_resource", "get_school_calendar", "search_public_web", "read_public_document"]
    assert not any(kind == "llm_calls" for kind, _ in meter.calls)
    assert meter.checkpoints[-1][2] == 2
    for document in result["documents"]:
        validate("DocumentRead", document)
    for item in result["candidates"]:
        validate("Candidate", item["candidate"])


def test_missing_services_produce_partial_research_not_fake_results():
    settings, institution, season = fixture()
    settings.secrets = {}
    calls = []
    g = HttpGateway(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: calls.append(request))), resolver=resolver)
    result = asyncio.run(ResearchRunner(settings, gateway=g).run(institution, season,
        identifiers=[{"provider": "neis", "external_key": "F10:123"}]))
    assert not calls and not result["candidates"] and not result["documents"]
    assert result["status"] == "partial"
    assert {error["provider"] for error in result["errors"]} == {"neis", "tavily"}


def test_budget_stop_survives_real_mcp_error_boundary():
    settings, institution, season = fixture()

    class BudgetStop(Exception):
        pass

    class StopMeter(Meter):
        async def reserve(self, kind, amount=1):
            raise BudgetStop("job_budget")

    meter = StopMeter()
    g = HttpGateway(settings, meter, httpx.AsyncClient(transport=httpx.MockTransport(lambda request: None)), resolver)
    with pytest.raises(BudgetStop):
        asyncio.run(ResearchRunner(settings, meter, g).run(institution, season,
            identifiers=[{"provider": "neis", "external_key": "F10:123"}]))


def test_model_retry_reserves_each_real_attempt_and_propagates_budget_stop():
    settings, institution, season = fixture()
    settings.data["execution"]["llm_max_retries"] = 1
    calls, meter = [], Meter()

    def handler(request):
        calls.append(request)
        return httpx.Response(503 if len(calls) == 1 else 200,
                              json={} if len(calls) == 1 else {"message": {"content": '{"candidates":[]}'}})

    g = HttpGateway(settings, meter, httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolver)
    document = {"status": "ok", "segments": [{"id": "s1", "text": "졸업식 불확실"}]}
    assert asyncio.run(ModelProvider(settings, meter, g).candidates(institution, season, document)) == []
    assert sum(kind == "llm_calls" for kind, _ in meter.calls) == 2
    assert sum(kind == "http_requests" for kind, _ in meter.calls) == 2
