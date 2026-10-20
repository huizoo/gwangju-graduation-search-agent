"""독립 검토의 반례를 원문 추출부터 SQLite 게시까지 재현한다."""

from uuid import uuid4

import pytest

from app.domain import ApiError, dumps
from app.providers import neis_document
from app.rules import extract_candidates
from app.storage import Service, Store
from test_rules import context, notice
from test_jobs_api import settings_for, institution_payload, job_payload
from app.api import create_app


@pytest.fixture
def source_context(tmp_path):
    """공식 채널과 기관 코드를 가진 격리 저장소를 준비한다."""
    service = Service(Store(tmp_path / "review.sqlite3"),
                      {"auto_rule_ids": ["neis_ceremony_row", "explicit_notice"]})
    inst, season, _, _, _ = context("notice")
    school = service.create_institution({
        **institution_payload(inst["name"]), "type": "elementary",
        "homepage_url": "https://school.example",
    })
    season = service.create_season({
        **{k: season[k] for k in ("academic_year", "search_start", "search_end")},
        "label": "검사", "export_key": "review", "timezone": "Asia/Seoul",
        "region_profile": "gwangju_5_gu",
    })
    with service.store.transaction(True) as conn:
        conn.execute("UPDATE institutions SET official_channels_json=? WHERE id=?",
                     (dumps(inst["official_channels"]), school["id"]))
        conn.execute("INSERT INTO institution_identifiers VALUES(?,?,?)", ("neis", "F10:123", school["id"]))
    return service, service.get_institution(school["id"]), season


def save_notice(ctx, text, path="notice"):
    """원문 후보를 실제 저장·검증 경로로 전달한다."""
    service, school, season = ctx
    doc = notice(school, text)
    doc["display_url"] = doc["source_key"] = f"https://school.example/{path}"
    doc["acquisition"]["final_url"] = doc["display_url"]
    return service.save_research(school["id"], season["id"], {
        "documents": [doc], "candidates": [
            {"document_id": doc["id"], "candidate": c}
            for c in extract_candidates(doc, school, season)
        ],
    })


@pytest.mark.parametrize("role", ["식전 공연", "기념촬영"])
def test_auxiliary_time_keeps_date_without_ceremony_time(source_context, role):
    """부대 일정의 시각은 제외하고 독립적인 정상 행사일은 유지한다."""
    service, _, season = source_context
    save_notice(source_context, f"2026학년도 검증초등학교 졸업식은 2027년 1월 7일 {role} 오전 10시에 시작합니다.")
    event = service.events(season["id"])["items"][0]
    assert event["publication"]["calendar_date"] == "2027-01-07"
    assert not event["publication"]["current_time_allowed"]


@pytest.mark.parametrize("subject", ["검증초등학교 졸업식 준비 설명회", "검증초등학교 병설유치원 졸업식"])
def test_other_event_or_institution_is_not_school_ceremony(source_context, subject):
    """학교명 부분일치와 행사명의 수식 관계를 혼동하지 않는다."""
    service, _, season = source_context
    save_notice(source_context, f"2026학년도 {subject}는 2027년 1월 7일 오전 10시에 시작합니다.")
    assert not any(e["publication"]["current_date_allowed"] for e in service.events(season["id"])["items"])


@pytest.mark.parametrize("negative", ["취소합니다", "미실시합니다", "실시하지 않습니다"])
def test_separate_official_negative_notice_blocks_previous_schedule(source_context, negative):
    """URL이 달라도 같은 기관·학년도·행사의 부정 공지는 이전 사용을 막는다."""
    service, _, season = source_context
    saved = save_notice(source_context, "2026학년도 검증초등학교 졸업식은 2027년 1월 7일 오전 11시에 시작합니다.")
    assert service.event_detail(saved["event_ids"][0])["event"]["publication"]["sales_ready"]
    save_notice(source_context, f"2026학년도 검증초등학교 졸업식은 {negative}.", "cancel")
    events = service.events(season["id"])["items"]
    assert len(events) == 1
    assert not events[0]["publication"]["current_date_allowed"]


def test_neis_and_html_share_event_and_detect_date_conflict(source_context):
    """기관·과정이 같은 행사는 자료원과 날짜가 달라도 하나의 충돌로 남긴다."""
    service, school, season = source_context
    row = {"ATPT_OFCDC_SC_CODE": "F10", "SD_SCHUL_CODE": "123", "SCHUL_NM": school["name"],
           "AY": "2026", "AA_YMD": "20270107", "EVENT_NM": "졸업식", "EVENT_CNTNT": "",
           "SCHUL_CRSE_SC_NM": "초등학교", "SIX_GRADE_EVENT_YN": "Y"}
    doc = neis_document(school, row, {})
    service.save_research(school["id"], season["id"], {
        "documents": [doc], "candidates": [{"document_id": doc["id"],
            "candidate": extract_candidates(doc, school, season)[0]}],
    })
    save_notice(source_context, "2026학년도 검증초등학교 졸업식은 2027년 1월 8일 오전 11시에 시작합니다.")
    events = service.events(season["id"])["items"]
    assert len(events) == 1
    assert events[0]["fields"]["event_date"]["automatic"]["status"] == "conflict"
    assert not events[0]["publication"]["current_date_allowed"]


