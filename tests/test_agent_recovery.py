"""실제 Agent·MCP와 격리 HTTP 경로로 제한된 판단 복구를 검사한다."""

import asyncio
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest

from app.jobs import BudgetStop
from app.providers import HttpGateway, ModelProvider, ProviderError
from app.research import ResearchRunner
from test_agent_loop import decision, resolve, setup_case
from test_grounded_claims import NORMAL, candidate


class RecordingMeter:
    """판단 예약과 체크포인트를 기록해 복구가 예산을 우회하지 않는지 확인한다."""

    def __init__(self):
        self.reservations, self.checkpoints = [], []

    async def reserve(self, kind, amount=1):
        self.reservations.append((kind, amount))
        return str(len(self.reservations))

    async def settle(self, reservation_id, actual_amount=None):
        pass

    async def checkpoint(self, result, stage):
        self.checkpoints.append((stage, deepcopy(result)))


def fixture():
    """외부 설정·사용자 DB를 읽지 않는 특정 행사 시각 조사를 준비한다."""
    settings, school, season = setup_case()
    target = {"id": str(uuid4()), "title": "졸업식", "event_type": "graduation_ceremony",
              "audience": None, "campus_label": None,
              "event_date": {"precision": "day", "date": "2027-01-07", "month": None}}
    return settings, school, season, target


def execute(monkeypatch, context, choices, *, pages=None, results=None, meter=None):
    """모델 제안만 고정하고 HTTP·MCP·근거 검사는 실제 구현으로 실행한다.

    Args:
        monkeypatch: 검사별 모델 제안 교체기.
        context: 격리 설정·학교·시즌·대상 행사.
        choices: 정해진 제안, 오류 또는 현재 문맥을 받는 함수 목록.
        pages: 주소 경로별 공개 HTML 사본.
        results: 검색이 발견한 URL 목록.
        meter: 예약과 체크포인트를 검사할 계량기.
    Returns:
        최종 조사 결과, 모델 문맥, 실제 HTTP 요청, 계량기.
    """
    settings, school, season, target = context
    contexts, requests = [], []
    actions = iter(choices)
    pages, results = pages or {}, results or []
    meter = meter or RecordingMeter()

    async def choose(self, institution, window, public):
        contexts.append(deepcopy(public))
        item = next(actions)
        if isinstance(item, BaseException):
            raise item
        return item(public) if callable(item) else deepcopy(item)

    def handler(request):
        requests.append(request)
        if request.url.host == "api.tavily.com":
            return httpx.Response(200, json={"results": [
                {"url": url, "title": "학교 안내", "content": "발견용 요약"} for url in results]})
        assert request.url.host == "93.184.216.34"
        return httpx.Response(200, text="<html><body>" + pages.get(request.url.path, "학교 정보") + "</body></html>",
                              headers={"content-type": "text/html"})

    monkeypatch.setattr(ModelProvider, "decide", choose)
    gateway = HttpGateway(settings, meter, httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolve)
    result = asyncio.run(ResearchRunner(settings, meter, gateway).run(
        school, season, purpose="time", target_event=target))
    return result, contexts, requests, meter


def search(query):
    """본식 시각을 찾는 검색 제안을 만든다."""
    return decision("search_web", objective="start_time", query=query)


def fetch(url):
    """공식 문서를 읽는 제안을 만든다."""
    return decision("fetch_document", objective="start_time", url=url)


def finish():
    """현재 확인 범위를 넘는 사실을 보충하지 않는 종료 제안을 만든다."""
    return decision(objective="start_time")


def test_duplicate_query_gets_one_correction_without_second_search(monkeypatch):
    result, contexts, requests, meter = execute(monkeypatch, fixture(), [
        search("검증학교 Graduation"), search(" 검증학교  GRADUATION "),
        fetch("https://school.example/notice"), finish(),
    ], results=["https://school.example/notice"])
    assert result["stop_reason"] == "agent_stop"
    assert len([request for request in requests if request.url.host == "api.tavily.com"]) == 1
    assert contexts[2]["decision_feedback"]["kind"] == "duplicate_request"
    assert contexts[2]["action_options"]["blocked_queries"] == ["검증학교 graduation"]
    assert contexts[3]["decision_feedback"] is None
    assert sum(kind == "replans" for kind, _ in meter.reservations) == len(contexts) == 4


