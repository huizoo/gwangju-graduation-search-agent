"""모델 오류를 안전하게 구분하고 현재 조사 상태로 생성 선택지를 제한한다."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from jsonschema import Draft202012Validator

from app.providers import HttpGateway, ModelProvider, ProviderError, canonical
from app.rules import value_check
from test_rules import notice


def settings():
    """운영 설정과 인증키를 읽지 않는 격리 설정을 만든다."""
    data = json.loads(Path("config/defaults.json").read_text())
    data["execution"].update(per_host_min_interval_ms=0, http_max_retries=0)
    return SimpleNamespace(data=data, secrets={})


def decision(kind="stop", query=None, url=None):
    """근거가 아직 없는 합법적인 행동 제안을 만든다."""
    return {"action": {"kind": kind, "objective": "event_date", "query": query, "url": url},
            "reason": "남은 공식 근거를 확인한다.", "candidates": [], "counts": []}


def case():
    """학교와 기간 및 작은 공식 문서 문맥을 준비한다."""
    school = {"id": str(uuid4()), "name": "검사초등학교", "type": "elementary",
              "homepage_url": "https://school.example"}
    period = {"search_start": "2026-12-01", "search_end": "2027-03-31"}
    context = {"purpose": "date_count", "missing_fields": ["event_date"], "documents": [
        {"id": str(uuid4()), "segments": [{"id": "s1", "text": "2026학년도 졸업식 2027. 1. 7. 오전 10시"}]}]}
    return school, period, context


def model_response(body):
    """실제 모델 어댑터에 전달할 격리된 HTTP 응답과 호출 목록을 만든다."""
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=body)

    config = settings()
    gateway = HttpGateway(config, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return ModelProvider(config, gateway=gateway), calls


@pytest.mark.parametrize("body, kind, expected", [
    ({"message": {"content": '{"reason":'}, "done_reason": "length"},
     "json_syntax", {"path": "$", "finish_reason": "length", "output_truncated": True}),
    ({"message": {"content": '{"reason": 42}'}, "done_reason": "stop"},
     "schema", {"path": "reason", "validator": "type", "finish_reason": "stop", "output_truncated": False}),
    ({"message": {"content": "{}"}}, "schema", {"path": "reason", "validator": "required"}),
    ({"message": {"content": "{}"}, "done_reason": "incomplete"},
     "schema", {"path": "reason", "validator": "required", "finish_reason": "incomplete"}),
    ({"message": {"content": []}}, "response_envelope", {"path": "$"}),
    ({"message": {}}, "response_envelope", {"path": "$"}),
])
def test_structured_failure_has_safe_kind_and_no_hidden_model_retry(body, kind, expected):
    model, calls = model_response(body)
    schema = {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}
    with pytest.raises(ProviderError) as caught:
        asyncio.run(model._structured("공개 지시", "공개 자료", schema))
    assert caught.value.code == "parse_failed"
    assert caught.value.diagnostic == model.last_failure == {"kind": kind, **expected}
    assert set(caught.value.as_dict()) == {"provider", "code", "message"}
    assert len(calls) == 1


def test_contract_diagnostic_does_not_copy_unknown_fields_or_values():
    marker = "private-raw-value-must-not-be-recorded"
    model, _ = model_response({"message": {"content": json.dumps({marker: marker})},
                               "done_reason": marker})
    schema = {"type": "object", "properties": {marker: {"type": "integer"}}}
    with pytest.raises(ProviderError) as caught:
        asyncio.run(model._structured("공개 지시", "공개 자료", schema))
    assert caught.value.diagnostic == {"kind": "schema", "path": "?", "validator": "type"}
    assert marker not in canonical(model.last_failure) + canonical(caught.value.as_dict())


def test_success_clears_previous_failure_and_retains_real_usage():
    model, _ = model_response({"message": {"content": '{"reason":"자료 없음"}'}, "done_reason": "stop",
                               "prompt_eval_count": 12, "eval_count": 8})
    model.last_failure = {"kind": "schema"}
    result = asyncio.run(model._structured("공개 지시", "공개 자료", {
        "type": "object", "properties": {"reason": {"type": "string"}}}))
    assert result["reason"] == "자료 없음" and model.last_failure is None
    assert model.last_usage == {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}


@pytest.mark.parametrize("options, kinds", [
    ({"fetch_urls": ["https://school.example/notice"], "search_allowed": True},
     {"search_web", "fetch_document", "stop"}),
    ({"fetch_urls": [], "search_allowed": True}, {"search_web", "stop"}),
    ({"fetch_urls": ["https://school.example/notice"], "search_allowed": False}, {"fetch_document", "stop"}),
    ({"fetch_urls": [], "search_allowed": False}, {"stop"}),
])
def test_generation_schema_exposes_only_remaining_actions(monkeypatch, options, kinds):
    school, period, context = case()
    context["action_options"] = {**options, "blocked_queries": ["검사초 졸업식"]}
    captured = {}

    async def structured(self, system, user, schema):
        captured.update(public=json.loads(user), schema=schema)
        return decision()

    monkeypatch.setattr(ModelProvider, "_structured", structured)
    asyncio.run(ModelProvider(settings()).decide(school, period, context))
    action = captured["schema"]["properties"]["action"]
    assert {branch["properties"]["kind"]["const"] for branch in action["oneOf"]} == kinds
    validator = Draft202012Validator(action)
    if "fetch_document" in kinds:
        assert validator.is_valid(decision("fetch_document", url=options["fetch_urls"][0])["action"])
    assert not validator.is_valid(decision("fetch_document", url="https://school.example/already-read")["action"])
    assert captured["public"]["action_options"] == context["action_options"]


def test_legacy_context_keeps_action_choices_without_dynamic_options(monkeypatch):
    school, period, context = case()

    async def structured(self, system, user, schema):
        action = Draft202012Validator(schema["properties"]["action"])
        assert action.is_valid(decision("fetch_document", url="https://school.example/notice")["action"])
        assert action.is_valid(decision("search_web", query="새로운 공개 검색어")["action"])
        return decision()

    monkeypatch.setattr(ModelProvider, "_structured", structured)
    asyncio.run(ModelProvider(settings()).decide(school, period, context))


def test_time_schema_omits_unneeded_count_payload_and_keeps_empty_array(monkeypatch):
    school, period, context = case()
    context["purpose"] = "time"

    async def structured(self, system, user, schema):
        counts = schema["properties"]["counts"]
        assert counts == {"type": "array", "maxItems": 0}
        validator = Draft202012Validator(counts)
        assert validator.is_valid([]) and not validator.is_valid([{"value": 10}])
        return decision()

    monkeypatch.setattr(ModelProvider, "_structured", structured)
    asyncio.run(ModelProvider(settings()).decide(school, period, context))


def test_feedback_survives_context_trimming_without_raw_diagnostic_details(monkeypatch):
    school, period, context = case()
    context.update(
        action_options={"fetch_urls": ["https://school.example/notice"], "search_allowed": True,
                        "blocked_queries": ["검사초 졸업식"]},
        decision_feedback={"kind": "invalid_contract",
                           "details": {"kind": "schema", "path": "action.query", "validator": "type",
                                       "raw_response": "private-value"},
                           "instruction": "query에는 검색어를 넣고 url은 null로 고치세요."},
        observations=[{"tool": "search_public_web", "results": [
            {"url": "https://school.example/old", "title": "지난 일정", "snippet": "긴 본문" * 2000}]}] * 8,
        available_links=[f"https://school.example/{index}" for index in range(300)])
    config = settings()

    async def structured(self, system, user, schema):
        public = json.loads(user)
        assert len(system) + len(user) + len(canonical(schema)) <= config.data["execution"]["llm_max_input_tokens"]
        assert public["action_options"] == context["action_options"]
        assert public["decision_feedback"]["details"] == {
            "kind": "schema", "path": "action.query", "validator": "type"}
        assert public["decision_feedback"]["instruction"] == context["decision_feedback"]["instruction"]
        assert "private-value" not in user
        assert public["available_links"] == []
        assert public["documents"][0]["segments"][0]["text"]
        return decision()

    monkeypatch.setattr(ModelProvider, "_structured", structured)
    asyncio.run(ModelProvider(config).decide(school, period, context))


def test_long_link_choices_leave_room_for_literal_evidence(monkeypatch):
    school, period, context = case()
    urls = ["https://school.example/notice?key=" + ("a" * 400) + str(index) for index in range(8)]
    context["action_options"] = {"fetch_urls": urls, "search_allowed": True, "blocked_queries": []}
    config = settings()

    async def structured(self, system, user, schema):
        public = json.loads(user)
        choices = public["action_options"]["fetch_urls"]
        assert 0 < len(choices) < len(urls)
        fetch = next(branch for branch in schema["properties"]["action"]["oneOf"]
                     if branch["properties"]["kind"]["const"] == "fetch_document")
        assert fetch["properties"]["url"]["enum"] == choices
        assert len(system) + len(user) + len(canonical(schema)) <= config.data["execution"]["llm_max_input_tokens"]
        assert "졸업식" in public["documents"][0]["segments"][0]["text"]
        return decision()

    monkeypatch.setattr(ModelProvider, "_structured", structured)
    asyncio.run(ModelProvider(config).decide(school, period, context))
    assert len(context["action_options"]["fetch_urls"]) == 8


def test_real_decide_identifies_wrong_action_field_in_matching_branch():
    school, period, context = case()
    answer = decision("search_web", query=None, url="https://school.example")
    model, calls = model_response({"message": {"content": json.dumps(answer)}})
    with pytest.raises(ProviderError) as caught:
        asyncio.run(model.decide(school, period, context))
    assert caught.value.diagnostic == {"kind": "schema", "path": "action.query", "validator": "type"}
    assert model.last_diagnostics is None and model.last_failure == caught.value.diagnostic
    assert len(calls) == 1


def test_evidence_mapping_failure_is_distinct_from_generation_schema(monkeypatch):
    school, period, context = case()

    async def structured(self, system, user, schema):
        value = decision()
        value["candidates"] = [{"evidence_block_id": "unknown-source"}]
        return value

    monkeypatch.setattr(ModelProvider, "_structured", structured)
    model = ModelProvider(settings())
    with pytest.raises(ProviderError) as caught:
        asyncio.run(model.decide(school, period, context))
    assert caught.value.diagnostic == {"kind": "evidence_mapping", "path": "candidates.0.evidence_block_id"}


def test_final_internal_contract_is_still_required(monkeypatch):
    school, period, context = case()

    async def structured(self, system, user, schema):
        value = decision()
        value["reason"] = 12
        return value

    monkeypatch.setattr(ModelProvider, "_structured", structured)
    model = ModelProvider(settings())
    with pytest.raises(ProviderError) as caught:
        asyncio.run(model.decide(school, period, context))
    assert caught.value.diagnostic == {"kind": "internal_contract", "path": "reason", "validator": "type"}


def grounded_response(days, source_line, start, end):
    """실제 모델 어댑터와 최종 값 검사를 연결할 공식 원문·응답을 준비한다.

    Args:
        days: 모델이 제출하는 실제 행사일 목록.
        source_line: 원문에서 그대로 인용할 행사 날짜 줄.
        start: 조사 시작일.
        end: 조사 종료일.
    Returns:
        격리 모델, 기관, 시즌, 원문, 공개 문맥.
    """
    school, period, context = case()
    school["official_channels"] = [{"url": "https://school.example",
                                    "verified_by_url": "https://directory.example",
                                    "verified_at": "2026-10-01T00:00:00Z"}]
    period.update(id=str(uuid4()), search_start=start, search_end=end)
    document = notice(school, f"검사초등학교\n2025학년도 졸업식\n{source_line}")
    context["documents"] = [document]
    context["action_options"] = {"fetch_urls": ["https://school.example/next"], "search_allowed": True,
                                 "blocked_queries": []}
    answer = decision("fetch_document", url="https://school.example/next")
    answer["candidates"] = [{
        "evidence_block_id": "b1", "event_title": "2025학년도 졸업식",
        "event_type": "graduation_ceremony", "academic_year": 2025, "ceremony_status": "scheduled",
        "dates": days, "start_time": None, "campus_label": None, "date_quote": source_line, "time_quote": None,
    }]
    model, _ = model_response({"message": {"content": json.dumps(answer)}, "done_reason": "stop"})
    return model, school, period, document, context


def check_claim(school, period, document, claim):
    """모델이 원문에 연결한 미승인 후보에 실제 최종 값 검사를 적용한다."""
    event = {**claim, "id": str(uuid4()), "institution": school}
    observation = {"id": str(uuid4()), "institution_id": school["id"], "season_id": period["id"],
                   "document_id": document["id"], "event_id": event["id"], "candidate": claim}
    return value_check("event_date", claim["event_date"], observation, document, event, period, institution=school)


def test_outside_period_claim_does_not_block_next_action_but_cannot_pass_value_check():
    model, school, period, document, context = grounded_response(
        ["2026-02-20"], "일시: 2026. 2. 20.", "2026-12-01", "2027-03-31")
    result = asyncio.run(model.decide(school, period, context))
    claim = result["candidates"][0]["candidate"]
    assert result["action"] == decision("fetch_document", url="https://school.example/next")["action"]
    assert claim["event_date"]["date"] == "2026-02-20"
    proof = check_claim(school, period, document, claim)
    assert proof["status"] == "rejected" and proof["checks"]["season"] == "fail"
    assert "season_unclear" in proof["reason_codes"]
    assert model.last_failure is None


def test_month_boundary_ceremony_keeps_all_actual_dates_and_passes_period_intersection():
    days = ["2026-01-31", "2026-02-01"]
    model, school, period, document, context = grounded_response(
        days, "일시: 2026. 1. 31., 2026. 2. 1.", "2026-02-01", "2026-02-28")
    result = asyncio.run(model.decide(school, period, context))
    claim = result["candidates"][0]["candidate"]
    assert claim["event_date"]["dates"] == days and claim["event_date"]["date"] == days[0]
    proof = check_claim(school, period, document, claim)
    assert proof["status"] == "supported" and proof["checks"]["season"] == "pass"


def test_changing_source_year_to_fit_period_still_cannot_pass_value_check():
    model, school, period, document, context = grounded_response(
        ["2027-02-20"], "일시: 2026. 2. 20.", "2026-12-01", "2027-03-31")
    result = asyncio.run(model.decide(school, period, context))
    claim = result["candidates"][0]["candidate"]
    assert claim["date_evidence"][0]["quote"] == "일시: 2026. 2. 20."
    proof = check_claim(school, period, document, claim)
    assert proof["status"] != "supported" and proof["checks"]["normalized_value"] != "pass"
    assert "value_not_supported" in proof["reason_codes"]


@pytest.mark.parametrize("invalid_day", ["2026-02-29", "2026-02-30"])
def test_impossible_calendar_day_still_fails_model_format_contract(invalid_day):
    model, school, period, _, context = grounded_response(
        [invalid_day], "일시: 2026. 2. 20.", "2026-02-01", "2026-02-28")
    with pytest.raises(ProviderError) as caught:
        asyncio.run(model.decide(school, period, context))
    assert caught.value.diagnostic == {
        "kind": "schema", "path": "candidates.0.dates.0", "validator": "format",
        "finish_reason": "stop", "output_truncated": False,
    }
