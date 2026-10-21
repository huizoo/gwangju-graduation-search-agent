"""기간·원문 학년도·행사 관계를 실제 Provider와 MCP 경로에서 검증한다."""

import asyncio
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest

from app.claims import grounded_claim, validate_count
from app.providers import HttpGateway, ModelProvider, NeisProvider
from app.providers_student import StudentProvider
from app.research import ResearchRunner
from app.rules import extract_candidates
from test_agent_loop import setup_case, resolve, decision
from test_grounded_claims import candidate, _count, grounded_context, _save
from test_rules import notice


def _period():
    return {"id": str(uuid4()), "academic_year": None,
            "search_start": "2026-01-01", "search_end": "2026-03-15"}


def _row(school, year, day):
    return {"ATPT_OFCDC_SC_CODE": "F10", "SD_SCHUL_CODE": "123", "SCHUL_NM": school["name"],
            "AY": str(year), "AA_YMD": day, "EVENT_NM": "졸업식", "EVENT_CNTNT": "",
            "SCHUL_CRSE_SC_NM": "초등학교", "SIX_GRADE_EVENT_YN": "Y"}


def _response(*rows):
    return httpx.Response(200, json={"SchoolSchedule": [
        {"head": [{"list_total_count": len(rows)}, {"RESULT": {"CODE": "INFO-000"}}]}, {"row": list(rows)}]})


def test_calendar_queries_full_period_without_ay_and_preserves_source_year():
    settings, school, _ = setup_case()
    requests = []

    def handler(request):
        query = dict(request.url.params)
        assert "AY" not in query
        requests.append({key: query[key] for key in ("AA_FROM_YMD", "AA_TO_YMD")})
        return _response(_row(school, 2025, "20260227"), _row(school, 2025, "20260302"),
                         _row(school, 2026, "20260303"))

    gateway = HttpGateway(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolver=resolve)
    result = asyncio.run(NeisProvider(settings, gateway=gateway).calendar(
        school, _period(), [{"provider": "neis", "external_key": "F10:123"}]))
    assert requests == [{"AA_FROM_YMD": "20260101", "AA_TO_YMD": "20260315"}]
    assert result["coverage_complete"] and len(result["documents"]) == 3
    assert [extract_candidates(doc, school, _period())[0]["academic_year"] for doc in result["documents"]] == [2025, 2025, 2026]


def test_wrong_institution_row_does_not_claim_full_calendar_coverage():
    settings, school, _ = setup_case()
    wrong = {**_row(school, 2025, "20260302"), "SD_SCHUL_CODE": "456"}
    gateway = HttpGateway(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: _response(_row(school, 2025, "20260227"), wrong))), resolver=resolve)
    result = asyncio.run(NeisProvider(settings, gateway=gateway).calendar(
        school, _period(), [{"provider": "neis", "external_key": "F10:123"}]))
    assert not result["coverage_complete"] and result["reason_codes"] == ["wrong_source"]
    assert len(result["documents"]) == 1


def test_different_event_year_counts_complete_without_false_conflict(monkeypatch):
    """188명·192명은 각각 2025·2026학년도 행사이며 상충값이 아니다."""
    settings, school, _ = setup_case()
    years = []

    async def students(self, institution, context, identifiers):
        year = context["academic_year"]
        years.append(year)
        amount = 188 if year == 2025 else 192
        document = notice(institution, f"{year}학년도 {institution['name']} 학생 현황\n6학년 학생 수 {amount}명")
        claim = _count(document, value=amount, academic_year=year,
                       kind="graduating_grade_enrollment", grade=6)
        return {"status": "ok", "reason_codes": [], "documents": [document], "counts": [claim]}

    async def forbidden(*args, **kwargs):
        raise AssertionError("각 행사 학년도의 날짜와 인원이 충분합니다.")

    def handler(request):
        assert "AY" not in request.url.params
        return _response(_row(school, 2025, "20260227"), _row(school, 2026, "20260302"))

    monkeypatch.setattr(StudentProvider, "lookup", students)
    monkeypatch.setattr(ModelProvider, "decide", forbidden)
    gateway = HttpGateway(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolver=resolve)
    result = asyncio.run(ResearchRunner(settings, gateway=gateway).run(
        school, _period(), identifiers=[{"provider": "neis", "external_key": "F10:123"}]))
    assert years == [2025, 2026]
    assert result["status"] == "completed" and result["conflicts"] == []
    assert result["stop_reason"] == "structured_evidence_sufficient"
    assert {item["academic_year"] for item in result["counts"]} == {2025, 2026}


