"""모델 후보의 원문 연결부터 저장·게시까지 정상과 반례를 함께 검사한다."""

from uuid import uuid4

import pytest

from app.domain import dumps, validate
from app.results import result_rows
from app.rules import extract_candidates
from app.storage import Service, Store
from test_jobs_api import institution_payload
from test_rules import notice


NORMAL = "검증대학교\n2026학년도 학위수여식\n일시 : 2027. 2. 20.(토) 14:00\n장소 : 본관 대강당\n대상 : 학부 및 대학원 졸업자"


@pytest.fixture
def grounded_context(tmp_path):
    """모델 없이 후보를 주입할 격리 대학·시즌·공식 채널을 만든다.

    Args:
        tmp_path: 검사별 임시 경로.
    Returns:
        서비스, 공식 기관, 조사 시즌.
    """
    service = Service(Store(tmp_path / "grounded.sqlite3"), {"auto_rule_ids": ["explicit_notice", "grounded_notice"]})
    school = service.create_institution({**institution_payload("검증대학교"), "type": "university",
                                         "homepage_url": "https://school.example"})
    season = service.create_season({"academic_year": 2026, "search_start": "2026-12-01", "search_end": "2027-03-31",
                                   "label": "검사 시즌", "export_key": "grounded", "timezone": "Asia/Seoul",
                                   "region_profile": "gwangju_5_gu"})
    channels = [{"url": "https://school.example", "verified_by_url": "https://directory.example/university",
                 "verified_at": "2026-10-21T01:00:00Z"}]
    with service.store.transaction(True) as conn:
        conn.execute("UPDATE institutions SET official_channels_json=? WHERE id=?", (dumps(channels), school["id"]))
    return service, service.get_institution(school["id"]), season


def _evidence(quote, segment="s1"):
    """지정 문서 단위의 연속 인용을 구성한다.

    Args:
        quote: 실제 인용할 문구.
        segment: 원문 분절 ID.
    Returns:
        Evidence 계약 객체.
    """
    return {"kind": "literal", "segment_id": segment, "quote": quote, "bbox": None}


def candidate(text=NORMAL, *, date="2027-02-20", start="14:00", date_quote="2027. 2. 20.(토)",
              time_quote="일시 : 2027. 2. 20.(토) 14:00", **changes):
    """기존 추출기의 재호출 결과에 의존하지 않는 모델 후보를 만든다.

    Args:
        text: 기관·시즌·행사가 들어 있는 문맥 인용.
        date: 후보 날짜.
        start: 후보 본식 시각.
        date_quote: 날짜 필드 인용.
        time_quote: 시각 필드 인용.
        **changes: 대상·캠퍼스 등 바꿀 후보 필드.
    Returns:
        스키마를 만족하는 Candidate.
    """
    value = {"event_title": "2026학년도 학위수여식", "event_type": "degree_award_ceremony",
             "event_key_hint": None, "audience": "졸업 대상 학생", "campus_label": None,
             "academic_year": 2026, "season_alignment": "explicit", "ceremony_status": "scheduled",
             "event_date": {"precision": "day", "date": date, "month": None},
             "start_time": start, "time_role": "ceremony_start" if start else None, "venue": None,
             "context_evidence": [_evidence(text)], "date_evidence": [_evidence(date_quote)],
             "time_evidence": [_evidence(time_quote)] if start else [], "venue_evidence": [],
             "is_change_notice": False, "supersedes_text": None, **changes}
    validate("Candidate", value)
    return value


def _save(ctx, text, claim=None, *, document=None, counts=None):
    """실제 서비스의 불변 원문·관측·행사 연결 경로로 후보를 저장한다.

    Args:
        ctx: 격리 서비스·학교·시즌.
        text: 원문.
        claim: 명시적으로 제출할 후보.
        document: 분절 등을 변경한 원문.
        counts: 같은 문서에 연결할 인원 후보.
    Returns:
        save_research의 저장 결과.
    """
    service, school, season = ctx
    doc = document or notice(school, text)
    channel = school["official_channels"][0]
    doc["authority_evidence"] = {"channel_url": channel["url"], "verified_by_url": channel["verified_by_url"],
                                 "verified_at": channel["verified_at"]}
    if document is None:
        doc["display_url"] = doc["source_key"] = f"https://school.example/notice/{uuid4()}"
        doc["acquisition"]["final_url"] = doc["display_url"]
    return service.save_research(school["id"], season["id"], {"documents": [doc],
        "candidates": [{"document_id": doc["id"], "candidate": claim or candidate(text)}],
        "counts": counts or []})


