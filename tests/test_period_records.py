"""사용자 기간과 사실 학년도 분리의 행사·수기·인원 반례를 검사한다."""

from copy import deepcopy

from app.validation.identity import academic_year, ceremony_term, same_event, scope
from app.records.natural import parse_turn
from app.records.natural import NaturalRecorder
from app.records.research_records import current_counts
from app.records.storage import Service, Store
from test_grounded_claims import grounded_context, candidate, NORMAL, _save
from test_research_records import _document, _write, _count, _grade_context, _school_event
from test_storage import operation


def _university_event(**changes):
    """대학의 명시한 행사 구분을 가진 검사 입력을 만든다.

    Args:
        **changes: 바꿀 행사 메타데이터.
    Returns:
        날짜와 별개인 행사 식별 입력.
    """
    return {"event_type": "degree_award_ceremony", "title": "2026학년도 전기 학위수여식",
            "academic_year": 2026, "ceremony_term": "전기", "audience": "학부",
            "campus_label": "본교", **changes}


def test_identity_preserves_date_conflict_and_separates_real_cohorts():
    """같은 행사 날짜 차이는 합치고 다른 학년도·전후기·회차는 분리한다."""
    school = {"type": "university", "campus_label": "본교"}
    event = _university_event()
    assert same_event(event, {**event, "event_date": {"date": "2027-02-20"}}, school)
    assert same_event(event, {**event, "event_type": "graduation_ceremony", "audience": "대학원"}, school)
    for changes in (
        {"academic_year": 2025, "title": "2025학년도 전기 학위수여식"},
        {"ceremony_term": "후기", "title": "2026학년도 후기 학위수여식"},
        {"campus_label": "제2캠퍼스"},
    ):
        assert not same_event(event, {**event, **changes}, school)
    first = _university_event(title="2026학년도 전기 제10회 학위수여식")
    second = _university_event(title="2026학년도 전기 제11회 학위수여식")
    assert not same_event(first, second, school)


def test_unknown_academic_year_is_not_a_cross_year_wildcard():
    """학년도 미상의 수기를 모든 연도 행사와 자동으로 연결하지 않는다."""
    school = {"type": "university"}
    unknown = _university_event(title="전기 학위수여식", academic_year=None)
    known = _university_event()
    assert academic_year(unknown) is None
    assert not same_event(unknown, known, school)
    assert same_event({**unknown, "event_key": "already-linked"},
                      {**known, "event_key_hint": "already-linked"}, school)
    assert len(scope(known, school)) == 6


def test_university_term_is_never_derived_from_calendar_date():
    """2월·8월만 보고 대학 학년도와 전후기를 만들어내지 않는다."""
    event = {"title": "학위수여식", "event_date": {"date": "2027-02-20"}}
    assert academic_year(event) is None
    assert ceremony_term(event) is None
    assert ceremony_term({"title": "2026학년도 후기 학위수여식"}) == "후기"
    assert ceremony_term({"title": "2026학년도전기학위수여식"}) == "전기"
    assert ceremony_term({"title": "전기공학과 학위수여식"}) is None
    assert academic_year({"title": "2025학년도", "academic_year": 2026}) is None
    source = {"event_title": "2026학년도 학위수여식", "academic_year": 2026,
              "context_evidence": [{"quote": "2026학년도 후기 학위수여식"}]}
    assert ceremony_term(source) == "후기"
    conflicting = {**_university_event(), **source, "ceremony_term": "전기"}
    assert not same_event(conflicting, _university_event(), {"type": "university"})


def test_natural_preserves_explicit_university_cohort_without_inferring():
    """직접 기록에서도 명시한 학년도·전후기와 날짜를 독립적으로 보존한다."""
    season = {"search_start": "2027-01-01", "search_end": "2027-05-31", "academic_year": None}
    moment = "2026-10-21T00:00:00+00:00"
    plain = parse_turn("검증대 학위수여식은 2월 20일 오전 10시야. 방금 전화해서 확인했어.", season, moment)
    explicit = parse_turn("검증대 2025학년도 후기 학위수여식은 2월 20일 오전 10시야. 방금 전화해서 확인했어.", season, moment)
    assert plain["date"] == explicit["date"] == "2027-02-20"
    assert "academic_year" not in plain and "ceremony_term" not in plain
    assert explicit["academic_year"] == 2025 and explicit["ceremony_term"] == "후기"