@pytest.mark.parametrize("year,day", [(2025, "2026-03-02"), (2024, "2026-02-27"), (2026, "2026-02-27")])
def test_school_source_year_is_not_replaced_by_actual_calendar_interval(year, day):
    _, school, _ = setup_case()
    period = _period()
    y, m, d = map(int, day.split("-"))
    text = f"{year}학년도 검증초등학교 졸업식은 {y}년 {m}월 {d}일입니다."
    source = notice(school, text)
    claim = extract_candidates(source, school, period)[0]
    assert claim["academic_year"] == year and claim["event_date"]["date"] == day
    assert grounded_claim("event_date", claim["event_date"], claim, source, school, period)
    invented = deepcopy(claim)
    invented["academic_year"] = year + 1
    assert not grounded_claim("event_date", invented["event_date"], invented, source, school, period)


@pytest.mark.parametrize("source_year", [2024, None])
def test_university_year_is_source_context_not_calendar_date(source_year):
    _, school, _ = setup_case()
    school.update(name="검증대학교", type="university")
    prefix = f"{source_year}학년도 " if source_year is not None else ""
    text = f"{prefix}검증대학교 졸업식 안내\n일시 : 2026. 2. 20.(금) 14:00"
    document = notice(school, text)
    claim = candidate(text, academic_year=source_year, date="2026-02-20",
                      date_quote="일시 : 2026. 2. 20.(금) 14:00",
                      time_quote="일시 : 2026. 2. 20.(금) 14:00")
    claim["event_title"] = prefix + "졸업식"
    claim["season_alignment"] = "explicit" if source_year else "source_context"
    assert grounded_claim("event_date", claim["event_date"], claim, document, school, _period())
    invented = deepcopy(claim)
    invented["academic_year"] = 2025
    assert not grounded_claim("event_date", invented["event_date"], invented, document, school, _period())


def test_count_requires_corresponding_event_year():
    _, school, _ = setup_case()
    document = notice(school, "2025학년도 검증초등학교 학생 현황\n6학년 학생 수 188명")
    claim = _count(document, value=188, academic_year=2025, kind="graduating_grade_enrollment", grade=6)
    assert validate_count(claim, document, school, _period(), event={"academic_year": 2025})["allowed"]
    assert not validate_count(claim, document, school, _period(), event={"academic_year": 2026})["allowed"]
    delayed_period = {**_period(), "search_start": "2026-03-01", "search_end": "2026-03-15"}
    assert validate_count(claim, document, school, delayed_period, event={"academic_year": 2025})["allowed"]


@pytest.mark.parametrize("source_kind", ["neis", "html", "model"])
def test_delayed_school_ceremony_queries_source_year_students_only(monkeypatch, source_kind):
    """3월로 연기된 2025학년도 본식의 날짜·인원은 날짜 학년도 2026으로 치환하지 않는다."""
    settings, school, _ = setup_case()
    school["homepage_url"] = "https://school.example/delayed"
    period = {**_period(), "search_start": "2026-03-01", "search_end": "2026-03-15"}
    years = []

    async def students(self, institution, context, identifiers):
        years.append(context["academic_year"])
        doc = notice(institution, "2025학년도 검증초등학교 학생 현황\n6학년 학생 수 185명")
        claim = _count(doc, value=185, academic_year=2025, kind="graduating_grade_enrollment", grade=6)
        return {"status": "ok", "reason_codes": [], "documents": [doc], "counts": [claim]}

    async def model(self, institution, context, state):
        if source_kind != "model":
            raise AssertionError("명시된 지연 행사와 같은 학년도 인원이 확인됐습니다.")
        doc = state["documents"][-1]
        claim = candidate(doc["segments"][0]["text"], academic_year=2025, event_title="2025학년도 졸업식",
                          event_type="graduation_ceremony", date="2026-03-02", start=None, date_quote="2026. 3. 2.")
        return decision(candidates=[{"document_id": doc["id"], "candidate": claim}])

    def handler(request):
        if request.url.host == "open.neis.go.kr":
            assert "AY" not in request.url.params
            if source_kind == "neis":
                return _response(_row(school, 2025, "20260302"))
            return httpx.Response(200, json={"RESULT": {"CODE": "INFO-200"}})
        text = ("<p>2025학년도 검증초등학교 졸업식 안내</p><p>일시 : 2026. 3. 2.</p>" if source_kind == "model" else
                "<p>2025학년도 검증초등학교 졸업식은 2026년 3월 2일 오전 10시에 시작합니다.</p>")
        return httpx.Response(200, text=f"<html><body>{text}</body></html>",
                              headers={"content-type": "text/html"})

    monkeypatch.setattr(StudentProvider, "lookup", students)
    monkeypatch.setattr(ModelProvider, "decide", model)
    gateway = HttpGateway(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), resolver=resolve)
    result = asyncio.run(ResearchRunner(settings, gateway=gateway).run(
        school, period, identifiers=[{"provider": "neis", "external_key": "F10:123"}]))
    assert result["status"] == "completed" and result["conflicts"] == [] and years == [2025], result
    assert {item["candidate"]["academic_year"] for item in result["candidates"]} == {2025}


