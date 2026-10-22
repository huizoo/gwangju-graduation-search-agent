"""실제 NEIS Adapter의 격리 응답을 저장·게시까지 연결하는 재확인 반례."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.core.domain import dumps, utcnow
from app.integrations.providers import HttpGateway, NeisProvider
from app.validation.rules import extract_candidates
from app.records.storage import Service, Store


@pytest.fixture
def neis_context(tmp_path):
    """실제 데이터·키와 분리한 학교·시즌·공식 관계를 준비한다."""
    service = Service(Store(tmp_path / "neis.sqlite3"), {"auto_rule_ids": ["neis_ceremony_row"]})
    season = service.create_season({
        "label": "검사 시즌", "academic_year": 2026, "search_start": "2026-12-01",
        "search_end": "2027-03-31", "export_key": "neis-recheck",
        "timezone": "Asia/Seoul", "region_profile": "gwangju_5_gu",
    })
    institution = service.create_institution({
        "name": "재확인초등학교", "type": "elementary", "district": "북구",
        "address": "광주광역시 북구 검사로 1", "campus_label": None,
        "phone": None, "homepage_url": None,
    })
    with service.store.transaction(True) as conn:
        conn.execute("UPDATE institutions SET official_channels_json=? WHERE id=?", (
            dumps([{"url": "https://open.neis.go.kr/hub",
                    "verified_by_url": "https://open.neis.go.kr/hub/schoolInfo",
                    "verified_at": utcnow()}]), institution["id"]))
        conn.execute("INSERT INTO institution_identifiers VALUES(?,?,?)",
                     ("neis", "F10:123", institution["id"]))
    return service, service.get_institution(institution["id"]), season


def row_for(institution, season, day="20270107", grade="SIX", academic_year=2026):
    """날짜와 대상 학년을 독립적으로 바꿀 수 있는 공식 행을 만든다."""
    return {
        "ATPT_OFCDC_SC_CODE": "F10", "SD_SCHUL_CODE": "123",
        "SCHUL_NM": institution["name"], "AY": str(academic_year),
        "AA_YMD": day, "EVENT_NM": "졸업식", "EVENT_CNTNT": "",
        "SCHUL_CRSE_SC_NM": "초등학교", f"{grade}_GRADE_EVENT_YN": "Y",
    }


async def collect_and_save(context, rows=None, *, failure=False, missing_key=False):
    """실제 Adapter 반환을 ResearchRunner와 같은 결과 형태로 저장한다."""
    service, institution, season = context
    data = json.loads((Path(__file__).parents[1] / "config/defaults.json").read_text())
    data["execution"].update(http_max_retries=0, per_host_min_interval_ms=0)
    settings = SimpleNamespace(data=data, secrets={"neis_api_key": "" if missing_key else "fixture-key"})

    def handle(request):
        if failure:
            raise httpx.ConnectError("격리된 연결 실패", request=request)
        selected = [row for row in (rows or [])
                    if request.url.params["AA_FROM_YMD"] <= row["AA_YMD"] <= request.url.params["AA_TO_YMD"]]
        if not selected:
            return httpx.Response(200, json={"RESULT": {"CODE": "INFO-200", "MESSAGE": "해당하는 데이터가 없습니다."}})
        return httpx.Response(200, json={"SchoolSchedule": [
            {"head": [{"list_total_count": len(selected)}, {"RESULT": {"CODE": "INFO-000"}}]},
            {"row": selected},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        gateway = HttpGateway(settings, client=client)
        calendar = await NeisProvider(settings, gateway=gateway).calendar(
            institution, season, [{"provider": "neis", "external_key": "F10:123"}])
    result = {
        "documents": calendar["documents"],
        "candidates": [{"document_id": document["id"], "candidate": candidate}
                       for document in calendar["documents"]
                       for candidate in extract_candidates(document, institution, season)],
        "coverage": {"calendar": {key: value for key, value in calendar.items() if key != "documents"}},
        "errors": ([{"provider": "neis", "code": code, "message": "격리된 자료원 오류"}
                    for code in calendar["reason_codes"]]
                   if calendar["status"] not in {"ok", "no_data"} else []),
    }
    saved = service.save_research(institution["id"], season["id"], result)
    return calendar, saved


def publication(context, event_id):
    """같은 서비스의 최종 게시 판정을 조회한다."""
    return context[0].event_detail(event_id)["event"]["publication"]


@pytest.mark.asyncio
async def test_failed_neis_recheck_preserves_general_date_but_withholds_current(neis_context):
    """실제로 실패한 필수 API 조회를 미시도 상태로 숨기지 않는다."""
    _, institution, season = neis_context
    _, saved = await collect_and_save(neis_context, [row_for(institution, season)])
    event_id = saved["event_ids"][0]
    before = publication(neis_context, event_id)
    assert before["current_date_allowed"]
    calendar, _ = await collect_and_save(neis_context, failure=True)
    assert calendar["status"] == "provider_error"
    after = publication(neis_context, event_id)
    assert after["calendar_date"] == before["calendar_date"]
    assert not after["current_date_allowed"]
    assert after["field_permissions"]["event_date"]["recheck_status"] == "failed"
    assert after["field_permissions"]["event_date"]["confirmed_at"] == before["field_permissions"]["event_date"]["confirmed_at"]


@pytest.mark.asyncio
async def test_same_neis_row_success_restores_current_after_failed_attempt(neis_context):
    """실패 후 같은 원문 행을 실제로 다시 얻으면 실패 표시를 해제한다."""
    _, institution, season = neis_context
    row = row_for(institution, season)
    _, saved = await collect_and_save(neis_context, [row])
    event_id = saved["event_ids"][0]
    await collect_and_save(neis_context, failure=True)
    assert not publication(neis_context, event_id)["current_date_allowed"]
    await collect_and_save(neis_context, [row])
    after = publication(neis_context, event_id)
    assert after["current_date_allowed"]
    assert after["field_permissions"]["event_date"]["recheck_status"] == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["missing_key", "auxiliary_search"])
async def test_unattempted_or_auxiliary_failure_does_not_withhold_neis(neis_context, cause):
    """키 미설정·보조 검색 실패를 실제 지원 API 재확인 실패로 만들지 않는다."""
    service, institution, season = neis_context
    _, saved = await collect_and_save(neis_context, [row_for(institution, season)])
    if cause == "missing_key":
        calendar, _ = await collect_and_save(neis_context, missing_key=True)
        assert calendar["status"] == "configuration_required"
    else:
        service.save_research(institution["id"], season["id"], {
            "documents": [], "candidates": [],
            "errors": [{"provider": "tavily", "code": "provider_error", "message": "격리된 보조 검색 실패"}],
        })
    assert publication(neis_context, saved["event_ids"][0])["current_date_allowed"]


@pytest.mark.asyncio
async def test_complete_neis_response_missing_one_row_holds_only_missing_event(neis_context):
    """완전한 재조회에서 빠진 행을 취소로 단정하지 않고 현재 사용만 보류한다."""
    service, institution, season = neis_context
    rows = [row_for(institution, season), row_for(institution, season, "20270108", "FIV")]
    await collect_and_save(neis_context, rows)
    ids = {item["publication"]["calendar_date"]: item["id"]
           for item in service.events(season["id"])["items"]}
    assert len(ids) == 2
    calendar, _ = await collect_and_save(neis_context, [rows[0]])
    assert calendar["coverage_complete"]
    assert publication(neis_context, ids["2027-01-07"])["current_date_allowed"]
    missing = publication(neis_context, ids["2027-01-08"])
    assert missing["calendar_date"] == "2027-01-08"
    assert not missing["current_date_allowed"]
    assert missing["state"] != "excluded"


@pytest.mark.asyncio
async def test_complete_empty_neis_response_does_not_keep_current_confirmation(neis_context):
    """성공한 전체 조회의 자료 없음은 과거 행을 재확인한 것으로 계산하지 않는다."""
    _, institution, season = neis_context
    _, saved = await collect_and_save(neis_context, [row_for(institution, season)])
    calendar, _ = await collect_and_save(neis_context, [])
    assert calendar["status"] == "no_data" and calendar["coverage_complete"]
    after = publication(neis_context, saved["event_ids"][0])
    assert after["calendar_date"] == "2027-01-07"
    assert not after["current_date_allowed"]
    assert after["state"] != "excluded"


@pytest.mark.asyncio
async def test_neis_failure_does_not_invalidate_another_season(neis_context):
    """동일 기관이어도 다른 시즌의 지원 원문까지 실패 처리하지 않는다."""
    service, institution, season = neis_context
    old_season = service.create_season({
        "label": "이전 검사 시즌", "academic_year": 2025, "search_start": "2025-12-01",
        "search_end": "2026-03-31", "export_key": "neis-previous",
        "timezone": "Asia/Seoul", "region_profile": "gwangju_5_gu",
    })
    old_context = (service, institution, old_season)
    _, old_saved = await collect_and_save(old_context, [row_for(institution, old_season, "20260107", academic_year=2025)])
    _, current_saved = await collect_and_save(neis_context, [row_for(institution, season)])
    await collect_and_save(neis_context, failure=True)
    assert not publication(neis_context, current_saved["event_ids"][0])["current_date_allowed"]
    assert publication(old_context, old_saved["event_ids"][0])["current_date_allowed"]


@pytest.mark.asyncio
async def test_changed_neis_date_keeps_event_identity_and_requires_conflict_review(neis_context):
    """같은 대상의 다른 날짜를 새 행사로 숨기거나 근거 없이 덮어쓰지 않는다."""
    service, institution, season = neis_context
    row = row_for(institution, season)
    await collect_and_save(neis_context, [row])
    changed = deepcopy(row)
    changed["AA_YMD"] = "20270108"
    await collect_and_save(neis_context, [changed])
    items = service.events(season["id"])["items"]
    assert len(items) == 1
    assert items[0]["fields"]["event_date"]["automatic"]["status"] == "conflict"
    assert not items[0]["publication"]["current_date_allowed"]