def test_current_counts_use_event_academic_year_across_overlapping_periods(grounded_context):
    """서로 겹친 사용자 기간에서도 인원은 각 실제 행사 학년도에만 연결한다."""
    service, school, season = grounded_context
    text = NORMAL + "\n졸업생 총 132명"
    doc = _document(school, text)
    saved = _save(grounded_context, text, candidate(text), document=doc)
    _write(grounded_context, [doc], counts=[_count(doc)])
    event_id = saved["event_ids"][0]
    other = service.create_season({"search_start": "2027-02-01", "search_end": "2027-04-30"})
    with service.store.transaction(True) as conn:
        conn.execute("INSERT OR IGNORE INTO event_seasons(season_id,event_id) VALUES(?,?)", (other["id"], event_id))
        projected = service._project(conn, event_id, season_id=other["id"])[0]
        assert current_counts(service, conn, other, [projected]) == {event_id: 132}
        changed = deepcopy(projected)
        changed["academic_year"] = 2025
        assert current_counts(service, conn, other, [changed]) == {}


def test_two_school_cohorts_keep_their_own_counts_in_one_period(grounded_context):
    """학년도 경계를 포함한 한 기간에서도 졸업 학년 인원이 섞이지 않는다."""
    ctx = _grade_context(grounded_context)
    service, school, season = ctx
    later = _school_event(ctx, day="2027-03-10", academic_year=2027)
    docs, claims = [], []
    for year, amount in ((2026, 188), (2027, 203)):
        doc = _document(school, f"{year}학년도 검증초등학교 학생 현황\n6학년 학생 수 {amount}명", f"students-{year}")
        docs.append(doc)
        claims.append(_count(doc, value=amount, kind="graduating_grade_enrollment", grade=6, academic_year=year))
    _write(ctx, docs, counts=claims)
    with service.store.transaction() as conn:
        events = service._events(conn, season["id"])
        counts = current_counts(service, conn, season, events)
        assert len(events) == 2 and len(counts) == 2
        assert counts[later["id"]] == 203
        assert {event["academic_year"]: counts[event["id"]] for event in events} == {2026: 188, 2027: 203}


def test_delayed_school_manual_date_keeps_explicit_original_academic_year(grounded_context):
    """새 학년도 달력에 들어간 연기 날짜도 기존 행사 학년도를 임의 변경하지 않는다."""
    service, school, season = _grade_context(grounded_context)
    event = service.events(season["id"])["items"][0]
    updated = service.apply_overrides(event["id"], {
        "expected_revision": event["revision"], "operations": [
            operation("event_date", {"precision": "day", "date": "2027-03-10", "month": None})]})
    assert updated["id"] == event["id"] and updated["academic_year"] == 2026
    assert updated["publication"]["calendar_date"] == "2027-03-10"
    with service.store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM manual_overrides WHERE field='event_date'").fetchone()[0] == 2


def test_natural_confirmation_is_shared_without_copying_in_overlapping_periods(grounded_context):
    """겹친 시즌에서 시간 보완·재조사·재시작해도 행사와 수기 원본 UUID를 유지한다."""
    service, school, first = grounded_context
    text = NORMAL.replace(" 14:00", "")
    doc = _document(school, text)
    saved = _save(grounded_context, text, candidate(text, start=None), document=doc)
    event_id = saved["event_ids"][0]
    second = service.create_season({"search_start": "2027-02-01", "search_end": "2027-04-30"})
    assert [event["id"] for event in service.events(second["id"])["items"]] == [event_id]
    recorded = NaturalRecorder(service).record({
        "season_id": second["id"],
        "text": "검증대학교 학위수여식은 2월 20일 오전 10시야. 방금 전화해서 확인했어.",
        "conversation_id": None, "expected_revision": None})
    assert recorded["status"] == "saved" and recorded["event_id"] == event_id
    with service.store.transaction() as conn:
        manual_ids = [row[0] for row in conn.execute("SELECT id FROM manual_overrides")]
        assert manual_ids
        assert conn.execute("SELECT count(DISTINCT event_id) FROM manual_overrides").fetchone()[0] == 1
    refreshed = service.save_research(school["id"], second["id"], {
        "documents": [doc], "candidates": [{"document_id": doc["id"], "candidate": candidate(text, start=None)}]})
    assert refreshed["event_ids"] == [event_id]
    reopened = Service(Store(service.store.path), service.settings)
    for season in (first, second):
        items = reopened.events(season["id"])["items"]
        assert len(items) == 1 and items[0]["id"] == event_id
        assert items[0]["publication"]["calendar_time"] == "10:00"
    with reopened.store.transaction() as conn:
        assert [row[0] for row in conn.execute("SELECT id FROM manual_overrides")] == manual_ids
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
