"""명시 학사력 변경 규칙을 실제 저장·게시·수기 보호에 연결하는 검사."""

import hashlib
import json

import pytest

from app.domain import dumps, utcnow
from app.providers import empty_document
from app.rules import extract_candidates
from app.storage import Service, Store


@pytest.fixture
def change_context(tmp_path):
    """실제 기관 기록과 분리한 대학·시즌·공식 채널을 준비한다."""
    service = Service(Store(tmp_path / "change.sqlite3"),
                      {"auto_rule_ids": ["academic_calendar_row"]})
    season = service.create_season({
        "label": "변경 공지 검사", "academic_year": 2025, "search_start": "2025-12-01",
        "search_end": "2026-03-31", "export_key": "change-notice",
        "timezone": "Asia/Seoul", "region_profile": "gwangju_5_gu",
    })
    institution = service.create_institution({
        "name": "검증대학교", "type": "university", "district": "북구",
        "address": "광주광역시 북구 검사로 1", "campus_label": "본교",
        "phone": None, "homepage_url": None,
    })
    channel = {"url": "https://university.example",
               "verified_by_url": "https://directory.example/university",
               "verified_at": utcnow()}
    with service.store.transaction(True) as conn:
        conn.execute("UPDATE institutions SET official_channels_json=? WHERE id=?",
                     (dumps([channel]), institution["id"]))
    return service, service.get_institution(institution["id"]), season


def change_document(context, before, after, path, *, group="학부", title="제63회 학위수여식"):
    """실제 문법을 사용하는 합성 변경 공지를 DocumentRead로 만든다."""
    _, institution, _ = context
    text = (f"{institution['name']} 2025학년도 {group} 학사력 변경 안내\n"
            f"- {title}\n(변경전) {before} → (변경후) {after}")
    document = empty_document(institution, f"https://university.example/notice/{path}")
    channel = institution["official_channels"][0]
    moment = utcnow()
    document.update(
        title="2025학년도 학부 학사력 변경", authority="official",
        authority_evidence={"channel_url": channel["url"],
                            "verified_by_url": channel["verified_by_url"],
                            "verified_at": channel["verified_at"]},
        status="ok", source_kind="html", content_type="text/html",
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        segments=[{"id": "body", "unit_id": "academic-change",
                   "text": text, "locator_kind": "html_text", "location": "body"}],
        partial=False, read_scope="전체 본문",
        acquisition={"method": "http", "transport_status": "ok", "http_status": 200,
                     "final_url": document["display_url"], "tls_verified": True},
        reading={"method": "html_parser", "scope": "full_text",
                 "content_match": "expected", "institution_match": "matched"},
        recheck={"status": "succeeded", "attempted_at": moment, "reason_codes": []},
        last_checked_at=moment,
    )
    return document


def save(context, document):
    """실제 parser의 후보를 서버 검증·저장 경로로 전달한다."""
    service, institution, season = context
    candidates = extract_candidates(document, institution, season)
    assert len(candidates) == 1
    return service.save_research(institution["id"], season["id"], {
        "documents": [document],
        "candidates": [{"document_id": document["id"], "candidate": candidates[0]}],
    })


def seed_previous_change(context):
    """앞선 명시 변경으로 확인된 날짜를 만든다."""
    document = change_document(context, "2026.2.24(화)", "2026.2.23(월)", "first")
    saved = save(context, document)
    event_id = saved["event_ids"][0]
    assert context[0].event_detail(event_id)["event"]["publication"]["calendar_date"] == "2026-02-23"
    return saved


def edges(context, event_id):
    """서버가 남긴 필드별 대체 이력을 조회한다."""
    with context[0].store.transaction() as conn:
        return [json.loads(row[0]) for row in conn.execute(
            "SELECT after_json FROM audit_log WHERE entity_id=? AND action='supersede_observation'",
            (event_id,))]