def test_repeated_url_recovers_once_to_unvisited_official_document(monkeypatch):
    context = fixture()
    context[1]["homepage_url"] = "https://school.example/"
    result, contexts, requests, _ = execute(monkeypatch, context, [
        fetch("https://SCHOOL.example/#first"), fetch("https://school.example/#second"), finish(),
    ], pages={"/": '<a href="/notice/new">새 안내</a><a href="/#seen">홈</a>'
                   '<a href="https://other.example/notice">다른 사이트</a>'})
    assert result["stop_reason"] == "agent_stop"
    assert [request.url.path for request in requests] == ["/", "/notice/new"]
    assert contexts[0]["action_options"]["fetch_urls"] == ["https://school.example/notice/new"]
    assert contexts[2]["action_options"]["fetch_urls"] == []
    recovery = [entry for entry in result["agent_trace"] if entry["kind"] == "recovery_action"]
    assert len(recovery) == 1 and recovery[0]["action"]["url"].endswith("/notice/new")


@pytest.mark.parametrize("document_limit", [False, True])
def test_repeated_request_without_usable_path_stops_before_more_calls(monkeypatch, document_limit):
    context = fixture()
    context[1]["homepage_url"] = "https://school.example/"
    if document_limit:
        context[0].data["execution"]["item_max_document_fetches"] = 1
    result, contexts, requests, _ = execute(monkeypatch, context, [
        fetch("https://school.example/#a"), fetch("https://school.example/#b"),
    ], pages={"/": '<a href="/notice/new">새 안내</a>' if document_limit else "학교 안내"})
    assert result["stop_reason"] == "repeated_action"
    assert result["missing_fields"] == ["start_time"]
    assert len(contexts) == 2 and len(requests) == 1
    assert not any(entry["kind"] == "recovery_action" for entry in result["agent_trace"])


def test_interleaved_duplicates_do_not_reset_correction_for_same_request(monkeypatch):
    context = fixture()
    context[1]["homepage_url"] = "https://school.example/"
    result, contexts, requests, _ = execute(monkeypatch, context, [
        search("검증학교 본식"), search("검증학교 본식"),
        fetch("https://school.example/#old"), search("검증학교  본식"),
    ])
    assert result["stop_reason"] == "repeated_action"
    assert len(contexts) == 4 and len(requests) == 2
    assert [entry["reason"] for entry in result["agent_trace"] if entry["kind"] == "action_rejected"] == [
        "duplicate_request", "duplicate_request", "duplicate_request"]


def test_successful_tool_starts_a_new_correction_episode(monkeypatch):
    result, contexts, requests, _ = execute(monkeypatch, fixture(), [
        search("검증학교 본식"), search("검증학교 본식"), fetch("https://school.example/notice"),
        search("검증학교 본식"), finish(),
    ], results=["https://school.example/notice"])
    assert result["stop_reason"] == "agent_stop" and len(contexts) == 5
    assert len(requests) == 2
    assert contexts[4]["decision_feedback"]["kind"] == "duplicate_request"
    assert not any(entry["kind"] == "recovery_action" for entry in result["agent_trace"])


def test_automatic_document_recovery_is_once_per_school(monkeypatch):
    context = fixture()
    context[1]["homepage_url"] = "https://school.example/"
    result, contexts, requests, _ = execute(monkeypatch, context, [
        fetch("https://school.example/#a"), fetch("https://school.example/#b"),
        fetch("https://school.example/#c"), fetch("https://school.example/#d"),
    ], pages={"/": '<a href="/notice/a">안내 A</a><a href="/notice/b">안내 B</a>'})
    assert result["stop_reason"] == "repeated_action"
    assert len(contexts) == 4 and [request.url.path for request in requests] == ["/", "/notice/a"]
    assert contexts[-1]["action_options"]["fetch_urls"] == ["https://school.example/notice/b"]
    assert len([entry for entry in result["agent_trace"] if entry["kind"] == "recovery_action"]) == 1