@pytest.mark.parametrize("requested_year,allowed", [(2025, True), (2026, False)])
def test_student_tool_uses_verified_source_year_permissions(requested_year, allowed):
    """MCP 도구도 날짜에서 예상한 학년도 대신 서버 검증으로 허용한 학년도만 조회한다."""
    from app.tool_boundary import ResearchTools
    settings, school, _ = setup_case()
    period = {**_period(), "search_start": "2026-03-01", "search_end": "2026-03-15"}
    calls = []

    class Students:
        async def lookup(self, institution, context, identifiers):
            calls.append(context["academic_year"])
            return {"status": "no_data", "reason_codes": [], "documents": [], "counts": []}

    async def execute():
        tools = ResearchTools(school, period, [], None, None, None, Students())
        tools.allow_student_years([2025])
        async with tools.connect():
            return await tools.call("get_graduating_students", {
                "institution_id": school["id"], "season_id": period["id"], "academic_year": requested_year})

    if allowed:
        assert asyncio.run(execute())["status"] == "no_data" and calls == [2025]
    else:
        with pytest.raises(ValueError):
            asyncio.run(execute())
        assert calls == []


@pytest.mark.parametrize("status,word", [("cancelled", "취소합니다."), ("not_held", "실시하지 않습니다.")])
def test_unknown_year_cancellation_proves_status_but_never_old_date_or_time(status, word):
    _, school, _ = setup_case()
    school.update(name="검증대학교", type="university")
    line = f"검증대학교 학위수여식은 2026년 2월 20일 {word}"
    doc = notice(school, line)
    claim = candidate(line, academic_year=None, event_title="학위수여식", event_type="degree_award_ceremony",
                      date="2026-02-20", start=None, date_quote="2026년 2월 20일",
                      season_alignment="source_context", ceremony_status=status)
    assert grounded_claim("ceremony_status", status, claim, doc, school, _period())
    assert not grounded_claim("event_date", claim["event_date"], claim, doc, school, _period())
    other_role = line.replace("학위수여식은", "학위수여식 기념촬영은")
    wrong = {**claim, "context_evidence": [{**claim["context_evidence"][0], "quote": other_role}]}
    assert not grounded_claim("ceremony_status", status, wrong, notice(school, other_role), school, _period())


def test_public_grade_count_cannot_cross_join_two_year_sections():
    _, school, _ = setup_case()
    text = "2025학년도 학생 현황\n6학년 학생 수 185명\n2026학년도 학생 현황\n6학년 학생 수 188명"
    document = notice(school, text)
    good = _count(document, value=185, academic_year=2025, kind="graduating_grade_enrollment", grade=6)
    assert validate_count(good, document, school, _period(), event={"academic_year": 2025})["allowed"]
    wrong = {**good, "value": 188}
    proof = validate_count(wrong, document, school, _period(), event={"academic_year": 2025})
    assert not proof["allowed"] and "count_grade_year_relation_missing" in proof["reasons"]


def test_student_provider_never_uses_period_start_as_implicit_ay():
    settings, school, _ = setup_case()
    provider = StudentProvider(settings)
    result = asyncio.run(provider.lookup(school, _period(), []))
    assert result["counts"] == [] and result["reason_codes"] == ["academic_year_required"]


def test_model_cannot_reinterpret_verified_grade_count_rows_as_ceremony(monkeypatch):
    """실제 진단의 학년 인원 행을 가짜 졸업일로 읽던 경로를 제거한다."""
    settings, school, _ = setup_case()
    captured = []
    document = notice(school, "2025학년도 6학년 학생 수 185명")
    document["source_key"] = "schoolinfo:schoolinfo_api09:official-code:2025"

    async def structured(self, system, user, schema):
        import json
        captured.append(json.loads(user))
        assert schema["properties"]["candidates"]["maxItems"] == 0
        assert schema["properties"]["counts"]["maxItems"] == 0
        return decision("search_web", query="검증초등학교 2026년 1월 졸업식")

    monkeypatch.setattr(ModelProvider, "_structured", structured)
    context = {"purpose": "date_count", "documents": [document], "missing_fields": ["event_date"],
               "conflicts": [], "candidate_reviews": [{"count": {"value": 185, "academic_year": 2025},
                                                       "checks": {"allowed": True, "reasons": []}}],
               "observations": [], "prior_actions": [], "available_links": [], "reusable_routes": []}
    result = asyncio.run(ModelProvider(settings).decide(school, _period(), context))
    assert result["action"]["kind"] == "search_web"
    assert captured[0]["documents"] == []
    assert captured[0]["candidate_reviews"][0]["proposed"]["academic_year"] == 2025
    assert "academic_year" not in captured[0]["season"]