def test_verified_change_updates_date_preserves_observations_and_deduplicates_audit(change_context):
    """이전값과 새값의 관계가 검증된 경우만 교체하고 재조회 이력을 중복하지 않는다."""
    service, _, season = change_context
    first = seed_previous_change(change_context)
    document = change_document(change_context, "2026.2.23(월)", "2026.2.20(금)", "second")
    second = save(change_context, document)
    assert first["event_ids"] == second["event_ids"]
    event_id = first["event_ids"][0]
    detail = service.event_detail(event_id)
    assert len(service.events(season["id"])["items"]) == 1
    assert detail["event"]["publication"]["calendar_date"] == "2026-02-20"
    assert detail["event"]["publication"]["current_date_allowed"]
    assert detail["event"]["publication"]["calendar_time"] is None
    assert not detail["event"]["publication"]["sales_ready"]
    assert len(detail["observations"]) == 2
    assert all(item["disposition"] != "rejected" for item in detail["observations"])
    expected = edges(change_context, event_id)
    assert len(expected) == 1
    assert expected[0]["field"] == "event_date"
    assert expected[0]["old_id"] == first["observation_ids"][0]
    assert expected[0]["new_id"] == second["observation_ids"][0]
    assert expected[0]["source_document_id"] == second["document_ids"][0]
    assert expected[0]["rule_id"] == "academic_calendar_row"
    save(change_context, document)
    assert edges(change_context, event_id) == expected
    assert len(service.event_detail(event_id)["observations"]) == 2


def test_newer_unrelated_change_still_conflicts_instead_of_winning(change_context):
    """대체 대상 이전값이 맞지 않는 새 공지는 시각이 최신이어도 승자가 아니다."""
    service, _, _ = change_context
    first = seed_previous_change(change_context)
    save(change_context, change_document(change_context, "2026.2.23(월)", "2026.2.20(금)", "second"))
    save(change_context, change_document(change_context, "2026.2.24(화)", "2026.2.22(일)", "unrelated"))
    detail = service.event_detail(first["event_ids"][0])
    assert detail["event"]["fields"]["event_date"]["automatic"]["status"] == "conflict"
    assert not detail["event"]["publication"]["current_date_allowed"]
    assert len(edges(change_context, first["event_ids"][0])) == 1


def test_date_replacement_never_rebinds_existing_manual_start_time(change_context):
    """날짜 대체 뒤 이전 날짜의 수기 시각을 새 날짜와 결합하지 않는다."""
    service, _, _ = change_context
    first = seed_previous_change(change_context)
    event_id = first["event_ids"][0]
    current = service.event_detail(event_id)["event"]
    service.apply_overrides(event_id, {"expected_revision": current["revision"], "operations": [{
        "field": "start_time", "action": "set", "value": "10:00",
        "basis": "phone_confirmed", "bound_date": "2026-02-23",
        "confirmed_at": utcnow(), "note": "", "resolves_fingerprint": None, "source_url": None,
    }]})
    before = service.event_detail(event_id)["event"]
    assert before["publication"]["sales_ready"]
    save(change_context, change_document(change_context, "2026.2.23(월)", "2026.2.20(금)", "second"))
    after = service.event_detail(event_id)["event"]
    assert after["fields"]["start_time"]["manual"] == before["fields"]["start_time"]["manual"]
    assert after["publication"]["calendar_date"] == "2026-02-20"
    assert after["publication"]["calendar_time"] is None
    assert not after["publication"]["sales_ready"]


