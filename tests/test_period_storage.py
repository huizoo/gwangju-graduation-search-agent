"""기간 저장·기존 파일 이관·고정 조사 조건의 실제 SQLite 경계를 검사한다."""

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from app.web.api import create_app
from app.core.domain import ApiError
from app.agent.jobs import JobManager, Meter
from app.records.migrations import EXTENSION_SQL
from app.records.storage import Service, Store
from test_grounded_claims import grounded_context, candidate, NORMAL, _save
from test_jobs_api import settings_for, institution_payload, job_payload, manual_event
from test_research_records import _document
from test_storage import operation
from test_neis_recheck import neis_context, collect_and_save, row_for


def test_range_only_season_is_immutable_reused_and_persistent(tmp_path):
    """기간만 입력해 만든 시즌을 같은 범위로 다시 선택해도 UUID를 유지한다."""
    service = Service(Store(tmp_path / "period.sqlite3"))
    payload = {"search_start": "2026-12-01", "search_end": "2027-03-31"}
    first = service.create_season(payload)
    assert first["academic_year"] is None
    assert first["label"] == "2026-12-01 ~ 2027-03-31"
    assert "2026-12-01" in first["export_key"] and "2027-03-31" in first["export_key"]
    assert service.create_season(payload) == first
    assert Service(Store(service.store.path)).list_seasons()["items"] == [first]
    with pytest.raises(ApiError) as error:
        service.patch_season(first["id"], {
            "expected_revision": first["revision"], "label": "변경",
            "search_start": "2026-12-02", "search_end": "2027-03-31"})
    assert error.value.code == "season_period_immutable" and error.value.status == 409
    assert service.list_seasons()["items"] == [first]
    different = service.create_season({**payload, "search_start": "2026-12-02"})
    assert different["id"] != first["id"]


@pytest.mark.parametrize(("start", "last_valid", "boundary"), [
    ("2027-01-31", "2027-07-30", "2027-07-31"),
    ("2027-08-31", "2028-02-28", "2028-02-29"),
    ("2026-08-31", "2027-02-27", "2027-02-28"),
    ("2028-02-29", "2028-08-28", "2028-08-29"),
])
def test_season_creation_checks_calendar_month_boundary(tmp_path, start, last_valid, boundary):
    """말일·윤일을 보정한 정확한 6개월 경계부터 종료일 선택을 거절한다."""
    service = Service(Store(tmp_path / "boundary.sqlite3"))
    assert service.create_season({"search_start": start, "search_end": last_valid})["search_end"] == last_valid
    with pytest.raises(ApiError) as error:
        service.create_season({"search_start": start, "search_end": boundary})
    assert error.value.code == "invalid_date_range"
    with pytest.raises(ApiError):
        service.create_season({"search_start": boundary, "search_end": start})
    assert service.list_seasons()["total"] == 1


