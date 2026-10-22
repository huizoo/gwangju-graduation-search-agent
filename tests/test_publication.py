"""보존한 게시 반례를 운영 게시 함수에 연결하고 실제 저장 흐름을 검사한다."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from app.validation import publication
from app.core.domain import dumps, utcnow, new_id
from app.integrations.providers import empty_document
from app.validation.rules import REGISTERED_RULES, extract_candidates
from app.records.storage import Service
from publication_cases import run as publication_cases
from test_storage import service, season, institution, operation


def test_reference_publication_cases_against_production(monkeypatch):
    """인계에서 가져온 양성·반례를 현재 운영 정책으로 그대로 재검사한다."""
    registry = publication.RuleRegistry()
    registry.register("explicit_notice", "1.0.0", lambda field, value, observation, document, event, season, **kwargs: publication._literal_value(field, value, publication._literal_support(observation["candidate"], field, document), observation["candidate"]))
    monkeypatch.setattr(publication, "RULES", registry)
    def calculate(event, season, context):
        context = deepcopy(context)
        if not context["use_fixture_rules"]:
            context["registered_rules"] = {}
        return publication.calculate_publication(event, season, context)
    def supplied(event, season, context):
        expected = calculate(event, season, context)
        if event.get("publication") != expected:
            raise ValueError("publication mismatch")
        return expected
    result = publication_cases(calculate, supplied)
    assert result["passed"] >= 40


def live_document(service, institution, clock="오전 10시", day="7", url="https://school.gen.go.kr/notice/1"):
    import hashlib
    text = f"시험학교 2026학년도 졸업식은 2027년 1월 {day}일 {clock} 시작합니다."
    document = empty_document(institution, url)
    now = utcnow()
    channel = {"url": "https://school.gen.go.kr", "verified_by_url": "https://www.gen.go.kr/schools", "verified_at": now}
    with service.store.transaction(True) as conn:
        existing = service._institution(conn, institution["id"])["official_channels"]
        if existing:
            channel = existing[0]
        else:
            conn.execute("UPDATE institutions SET official_channels_json=? WHERE id=?", (dumps([channel]), institution["id"]))
    document.update(authority="official", authority_evidence={"channel_url": channel["url"], "verified_by_url": channel["verified_by_url"], "verified_at": channel["verified_at"]}, status="ok", source_kind="html", content_type="text/html", content_hash=hashlib.sha256(text.encode()).hexdigest(), segments=[{"id": "s1", "unit_id": "notice", "text": text, "locator_kind": "html_text", "location": "notice"}], partial=False, read_scope="전체 본문", acquisition={"method": "http", "transport_status": "ok", "http_status": 200, "final_url": url, "tls_verified": True}, reading={"method": "html_parser", "scope": "full_text", "content_match": "expected", "institution_match": "matched"}, recheck={"status": "succeeded", "attempted_at": now, "reason_codes": []}, last_checked_at=now)
    return document


def result_for(document, institution, season):
    return {"documents": [document], "candidates": [{"document_id": document["id"], "candidate": candidate} for candidate in extract_candidates(document, institution, season)]}


def test_actual_rules_persist_and_publish_only_when_enabled(service):
    s, i = season(service), institution(service)
    document = live_document(service, i)
    i = service.get_institution(i["id"])
    research = result_for(document, i, s)
    service.save_research(i["id"], s["id"], research)
    assert service.events(s["id"], "sales")["total"] == 0
    service.settings = {"auto_rule_ids": ["explicit_notice"]}
    saved = service.save_research(i["id"], s["id"], research)
    detail = service.event_detail(saved["event_ids"][0])
    assert detail["event"]["publication"]["sales_ready"]
    assert detail["event"]["publication"]["calendar_time"] == "10:00"
    proof = detail["event"]["fields"]["start_time"]["automatic"]["value_check"]
    assert proof["status"] == "supported" and proof["checks"]["time_role"] == "pass"
    assert len(detail["documents"]) == 1
    service.reject_observation(detail["observations"][0]["id"], "가상 시험에서 원문 기각을 가정")
    assert service.events(s["id"], "calendar")["total"] == 0
    assert service.event_detail(saved["event_ids"][0])["observations"][0]["disposition"] == "rejected"


def test_authority_same_instant_accepts_offset_normalization(service):
    """KST 공식 명부 시각이 UTC로 저장되어도 관계를 잃지 않으며 다른 시각은 거절한다."""
    service.settings = {"auto_rule_ids": ["explicit_notice"]}
    s, i = season(service), institution(service)
    document = live_document(service, i)
    channel = service.get_institution(i["id"])["official_channels"][0]
    instant = datetime.fromisoformat(channel["verified_at"].replace("Z", "+00:00"))
    channel["verified_at"] = instant.astimezone(timezone(timedelta(hours=9))).isoformat()
    with service.store.transaction(True) as conn:
        conn.execute("UPDATE institutions SET official_channels_json=? WHERE id=?", (dumps([channel]), i["id"]))
    i = service.get_institution(i["id"])
    saved = service.save_research(i["id"], s["id"], result_for(document, i, s))
    assert service.event_detail(saved["event_ids"][0])["event"]["publication"]["sales_ready"]
    channel["verified_at"] = (instant - timedelta(seconds=1)).isoformat()
    with service.store.transaction(True) as conn:
        conn.execute("UPDATE institutions SET official_channels_json=? WHERE id=?", (dumps([channel]), i["id"]))
    assert not service.event_detail(saved["event_ids"][0])["event"]["publication"]["calendar_eligible"]


def test_conflict_first_seen_and_reconfirmation_survive_repeat(service):
    service.settings = {"auto_rule_ids": ["explicit_notice"]}
    s, i = season(service), institution(service)
    first = live_document(service, i)
    i = service.get_institution(i["id"])
    saved = service.save_research(i["id"], s["id"], result_for(first, i, s))
    event_id = saved["event_ids"][0]
    second = live_document(service, i, clock="오전 11시", url="https://school.gen.go.kr/notice/2")
    research = result_for(second, i, s)
    service.save_research(i["id"], s["id"], research)
    current = service.event_detail(event_id)["event"]
    assert current["publication"]["calendar_date"] == "2027-01-07"
    assert current["publication"]["calendar_time"] is None
    automatic = current["fields"]["start_time"]["automatic"]
    detected, fp = automatic["conflict_detected_at"], automatic["fingerprint"]
    service.apply_overrides(event_id, {"expected_revision": current["revision"], "operations": [operation("start_time", "11:00", bound_date="2027-01-07", note="전화로 변경 공지의 실제 시작 시각 확인", resolves_fingerprint=fp)]})
    assert service.event_detail(event_id)["event"]["publication"]["sales_ready"]
    service.save_research(i["id"], s["id"], research)
    current = service.event_detail(event_id)["event"]
    assert current["fields"]["start_time"]["automatic"]["conflict_detected_at"] == detected
    assert current["publication"]["calendar_time"] == "11:00"
    third = live_document(service, i, clock="오후 1시", url="https://school.gen.go.kr/notice/3")
    service.save_research(i["id"], s["id"], result_for(third, i, s))
    assert service.event_detail(event_id)["event"]["publication"]["calendar_time"] is None


def test_failed_recheck_preserves_original_confirmation_and_body(service):
    service.settings = {"auto_rule_ids": ["explicit_notice"]}
    s, i = season(service), institution(service)
    original = live_document(service, i)
    i = service.get_institution(i["id"])
    saved = service.save_research(i["id"], s["id"], result_for(original, i, s))
    event_id = saved["event_ids"][0]
    before = service.event_detail(event_id)["event"]["publication"]
    failure = empty_document(i, original["display_url"])
    failed_at = utcnow()
    failure.update(status="access_failed", last_checked_at=failed_at, recheck={"status": "failed", "attempted_at": failed_at, "reason_codes": ["connection_reset"]})
    service.save_research(i["id"], s["id"], {"documents": [failure], "candidates": []})
    detail = service.event_detail(event_id)
    after = detail["event"]["publication"]
    assert after["calendar_date"] == before["calendar_date"]
    assert after["calendar_time"] == before["calendar_time"]
    assert not after["sales_ready"]
    assert after["field_permissions"]["event_date"]["confirmed_at"] == before["field_permissions"]["event_date"]["confirmed_at"]
    assert detail["documents"][0]["segments"] == original["segments"]
    assert detail["documents"][0]["recheck"]["status"] == "failed"
    # 전화는 고장 난 웹 경로와 독립적이다.
    operations = [operation("ceremony_status", "scheduled"), operation("event_date", {"precision": "day", "date": "2027-01-07", "month": None}), operation("start_time", "10:00", bound_date="2027-01-07")]
    service.apply_overrides(event_id, {"expected_revision": detail["event"]["revision"], "operations": operations})
    assert service.event_detail(event_id)["event"]["publication"]["sales_ready"]


def test_first_manual_difference_has_resolvable_detection_time(service):
    service.settings = {"auto_rule_ids": ["explicit_notice"]}
    s, i = season(service), institution(service)
    doc = live_document(service, i)
    i = service.get_institution(i["id"])
    saved = service.save_research(i["id"], s["id"], result_for(doc, i, s))
    event_id = saved["event_ids"][0]
    e = service.event_detail(event_id)["event"]
    service.apply_overrides(event_id, {"expected_revision": e["revision"], "operations": [operation("start_time", "11:00", bound_date="2027-01-07")]})
    e = service.event_detail(event_id)["event"]
    assert e["fields"]["start_time"]["automatic"]["conflict_detected_at"]
    assert e["publication"]["calendar_time"] is None
    service.apply_overrides(event_id, {"expected_revision": e["revision"], "operations": [operation("start_time", "11:00", bound_date="2027-01-07", resolves_fingerprint=e["fields"]["start_time"]["automatic"]["fingerprint"], note="공지와 다르지만 담당자에게 실제 시작 시각 확인")]})
    assert service.event_detail(event_id)["event"]["publication"]["calendar_time"] == "11:00"


def test_changed_source_without_new_support_withholds_current_sales(service):
    import hashlib
    service.settings = {"auto_rule_ids": ["explicit_notice"]}
    s, i = season(service), institution(service)
    original = live_document(service, i)
    i = service.get_institution(i["id"])
    saved = service.save_research(i["id"], s["id"], result_for(original, i, s))
    changed = deepcopy(original)
    changed["id"] = new_id()
    changed["segments"][0]["text"] = "시험학교 2026학년도 졸업식은 취소되었습니다."
    changed["content_hash"] = hashlib.sha256(changed["segments"][0]["text"].encode()).hexdigest()
    changed["retrieved_at"] = changed["last_checked_at"] = utcnow()
    changed["recheck"] = {"status": "succeeded", "attempted_at": changed["retrieved_at"], "reason_codes": []}
    result = result_for(changed, i, s)
    assert result["candidates"][0]["candidate"]["ceremony_status"] == "cancelled"
    service.save_research(i["id"], s["id"], result)
    after = service.event_detail(saved["event_ids"][0])["event"]["publication"]
    assert after["calendar_date"] is None
    assert not after["sales_ready"]
    assert after["state"] == "excluded"


def test_cancellation_warning_holds_event_until_real_reconfirmation(service):
    import hashlib
    service.settings = {"auto_rule_ids": ["explicit_notice"]}
    s, i = season(service), institution(service)
    original = live_document(service, i)
    i = service.get_institution(i["id"])
    saved = service.save_research(i["id"], s["id"], result_for(original, i, s))
    event_id = saved["event_ids"][0]
    changed = deepcopy(original)
    changed.update(id=new_id(), retrieved_at=utcnow())
    changed["segments"][0]["text"] = "시험학교 2026학년도 졸업식은 취소되었습니다."
    changed["content_hash"] = hashlib.sha256(changed["segments"][0]["text"].encode()).hexdigest()
    result = {"documents": [changed], "candidates": [], "event_blockers": [{"source_key": changed["source_key"], "document_id": changed["id"], "reason_codes": ["cancelled_notice"]}]}
    service.save_research(i["id"], s["id"], result)
    e = service.event_detail(event_id)["event"]
    assert not e["publication"]["calendar_eligible"]
    assert "cancelled_notice" in e["fields"]["ceremony_status"]["reason_codes"]
    fp = e["fields"]["ceremony_status"]["automatic"]["fingerprint"]
    ops = [operation("ceremony_status", "scheduled", resolves_fingerprint=fp, note="담당자에게 취소 공지 오류임을 재확인"), operation("event_date", {"precision": "day", "date": "2027-01-07", "month": None}), operation("start_time", "10:00", bound_date="2027-01-07")]
    service.apply_overrides(event_id, {"expected_revision": e["revision"], "operations": ops})
    assert service.event_detail(event_id)["event"]["publication"]["sales_ready"]
    service.save_research(i["id"], s["id"], result)
    assert service.event_detail(event_id)["event"]["publication"]["sales_ready"]


def test_repeated_failure_after_recovery_is_not_hidden_by_error_deduplication(service):
    service.settings = {"auto_rule_ids": ["explicit_notice"]}
    s, i = season(service), institution(service)
    doc = live_document(service, i)
    i = service.get_institution(i["id"])
    saved = service.save_research(i["id"], s["id"], result_for(doc, i, s))
    event_id = saved["event_ids"][0]
    def fail():
        failure = empty_document(i, doc["display_url"])
        now = utcnow()
        failure.update(status="access_failed", last_checked_at=now, recheck={"status": "failed", "attempted_at": now, "reason_codes": ["connection_reset"]})
        service.save_research(i["id"], s["id"], {"documents": [failure], "candidates": []})
    fail()
    assert not service.event_detail(event_id)["event"]["publication"]["sales_ready"]
    recovered = live_document(service, i)
    service.save_research(i["id"], s["id"], result_for(recovered, i, s))
    assert service.event_detail(event_id)["event"]["publication"]["sales_ready"]
    fail()
    assert not service.event_detail(event_id)["event"]["publication"]["sales_ready"]


def test_official_http_homepage_requires_https_final_acquisition(service):
    service.settings = {"auto_rule_ids": [f"explicit_notice@{REGISTERED_RULES['explicit_notice']}"]}
    s, i = season(service), institution(service)
    document = live_document(service, i)
    channel = service.get_institution(i["id"])["official_channels"][0]
    channel["url"] = "http://school.gen.go.kr"
    with service.store.transaction(True) as conn:
        conn.execute("UPDATE institutions SET official_channels_json=? WHERE id=?", (dumps([channel]), i["id"]))
    document["authority_evidence"]["channel_url"] = channel["url"]
    document["display_url"] = document["source_key"] = "http://school.gen.go.kr/notice/1"
    i = service.get_institution(i["id"])
    saved = service.save_research(i["id"], s["id"], result_for(document, i, s))
    assert service.event_detail(saved["event_ids"][0])["event"]["publication"]["sales_ready"]
    with service.store.transaction(True) as conn:
        doc_id = saved["document_ids"][0]
        raw = dict(conn.execute("SELECT acquisition_json FROM documents WHERE id=?", (doc_id,)).fetchone())
        import json
        acquisition = json.loads(raw["acquisition_json"])
        acquisition["final_url"] = "http://school.gen.go.kr/notice/1"
        conn.execute("UPDATE documents SET acquisition_json=? WHERE id=?", (dumps(acquisition), doc_id))
    assert not service.event_detail(saved["event_ids"][0])["event"]["publication"]["calendar_eligible"]


def test_same_body_redirect_to_unverified_host_does_not_refresh_official_support(service):
    service.settings = {"auto_rule_ids": ["explicit_notice"]}
    s, i = season(service), institution(service)
    doc = live_document(service, i)
    i = service.get_institution(i["id"])
    saved = service.save_research(i["id"], s["id"], result_for(doc, i, s))
    event_id = saved["event_ids"][0]
    moved = deepcopy(doc)
    moved.update(id=new_id(), authority="unverified", authority_evidence=None, retrieved_at=utcnow())
    moved["acquisition"]["final_url"] = "https://other.example.org/notice"
    moved["recheck"] = {"status": "succeeded", "attempted_at": moved["retrieved_at"], "reason_codes": []}
    service.save_research(i["id"], s["id"], {"documents": [moved], "candidates": []})
    publication = service.event_detail(event_id)["event"]["publication"]
    assert publication["calendar_date"] == "2027-01-07"
    assert not publication["sales_ready"]