def test_resume_cannot_bypass_active_job_guard(tmp_path):
    """새 작업 생성과 재개가 동일한 원자적 활성 작업 경계를 사용한다."""
    runtime = create_app(settings_for(tmp_path)).state.runtime
    school = runtime.service.create_institution(institution_payload())
    season = runtime.service.list_seasons()["items"][0]
    payload = job_payload(season["id"], [school["id"]])
    a = runtime.jobs.create(payload, str(uuid4()))
    runtime.jobs.pause(a["id"])
    b = runtime.jobs.create(payload, str(uuid4()))
    with pytest.raises(ApiError, match="조사"):
        runtime.jobs.resume(a["id"], {"expected_revision": runtime.jobs.get(a["id"])["revision"], "increase_limits": None})
    assert runtime.jobs.get(a["id"])["status"] == "paused"
    assert runtime.jobs.get(b["id"])["status"] == "queued"


def test_explicit_main_start_after_performance_is_preserved(source_context):
    """부대 일정과 본식이 함께 적혀 있어도 명시된 본식 시각은 제공한다."""
    service, _, season = source_context
    save_notice(source_context,
        "2026학년도 검증초등학교 졸업식은 2027년 1월 7일 식전 공연 오전 9시에 시작, 졸업식은 오전 10시에 시작합니다.")
    assert service.events(season["id"])["items"][0]["publication"]["calendar_time"] == "10:00"


@pytest.mark.parametrize("text", [
    "2026학년도 검증초등학교 병설유치원 졸업식은 취소합니다.",
    "2025학년도 검증초등학교 졸업식은 취소합니다.",
    "2026학년도 검증초등학교 졸업식은 취소하지 않습니다.",
    "2026학년도 검증초등학교 졸업식은 식전 공연을 취소합니다.",
])
def test_other_scope_or_negated_cancellation_keeps_valid_schedule(source_context, text):
    """다른 기관·학년도·부대행사의 취소를 정상 행사에 전파하지 않는다."""
    service, _, season = source_context
    save_notice(source_context, "2026학년도 검증초등학교 졸업식은 2027년 1월 7일 오전 11시에 시작합니다.")
    save_notice(source_context, text, "other")
    assert service.events(season["id"])["items"][0]["publication"]["sales_ready"]


def test_different_special_school_courses_remain_separate(source_context):
    """여러 과정이 실제로 구분되는 행사를 무조건 합치지 않는다."""
    service, school, season = source_context
    with service.store.transaction(True) as conn:
        conn.execute("UPDATE institutions SET type='special_school' WHERE id=?", (school["id"],))
    school = service.get_institution(school["id"])
    for course, grade in [("초등학교", "SIX"), ("유치원", "ONE")]:
        row = {"ATPT_OFCDC_SC_CODE": "F10", "SD_SCHUL_CODE": "123", "SCHUL_NM": school["name"],
               "AY": "2026", "AA_YMD": "20270107", "EVENT_NM": "졸업식", "EVENT_CNTNT": "",
               "SCHUL_CRSE_SC_NM": course, f"{grade}_GRADE_EVENT_YN": "Y"}
        doc = neis_document(school, row, {})
        service.save_research(school["id"], season["id"], {"documents": [doc], "candidates": [
            {"document_id": doc["id"], "candidate": extract_candidates(doc, school, season)[0]}]})
    assert len(service.events(season["id"], view="calendar")["items"]) == 2


def test_explicit_different_ceremony_sequences_are_not_merged(source_context):
    """회차가 명시적으로 다른 두 본식을 날짜 충돌 하나로 합치지 않는다."""
    service, _, season = source_context
    for sequence, day in [(1,7),(2,8)]:
        save_notice(source_context, f"2026학년도 검증초등학교 제{sequence}회 졸업식은 2027년 1월 {day}일 오전 10시에 시작합니다.", f"sequence-{sequence}")
    events = service.events(season["id"], view="calendar")["items"]
    assert len(events) == 2
    assert {e["publication"]["calendar_date"] for e in events} == {"2027-01-07","2027-01-08"}


def test_bound_postponement_holds_old_date_without_calling_it_cancelled(source_context):
    """기존 취소 경고 경로의 연기 보류 기능을 행사 관계 검사로 유지한다."""
    service, _, season = source_context
    save_notice(source_context, "2026학년도 검증초등학교 졸업식은 2027년 1월 7일 오전 10시에 시작합니다.")
    save_notice(source_context, "2026학년도 검증초등학교 졸업식은 연기합니다.", "postponed")
    from app.results import result_rows
    row = result_rows(service.snapshot(season["id"]))[0]
    assert row["date"] is row["start_time"] is row["status"] is None