def _claim_allowed(ctx, text, claim, field, *, document=None):
    """원문 관계 검사의 공개 진입점을 실행한다.

    Args:
        ctx: 격리 서비스·학교·시즌.
        text: 읽은 원문.
        claim: 확인할 모델 후보.
        field: 확인할 필드.
        document: 별도로 구성한 문서.
    Returns:
        필드와 원문 관계가 지원되는지 여부.
    """
    from app.claims import grounded_claim
    _, school, season = ctx
    return grounded_claim(field, claim[field], claim, document or notice(school, text), school, season)


def test_model_candidate_without_rule_reextraction_is_linked_and_published(grounded_context):
    """명확한 표 형식 후보는 키 힌트와 기존 파서 재추출 없이 게시된다."""
    service, school, season = grounded_context
    doc = notice(school, NORMAL)
    assert extract_candidates(doc, school, season) == []
    claim = candidate()
    assert claim["event_key_hint"] is None
    saved = _save(grounded_context, NORMAL, claim)
    assert len(saved["event_ids"]) == 1
    rows = result_rows(service.snapshot(season["id"]))
    assert len(rows) == 1
    assert rows[0]["date"] == "2027-02-20" and rows[0]["start_time"] == "14:00"


def test_calendar_header_and_immediately_following_ceremony_publish(grounded_context):
    """연월 제목·월일 행·바로 뒤 본식의 관계로 날짜를 확인한다."""
    text = "2026.08\n08.21(금)\n2025학년도 후기 학위수여식\n2027.02\n02.18(목)\n동계 교직원 세미나\n02.19(금)\n2026학년도 전기 학위수여식"
    block = "02.19(금)\n2026학년도 전기 학위수여식"
    claim = candidate(block, date="2027-02-19", start=None, date_quote="02.19(금)")
    service, school, season = grounded_context
    doc = notice(school, text)
    assert extract_candidates(doc, school, season) == []
    assert _claim_allowed(grounded_context, text, claim, "event_date", document=doc)
    _save(grounded_context, text, claim, document=doc)
    assert result_rows(service.snapshot(season["id"]))[0]["date"] == "2027-02-19"


@pytest.mark.parametrize("mutation", ["wrong_header_year", "wrong_header_month", "other_event", "other_academic_year", "missing_header", "split_segment"])
def test_calendar_cannot_borrow_header_or_neighbor_event(grounded_context, mutation):
    """같은 달력에 있다는 이유만으로 다른 연도·월·행·분절의 값을 채택하지 않는다."""
    text = "2027.02\n02.18(목)\n동계 교직원 세미나\n02.19(금)\n2026학년도 전기 학위수여식"
    block = "02.19(금)\n2026학년도 전기 학위수여식"
    quote = "02.19(금)"
    if mutation == "wrong_header_year":
        text = text.replace("2027.02", "2026.02")
    elif mutation == "wrong_header_month":
        text = text.replace("2027.02", "2027.03")
    elif mutation == "other_event":
        block = text
        quote = "02.18(목)"
    elif mutation == "other_academic_year":
        text = text.replace("2026학년도", "2025학년도")
        block = block.replace("2026학년도", "2025학년도")
    elif mutation == "missing_header":
        text = text.replace("2027.02\n", "")
    _, school, _ = grounded_context
    doc = notice(school, text)
    if mutation == "split_segment":
        doc["segments"][0]["text"] = text.replace("2027.02\n", "")
        doc["segments"].append({"id": "s2", "locator_kind": "html_text", "location": "header", "text": "2027.02", "unit_id": "header"})
    claim = candidate(block, date="2027-02-19", start=None, date_quote=quote)
    assert not _claim_allowed(grounded_context, text, claim, "event_date", document=doc)


@pytest.mark.parametrize("field", ["event_date", "start_time", "ceremony_status"])
def test_grounded_normal_fields_are_supported(grounded_context, field):
    """날짜·본식 시각·개최 상태가 한 공식 행사 단위에 연결된다."""
    assert _claim_allowed(grounded_context, NORMAL, candidate(), field)