def test_university_term_must_match_source_even_when_date_is_valid():
    _, school, _ = setup_case()
    school.update(name="검증대학교", type="university")
    text = "2025학년도 후기 검증대학교 학위수여식\n일시 : 2026. 2. 20.(금) 14:00"
    document = notice(school, text)
    claim = candidate(text, academic_year=2025, event_title="학위수여식", date="2026-02-20",
                      date_quote="일시 : 2026. 2. 20.(금) 14:00",
                      time_quote="일시 : 2026. 2. 20.(금) 14:00")
    assert grounded_claim("event_date", claim["event_date"], claim, document, school, _period())
    claim["ceremony_term"] = "전기"
    assert not grounded_claim("event_date", claim["event_date"], claim, document, school, _period())


def test_model_calendar_excerpt_selection_uses_actual_period_not_school_ay(monkeypatch):
    """대학의 서로 다른 원문 학년도 두 본식 모두 달력 헤더의 실제 날짜로 선택한다."""
    settings, school, _ = setup_case()
    school.update(name="검증대학교", type="university")
    document = notice(school, "2026.02\n02.20(금)\n2024학년도 후기 학위수여식\n2026.03\n03.02(월)\n2025학년도 전기 학위수여식")
    captured = []

    async def structured(self, system, user, schema):
        import json
        captured.append(json.loads(user))
        return decision()

    monkeypatch.setattr(ModelProvider, "_structured", structured)
    context = {"purpose": "date_count", "documents": [document], "missing_fields": ["event_date"],
               "conflicts": [], "candidate_reviews": [], "observations": [], "prior_actions": [],
               "available_links": [], "reusable_routes": []}
    asyncio.run(ModelProvider(settings).decide(school, _period(), context))
    excerpts = captured[0]["documents"][0]["segments"]
    assert [part["section_header"] for part in excerpts] == ["2026.02", "2026.03"]
    assert "2024학년도 후기" in excerpts[0]["text"] and "2025학년도 전기" in excerpts[1]["text"]
    assert all(part["text"] in document["segments"][0]["text"] for part in excerpts)


def test_verified_multiple_days_preserve_source_and_display_only_period_intersection(tmp_path):
    """2월 20·21일 본식은 원문 전체를 보존하고 21일 시즌에는 21일만 표시한다."""
    from app.domain import dumps, utcnow
    from app.results import result_rows
    from app.storage import Service, Store
    from test_jobs_api import institution_payload
    service = Service(Store(tmp_path / "multi.sqlite3"), {"auto_rule_ids": ["grounded_notice"]})
    school = service.create_institution({**institution_payload("검증대학교"), "type": "university",
                                         "homepage_url": "https://school.example"})
    with service.store.transaction(True) as conn:
        conn.execute("UPDATE institutions SET official_channels_json=? WHERE id=?", (dumps([{
            "url": "https://school.example", "verified_by_url": "https://directory.example/university",
            "verified_at": utcnow()}]), school["id"]))
    school = service.get_institution(school["id"])
    season = service.create_season({"search_start": "2026-02-21", "search_end": "2026-02-21"})
    text = "2025학년도 검증대학교 학위수여식\n일시 : 2026. 2. 20.(금), 2026. 2. 21.(토) 14:00"
    line = text.splitlines()[-1]
    document = notice(school, text)
    channel = school["official_channels"][0]
    document["authority_evidence"] = {"channel_url": channel["url"], "verified_by_url": channel["verified_by_url"],
                                      "verified_at": channel["verified_at"]}
    claim = candidate(text, academic_year=2025, event_title="2025학년도 학위수여식",
                      date="2026-02-20", date_quote=line, time_quote=line)
    claim["event_date"]["dates"] = ["2026-02-20", "2026-02-21"]
    assert grounded_claim("event_date", claim["event_date"], claim, document, school, season)
    saved = service.save_research(school["id"], season["id"], {
        "documents": [document], "candidates": [{"document_id": document["id"], "candidate": claim}]})
    event = service.event_detail(saved["event_ids"][0])["event"]
    assert event["academic_year"] == 2025
    assert event["fields"]["event_date"]["effective_value"]["dates"] == ["2026-02-20", "2026-02-21"]
    row = result_rows(service.snapshot(season["id"]))[0]
    assert row["date"] == "2026-02-21" and row["dates"] == ["2026-02-21"]
    assert row["start_time"] == "14:00"
    # 직접 확인한 첫날 시각은 자동 공통시각의 증명을 상속하지 않는다.
    from test_publication import operation
    service.apply_overrides(event["id"], {"expected_revision": event["revision"], "operations": [
        operation("start_time", "14:00", bound_date="2026-02-20")]})
    assert result_rows(service.snapshot(season["id"]))[0]["start_time"] is None
    first_day = service.create_season({"search_start": "2026-02-20", "search_end": "2026-02-20"})
    assert result_rows(service.snapshot(first_day["id"]))[0]["start_time"] == "14:00"


