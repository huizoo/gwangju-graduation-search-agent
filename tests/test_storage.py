"""지속 저장·revision·기관 목록 원자성을 검사한다."""
from pathlib import Path
from datetime import datetime, timezone
import pytest
from app.domain import ApiError
from app.rules import REGISTERED_RULES
from app.storage import Store, Service


@pytest.fixture
def service(tmp_path):
    return Service(Store(tmp_path / "test.sqlite3"))


def season(service):
    return service.create_season({"label": "2026학년도", "academic_year": 2026, "search_start": "2026-12-01", "search_end": "2027-03-31", "export_key": "year2026", "timezone": "Asia/Seoul", "region_profile": "gwangju_5_gu"})


def institution(service):
    return service.create_institution({"name": "시험학교", "type": "high", "district": "북구", "address": "광주광역시 북구 시험로 1", "campus_label": None, "phone": None, "homepage_url": None})


def operation(field, value, **kwargs):
    return {"field": field, "action": "set", "value": value, "basis": "phone_confirmed", "confirmed_at": datetime.now(timezone.utc).isoformat(), "note": "", "resolves_fingerprint": None, "source_url": None, **kwargs}


def event(service, date="2027-01-07", confirmed=True):
    s, i = season(service), institution(service)
    operations = [operation("ceremony_status", "scheduled"), operation("event_date", {"precision": "day", "date": date, "month": None}), operation("start_time", "10:00", bound_date=date)]
    if not confirmed:
        for item in operations:
            item["confirmed_at"] = None
    e = service.create_event({"institution_id": i["id"], "season_id": s["id"], "title": "졸업식", "event_type": "graduation_ceremony", "audience": None, "campus_label": None, "allow_separate_event": False, "operations": operations})
    return e, s, i


def registry_payload():
    return {"schema_version": "4.0", "source": {"provider": "test-official-file", "source_name": "검사 전용 목록", "official_url": "https://www.gen.go.kr/", "as_of": "2026-10-20", "scope_types": ["high"], "scope_districts": ["북구"], "coverage_complete": False, "completeness_note": "일부 목록", "expected_total": 1, "source_hash": "a" * 64}, "rows": [{"external_key": "test1", "name": "시험학교", "type": "high", "district": "북구", "address": "광주광역시 북구 시험로 1", "campus_label": None, "phone": None, "homepage_url": None, "operation_status": "active", "operation_effective_date": None, "operation_evidence_url": None}]}


def test_nested_transaction_rolls_back_everything(service):
    original = service.store.revision()
    with pytest.raises(RuntimeError):
        with service.store.transaction(True):
            institution(service)
            raise RuntimeError("abort")
    assert service.list_institutions()["total"] == 0
    assert service.store.revision() == original
    with service.store.transaction():
        with pytest.raises(RuntimeError):
            institution(service)


def test_revision_restart_backup_and_snapshot(service, tmp_path):
    e, s, i = event(service)
    reopened = Service(Store(service.store.path))
    assert reopened.event_detail(e["id"])["event"]["publication"]["sales_ready"]
    snap = reopened.snapshot(s["id"])
    assert snap["content_revision"] == snap["overview"]["content_revision"]
    assert snap["overview"]["date_confirmed_institutions"] == 1
    with pytest.raises(ApiError) as failure:
        service.apply_overrides(e["id"], {"expected_revision": 999, "operations": [{"field": "venue", "action": "clear", "note": ""}]})
    assert failure.value.status == 409
    path = service.store.backup(tmp_path / "backup.sqlite3")
    assert Service(Store(path)).events(s["id"], "sales")["total"] == 1


def test_registry_preview_commit_and_user_profile_protection(service):
    existing = institution(service)
    service.patch_institution(existing["id"], {"expected_revision": 1, "set": {"phone": "062-123-4567"}, "release_fields": [], "note": "직접 확인"})
    preview = service.registry_preview(registry_payload())
    assert preview["duplicate_candidates"]
    payload = {"expected_preview_hash": preview["preview_hash"], "expected_content_revision": preview["content_revision"], "allow_partial": False, "mappings": [{"row_index": 0, "decision": "update", "institution_id": existing["id"]}]}
    result = service.registry_commit(preview["id"], payload, "key1")
    assert service.registry_commit(preview["id"], payload, "key1") == result
    assert service.get_institution(existing["id"])["phone"] == "062-123-4567"
    assert service.get_institution(existing["id"])["profile_original"]["phone"] is None
    assert service.get_institution(existing["id"])["official_channels"] == []


def test_stale_registry_preview_rejected(service):
    preview = service.registry_preview(registry_payload())
    institution(service)
    with pytest.raises(ApiError) as failure:
        service.registry_commit(preview["id"], {"expected_preview_hash": preview["preview_hash"], "expected_content_revision": preview["content_revision"], "allow_partial": False, "mappings": []})
    assert failure.value.code == "preview_changed"
    assert service.registry_snapshots()["total"] == 0


def test_runtime_settings_pin_rule_version(service):
    from types import SimpleNamespace
    service.settings = SimpleNamespace(data={"publication": {"auto_rule_ids": ["explicit_notice"]}})
    assert service._enabled_rules() == {}
    version = REGISTERED_RULES["explicit_notice"]
    service.settings.data["publication"]["auto_rule_ids"] = [f"explicit_notice@{version}"]
    assert service._enabled_rules() == {"explicit_notice": version}
    service.settings.data["publication"]["auto_rule_ids"] = ["explicit_notice@99.0.0"]
    assert service._enabled_rules() == {}
