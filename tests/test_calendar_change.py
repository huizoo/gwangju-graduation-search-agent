"""실제 변경공지 파싱과 미해결 공식성 차단을 합성 문법 반례와 구분해 검사한다."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.domain import validate
from app.providers import empty_document, utcnow
from app.publication import RuleRegistry, calculate_publication
from app.rules import extract_candidates, value_check, verify

FIXTURES = Path(__file__).parent / "fixtures/calendar-change"
CAPTURE_HASHES = {
    "sogang": "a65765793f1534dbffa707e8347040c97227ad6efadde297ce3aa90e836ab97b",
    "cku": "285f68da91048387e7a63a38e5e2c7c79eac9b9d01683f68843b35d1045fd355",
    "cbbh": "8c41eb27e7063fdc4332c3028155489b54c7634f422f76199219373935a90a70",
}
NAMES = {"sogang": "서강대학교", "cku": "가톨릭관동대학교", "cbbh": "충북비즈니스고등학교"}
SYNTHETIC_TEXT = (
    "2025학년도 학부 학사력 변경 안내\n"
    "- 제63회 학위수여식\n"
    "(변경전) 2026.2.23(월) → (변경후) 2026.2.20(금)"
)


def capture(tag):
    """재취득한 DocumentRead의 복사본을 수정 없이 읽는다.

    Args:
        tag: 실제 캡처 기관의 고정 이름.
    Returns:
        원본과 바이트 해시가 같은, 여전히 공식성 미확인인 문서.
    """
    raw = (FIXTURES / f"{tag}-document.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == CAPTURE_HASHES[tag]
    document = json.loads(raw)
    validate("DocumentRead", document)
    assert document["authority"] == "unverified"
    assert document["authority_evidence"] is None
    return document


def season(year=2025):
    """해당 학년도 겨울 조사 기간을 격리된 식별자로 구성한다."""
    return {"id": str(uuid4()), "academic_year": year,
            "search_start": f"{year}-12-01", "search_end": f"{year+1}-03-31"}


def institution(document, tag="sogang"):
    """캡처의 기관 연결만 유지하며 공식 채널을 새로 승인하지 않는다."""
    return {"id": document["institution_id"], "name": NAMES[tag], "official_channels": []}


def synthetic_document(text):
    """실제 캡처와 구분되는 예제 도메인의 합성 문법 입력을 만든다."""
    school = {"id": str(uuid4()), "name": "합성검증대학교", "official_channels": []}
    document = empty_document(school, "https://synthetic.example/change")
    document.update(status="ok", source_kind="html", content_type="text/html",
                    content_hash=hashlib.sha256(text.encode()).hexdigest(),
                    segments=[{"id": "s1", "locator_kind": "html_text", "location": "synthetic grammar case",
                               "unit_id": "synthetic", "text": text}])
    return document, school


def actual_context():
    """원문을 승인 상태로 변경하지 않고 실제 후보의 검증 연결을 만든다."""
    document = capture("sogang")
    school, target = institution(document), season()
    candidate = extract_candidates(document, school, target)[0]
    event = {"id": str(uuid4()), "institution": school, "season_id": target["id"],
             "event_key": candidate["event_key_hint"], "event_type": candidate["event_type"],
             "audience": candidate["audience"], "campus_label": candidate["campus_label"]}
    observation = {"id": str(uuid4()), "institution_id": school["id"], "season_id": target["id"],
                   "event_id": event["id"], "document_id": document["id"], "candidate": candidate,
                   "created_at": utcnow(), "disposition": "reference"}
    return document, school, target, candidate, event, observation


def test_actual_sogang_capture_extracts_only_explicit_new_day_and_change_text():
    document = capture("sogang")
    candidates = extract_candidates(document, institution(document), season())
    assert len(candidates) == 1
    candidate = candidates[0]
    validate("Candidate", candidate)
    assert candidate["event_title"] == "제63회 학위수여식"
    assert candidate["academic_year"] == 2025
    assert candidate["event_date"] == {"precision": "day", "date": "2026-02-20", "month": None}
    assert candidate["is_change_notice"] is True
    assert candidate["supersedes_text"] == "(변경전) 2026.2.23(월) → (변경후) 2026.2.20(금)"
    assert candidate["audience"] == "학부 졸업 대상 학생"
    assert candidate["start_time"] is None and candidate["time_role"] is None
    assert candidate["time_evidence"] == []
    for evidence in candidate["context_evidence"] + candidate["date_evidence"]:
        assert evidence["quote"] in document["segments"][0]["text"]
    assert document["authority"] == "unverified"


def test_actual_sogang_previous_academic_year_is_not_current_season():
    document = capture("sogang")
    assert extract_candidates(document, institution(document), season(2026)) == []


@pytest.mark.parametrize("tag, year", [("cku", 2026), ("cbbh", 2023)])
def test_actual_other_table_grammar_or_missing_academic_year_remains_unsupported(tag, year):
    document = capture(tag)
    assert extract_candidates(document, institution(document, tag), season(year)) == []


@pytest.mark.parametrize("field", ["event_date", "ceremony_status"])
def test_actual_unverified_capture_fails_value_check_even_when_parser_finds_candidate(field):
    document, school, target, candidate, event, observation = actual_context()
    proof = value_check(field, candidate[field], observation, document, event, target, institution=school)
    validate("ValueCheck", proof)
    assert proof["rule_id"] == "academic_calendar_row" and proof["rule_version"] == "1.0.0"
    assert proof["status"] == "rejected" and proof["checks"]["content"] == "fail"
    assert not verify(field, candidate[field], observation, document, event, target, institution=school)
    assert document["authority"] == "unverified" and document["authority_evidence"] is None


def test_actual_unverified_capture_is_held_by_publication_boundary(monkeypatch):
    document, school, target, candidate, event, observation = actual_context()
    registry = RuleRegistry()
    registry.register("academic_calendar_row", "1.0.0", verify)
    monkeypatch.setattr("app.publication.RULES", registry)
    event["fields"] = {}
    for field in ("ceremony_status", "event_date"):
        proof = value_check(field, candidate[field], observation, document, event, target, institution=school)
        # 호출자가 상태 라벨을 낙관적으로 붙여도 실제 근거 검사를 우회하지 못해야 한다.
        event["fields"][field] = {"automatic": {"status": "official_confirmed", "value": candidate[field],
            "evidence_ids": [observation["id"]], "value_check": proof}}
    publication = calculate_publication(event, target, {
        "now": utcnow(), "registered_rules": registry.versions(),
        "documents": {document["id"]: document}, "observations": {observation["id"]: observation},
    })
    validate("Publication", publication)
    assert publication["state"] == "hold"
    assert publication["calendar_date"] is None and publication["calendar_time"] is None
    assert not publication["calendar_eligible"] and not publication["sales_ready"]
    assert not publication["current_date_allowed"]
    assert document["authority"] == "unverified"


@pytest.mark.parametrize("text", [
    SYNTHETIC_TEXT.replace("2025학년도", "2024학년도"),
    "2024학년도 학부 학사력 변경\n" + SYNTHETIC_TEXT,
    SYNTHETIC_TEXT.replace("학부 ", ""),
    "2025학년도 대학원 학사력 변경\n" + SYNTHETIC_TEXT,
    SYNTHETIC_TEXT.replace("제63회 학위수여식", "학위수여식"),
    SYNTHETIC_TEXT.replace("2026.2.23", "2.23"),
    SYNTHETIC_TEXT.replace("2026.2.20", "2.20"),
    SYNTHETIC_TEXT.replace("2026.2.23", "2026.2.30"),
    SYNTHETIC_TEXT.replace("2026.2.23(월)", "2026.2.23(월)~2026.2.24(화)"),
    SYNTHETIC_TEXT.replace("2026.2.20(금)", "2026.2.20(금)~2026.2.21(토)"),
    SYNTHETIC_TEXT.replace("학위수여식", "입학식"),
    SYNTHETIC_TEXT.replace("학위수여식", "학위복 대여"),
    SYNTHETIC_TEXT.replace("학위수여식", "학위수여식 예행연습"),
    SYNTHETIC_TEXT.replace("2026.2.23", "2024.2.23"),
    SYNTHETIC_TEXT.replace("2026.2.20(금)", "2026.2.23(월)"),
    SYNTHETIC_TEXT + " 14:09:44",
])
def test_synthetic_ambiguous_change_grammar_does_not_create_candidate(text):
    document, school = synthetic_document(text)
    assert extract_candidates(document, school, season()) == []


def test_synthetic_round_and_audience_changes_keep_separate_event_identity():
    candidates = []
    for text in (SYNTHETIC_TEXT, SYNTHETIC_TEXT.replace("제63회", "제64회"),
                 SYNTHETIC_TEXT.replace("학부", "대학원")):
        document, school = synthetic_document(text)
        candidates.append(extract_candidates(document, school, season())[0])
    assert len({candidate["event_key_hint"] for candidate in candidates}) == 3
    assert candidates[0]["audience"] != candidates[2]["audience"]
    assert candidates[0]["event_title"] != candidates[1]["event_title"]


def test_synthetic_posting_time_is_not_ceremony_start():
    document, school = synthetic_document("등록일 2025.04.16 14:09:44\n" + SYNTHETIC_TEXT)
    candidate = extract_candidates(document, school, season())[0]
    assert candidate["start_time"] is None and candidate["time_evidence"] == []
    assert "14:09:44" not in candidate["context_evidence"][0]["quote"]
