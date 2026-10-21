"""학교별 Agent가 관찰을 보고 행동을 바꾸는 실제 MCP 반복 경로를 검사한다."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.domain import validate
from app.providers import HttpGateway, ModelProvider, neis_document
from app.providers_student import StudentProvider
from app.research import ResearchRunner


def setup_case():
    """외부 서비스와 사용자 DB를 사용하지 않는 학교·시즌을 준비한다."""
    data = json.loads(Path("config/defaults.json").read_text())
    data["execution"].update(per_host_min_interval_ms=0, http_max_retries=0, llm_max_retries=0, item_max_replans=6)
    settings = SimpleNamespace(data=data, secrets={"neis_api_key": "fixture", "tavily_api_key": "fixture"})
    institution = {"id": str(uuid4()), "name": "검증초등학교", "type": "elementary", "homepage_url": None,
        "official_channels": [{"url": "https://open.neis.go.kr/hub", "verified_by_url": "https://open.neis.go.kr/hub/schoolInfo", "verified_at": "2026-10-01T00:00:00Z"},
            {"url": "https://school.example", "verified_by_url": "https://open.neis.go.kr/hub/schoolInfo", "verified_at": "2026-10-01T00:00:00Z"}]}
    season = {"id": str(uuid4()), "academic_year": 2026, "search_start": "2026-12-01", "search_end": "2027-03-31"}
    return settings, institution, season


async def resolve(_):
    return ["93.184.216.34"]


async def no_students(self, institution, season, identifiers):
    return {"status": "no_data", "reason_codes": [], "documents": [], "counts": []}


def decision(kind="stop", objective="event_date", query=None, url=None, reason="공개 근거를 확인했습니다.", candidates=None, counts=None):
    return {"action": {"kind": kind, "objective": objective, "query": query, "url": url},
            "candidates": candidates or [], "counts": counts or [], "reason": reason}


def no_calendar():
    return httpx.Response(200, json={"RESULT": {"CODE": "INFO-200", "MESSAGE": "해당하는 데이터가 없습니다."}})


def test_structured_date_and_students_stop_without_model_or_time_search(monkeypatch):
    """인원 검증기는 별도 검사의 책임이며 여기서는 완료조건과 호출 경계를 확인한다."""
    settings, institution, season = setup_case()
    row = {"ATPT_OFCDC_SC_CODE": "F10", "SD_SCHUL_CODE": "123", "SCHUL_NM": institution["name"],
           "AY": "2026", "AA_YMD": "20270107", "EVENT_NM": "졸업식", "EVENT_CNTNT": "",
           "SCHUL_CRSE_SC_NM": "초등학교", "SIX_GRADE_EVENT_YN": "Y"}
    calls = []

    async def students(self, school, window, identifiers):
        document = neis_document(school, row, {"AY": "2026"})
        count = {"document_id": document["id"], "value": 120, "academic_year": 2026,
                 "kind": "graduating_grade_enrollment", "grade": 6, "unit": "persons",
                 "evidence": [{"segment_id": "row", "quote": document["segments"][0]["text"]}],
                 "source": "schoolinfo_api09", "source_school_code": "123", "source_field": "COL_S6"}
        return {"status": "ok", "reason_codes": [], "documents": [document], "counts": [count]}

    def handler(request):
        calls.append(request.url.host)
        assert request.url.host == "open.neis.go.kr"
        return httpx.Response(200, json={"SchoolSchedule": [{"head": [{"list_total_count": 1}, {"RESULT": {"CODE": "INFO-000"}}]}, {"row": [row]}]})

    async def forbidden(*args, **kwargs):
        raise AssertionError("충분한 정형 근거 뒤 모델을 호출했습니다.")

    monkeypatch.setattr(StudentProvider, "lookup", students)
    monkeypatch.setattr(ModelProvider, "decide", forbidden)
    monkeypatch.setattr("app.claims.validate_count", lambda *args, **kwargs: {"allowed": True, "reasons": []})
    gateway = HttpGateway(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolver=resolve)
    result = asyncio.run(ResearchRunner(settings, gateway=gateway).run(institution, season, identifiers=[{"provider": "neis", "external_key": "F10:123"}]))
    assert result["status"] == "completed" and result["stop_reason"] == "structured_evidence_sufficient"
    assert result["queries"] == [] and calls == ["open.neis.go.kr"]
    assert [item["tool"] for item in result["trace"]] == ["read_resource", "get_school_calendar", "get_graduating_students"]
    assert not any(item["kind"] == "decision" for item in result["agent_trace"])


def test_agent_changes_action_after_old_year_search_and_document_observations(monkeypatch):
    settings, institution, season = setup_case()
    institution["homepage_url"] = "https://school.example/old"
    contexts, calls = [], []
    monkeypatch.setattr(StudentProvider, "lookup", no_students)

    def handler(request):
        calls.append((request.url.host, request.url.path))
        if request.url.host == "open.neis.go.kr":
            return no_calendar()
        if request.url.host == "api.tavily.com":
            assert "시간" not in json.loads(request.content)["query"]
            return httpx.Response(200, json={"results": [{"url": "https://school.example/new", "title": "올해 안내", "content": "발견용 요약"}]})
        text = ("2025학년도 검증초등학교 졸업식은 2026년 1월 7일입니다." if request.url.path == "/old"
                else "2026학년도 검증초등학교 졸업식은 2027년 1월 7일 오전 10시에 시작합니다.")
        return httpx.Response(200, text=f"<html><body>{text}</body></html>", headers={"content-type": "text/html"})

    async def model(self, school, window, context):
        contexts.append(deepcopy(context))
        if len(contexts) == 1:
            assert any(item.get("outcome") == "wrong_year" for item in context["observations"])
            return decision("search_web", query='"검증초등학교" "2026학년도" 졸업식 날짜', reason="읽은 안내는 이전 학년도이므로 올해 공지를 찾습니다.")
        if len(contexts) == 2:
            assert context["observations"][-1]["tool"] == "search_public_web"
            assert "https://school.example/new" in context["available_links"]
            return decision("fetch_document", url="https://school.example/new", reason="검색에서 발견한 공식 원문을 읽습니다.")
        assert context["observations"][-1]["document_id"] in {item["id"] for item in context["documents"]}
        assert context["missing_fields"] == ["graduation_count"]
        return decision(reason="날짜와 같은 원문의 본식 시각은 확인했으며 인원 근거는 확보하지 못했습니다.")

    monkeypatch.setattr(ModelProvider, "decide", model)
    gateway = HttpGateway(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolver=resolve)
    result = asyncio.run(ResearchRunner(settings, gateway=gateway).run(institution, season, identifiers=[{"provider": "neis", "external_key": "F10:123"}]))
    assert len(contexts) == 3 and result["stop_reason"] == "agent_stop"
    assert [item["action"]["kind"] for item in result["agent_trace"] if item["kind"] == "decision"] == ["search_web", "fetch_document", "stop"]
    assert any(item["candidate"]["start_time"] == "10:00" for item in result["candidates"])
    assert result["missing_fields"] == ["graduation_count"]
    assert sum(host == "api.tavily.com" for host, _ in calls) == 1


def test_purpose_and_duplicate_guards_reject_before_tools(monkeypatch):
    settings, institution, season = setup_case()
    monkeypatch.setattr(StudentProvider, "lookup", no_students)
    actions = iter([
        decision("search_web", objective="start_time", query="검증초등학교 졸업식 시간"),
        decision("search_web", query="검증초등학교 졸업식 날짜"),
        decision("search_web", query=" 검증초등학교  졸업식 날짜 "),
        decision("fetch_document", url="https://other.example/notice"),
        decision(),
    ])
    searches = []

    async def model(*args):
        return next(actions)

    def handler(request):
        if request.url.host == "open.neis.go.kr":
            return no_calendar()
        assert request.url.host == "api.tavily.com"
        searches.append(json.loads(request.content)["query"])
        return httpx.Response(200, json={"results": []})

    monkeypatch.setattr(ModelProvider, "decide", model)
    gateway = HttpGateway(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolver=resolve)
    result = asyncio.run(ResearchRunner(settings, gateway=gateway).run(institution, season, identifiers=[{"provider": "neis", "external_key": "F10:123"}]))
    assert searches == ["검증초등학교 졸업식 날짜"]
    assert [item["reason"] for item in result["agent_trace"] if item["kind"] == "action_rejected"] == ["purpose_mismatch", "duplicate_request", "unverified_source"]


def test_failed_route_is_observed_and_does_not_become_missing_fact(monkeypatch):
    settings, institution, season = setup_case()
    monkeypatch.setattr(StudentProvider, "lookup", no_students)
    contexts = []

    async def model(self, school, window, context):
        contexts.append(context)
        return decision(reason="공식 경로 접근 실패를 정보 미공개로 단정하지 않습니다.")

    def handler(request):
        if request.url.host == "open.neis.go.kr":
            return no_calendar()
        return httpx.Response(503)

    monkeypatch.setattr(ModelProvider, "decide", model)
    gateway = HttpGateway(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolver=resolve)
    result = asyncio.run(ResearchRunner(settings, gateway=gateway).run(institution, season,
        identifiers=[{"provider": "neis", "external_key": "F10:123"}],
        routes=[{"id": "route", "url": "https://school.example/calendar", "purpose": "date_count", "status": "verified"}]))
    assert contexts[0]["observations"][-1]["outcome"] == "access_failed"
    assert result["routes"][0]["failure_reason"] == "access_failed"
    assert result["routes"][0]["reusable"] is False
    assert result["missing_fields"] == ["event_date", "graduation_count"]


def test_time_search_binds_existing_event_and_never_uses_new_date_clock(monkeypatch):
    settings, institution, season = setup_case()
    institution["homepage_url"] = "https://school.example/notice"
    target = {"id": str(uuid4()), "title": "졸업식", "event_type": "graduation_ceremony", "audience": None,
              "campus_label": None, "event_date": {"precision": "day", "date": "2027-01-07", "month": None}}
    contexts = []

    async def forbidden(*args, **kwargs):
        raise AssertionError("시각 조사에서 졸업 학년 인원을 다시 조회했습니다.")

    async def model(self, school, window, context):
        contexts.append(context)
        return decision(objective="start_time", reason="다른 날짜의 시각을 대상 행사에 붙이지 않습니다.")

    def handler(request):
        assert request.url.host == "93.184.216.34"
        return httpx.Response(200, text="<html><body>2026학년도 검증초등학교 졸업식은 2027년 1월 8일 오전 10시에 시작합니다.</body></html>", headers={"content-type": "text/html"})

    monkeypatch.setattr(StudentProvider, "lookup", forbidden)
    monkeypatch.setattr(ModelProvider, "decide", model)
    gateway = HttpGateway(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolver=resolve)
    result = asyncio.run(ResearchRunner(settings, gateway=gateway).run(institution, season, purpose="time", target_event=target))
    assert result["target_event_id"] == target["id"]
    assert result["missing_fields"] == ["start_time"] and contexts[0]["purpose"] == "time"
    assert [item["tool"] for item in result["trace"]] == ["read_resource", "read_public_document"]


def test_model_context_counts_serialized_newlines_and_keeps_recent_observation(monkeypatch):
    """실제 홈페이지에서 발견한 JSON escape 길이 초과를 재현한다."""
    settings, institution, season = setup_case()
    captured = []

    async def structured(self, system, user, schema):
        from app.providers import canonical
        assert len(system) + len(user) + len(canonical(schema)) <= settings.data["execution"]["llm_max_input_tokens"]
        captured.append(json.loads(user))
        return decision()

    monkeypatch.setattr(ModelProvider, "_structured", structured)
    context = {"purpose": "date_count", "expected_fields": ["event_date", "graduation_count"],
        "missing_fields": ["event_date", "graduation_count"], "conflicts": [], "target_event": None,
        "prior_actions": [], "candidate_reviews": [], "reusable_routes": [], "available_tools": ["search_web", "fetch_document"],
        "available_links": [f"https://school.example/notice/{i}" for i in range(300)],
        "observations": [{"tool": "search_public_web", "results": [{"url": "https://school.example/new", "title": "일정", "snippet": "본문" * 2000}]}],
        "limits_remaining": {"decisions": 5, "search_calls": 3, "document_fetches": 6},
        "documents": [{"id": str(uuid4()), "display_url": "https://school.example", "title": "학교 홈페이지", "status": "ok", "source_kind": "html",
                       "segments": [{"id": "s1", "text": "메뉴\n\"링크\"\n" * 300 + "2026학년도 졸업식\n2027.1.7.\n" + "내용\n" * 1000}]}]}
    result = asyncio.run(ModelProvider(settings).decide(institution, season, context))
    assert result["action"]["kind"] == "stop"
    assert captured[0]["observations"][-1]["tool"] == "search_public_web"
    assert captured[0]["documents"][0]["segments"][0]["text"]
    assert "2026학년도 졸업식" in captured[0]["documents"][0]["segments"][0]["text"]
    assert context["observations"][0]["results"][0]["snippet"] == "본문" * 2000


@pytest.mark.parametrize("changed_quote", [False, True])
def test_compact_model_claim_restores_internal_contract_only_with_literal_evidence(monkeypatch, changed_quote):
    settings, institution, season = setup_case()
    text = "2026학년도 검증초등학교 졸업식\n일시: 2027. 1. 7. 오전 10시"
    doc_id = str(uuid4())
    claim = {"evidence_block_id": "b1", "event_title": "2026학년도 졸업식", "event_type": "graduation_ceremony",
             "academic_year": 2026, "ceremony_status": "scheduled", "dates": ["2027-01-07"],
             "start_time": "10:00", "campus_label": None,
             "date_quote": "2027. 1. 7.", "time_quote": "오전 11시" if changed_quote else "오전 10시"}

    async def structured(self, system, user, schema):
        assert "context_quote" not in schema["properties"]["candidates"]["items"]["properties"]
        assert schema["properties"]["candidates"]["items"]["properties"]["evidence_block_id"]["enum"] == ["b1"]
        return decision(candidates=[claim])

    monkeypatch.setattr(ModelProvider, "_structured", structured)
    context = {"purpose": "date_count", "expected_fields": ["event_date", "graduation_count"],
               "missing_fields": ["event_date", "graduation_count"], "conflicts": [],
               "documents": [{"id": doc_id, "segments": [{"id": "s1", "text": text}]}]}
    model = ModelProvider(settings)
    result = asyncio.run(model.decide(institution, season, context))
    candidate = result["candidates"][0]["candidate"]
    validate("Candidate", candidate)
    assert candidate["event_key_hint"] is None and candidate["season_alignment"] == "explicit"
    assert candidate["event_date"]["date"] == "2027-01-07"
    assert candidate["context_evidence"][0]["quote"] == text
    assert model.last_diagnostics["original_proposal"]["candidates"][0] == claim
    if changed_quote:
        assert candidate["start_time"] is None and candidate["time_evidence"] == []
        assert model.last_diagnostics["rejected_fields"][0]["field"] == "start_time"
        assert model.last_diagnostics["rejected_fields"][0]["proposed_quote"] == "오전 11시"
    else:
        assert candidate["time_evidence"][0]["quote"] == "오전 10시"


def test_calendar_excerpt_preserves_the_actual_row_and_nearest_month_header(monkeypatch):
    settings, institution, season = setup_case()
    institution.update(type="university", name="검증대학교")
    text = "2026.08\n08.20(목)\n2025학년도 후기 학위수여일\n2027.02\n1\n2\n3\n02.18(목)\n교직원 세미나\n02.19(금)\n2026학년도 전기 학위수여식\n학교 정보"
    captured = []

    async def structured(self, system, user, schema):
        captured.append(json.loads(user))
        for branch in schema["properties"]["action"]["oneOf"]:
            assert "graduation_count" not in branch["properties"]["objective"]["enum"]
        count = schema["properties"]["counts"]["items"]
        assert count["properties"]["source"] == {"const": "public_document"}
        assert count["properties"]["kind"] == {"const": "ceremony_graduates"}
        from jsonschema import Draft202012Validator, FormatChecker
        day = Draft202012Validator(schema["properties"]["candidates"]["items"]["properties"]["dates"]["items"],
                                   format_checker=FormatChecker())
        assert day.is_valid("2027-02-19") and day.is_valid("2026-12-07")
        assert not day.is_valid("2026-02-19") and not day.is_valid("2027-02-30")
        return decision()

    monkeypatch.setattr(ModelProvider, "_structured", structured)
    context = {"purpose": "date_count", "expected_fields": ["event_date", "graduation_count"],
               "missing_fields": ["event_date", "graduation_count"], "documents": [{"id": str(uuid4()), "segments": [{"id": "s2", "text": text}]}]}
    asyncio.run(ModelProvider(settings).decide(institution, season, context))
    excerpt = captured[0]["documents"][0]["segments"][0]
    assert excerpt == {"id": "s2", "evidence_block_id": "b1", "text": "02.19(금)\n2026학년도 전기 학위수여식", "section_header": "2027.02"}
    assert excerpt["text"] in text and excerpt["section_header"] in text


def test_model_cannot_select_an_unpresented_document_segment(monkeypatch):
    """잘라서 보여주지 않은 구간 ID를 근거로 선택하면 후보 전체를 거절한다."""
    from app.providers import ProviderError
    settings, institution, season = setup_case()
    doc_id = str(uuid4())
    claim = {"evidence_block_id": "b99", "event_title": "2026학년도 졸업식",
             "event_type": "graduation_ceremony", "academic_year": 2026, "ceremony_status": "scheduled",
             "dates": ["2027-01-07"], "start_time": None, "campus_label": None,
             "date_quote": "2027. 1. 7.", "time_quote": None}

    async def structured(self, system, user, schema):
        assert json.loads(user)["documents"][0]["segments"][0]["id"] == "ceremony"
        return decision(candidates=[claim])

    monkeypatch.setattr(ModelProvider, "_structured", structured)
    context = {"purpose": "date_count", "missing_fields": ["event_date"],
               "documents": [{"id": doc_id, "segments": [
                   {"id": "ceremony", "text": "2026학년도 검증초등학교 졸업식 2027. 1. 7."},
                   {"id": "hidden", "text": "학교 알림 2027. 1. 7."}]}]}
    model = ModelProvider(settings)
    with pytest.raises(ProviderError) as error:
        asyncio.run(model.decide(institution, season, context))
    assert error.value.code == "parse_failed"
    assert model.last_diagnostics["original_proposal"]["candidates"][0] == claim


def test_model_without_read_evidence_cannot_generate_fact_candidates(monkeypatch):
    """충돌 관찰만 있는 경우 조사 행동은 허용하고 사실 제안은 비운다."""
    settings, institution, season = setup_case()

    async def structured(self, system, user, schema):
        assert schema["properties"]["candidates"]["maxItems"] == 0
        assert schema["properties"]["counts"]["maxItems"] == 0
        for branch in schema["properties"]["action"]["oneOf"]:
            assert set(branch["properties"]["objective"]["enum"]) == {"conflict", "change_notice", "event_identity"}
        return decision(objective="conflict")

    monkeypatch.setattr(ModelProvider, "_structured", structured)
    context = {"purpose": "date_count", "documents": [], "conflicts": ["event_date"],
               "missing_fields": ["event_date", "graduation_count"]}
    result = asyncio.run(ModelProvider(settings).decide(institution, season, context))
    assert result["candidates"] == result["counts"] == []