@pytest.mark.parametrize("mutation", ["gown", "certificate", "department", "other_school", "other_year", "fabricated_date"])
def test_wrong_event_subject_year_or_literal_cannot_publish(grounded_context, mutation):
    """공식 도메인이라는 이유만으로 잘못 연결된 후보를 승인하지 않는다."""
    text = NORMAL
    if mutation == "gown":
        text = text.replace("학위수여식\n일시", "학위복 대여 안내\n대여 기간")
    elif mutation == "certificate":
        text = text.replace("학위수여식\n일시", "학위증 수령 안내\n수령 기간")
    elif mutation == "department":
        text = text.replace("학위수여식", "컴퓨터공학과 학위수여식")
    elif mutation == "other_school":
        text = text.replace("검증대학교", "다른대학교")
    elif mutation == "other_year":
        text = text.replace("2026학년도", "2025학년도")
    claim = candidate(text, start=None)
    if mutation == "fabricated_date":
        claim["event_date"]["date"] = "2027-02-21"
        claim["date_evidence"][0]["quote"] = "2027. 2. 21.(일)"
    assert not _claim_allowed(grounded_context, text, claim, "event_date")
    service, _, season = grounded_context
    _save(grounded_context, text, claim)
    assert not any(row["date"] for row in result_rows(service.snapshot(season["id"])))


@pytest.mark.parametrize("activity", ["식전 공연", "기념촬영", "입장", "학위복 대여"])
def test_auxiliary_time_does_not_hide_normal_ceremony_date(grounded_context, activity):
    """잘못 제안한 부대 시각만 보류하고 날짜의 게시 권한은 유지한다."""
    text = NORMAL.replace(" 14:00", "") + f"\n{activity} : 09:30"
    claim = candidate(text, start="09:30", time_quote=f"{activity} : 09:30")
    assert _claim_allowed(grounded_context, text, claim, "event_date")
    assert not _claim_allowed(grounded_context, text, claim, "start_time")
    service, _, season = grounded_context
    _save(grounded_context, text, claim)
    row = result_rows(service.snapshot(season["id"]))[0]
    assert row["date"] == "2027-02-20" and row["start_time"] is None


def test_other_segment_time_cannot_borrow_main_ceremony_context(grounded_context):
    """다른 행사 단위의 시각에 본식 문맥을 덧붙여도 근거가 되지 않는다."""
    _, school, _ = grounded_context
    text = NORMAL.replace(" 14:00", "")
    doc = notice(school, text)
    doc["segments"].append({"id": "s2", "locator_kind": "html_text", "location": "aside",
                            "text": "기념촬영 : 14:00", "unit_id": "photo"})
    claim = candidate(text, time_quote="기념촬영 : 14:00")
    claim["time_evidence"][0]["segment_id"] = "s2"
    assert not _claim_allowed(grounded_context, text, claim, "start_time", document=doc)


def test_same_representative_ceremony_does_not_split_undergraduate_and_graduate(grounded_context):
    """같은 대표 행사 대상의 학부·대학원 표현만으로 별도 행을 만들지 않는다."""
    service, _, season = grounded_context
    for audience in ("학부 졸업생", "대학원 졸업생"):
        _save(grounded_context, NORMAL, candidate(audience=audience))
    assert len(service.events(season["id"])["items"]) == 1
    rows = result_rows(service.snapshot(season["id"]))
    assert len(rows) == 1 and rows[0]["date"] == "2027-02-20"


@pytest.mark.parametrize("distinction", ["sequence", "campus"])
def test_explicit_distinct_representative_ceremonies_remain_separate(grounded_context, distinction):
    """회차나 캠퍼스가 명시적으로 다른 대표 본식은 각각 게시한다."""
    service, _, season = grounded_context
    for index, date in enumerate(("2027-02-20", "2027-02-21"), start=1):
        title = f"2026학년도 제{index}회 학위수여식" if distinction == "sequence" else "2026학년도 학위수여식"
        campus = f"제{index}캠퍼스" if distinction == "campus" else None
        quote = f"2027. 2. {19+index}."
        text = f"검증대학교 {campus or ''}\n{title}\n일시 : {quote} 14:00\n장소 : 대강당\n대상 : 학부 및 대학원 졸업자"
        claim = candidate(text, date=date, date_quote=quote, time_quote=f"일시 : {quote} 14:00",
                          event_title=title, campus_label=campus)
        _save(grounded_context, text, claim)
    rows = result_rows(service.snapshot(season["id"]))
    assert len(rows) == 2 and {row["date"] for row in rows} == {"2027-02-20", "2027-02-21"}