def test_date_replacement_preserves_manual_date_and_creates_difference(change_context):
    """대체된 자동 날짜가 수기 원본을 덮어쓰거나 확인 절차를 생략하지 않는다."""
    service, _, _ = change_context
    first = seed_previous_change(change_context)
    event_id = first["event_ids"][0]
    current = service.event_detail(event_id)["event"]
    service.apply_overrides(event_id, {"expected_revision": current["revision"], "operations": [{
        "field": "event_date", "action": "set",
        "value": {"precision": "day", "date": "2026-02-23", "month": None},
        "basis": "phone_confirmed", "confirmed_at": utcnow(), "note": "",
        "resolves_fingerprint": None, "source_url": None,
    }]})
    before = service.event_detail(event_id)["event"]
    save(change_context, change_document(change_context, "2026.2.23(월)", "2026.2.20(금)", "second"))
    after = service.event_detail(event_id)["event"]
    assert after["fields"]["event_date"]["manual"] == before["fields"]["event_date"]["manual"]
    assert after["fields"]["event_date"]["automatic"]["value"]["date"] == "2026-02-20"
    assert after["fields"]["event_date"]["has_web_difference"]
    assert not after["publication"]["current_date_allowed"]


def test_rejected_replacement_does_not_keep_applying_historical_audit_edge(change_context):
    """대체 관측이 기각되면 감사 로그만 믿고 이전 근거를 계속 제외하지 않는다."""
    service, _, _ = change_context
    first = seed_previous_change(change_context)
    second = save(change_context, change_document(change_context, "2026.2.23(월)", "2026.2.20(금)", "second"))
    event_id = first["event_ids"][0]
    service.reject_observation(second["observation_ids"][0], "검사에서 새 문서의 근거 무효를 가정")
    after = service.event_detail(event_id)
    assert after["event"]["publication"]["calendar_date"] == "2026-02-23"
    assert len(edges(change_context, event_id)) == 1
    assert any(item["id"] == second["observation_ids"][0] and item["disposition"] == "rejected"
               for item in after["observations"])


def test_cyclic_change_claims_remain_conflicting_without_recorded_supersession(change_context):
    """서로의 새 날짜를 되돌리는 순환 주장은 게시 순서 추측으로 해소하지 않는다."""
    service, _, _ = change_context
    first = save(change_context, change_document(
        change_context, "2026.2.20(금)", "2026.2.23(월)", "forward"))
    second = save(change_context, change_document(
        change_context, "2026.2.23(월)", "2026.2.20(금)", "reverse"))
    assert first["event_ids"] == second["event_ids"]
    event_id = first["event_ids"][0]
    current = service.event_detail(event_id)["event"]
    assert current["fields"]["event_date"]["automatic"]["status"] == "conflict"
    assert not current["publication"]["current_date_allowed"]
    assert edges(change_context, event_id) == []


@pytest.mark.parametrize("identity", [{"title": "제64회 학위수여식"}])
def test_different_round_or_audience_never_supersedes_another_event(change_context, identity):
    """이전 날짜가 같아도 명시 회차가 다르면 별도 행사로 보존한다."""
    service, _, season = change_context
    first = seed_previous_change(change_context)
    second = save(change_context, change_document(
        change_context, "2026.2.23(월)", "2026.2.20(금)", "another-event", **identity))
    assert first["event_ids"] != second["event_ids"]
    assert len(service.events(season["id"])["items"]) == 2
    assert service.event_detail(first["event_ids"][0])["event"]["publication"]["calendar_date"] == "2026-02-23"
    assert edges(change_context, first["event_ids"][0]) == []
    assert edges(change_context, second["event_ids"][0]) == []


def test_degree_group_alone_does_not_create_another_representative_event(change_context):
    """대표 행사 과정 표기만 다르면 같은 행사에서 변경 근거를 검토한다."""
    service, _, season = change_context
    first = seed_previous_change(change_context)
    second = save(change_context, change_document(
        change_context, "2026.2.23(월)", "2026.2.20(금)", "same-representative", group="대학원"))
    assert first["event_ids"] == second["event_ids"]
    assert len(service.events(season["id"])["items"]) == 1
    # 서로 다른 과정 변경 행을 근거 없이 전후 대체 관계로 추정하지 않는다.
    assert service.event_detail(first["event_ids"][0])["event"]["publication"]["current_date_allowed"] is False
    assert edges(change_context, first["event_ids"][0]) == []