def _old_database(source, path):
    """현재 격리 자료를 이전 4.0+경험 2 구조의 새 파일로 옮긴다.

    Args:
        source: 검사에서만 만든 현재 저장소.
        path: 새 이전 버전 파일 경로.
    Returns:
        이관 전 원본 열·값 목록. 사용자 DB는 열지 않는다.
    """
    with sqlite3.connect(path) as legacy, source.transaction() as current:
        legacy.executescript(Path("app/records/schema.sql").read_text(encoding="utf-8"))
        legacy.executescript(EXTENSION_SQL)
        tables = [row[0] for row in legacy.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name!='sqlite_sequence'")]
        for table in tables:
            columns = [row[1] for row in legacy.execute(f'PRAGMA table_info("{table}")')]
            names = ",".join(f'"{column}"' for column in columns)
            rows = [tuple(row[column] for column in columns) for row in current.execute(f'SELECT * FROM "{table}"')]
            if table == "app_meta":
                rows = [row for row in rows if row[0] not in {"period_schema", "assessed_rules"}]
            if table == "seasons":
                # 과거 단일 시즌값을 실제 행사 학년도의 근거로 사용하지 않는지 확인한다.
                year_index, label_index = columns.index("academic_year"), columns.index("label")
                rows = [tuple(2033 if i == year_index else "원래 사용자 시즌" if i == label_index else value
                              for i, value in enumerate(row)) for row in rows]
            legacy.execute(f'DELETE FROM "{table}"')
            if rows:
                legacy.executemany(f'INSERT INTO "{table}" ({names}) VALUES({",".join("?" for _ in columns)})', rows)
        legacy.commit()
        assert not legacy.execute("PRAGMA foreign_key_check").fetchall()
        return {table: {
            "columns": [row[1] for row in legacy.execute(f'PRAGMA table_info("{table}")')],
            "rows": sorted(legacy.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr)}
            for table in tables if table != "app_meta"}


def test_period_migration_backs_up_and_preserves_legacy_records(grounded_context, tmp_path):
    """기존 UUID·원문·수기·시즌값을 보존하며 명시 근거로만 행사 학년도를 이관한다."""
    service, school, season = grounded_context
    saved = _save(grounded_context, NORMAL, candidate())
    source_event = saved["event_ids"][0]
    unknown_school = service.create_institution({**institution_payload("학년도미상대학교"), "type": "university"})
    unknown = service.create_event({
        "institution_id": unknown_school["id"], "season_id": season["id"],
        "title": "학위수여식", "event_type": "degree_award_ceremony", "audience": None,
        "campus_label": None, "allow_separate_event": False, "operations": [
            operation("ceremony_status", "scheduled"),
            operation("event_date", {"precision": "day", "date": "2027-02-21", "month": None})]})
    elementary = service.create_institution({**institution_payload("수기초등학교"), "type": "elementary"})
    elementary_event = service.create_event({
        "institution_id": elementary["id"], "season_id": season["id"],
        "title": "졸업식", "event_type": "graduation_ceremony", "audience": None,
        "campus_label": None, "allow_separate_event": False, "operations": [
            operation("ceremony_status", "scheduled"),
            operation("event_date", {"precision": "day", "date": "2027-01-07", "month": None})]})
    path = tmp_path / "legacy-v2.sqlite3"
    before = _old_database(service.store, path)
    migrated = Store(path)
    backups = list((tmp_path / "backups").glob("before-period-1-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("SELECT academic_year,label FROM seasons").fetchone() == (2033, "원래 사용자 시즌")
        assert not backup.execute("SELECT name FROM sqlite_master WHERE name='event_seasons'").fetchone()
        assert backup.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    with migrated.transaction() as conn:
        for table, snapshot in before.items():
            columns = ",".join(f'"{column}"' for column in snapshot["columns"])
            actual = sorted((tuple(row) for row in conn.execute(f'SELECT {columns} FROM "{table}"')), key=repr)
            assert actual == snapshot["rows"], table
        years = {row["id"]: row["academic_year"] for row in conn.execute("SELECT * FROM events")}
        assert years[source_event] == 2026
        assert years[unknown["id"]] is years[elementary_event["id"]] is None
        assert all(row["notnull"] == 0 for row in conn.execute("PRAGMA table_info(seasons)") if row["name"] == "academic_year")
        assert conn.execute("SELECT count(*) FROM event_seasons").fetchone()[0] == len(years)
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    Store(path)
    assert len(list((tmp_path / "backups").glob("before-period-1-*.sqlite3"))) == 1


def _time_job(tmp_path):
    """외부 호출 없이 날짜가 확인된 한 행사와 시간 작업을 준비한다.

    Args:
        tmp_path: 검사 전용 데이터 폴더.
    Returns:
        앱 실행 객체, 행사, 생성한 시간 작업.
    """
    runtime = create_app(settings_for(tmp_path)).state.runtime
    school = runtime.service.create_institution(institution_payload("기간검사학교"))
    season = runtime.service.list_seasons()["items"][0]
    event = runtime.service.create_event(manual_event(school["id"], season["id"]))
    job = runtime.jobs.create({**job_payload(season["id"], [school["id"]]),
                               "purpose": "time", "target_event_id": event["id"]}, str(uuid4()))
    return runtime, event, job


def test_time_job_snapshot_is_fixed_and_resume_keeps_identity(tmp_path):
    """중지·재개해도 기간과 대상 행사일·작업 UUID를 그대로 사용한다."""
    runtime, event, job = _time_job(tmp_path)
    frozen = deepcopy(job["scope_snapshot"])
    assert frozen["target_event_date"] == event["fields"]["event_date"]["effective_value"]
    paused = runtime.jobs.pause(job["id"])
    assert paused["status"] == "paused"
    resumed = runtime.jobs.resume(job["id"], {"expected_revision": paused["revision"], "increase_limits": None})
    assert resumed["id"] == job["id"] and resumed["scope_snapshot"] == frozen
    with runtime.store.transaction() as conn:
        assert json.loads(conn.execute("SELECT scope_snapshot_json FROM jobs WHERE id=?", (job["id"],)).fetchone()[0]) == frozen
    reopened = create_app(runtime.settings).state.runtime
    assert reopened.jobs.get(job["id"])["scope_snapshot"] == frozen


def test_recovery_preserves_old_job_but_explains_why_it_cannot_resume(tmp_path):
    """이전 조사 계약의 중단 작업은 기록을 유지하고 불가능한 재개를 안내하지 않는다."""
    runtime, _, job = _time_job(tmp_path)
    runtime.jobs.pause(job["id"])
    old_snapshot = {**job["scope_snapshot"], "research_version": "2.0"}
    with runtime.store.transaction(True) as conn:
        conn.execute("UPDATE jobs SET scope_snapshot_json=? WHERE id=?",
                     (json.dumps(old_snapshot), job["id"]))
    runtime.jobs.recover()
    saved = runtime.jobs.get(job["id"])
    assert saved["status"] == "paused" and saved["paused_reason"] == "configuration_required"
    assert saved["scope_snapshot"] == old_snapshot and saved["target_count"] == 1
    with pytest.raises(ApiError) as error:
        runtime.jobs.resume(job["id"], {"expected_revision": saved["revision"], "increase_limits": None})
    assert error.value.code == "settings_changed"


def test_changed_period_blocks_resume_checkpoint_and_direct_save(tmp_path):
    """보존 파일의 기간이 외부에서 바뀌어도 늦은 결과를 다른 조건으로 저장하지 않는다."""
    runtime, event, job = _time_job(tmp_path)
    paused = runtime.jobs.pause(job["id"])
    frozen = job["scope_snapshot"]["season"]
    with runtime.store.transaction(True) as conn:
        conn.execute("UPDATE seasons SET search_end='2027-03-30',revision=revision+1 WHERE id=?", (job["season_id"],))
    before = runtime.store.revision()
    with pytest.raises(ApiError) as error:
        runtime.jobs.resume(job["id"], {"expected_revision": paused["revision"], "increase_limits": None})
    assert error.value.code == "season_changed"
    item = runtime.jobs.items(job["id"])[0]
    meter = Meter(runtime.jobs, job["id"], item["id"], str(uuid4()))
    with pytest.raises(ApiError) as error:
        asyncio.run(meter.checkpoint({"documents": [], "candidates": []}))
    assert error.value.code == "season_changed"
    with pytest.raises(ApiError) as error:
        runtime.service.save_research(event["institution"]["id"], job["season_id"],
                                     {"season_snapshot": frozen, "documents": [], "candidates": []})
    assert error.value.code == "season_changed"
    assert runtime.store.revision() == before
    assert runtime.jobs.get(job["id"])["scope_snapshot"]["season"] == frozen


def test_changed_target_date_blocks_resume_and_late_checkpoint(tmp_path):
    """시간 조사 중 실제 행사 날짜를 정정하면 이전 날짜의 결과는 게시하지 않는다."""
    runtime, event, job = _time_job(tmp_path)
    paused = runtime.jobs.pause(job["id"])
    updated = runtime.service.apply_overrides(event["id"], {
        "expected_revision": event["revision"], "operations": [
            operation("event_date", {"precision": "day", "date": "2027-01-08", "month": None})]})
    assert updated["publication"]["calendar_date"] == "2027-01-08"
    with pytest.raises(ApiError) as error:
        runtime.jobs.resume(job["id"], {"expected_revision": paused["revision"], "increase_limits": None})
    assert error.value.code == "event_date_changed"
    item = runtime.jobs.items(job["id"])[0]
    with pytest.raises(ApiError) as error:
        asyncio.run(Meter(runtime.jobs, job["id"], item["id"], str(uuid4())).checkpoint(
            {"documents": [], "candidates": []}))
    assert error.value.code == "event_date_changed"
    assert runtime.jobs.get(job["id"])["scope_snapshot"]["target_event_date"]["date"] == "2027-01-07"


def test_paused_job_cannot_resume_while_another_period_is_running(tmp_path):
    """새 시즌 조사가 대기 중이어도 기존 시간 작업을 동시에 재개하지 않는다."""
    runtime, _, first = _time_job(tmp_path)
    paused = runtime.jobs.pause(first["id"])
    period = runtime.service.create_season({"search_start": "2027-01-01", "search_end": "2027-02-28"})
    second = runtime.jobs.create({**job_payload(period["id"]), "purpose": "date_count",
                                  "target_event_id": None}, str(uuid4()))
    with pytest.raises(ApiError) as error:
        runtime.jobs.resume(first["id"], {"expected_revision": paused["revision"], "increase_limits": None})
    assert error.value.code == "job_active"
    assert runtime.jobs.get(first["id"])["status"] == "paused"
    assert runtime.jobs.get(second["id"])["status"] == "queued"


def test_unknown_university_year_does_not_merge_unrelated_source_dates(grounded_context):
    """학년도·회차 관계가 없는 대학 공지의 다른 날짜를 같은 행사로 확정하지 않는다."""
    service, school, season = grounded_context
    first_text = NORMAL.replace("2026학년도 ", "")
    first_claim = candidate(first_text, academic_year=None, event_title="학위수여식", season_alignment="source_context")
    first_doc = _document(school, first_text, "unknown-first")
    first = _save(grounded_context, first_text, first_claim, document=first_doc)
    assert len(first["event_ids"]) == 1
    second_text = first_text.replace("2. 20.", "3. 10.")
    second_claim = candidate(second_text, date="2027-03-10", academic_year=None,
                             event_title="학위수여식", season_alignment="source_context",
                             date_quote="2027. 3. 10.(토)", time_quote="일시 : 2027. 3. 10.(토) 14:00")
    second_doc = _document(school, second_text, "unknown-other")
    second = _save(grounded_context, second_text, second_claim, document=second_doc)
    with service.store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        observation = conn.execute("SELECT event_id FROM observations WHERE id=?", (second["observation_ids"][0],)).fetchone()
        assert observation["event_id"] is None


def test_known_same_ceremony_keeps_date_conflict_on_one_event(grounded_context):
    """학년도와 행사 관계가 같은 날짜 충돌은 새 행사로 숨기지 않는다."""
    service, _, season = grounded_context
    first_doc = _document(grounded_context[1], NORMAL, "same-first")
    first = _save(grounded_context, NORMAL, candidate(), document=first_doc)
    second_text = NORMAL.replace("2. 20.", "2. 21.")
    second_claim = candidate(second_text, date="2027-02-21",
                             date_quote="2027. 2. 21.(토)", time_quote="일시 : 2027. 2. 21.(토) 14:00")
    second_doc = _document(grounded_context[1], second_text, "same-other")
    second = _save(grounded_context, second_text, second_claim, document=second_doc)
    assert first["event_ids"] == second["event_ids"]
    event = service.events(season["id"])["items"][0]
    assert event["fields"]["event_date"]["automatic"]["status"] == "conflict"
    assert not event["publication"]["current_date_allowed"]
    with service.store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        assert conn.execute("SELECT count(DISTINCT event_id) FROM observations").fetchone()[0] == 1


@pytest.mark.parametrize(("school_type", "name"), [
    ("elementary", "검증초등학교"), ("middle", "검증중학교"), ("high", "검증고등학교")])
@pytest.mark.parametrize("relationship", ["same_date", "unknown_other_date", "explicit_year_conflict"])
def test_school_year_unknown_date_relationship_is_not_inferred(grounded_context, school_type, name, relationship):
    """학교도 미상 학년도의 다른 날짜를 합치지 않고 같은 날 보완·명시 학년도 충돌은 유지한다."""
    service, school, season = grounded_context
    with service.store.transaction(True) as conn:
        conn.execute("UPDATE institutions SET name=?,type=? WHERE id=?", (name, school_type, school["id"]))
    school = service.get_institution(school["id"])
    context = service, school, season
    known_year = relationship == "explicit_year_conflict"
    title = "2026학년도 졸업식" if known_year else "졸업식"
    first_text = f"{name}\n{title}\n일시 : 2027. 2. 20.(토) 14:00"
    claim_options = {"academic_year": 2026 if known_year else None, "event_title": title,
                     "event_type": "graduation_ceremony",
                     "season_alignment": "explicit" if known_year else "source_context"}
    first = _save(context, first_text, candidate(first_text, **claim_options))
    event_id = first["event_ids"][0]
    assert service.event_detail(event_id)["event"]["publication"]["calendar_date"] == "2027-02-20"
    day = "20" if relationship == "same_date" else "21"
    second_text = first_text.replace("2. 20.", f"2. {day}.")
    claim = candidate(second_text, date=f"2027-02-{day}", date_quote=f"2027. 2. {day}.(토)",
                      time_quote=f"일시 : 2027. 2. {day}.(토) 14:00", **claim_options)
    second = _save(context, second_text, claim)
    event = service.event_detail(event_id)["event"]
    with service.store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        linked = conn.execute("SELECT event_id FROM observations WHERE id=?",
                              (second["observation_ids"][0],)).fetchone()[0]
    if relationship == "unknown_other_date":
        assert linked is None
        assert event["publication"]["calendar_date"] == "2027-02-20"
    else:
        assert linked == event_id
        if known_year:
            assert event["fields"]["event_date"]["automatic"]["status"] == "conflict"
            assert not event["publication"]["current_date_allowed"]
        else:
            assert event["publication"]["calendar_date"] == "2027-02-20"
            assert event["publication"]["calendar_time"] == "14:00"


@pytest.mark.parametrize("purpose", ["date_count", "time"])
def test_overlapping_period_reuses_public_event_sources_without_private_notes(grounded_context, tmp_path, purpose):
    """새 기간의 재조사도 같은 행사 공개 근거를 재사용하고 수기 메모는 보내지 않는다."""
    captured = []

    class Runner:
        """실제 작업 관리자가 전달하는 공개 입력을 기록한다."""

        def __init__(self, settings, meter=None):
            """외부 제공자를 초기화하지 않고 작업 입력만 받는다.

            Args:
                settings: 격리 설정.
                meter: 실제 작업 관리자의 사용량 계량기.
            """

        async def run(self, institution, season, *, identifiers=None, purpose=None,
                      target_event=None, routes=None, existing_evidence=None):
            """실행 경로의 입력을 보존하고 추가 근거 없는 결과를 반환한다.

            Args:
                institution: 재사용 공개 경로가 포함된 학교.
                season: 고정한 조사 기간.
                identifiers: 공식 기관 코드.
                purpose: 날짜·인원 또는 시간 조사.
                target_event: 시간 조사의 고정 행사.
                routes: 검증한 학교 경로.
                existing_evidence: 작업 관리자가 선택한 공개 문서.
            Returns:
                외부 수집을 하지 않은 빈 조사 결과.
            """
            captured.append({"institution": institution, "season": season,
                             "target_event": target_event, "evidence": existing_evidence})
            return {"documents": [], "candidates": [], "counts": [], "errors": []}

    service, school, origin = grounded_context
    text = NORMAL.replace(" 14:00", "")
    observed = _document(school, text, "observed-in-original-period")
    saved = _save(grounded_context, text, candidate(text, start=None), document=observed)
    event_id = saved["event_ids"][0]
    venue = _document(school, "공개 장소 안내 본관 대강당", "manual-linked-document")
    time = _document(school, "공개 본식 시각 안내 오전 10시", "manual-linked-url")
    with service.store.transaction(True) as conn:
        venue_id = service._save_document(conn, school["id"], venue)
        service._save_document(conn, school["id"], time)
    private_note = "PRIVATE_MANUAL_NOTE_가족 연락처와 통화 내용"
    event = service.event_detail(event_id)["event"]
    service.apply_overrides(event_id, {"expected_revision": event["revision"], "operations": [
        operation("venue", "본관 대강당", basis="document_confirmed", bound_date="2027-02-20",
                  note=private_note, source_url=venue["display_url"], source_document_id=venue_id),
        operation("start_time", "10:00", basis="document_confirmed", bound_date="2027-02-20",
                  note=private_note, source_url=time["display_url"])]})
    rejected = _document(school, text, "rejected-original-source")
    rejected_result = _save(grounded_context, text, candidate(text, start=None), document=rejected)
    service.reject_observation(rejected_result["observation_ids"][0], "검사에서 잘못 연결한 근거를 기각")
    outside_text = text.replace("2026학년도", "2027학년도").replace("2. 20.", "3. 10.")
    outside = _document(school, outside_text, "another-ceremony-outside-period")
    _save(grounded_context, outside_text, candidate(
        outside_text, academic_year=2027, event_title="2027학년도 학위수여식",
        date="2027-03-10", start=None, date_quote="2027. 3. 10.(토)"), document=outside)
    selected = service.create_season({"search_start": "2027-02-01", "search_end": "2027-02-28"})
    assert [item["id"] for item in service.events(selected["id"])["items"]] == [event_id]
    manager = JobManager(service, settings_for(tmp_path), runner_factory=Runner)
    payload = {**job_payload(selected["id"], [school["id"]] if purpose == "time" else None),
               "purpose": purpose, "target_event_id": event_id if purpose == "time" else None}
    job = manager.create(payload, str(uuid4()))
    asyncio.run(manager.run(job["id"]))
    assert len(captured) == 1 and manager.get(job["id"])["status"] == "completed"
    expected = {observed["display_url"], venue["display_url"], time["display_url"]}
    assert {item["display_url"] for item in captured[0]["institution"]["known_documents"]} == expected
    assert {item["display_url"] for item in captured[0]["evidence"]["documents"]} == expected
    assert private_note not in json.dumps(captured, ensure_ascii=False)
    assert captured[0]["season"]["id"] == selected["id"] != origin["id"]
    with service.store.transaction() as conn:
        assert all(row[0] == private_note for row in conn.execute("SELECT note FROM manual_overrides"))


@pytest.mark.parametrize("day", ["2027-02-20", "2027-03-10"])
def test_unknown_year_notice_does_not_duplicate_known_university_event(grounded_context, day):
    """학년도 생략 공지를 새 행사로 만들어 기존 행사 관계의 불확실성을 숨기지 않는다."""
    service, school, season = grounded_context
    original = _save(grounded_context, NORMAL, candidate())
    event_id = original["event_ids"][0]
    text = NORMAL.replace("2026학년도 ", "")
    date_quote = "2027. 2. 20.(토)"
    if day == "2027-03-10":
        text = text.replace("2. 20.", "3. 10.")
        date_quote = "2027. 3. 10.(토)"
    claim = candidate(text, date=day, academic_year=None, event_title="학위수여식",
                      season_alignment="source_context", date_quote=date_quote,
                      time_quote=f"일시 : {date_quote} 14:00")
    doc = _document(school, text, "academic-year-omitted")
    latest = _save(grounded_context, text, claim, document=doc)
    with service.store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        assert conn.execute("SELECT academic_year FROM events WHERE id=?", (event_id,)).fetchone()[0] == 2026
        observed = conn.execute("SELECT * FROM observations WHERE id=?", (latest["observation_ids"][0],)).fetchone()
        assert json.loads(observed["candidate_json"])["academic_year"] is None
        assert observed["event_id"] == (event_id if day == "2027-02-20" else None)
    assert service.event_detail(event_id)["event"]["publication"]["calendar_date"] == "2027-02-20"


def test_unknown_year_cancellation_for_exact_known_date_holds_only_that_event(grounded_context):
    """유일한 기존 행사일을 명시한 취소를 읽으면 학년도 생략만으로 정상 일정을 유지하지 않는다."""
    service, school, season = grounded_context
    original = _save(grounded_context, NORMAL, candidate())
    event_id = original["event_ids"][0]
    text = "검증대학교 학위수여식은 2027년 2월 20일 예정이었으나 취소되었습니다."
    claim = candidate(text, academic_year=None, event_title="학위수여식",
                      season_alignment="source_context", ceremony_status="cancelled",
                      date_quote="2027년 2월 20일", start=None)
    doc = _document(school, text, "separate-cancellation")
    _save(grounded_context, text, claim, document=doc)
    event = service.event_detail(event_id)["event"]
    assert event["fields"]["ceremony_status"]["effective_value"] == "cancelled"
    assert event["publication"]["state"] == "excluded"
    assert not event["publication"]["current_date_allowed"]
    assert not event["publication"]["current_time_allowed"]
    with service.store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1


@pytest.mark.parametrize(("sequence", "day"), [("제8회", "21"), ("제9회", "20")])
def test_unknown_year_cancellation_does_not_spread_to_other_date_or_sequence(grounded_context, sequence, day):
    """날짜나 명시 회차가 다른 취소는 기존 정상 행사를 보류하지 않는다."""
    service, school, _ = grounded_context
    text = NORMAL.replace("2026학년도 ", "2026학년도 제8회 ")
    first = _save(grounded_context, text, candidate(text, event_title="2026학년도 제8회 학위수여식"))
    event_id = first["event_ids"][0]
    cancellation = f"검증대학교 {sequence} 학위수여식은 2027년 2월 {day}일 예정이었으나 취소되었습니다."
    claim = candidate(cancellation, academic_year=None, event_title=f"{sequence} 학위수여식",
                      season_alignment="source_context", ceremony_status="cancelled",
                      date=f"2027-02-{day}", date_quote=f"2027년 2월 {day}일", start=None)
    doc = _document(school, cancellation, f"other-cancellation-{day}")
    _save(grounded_context, cancellation, claim, document=doc)
    assert service.event_detail(event_id)["event"]["publication"]["current_date_allowed"]


def test_unknown_year_time_evidence_completes_existing_known_event(grounded_context):
    """학년도를 생략한 같은 본식 공지의 시각을 기존 행사에 보완하고 원문 학년도는 남겨 둔다."""
    service, school, season = grounded_context
    original = NORMAL.replace(" 14:00", "")
    first = _save(grounded_context, original, candidate(original, start=None))
    event_id = first["event_ids"][0]
    updated = NORMAL.replace("2026학년도 ", "").replace("14:00", "10:00")
    doc = _document(school, updated, "time-without-academic-year")
    claim = candidate(updated, academic_year=None, event_title="학위수여식",
                      season_alignment="source_context", start="10:00",
                      time_quote="일시 : 2027. 2. 20.(토) 10:00")
    result = service.save_research(school["id"], season["id"], {
        "purpose": "time", "target_event_id": event_id,
        "documents": [doc], "candidates": [{"document_id": doc["id"], "candidate": claim}]})
    assert result["event_ids"] == [event_id]
    event = service.event_detail(event_id)["event"]
    assert event["academic_year"] == 2026 and event["publication"]["calendar_time"] == "10:00"
    with service.store.transaction() as conn:
        observed = conn.execute("SELECT candidate_json FROM observations WHERE id=?", (result["observation_ids"][0],)).fetchone()
        assert json.loads(observed[0])["academic_year"] is None


def test_new_school_phone_record_keeps_unknown_year_and_clear_date(grounded_context):
    """날짜만 직접 확인한 새 초등학교 행사에는 학년도를 추정해 넣지 않는다."""
    from app.records.natural import NaturalRecorder
    service, _, season = grounded_context
    school = service.create_institution({**institution_payload("지연초등학교"), "type": "elementary"})
    result = NaturalRecorder(service).record({
        "season_id": season["id"], "text": "지연초등학교 졸업식은 3월 2일 오전 10시야. 방금 전화해서 확인했어.",
        "conversation_id": None, "expected_revision": None})
    assert result["status"] == "saved" and not result["questions"]
    event = service.event_detail(result["event_id"])["event"]
    assert event["academic_year"] is None
    assert event["publication"]["calendar_date"] == "2027-03-02"
    assert event["publication"]["calendar_time"] == "10:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["no_data", "provider_error"])
async def test_date_only_neis_recheck_preserves_connected_document_outside_queried_period(neis_context, status):
    """같은 정본에 연결돼도 이번 날짜 범위 밖 NEIS 원문은 누락·실패로 처리하지 않는다."""
    from app.core.domain import utcnow
    service, school, season = neis_context
    january = row_for(school, season, "20270107")
    february = row_for(school, season, "20270220")
    _, first = await collect_and_save(neis_context, [january])
    narrow = service.create_season({"search_start": "2027-01-01", "search_end": "2027-01-10"})
    assert [event["id"] for event in service.events(narrow["id"])["items"]] == first["event_ids"]
    await collect_and_save(neis_context, [january, february])
    with service.store.transaction() as conn:
        sources = {}
        for row in conn.execute("SELECT id,segments_json,recheck_json FROM documents"):
            raw = json.loads(next(segment["text"] for segment in json.loads(row["segments_json"]) if segment["id"] == "row"))
            sources[raw["AA_YMD"]] = (row["id"], row["recheck_json"])
        assert json.loads(sources["20270220"][1])["status"] != "failed"
    service.save_research(school["id"], narrow["id"], {
        "documents": [], "candidates": [], "coverage": {"calendar": {
            "status": status, "coverage_complete": status == "no_data",
            "checked_at": utcnow(), "reason_codes": ["provider_error"] if status == "provider_error" else []}}})
    with service.store.transaction() as conn:
        after = {row["id"]: row["recheck_json"] for row in conn.execute("SELECT id,recheck_json FROM documents")}
    assert json.loads(after[sources["20270107"][0]])["status"] == "failed"
    assert after[sources["20270107"][0]] != sources["20270107"][1]
    assert after[sources["20270220"][0]] == sources["20270220"][1]


@pytest.mark.asyncio
async def test_neis_explicit_year_enriches_unknown_manual_event_without_copying(neis_context):
    """학년도 없는 수기 행사를 NEIS 명시 학년도로 보완하면서 수기 UUID를 보존한다."""
    from app.records.natural import NaturalRecorder
    service, school, season = neis_context
    recorded = NaturalRecorder(service).record({
        "season_id": season["id"],
        "text": "재확인초등학교 졸업식은 1월 7일 오전 10시야. 방금 전화해서 확인했어.",
        "conversation_id": None, "expected_revision": None})
    event_id = recorded["event_id"]
    assert service.event_detail(event_id)["event"]["academic_year"] is None
    with service.store.transaction() as conn:
        originals = [tuple(row) for row in conn.execute(
            "SELECT id,field,value_json,confirmed_at,note FROM manual_overrides ORDER BY id")]
    _, saved = await collect_and_save(neis_context, [row_for(school, season, academic_year=2026)])
    assert saved["event_ids"] == [event_id]
    event = service.event_detail(event_id)["event"]
    assert event["academic_year"] == 2026 and event["publication"]["calendar_time"] == "10:00"
    with service.store.transaction() as conn:
        assert [tuple(row) for row in conn.execute(
            "SELECT id,field,value_json,confirmed_at,note FROM manual_overrides ORDER BY id")] == originals
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