@pytest.mark.parametrize("timing,time_quote", [
    ("일시 : 2026. 2. 20.(금) 14:00\n일시 : 2026. 2. 21.(토)", "14:00"),
    ("일시 : 2026. 2. 20.(금) 14:00, 2026. 2. 21.(토)", "14:00"),
    ("일시 : 2026. 2. 20.(금) 14:00\n일시 : 2026. 2. 21.(토) 15:00", "14:00"),
    ("일시 : 2026. 2. 20.(금), 2026. 2. 21.(토) 각각 14:00, 15:00", "14:00"),
    ("일시 : 2026. 2. 20.(금), 2026. 2. 21.(토) 14:00 (20일만 해당)", "14:00"),
    ("일시 : 2026. 2. 20.(금) ~ 2026. 2. 21.(토) 14:00", "14:00"),
])
def test_date_specific_time_is_not_shared_with_other_days(timing, time_quote):
    """특정 날의 시각만 제출해도 정상인 두 행사일은 유지하고 공통 시각은 보류한다."""
    _, school, _ = setup_case()
    school.update(name="검증대학교", type="university")
    text = "2025학년도 검증대학교 학위수여식\n" + timing
    document = notice(school, text)
    claim = candidate(text, academic_year=2025, event_title="2025학년도 학위수여식",
                      date="2026-02-20", date_quote=timing, time_quote=time_quote)
    claim["event_date"]["dates"] = ["2026-02-20", "2026-02-21"]
    season = {**_period(), "search_start": "2026-02-21", "search_end": "2026-02-21"}
    assert grounded_claim("event_date", claim["event_date"], claim, document, school, season)
    assert not grounded_claim("start_time", claim["start_time"], claim, document, school, season)


def test_date_range_explicitly_saying_daily_has_common_time():
    _, school, _ = setup_case()
    school.update(name="검증대학교", type="university")
    timing = "일시 : 2026. 2. 20.(금) ~ 2026. 2. 21.(토) 매일 14:00"
    text = "2025학년도 검증대학교 학위수여식\n" + timing
    document = notice(school, text)
    claim = candidate(text, academic_year=2025, event_title="2025학년도 학위수여식",
                      date="2026-02-20", date_quote=timing, time_quote="14:00")
    claim["event_date"]["dates"] = ["2026-02-20", "2026-02-21"]
    assert grounded_claim("start_time", "14:00", claim, document, school, _period())


def test_partial_time_keeps_verified_dates_through_storage_and_overlapping_period(grounded_context):
    """보류한 공통 시각 후보는 이력에 남고 두 시즌 모두 정상 날짜만 제공한다."""
    from app.results import result_rows
    service, _, season = grounded_context
    timing = "일시 : 2027. 2. 20.(토) 14:00\n일시 : 2027. 2. 21.(일)"
    text = "2026학년도 검증대학교 학위수여식\n" + timing
    claim = candidate(text, date_quote=timing, time_quote="14:00")
    claim["event_date"]["dates"] = ["2027-02-20", "2027-02-21"]
    saved = _save(grounded_context, text, claim)
    row = result_rows(service.snapshot(season["id"]))[0]
    assert row["dates"] == ["2027-02-20", "2027-02-21"] and row["start_time"] is None
    second_day = service.create_season({"search_start": "2027-02-21", "search_end": "2027-02-21"})
    other = result_rows(service.snapshot(second_day["id"]))[0]
    assert other["event_ids"] == saved["event_ids"]
    assert other["date"] == "2027-02-21" and other["start_time"] is None
    with service.store.transaction() as conn:
        import json
        original = json.loads(conn.execute("SELECT candidate_json FROM observations WHERE event_id=?",
                                          (saved["event_ids"][0],)).fetchone()[0])
    assert original["start_time"] == "14:00"