def test_action_options_exclude_visited_and_nonofficial_links_and_cap_choices(monkeypatch):
    context = fixture()
    context[1]["homepage_url"] = "https://school.example/"
    links = "".join(f'<a href="/notice/{index:02}">안내 {index}</a>' for index in range(12))
    result, contexts, _, _ = execute(monkeypatch, context, [finish()], pages={
        "/": links + '<a href="/#old">이미 읽음</a><a href="https://other.example/a">외부</a>'})
    assert result["stop_reason"] == "agent_stop"
    assert contexts[0]["action_options"]["fetch_urls"] == [
        f"https://school.example/notice/{index:02}" for index in range(8)]


def invalid_response():
    """원문 없이 필드 경로와 규칙만 포함한 형식 오류를 만든다."""
    return ProviderError("parse_failed", "합성 형식 오류", "ollama", diagnostic={
        "kind": "schema", "path": "action.query", "validator": "type", "output_truncated": False})


@pytest.mark.parametrize("success_between", [False, True])
def test_format_correction_is_once_per_school_even_after_valid_action(monkeypatch, success_between):
    error = invalid_response()
    choices = [error]
    if success_between:
        choices.append(search("검증학교 본식"))
    choices.append(invalid_response())
    result, contexts, requests, meter = execute(monkeypatch, fixture(), choices)
    assert result["stop_reason"] == "model_unavailable"
    assert len(contexts) == (3 if success_between else 2)
    feedback = contexts[1]["decision_feedback"]
    assert feedback["kind"] == "invalid_contract"
    assert feedback["details"] == error.diagnostic
    assert len(requests) == int(success_between)
    assert sum(kind == "replans" for kind, _ in meter.reservations) == len(contexts)
    failures = [entry for entry in result["agent_trace"] if entry["kind"] == "decision_failed"]
    assert len(failures) == 2
    assert all(entry["diagnostic"] == error.diagnostic for entry in failures)


@pytest.mark.parametrize("diagnostic, required_text", [
    (
        {"kind": "json_syntax", "output_truncated": True, "finish_reason": "length"},
        ("출력 한도", "완결된 JSON", "새 값을 만들지"),
    ),
    (
        {"kind": "schema", "path": "candidates.0.dates.0", "validator": "format",
         "output_truncated": False},
        ("YYYY-MM-DD", "2026-12-01~2027-03-31", "원문 날짜를 바꾸지"),
    ),
    (
        {"kind": "schema", "path": "action.query", "validator": "required",
         "output_truncated": False},
        ("필수 항목", "null 또는 빈 배열", "사실을 만들어 채우지"),
    ),
    (
        {"kind": "evidence_mapping", "path": "candidates.0.evidence_block_id",
         "output_truncated": False},
        ("evidence_block_id", "실제 인용", "새 사실을 만들지"),
    ),
])
def test_specific_format_failure_reaches_one_correction_without_inventing_facts(
        monkeypatch, diagnostic, required_text):
    """실제 실패 종류별 지시가 한 번의 수정 문맥과 내부 이력에 함께 보존된다."""
    error = ProviderError("parse_failed", "합성 형식 오류", "ollama", diagnostic=diagnostic)
    result, contexts, requests, meter = execute(monkeypatch, fixture(), [error, finish()])
    assert result["stop_reason"] == "agent_stop"
    assert len(contexts) == 2 and requests == []
    feedback = contexts[1]["decision_feedback"]
    assert feedback["kind"] == "invalid_contract" and feedback["details"] == diagnostic
    assert all(text in feedback["instruction"] for text in required_text)
    failures = [entry for entry in result["agent_trace"] if entry["kind"] == "decision_failed"]
    assert len(failures) == 1 and failures[0]["diagnostic"] == diagnostic
    assert result["candidates"] == [] and result["missing_fields"] == ["start_time"]
    assert sum(kind == "replans" for kind, _ in meter.reservations) == 2