def test_multiple_actual_ceremony_days_exclude_rental_period(grounded_context):
    """여러 날의 본식은 유지하고 별도 학위복 대여 기간은 합치지 않는다."""
    service, _, season = grounded_context
    dates = ["2027-02-20", "2027-02-21"]
    date_line = "일시 : 2027. 2. 20.(토), 2027. 2. 21.(일) 14:00"
    text = NORMAL.replace("일시 : 2027. 2. 20.(토) 14:00", date_line)
    text += "\n학위복 대여\n대여 기간 : 2027. 2. 18.(목) ~ 2027. 2. 22.(월)"
    claim = candidate(text, date_quote=date_line, time_quote=date_line)
    claim["event_date"]["dates"] = dates
    validate("Candidate", claim)
    assert _claim_allowed(grounded_context, text, claim, "event_date")
    _save(grounded_context, text, claim)
    row = result_rows(service.snapshot(season["id"]))[0]
    assert row["date"] == dates[0] and row["dates"] == dates
    assert "2027-02-18" not in row["dates"] and "2027-02-22" not in row["dates"]


def test_rental_dates_cannot_extend_ceremony_date_list(grounded_context):
    """문서에 존재하는 다른 날짜도 본식 날짜 목록으로 승격하지 않는다."""
    text = NORMAL + "\n학위복 대여 기간 : 2027. 2. 18.(목) ~ 2027. 2. 22.(월)"
    claim = candidate(text, date="2027-02-18", date_quote=text)
    claim["event_date"]["dates"] = ["2027-02-18", "2027-02-20", "2027-02-22"]
    assert not _claim_allowed(grounded_context, text, claim, "event_date")


def test_auxiliary_details_do_not_block_explicit_main_time(grounded_context):
    """다른 일정이 함께 있어도 명시된 본식 시각은 게시할 수 있다."""
    text = NORMAL + "\n식전 공연 : 13:30\n기념촬영 : 15:00\n학위복 대여 : 09:00 ~ 17:00"
    service, _, season = grounded_context
    _save(grounded_context, text, candidate(text))
    row = result_rows(service.snapshot(season["id"]))[0]
    assert row["date"] == "2027-02-20" and row["start_time"] == "14:00"


def _count(document, *, value=132, kind="ceremony_graduates", grade=None, **changes):
    """인원 종류와 기준 학년도를 가진 공개 문서 후보를 만든다.

    Args:
        document: 인용할 공식 문서.
        value: 문서가 명시하는 인원.
        kind: 졸업 학년 재학생 또는 대표 본식 졸업자.
        grade: 재학생 수일 때의 학년.
        **changes: 반례에서 변경할 후보 필드.
    Returns:
        CountClaim 계약 객체.
    """
    claim = {"document_id": document["id"], "value": value, "academic_year": 2026,
             "kind": kind, "grade": grade, "unit": "persons", "source": "public_document",
             "evidence": [{"segment_id": "s1", "quote": document["segments"][0]["text"]}],
             "event_date": "2027-02-20" if grade is None else None, **changes}
    validate("CountClaim", claim)
    return claim


def test_current_graduating_grade_enrollment_is_a_valid_count(grounded_context):
    """초등 6학년 재학생 수를 같은 학년도의 인원으로 사용할 수 있다."""
    from app.claims import validate_count
    _, institution, season = grounded_context
    school = {**institution, "type": "elementary", "name": "검증초등학교"}
    doc = notice(school, "2026학년도 검증초등학교 학생 현황\n6학년 학생 수 188명")
    claim = _count(doc, value=188, kind="graduating_grade_enrollment", grade=6)
    assert validate_count(claim, doc, school, season)["allowed"]


