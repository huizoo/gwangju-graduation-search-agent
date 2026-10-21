"""서버 규칙의 정상 결과와 날짜·시간 역할·기관·인용 반례를 검증한다."""

import hashlib
from copy import deepcopy
from uuid import uuid4

import pytest

from app.domain import validate
from app.providers import empty_document, neis_document
from app.rules import extract_candidates, value_check, verify


def context(source="neis"):
    institution = {"id": str(uuid4()), "name": "검증초등학교", "official_channels": [
        {"url": "https://open.neis.go.kr/hub", "verified_by_url": "https://open.neis.go.kr/hub/schoolInfo",
         "verified_at": "2026-10-01T00:00:00Z"},
        {"url": "https://school.example", "verified_by_url": "https://open.neis.go.kr/hub/schoolInfo",
         "verified_at": "2026-10-01T00:00:00Z"}]}
    season = {"id": str(uuid4()), "academic_year": 2026, "search_start": "2026-12-01", "search_end": "2027-03-31"}
    if source == "neis":
        row = {"ATPT_OFCDC_SC_CODE": "F10", "SD_SCHUL_CODE": "123", "SCHUL_NM": "검증초등학교",
               "AY": "2026", "AA_YMD": "20270107", "EVENT_NM": "졸업식", "EVENT_CNTNT": "",
               "SCHUL_CRSE_SC_NM": "초등학교", "SIX_GRADE_EVENT_YN": "Y"}
        document = neis_document(institution, row, {"ATPT_OFCDC_SC_CODE": "F10", "SD_SCHUL_CODE": "123", "AY": "2026"})
    else:
        text = "2026학년도 검증초등학교 졸업식은 2027년 1월 7일 오전 11시에 시작합니다."
        document = notice(institution, text)
    candidate = extract_candidates(document, institution, season)[0]
    event = {"id": str(uuid4()), "institution": institution, "event_type": candidate["event_type"],
             "audience": candidate["audience"], "academic_year": candidate["academic_year"], "campus_label": None, "_identifiers": {"neis": "F10:123"}}
    observation = {"id": str(uuid4()), "institution_id": institution["id"], "season_id": season["id"],
                   "event_id": event["id"], "document_id": document["id"], "candidate": candidate}
    return institution, season, document, event, observation


def notice(institution, text):
    document = empty_document(institution, "https://school.example/notice")
    document.update(status="ok", authority="official", source_kind="html", content_type="text/html",
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        authority_evidence={"channel_url": "https://school.example", "verified_by_url": "https://open.neis.go.kr/hub/schoolInfo",
                            "verified_at": "2026-10-01T00:00:00Z"},
        segments=[{"id": "s1", "locator_kind": "html_text", "location": "body", "text": text, "unit_id": "main"}])
    document["reading"].update(method="html_parser", scope="full_text", content_match="expected", institution_match="matched")
    document["acquisition"].update(transport_status="ok", http_status=200, final_url=document["display_url"], tls_verified=True)
    return document


@pytest.mark.parametrize("field", ["event_date", "ceremony_status"])
def test_neis_row_supports_date_and_status_without_model(field):
    institution, season, document, event, observation = context()
    candidate = observation["candidate"]
    proof = value_check(field, candidate[field], observation, document, event, season, institution=institution)
    validate("ValueCheck", proof)
    assert proof["status"] == "supported" and proof["rule_id"] == "neis_ceremony_row"
    assert candidate["start_time"] is None


@pytest.mark.parametrize("field", ["event_date", "start_time", "ceremony_status"])
def test_explicit_notice_supports_literal_day_and_actual_start(field):
    institution, season, document, event, observation = context("notice")
    candidate = observation["candidate"]
    proof = value_check(field, candidate[field], observation, document, event, season, institution=institution)
    validate("ValueCheck", proof)
    assert proof["status"] == "supported" and proof["rule_id"] == "explicit_notice"
    assert candidate["start_time"] == "11:00"


@pytest.mark.parametrize("mutation", [
    "wrong_institution", "wrong_school_code", "wrong_year", "wrong_quote", "changed_hash",
    "changed_date", "wrong_event", "partial", "unverified",
])
def test_neis_negative_cases_never_pass(mutation):
    institution, season, document, event, observation = context()
    value = deepcopy(observation["candidate"]["event_date"])
    if mutation == "wrong_institution":
        observation["institution_id"] = str(uuid4())
    elif mutation == "wrong_school_code":
        event["_identifiers"]["neis"] = "F10:999"
    elif mutation == "wrong_year":
        observation["candidate"]["academic_year"] = 2025
    elif mutation == "wrong_quote":
        observation["candidate"]["date_evidence"][0]["quote"] = "20270108"
    elif mutation == "changed_hash":
        document["content_hash"] = "a" * 64
    elif mutation == "changed_date":
        value["date"] = observation["candidate"]["event_date"]["date"] = "2027-01-08"
    elif mutation == "wrong_event":
        observation["event_id"] = str(uuid4())
    elif mutation == "partial":
        document["partial"] = True
    else:
        document["authority"] = "unverified"
    assert not verify("event_date", value, observation, document, event, season, institution=institution)


@pytest.mark.parametrize("tail, expected", [
    ("오전 10시 30분에 입장합니다.", None),
    ("입장은 오전 10시 30분에 시작합니다.", None),
    ("오전 11시에 시작합니다. 입장은 오전 10시 30분부터입니다.", "11:00"),
    ("오후 1시 30분에 시작합니다.", "13:30"),
    ("오전 13시에 시작합니다.", None),
    ("10:99에 시작합니다.", None),
])
def test_notice_does_not_confuse_admission_or_invalid_time(tail, expected):
    institution, season, _, _, _ = context("notice")
    document = notice(institution, "2026학년도 검증초등학교 졸업식은 2027년 1월 7일 " + tail)
    candidates = extract_candidates(document, institution, season)
    assert candidates and candidates[0]["start_time"] == expected