def test_successful_format_correction_does_not_reset_prior_search(monkeypatch):
    result, contexts, requests, meter = execute(monkeypatch, fixture(), [
        search("검증학교 본식"), invalid_response(), finish(),
    ])
    assert result["stop_reason"] == "agent_stop" and len(requests) == 1
    assert contexts[-1]["action_options"]["blocked_queries"] == ["검증학교 본식"]
    assert contexts[-1]["decision_feedback"]["kind"] == "invalid_contract"
    assert sum(kind == "replans" for kind, _ in meter.reservations) == 3


@pytest.mark.parametrize("truthful", [False, True])
def test_last_decision_preserves_only_grounded_time_without_unnecessary_call(monkeypatch, truthful):
    context = fixture()
    context[1].update(name="검증대학교", type="university", homepage_url="https://school.example/notice")
    context[3].update(title="2026학년도 학위수여식", event_type="degree_award_ceremony",
                      academic_year=2026, event_date={"precision": "day", "date": "2027-02-20", "month": None})

    def final(public):
        document = next(document for document in public["documents"] if document["display_url"].endswith("/notice"))
        claim = candidate(start="14:00" if truthful else "15:00")
        return decision("fetch_document", objective="start_time", url="https://school.example/notice",
                        candidates=[{"document_id": document["id"], "candidate": claim}])

    result, contexts, requests, meter = execute(monkeypatch, context, [
        *[fetch(f"https://school.example/blank/{index}") for index in range(5)], final,
    ], pages={"/notice": NORMAL})
    assert len(contexts) == 6 and len(requests) == 6
    assert sum(kind == "replans" for kind, _ in meter.reservations) == 6
    assert result["stop_reason"] == ("evidence_sufficient" if truthful else "decision_limit")
    assert result["missing_fields"] == ([] if truthful else ["start_time"])
    saved = next(payload for stage, payload in reversed(meter.checkpoints) if stage == "agent_decision")
    assert saved["candidates"] == result["candidates"]
    assert any(item["extraction_method"] == "model_candidate" for item in saved["candidates"])


@pytest.mark.parametrize("reason", ["item_budget", "user_pause", "cancelled"])
def test_budget_and_user_stop_during_format_correction_are_not_parse_failures(monkeypatch, reason):
    stop = BudgetStop(reason)
    meter = RecordingMeter()
    with pytest.raises(BudgetStop) as caught:
        execute(monkeypatch, fixture(), [invalid_response(), stop], meter=meter)
    assert caught.value is stop
    assert sum(kind == "replans" for kind, _ in meter.reservations) == 2
    assert any(stage == "decision_rejected" for stage, _ in meter.checkpoints)


@pytest.mark.parametrize("reason", ["item_budget", "user_pause", "cancelled"])
def test_recovery_read_respects_stop_and_keeps_prior_date_checkpoint(monkeypatch, reason):
    context = fixture()
    context[1]["homepage_url"] = "https://school.example/"
    stop = BudgetStop(reason)

    class StopBeforeSecondHttp(RecordingMeter):
        """복구 읽기의 전송 예약에서 중단해 기존 정상 날짜의 보존을 검사한다."""

        async def reserve(self, kind, amount=1):
            if kind == "http_requests" and any(prior == kind for prior, _ in self.reservations):
                raise stop
            return await super().reserve(kind, amount)

    meter = StopBeforeSecondHttp()
    with pytest.raises(BudgetStop) as caught:
        execute(monkeypatch, context, [
            fetch("https://school.example/#a"), fetch("https://school.example/#b"),
        ], pages={"/": '2026학년도 검증초등학교 졸업식은 2027년 1월 7일입니다.'
                       '<a href="/notice/new">새 안내</a>'}, meter=meter)
    assert caught.value is stop
    saved = next(payload for stage, payload in reversed(meter.checkpoints) if stage == "document_read")
    assert any(item["candidate"]["event_date"]["date"] == "2027-01-07" for item in saved["candidates"])
    assert all(item["candidate"]["start_time"] is None for item in saved["candidates"])
    assert sum(kind == "http_requests" for kind, _ in meter.reservations) == 1