@pytest.mark.parametrize("mutation", ["wrong_grade", "wrong_year", "other_school", "wrong_number", "not_students"])
def test_enrollment_count_requires_exact_school_year_grade_and_value(grounded_context, mutation):
    """같은 사이트에 적힌 다른 학교·학년·학년도 수를 사용하지 않는다."""
    from app.claims import validate_count
    _, institution, season = grounded_context
    school = {**institution, "type": "elementary", "name": "검증초등학교"}
    text = "2026학년도 검증초등학교 학생 현황\n6학년 학생 수 188명"
    if mutation == "wrong_grade":
        text = text.replace("6학년", "5학년")
    elif mutation == "wrong_year":
        text = text.replace("2026학년도", "2025학년도")
    elif mutation == "other_school":
        text = text.replace("검증초등학교", "다른초등학교")
    elif mutation == "not_students":
        text = text.replace("학생 수", "학부모 참석 수")
    doc = notice(school, text)
    claim = _count(doc, value=189 if mutation == "wrong_number" else 188,
                   kind="graduating_grade_enrollment", grade=6)
    assert not validate_count(claim, doc, school, season)["allowed"]


def test_university_count_binds_to_same_representative_ceremony(grounded_context):
    """대학 인원은 해당 본식의 공개 졸업자 수와 날짜를 함께 확인한다."""
    from app.claims import validate_count
    _, school, season = grounded_context
    doc = notice(school, NORMAL + "\n졸업생 총 132명")
    claim = _count(doc)
    assert validate_count(claim, doc, school, season, event=candidate())["allowed"]


@pytest.mark.parametrize("mutation", ["other_department", "department_count_only", "other_school", "wrong_year", "other_event_date", "wrong_number", "number_suffix", "no_event", "all_enrollment"])
def test_university_count_rejects_other_cohort_or_event(grounded_context, mutation):
    """대학 전체 재학생이나 다른 행사 졸업자를 대표 본식 인원으로 바꾸지 않는다."""
    from app.claims import validate_count
    _, school, season = grounded_context
    text = NORMAL + "\n졸업생 총 132명"
    if mutation == "other_department":
        text = text.replace("학위수여식", "컴퓨터공학과 학위수여식")
    elif mutation == "department_count_only":
        text = text.replace("졸업생 총 132명", "컴퓨터공학과 졸업생 총 132명")
    elif mutation == "number_suffix":
        text = text.replace("총 132명", "총 1132명")
    elif mutation == "other_school":
        text = text.replace("검증대학교", "다른대학교")
    elif mutation == "wrong_year":
        text = text.replace("2026학년도", "2025학년도")
    elif mutation == "all_enrollment":
        text = text.replace("졸업생 총", "전체 재학생 총")
    doc = notice(school, text)
    claim = _count(doc, value=133 if mutation == "wrong_number" else 132)
    event = None if mutation == "no_event" else candidate()
    if mutation == "other_event_date":
        event["event_date"]["date"] = "2027-02-21"
    assert not validate_count(claim, doc, school, season, event=event)["allowed"]


@pytest.mark.parametrize("reported_count, expected", [(132, 132), (133, None)])
def test_university_count_reaches_same_output_or_preserves_date_when_rejected(grounded_context, reported_count, expected):
    """인원 후보도 실제 저장·게시를 통과해야 하며 실패해도 날짜는 남는다."""
    service, school, season = grounded_context
    text = NORMAL + "\n졸업생 총 132명"
    doc = notice(school, text)
    claim = _count(doc, value=reported_count)
    _save(grounded_context, text, candidate(text), document=doc, counts=[claim])
    row = result_rows(service.snapshot(season["id"]))[0]
    assert row["date"] == "2027-02-20"
    assert row["graduation_count"] == expected


@pytest.mark.parametrize("mutation", ["campus", "sequence", "past_cohort_total"])
def test_count_same_day_does_not_override_explicit_ceremony_scope(grounded_context, mutation):
    """같은 날에도 다른 캠퍼스·회차·과거 졸업자의 인원을 옮기지 않는다."""
    from app.claims import validate_count
    _, school, season = grounded_context
    text = NORMAL + "\n졸업생 총 132명"
    event = candidate()
    if mutation == "campus":
        text = text.replace("검증대학교", "검증대학교 제2캠퍼스")
        event["campus_label"] = "제1캠퍼스"
    elif mutation == "sequence":
        text = text.replace("학위수여식", "제2회 학위수여식")
        event["event_title"] = "2026학년도 제1회 학위수여식"
    else:
        text = text.replace("졸업생 총 132명", "2025학년도 졸업생 총 132명")
    doc = notice(school, text)
    assert not validate_count(_count(doc), doc, school, season, event=event)["allowed"]