@pytest.mark.parametrize("text", [
    "검증초등학교 졸업식은 2027년 1월 7일 오전 11시에 시작합니다.",
    "2025학년도 검증초등학교 졸업식은 2026년 1월 7일 오전 11시에 시작합니다.",
    "2026학년도 다른초등학교 졸업식은 2027년 1월 7일 오전 11시에 시작합니다.",
    "2026학년도 검증초등학교 졸업식은 2027년 1월에 합니다.",
    "2026학년도 검증초등학교 졸업식은 2027년 2월 30일 오전 11시에 시작합니다.",
    "2026학년도 검증초등학교 졸업식은 2027년 1월 7일에서 2027년 1월 8일로 변경합니다.",
    "2026학년도 검증초등학교 졸업식 예행연습은 2027년 1월 7일 오전 11시에 시작합니다.",
])
def test_ambiguous_wrong_or_month_only_text_is_not_filled(text):
    institution, season, _, _, _ = context("notice")
    assert extract_candidates(notice(institution, text), institution, season) == []


def test_neis_does_not_infer_start_time_from_event_content():
    institution, season, document, event, observation = context()
    observation["candidate"].update(start_time="11:00", time_role="ceremony_start",
                                   time_evidence=deepcopy(observation["candidate"]["context_evidence"]))
    assert not verify("start_time", "11:00", observation, document, event, season, institution=institution)


def test_synthetic_or_different_unit_time_quote_is_not_supported():
    institution, season, document, event, observation = context("notice")
    observation["candidate"]["time_evidence"][0]["quote"] = "오전 10시에 시작합니다."
    assert not verify("start_time", "11:00", observation, document, event, season, institution=institution)


def section_text():
    return ("2025학년도 학위수여식 개최 안내\n[행사개요]\n1. 일시:\n2026년 2월\n20일(금) 14:00\n"
            "- 15:40 (예정)\n2. 장소: 광주과학기술원 오룡관 대강당\n3. 대상: 학위수여대상자\n"
            "[학위복 대여 및 반납]\n1. 일시: 2026. 2. 20. 10:00 - 17:00\n"
            "[학위수여 안내]\n2026. 2. 20. 13:30까지 입장")


def test_explicit_section_binds_ceremony_window_not_gown_or_admission():
    institution, season, _, _, _ = context("notice")
    institution["name"] = "광주과학기술원"
    season.update(academic_year=2025, search_start="2025-12-01", search_end="2026-03-31")
    document = notice(institution, section_text())
    candidates = extract_candidates(document, institution, season)
    assert len(candidates) == 1
    assert candidates[0]["start_time"] == "14:00"
    assert candidates[0]["event_date"]["date"] == "2026-02-20"
    assert candidates[0]["venue"] == "광주과학기술원 오룡관 대강당"
    assert "10:00" not in candidates[0]["time_evidence"][0]["quote"]


@pytest.mark.parametrize("mutation", ["new_season", "other_institution", "rental_heading", "admission", "two_dates", "bad_range"])
def test_explicit_section_rejects_false_event_unit(mutation):
    institution, season, _, _, _ = context("notice")
    institution["name"] = "광주과학기술원"
    season.update(academic_year=2025, search_start="2025-12-01", search_end="2026-03-31")
    text = section_text()
    if mutation == "new_season":
        season.update(academic_year=2026, search_start="2026-12-01", search_end="2027-03-31")
    elif mutation == "other_institution":
        institution["name"] = "다른대학교"
    elif mutation == "rental_heading":
        text = text.replace("[행사개요]", "[학위복 대여]")
    elif mutation == "admission":
        text = text.replace("1. 일시:", "1. 입장 일시:")
    elif mutation == "two_dates":
        text = text.replace("2. 장소:", "다른 행사 2026년 2월 21일\n2. 장소:")
    else:
        text = text.replace("- 15:40", "- 13:40")
    assert not extract_candidates(notice(institution, text), institution, season)


def test_neis_scope_separates_special_school_courses_without_date_in_identity():
    import json
    institution, season, document, _, _ = context()
    row = json.loads(document["segments"][0]["text"])
    alternate = dict(row, SCHUL_CRSE_SC_NM="유치원", ONE_GRADE_EVENT_YN="Y", SIX_GRADE_EVENT_YN="N",
                     AA_YMD="20261230")
    changed_date = dict(row, AA_YMD="20270108")
    first = extract_candidates(document, institution, season)[0]
    second_doc = neis_document(institution, alternate, {})
    second = extract_candidates(second_doc, institution, season)[0]
    changed_doc = neis_document(institution, changed_date, {})
    changed = extract_candidates(changed_doc, institution, season)[0]
    assert first["audience"] == "초등학교 6학년" and second["audience"] == "유치원 1학년"
    assert first["event_key_hint"] != second["event_key_hint"]
    assert document["source_key"] != second_doc["source_key"]
    assert first["event_key_hint"] == changed["event_key_hint"]
    assert document["source_key"] == changed_doc["source_key"]


def test_old_unscoped_neis_candidate_does_not_pass_new_scope_check():
    institution, season, document, event, observation = context()
    observation["candidate"]["audience"] = event["audience"] = "졸업 대상 학생"
    assert not verify("event_date", observation["candidate"]["event_date"], observation, document, event, season)
