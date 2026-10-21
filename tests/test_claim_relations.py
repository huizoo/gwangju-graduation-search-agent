"""줄바꿈·부분 인용에도 실제 행사와 속성 관계를 보존하는 저장 경로 검사."""

import pytest

from app.results import result_rows
from test_grounded_claims import NORMAL, candidate, grounded_context, _save
from test_review_regressions import source_context, save_notice


@pytest.mark.parametrize("notice", [
    "졸업식은 개최하지 않습니다.",
    "학위수여식은\n취소합니다.",
    "위 행사는 개최하지 않습니다.",
    "학위수여식은\n미실시합니다.",
    "식전 공연은 진행하며 본식은 취소합니다.",
])
@pytest.mark.parametrize("cropped", [False, True])
def test_same_ceremony_negative_blocks_scheduled_candidate(grounded_context, notice, cropped):
    """모델이 부정을 누락하거나 인용 밖으로 잘라도 과거 일시를 게시하지 않는다."""
    service, _, season = grounded_context
    text = NORMAL + "\n" + notice
    claim = candidate(NORMAL if cropped else text)
    _save(grounded_context, text, claim)
    row = result_rows(service.snapshot(season["id"]))[0]
    assert row["date"] is None and row["start_time"] is None


@pytest.mark.parametrize("activity", ["식전 공연", "기념촬영", "입장", "학위복 대여"])
def test_auxiliary_heading_owns_following_datetime(grounded_context, activity):
    """본식 제목 아래라도 더 가까운 부대 행사 제목의 일시는 본식 값이 아니다."""
    service, _, season = grounded_context
    text = NORMAL.replace("일시 :", activity + "\n일시 :")
    _save(grounded_context, text, candidate(text))
    row = result_rows(service.snapshot(season["id"]))[0]
    assert row["date"] is None and row["start_time"] is None


@pytest.mark.parametrize("activity", ["식전 공연", "기념촬영"])
def test_auxiliary_heading_rejects_only_time_when_date_is_independent(grounded_context, activity):
    """정상 날짜 뒤에 별도로 적힌 부대 행사 시각은 시간 필드만 보류한다."""
    service, _, season = grounded_context
    text = NORMAL.replace(" 14:00", "") + f"\n{activity}\n시작 : 13:30"
    _save(grounded_context, text, candidate(text, start="13:30", time_quote="시작 : 13:30"))
    row = result_rows(service.snapshot(season["id"]))[0]
    assert row["date"] == "2027-02-20" and row["start_time"] is None


@pytest.mark.parametrize("notice", [
    "식전 공연은 취소합니다.",
    "기념촬영\n실시하지 않습니다.",
    "입학식\n취소합니다.",
    "다른대학교 졸업식\n취소합니다.",
    "2025학년도 학위수여식은 취소합니다.",
    "학위수여식은 취소되지 않았습니다.",
])
def test_other_event_or_denied_cancellation_does_not_hide_main_schedule(grounded_context, notice):
    """같은 문서의 다른 행사 취소와 취소 부정은 정상 본식 일시를 막지 않는다."""
    service, _, season = grounded_context
    text = NORMAL + "\n" + notice
    # 정확한 대상 행사 구간을 제출해도 원래 분절 전체의 관계를 검사한다.
    _save(grounded_context, text, candidate(NORMAL))
    row = result_rows(service.snapshot(season["id"]))[0]
    assert row["date"] == "2027-02-20" and row["start_time"] == "14:00"


def test_explicit_main_heading_restores_time_after_auxiliary_section(grounded_context):
    """식전 공연 뒤에 구분해 적은 본식 시각은 정상적으로 제공한다."""
    service, _, season = grounded_context
    text = NORMAL.replace(" 14:00", "") + "\n식전 공연\n시작 : 13:30\n본식\n시작 : 14:00"
    _save(grounded_context, text, candidate(text, time_quote="시작 : 14:00"))
    row = result_rows(service.snapshot(season["id"]))[0]
    assert row["date"] == "2027-02-20" and row["start_time"] == "14:00"


def test_separate_multiline_cancellation_retracts_existing_schedule(grounded_context):
    """별도 URL의 같은 행사 취소도 날짜 없이 관계를 확인하면 기존 사용을 막는다."""
    service, _, season = grounded_context
    _save(grounded_context, NORMAL, candidate())
    text = "검증대학교\n2026학년도 학위수여식\n취소합니다."
    claim = candidate(text, start=None, ceremony_status="cancelled",
                      event_date={"precision": "unknown", "date": None, "month": None},
                      date_evidence=[])
    _save(grounded_context, text, claim)
    rows = result_rows(service.snapshot(season["id"]))
    assert len(rows) == 1
    assert rows[0]["status"] == "cancelled"
    assert rows[0]["date"] is None and rows[0]["start_time"] is None


def test_registered_single_line_parser_cannot_skip_following_cancellation(source_context):
    """정형 파서의 정상 한 줄도 후속 본식 취소를 누락한 채 승인하지 않는다."""
    service, _, season = source_context
    save_notice(source_context,
        "2026학년도 검증초등학교 졸업식은 2027년 1월 7일 오전 11시에 시작합니다.\n"
        "위 행사는 개최하지 않습니다.")
    row = result_rows(service.snapshot(season["id"]))[0]
    assert row["date"] is None and row["start_time"] is None


@pytest.mark.parametrize("hint", [None, "main"])
def test_undated_cancellation_cannot_choose_between_distinct_ceremonies(grounded_context, hint):
    """여러 회차 중 대상이 없는 취소 공지를 임의의 한 회차에 연결하지 않는다."""
    service, _, season = grounded_context
    for sequence, day in ((1, "20"), (2, "21")):
        title = f"2026학년도 제{sequence}회 학위수여식"
        line = f"일시 : 2027. 2. {day}. 14:00"
        text = f"검증대학교\n{title}\n{line}"
        _save(grounded_context, text, candidate(text, event_title=title,
              date=f"2027-02-{day}", date_quote=f"2027. 2. {day}.", time_quote=line))
    text = "검증대학교\n2026학년도 학위수여식\n취소합니다."
    _save(grounded_context, text, candidate(text, start=None, event_key_hint=hint,
          ceremony_status="cancelled", event_date={"precision": "unknown", "date": None, "month": None},
          date_evidence=[]))
    rows = result_rows(service.snapshot(season["id"]))
    assert len(rows) == 2
    assert {row["date"] for row in rows} == {"2027-02-20", "2027-02-21"}
    assert not any(row["status"] for row in rows)


def test_undated_other_academic_year_cancellation_keeps_current_event(grounded_context):
    """다른 학년도의 취소로 현재 행사의 날짜·시각을 숨기지 않는다."""
    service, _, season = grounded_context
    _save(grounded_context, NORMAL, candidate())
    text = "검증대학교\n2025학년도 학위수여식\n취소합니다."
    _save(grounded_context, text, candidate(text, start=None, academic_year=2025,
          event_title="2025학년도 학위수여식", ceremony_status="cancelled",
          event_date={"precision": "unknown", "date": None, "month": None}, date_evidence=[]))
    rows = result_rows(service.snapshot(season["id"]))
    assert len(rows) == 1 and rows[0]["date"] == "2027-02-20" and rows[0]["start_time"] == "14:00"
