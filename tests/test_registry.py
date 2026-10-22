"""공식 파일 적재의 지역·유형·출처·완전성 검사를 검증한다."""

import hashlib
import json
from copy import deepcopy

import pytest

from app.integrations.providers import ProviderError
from app.integrations.registry import TYPES, district_for, load_official_file, neis_registry, validate_registry_import
from app.integrations.registry import normalize_homepage


def payload():
    source = {"provider": "official-file", "source_name": "공식 목록 검사", "official_url": "https://data.go.kr/official-file",
        "as_of": "2026-10-01", "scope_types": sorted(TYPES), "scope_districts": ["북구"], "coverage_complete": False,
        "completeness_note": "정규화 형식 검사 표본. 실제 목록 정확성 증거 아님.", "expected_total": None, "source_hash": "a" * 64}
    rows = [{"external_key": kind + ":1", "name": f"{kind} 기관", "type": kind, "district": "북구",
        "address": "광주광역시 북구 검증로 1", "campus_label": "광주캠퍼스" if kind in {"university", "junior_college"} else None,
        "phone": None, "homepage_url": None, "operation_status": "unknown",
        "operation_effective_date": None, "operation_evidence_url": None} for kind in sorted(TYPES)]
    return {"schema_version": "4.0", "source": source, "rows": rows}


def test_official_file_contract_supports_every_target_type():
    original = payload()
    preview = validate_registry_import(original)
    assert preview["accepted_rows"] == 9 and not preview["rejected_rows"]
    assert preview["source_verified"] is False and preview["coverage_complete"] is False
    assert original == payload()


@pytest.mark.parametrize("raw", [None,"None","null","","https://none/","http://null/"])
def test_missing_homepage_never_becomes_a_clickable_host(raw):
    """공식 명부의 결측값으로 가짜 학교 홈페이지를 만들지 않는다."""
    assert normalize_homepage(raw) is None


def test_real_homepage_keeps_original_scheme_or_adds_missing_scheme():
    assert normalize_homepage("http://school.gen.hs.kr/") == "http://school.gen.hs.kr/"
    assert normalize_homepage("school.gen.hs.kr") == "https://school.gen.hs.kr"


def test_duplicate_outside_gwangju_and_unknown_campus_remain_rejected():
    data = payload()
    data["rows"][0]["address"] = "경기도 광주시 북구 검증로 1"
    university = next(row for row in data["rows"] if row["type"] == "university")
    university["campus_label"] = None
    data["rows"].append(deepcopy(data["rows"][1]))
    result = validate_registry_import(data)
    assert len(result["rejected_rows"]) == 3
    assert district_for("경기도 광주시") is None
    assert district_for("광주광역시 광산구 첨단로 1") == "광산구"
    assert district_for("전남광주통합특별시 광산구 산월로 12-17") == "광산구"
    assert district_for("전남광주통합특별시 목포시 영산로 1") is None


@pytest.mark.parametrize("change", ["count", "date", "note", "duplicate"])
def test_incomplete_or_inconsistent_file_cannot_claim_complete(change):
    data = payload()
    data["source"].update(coverage_complete=True, expected_total=9)
    if change == "count":
        data["source"]["expected_total"] = 100
    elif change == "date":
        data["source"]["as_of"] = None
    elif change == "note":
        data["source"]["completeness_note"] = ""
    else:
        data["rows"].append(deepcopy(data["rows"][0]))
    with pytest.raises(ProviderError) as caught:
        validate_registry_import(data)
    assert caught.value.code == "coverage_incomplete"


def test_official_json_preserves_raw_file_hash(tmp_path):
    original = payload()
    path = tmp_path / "registry.json"
    raw = json.dumps(original["rows"], ensure_ascii=False).encode()
    path.write_bytes(raw)
    imported = load_official_file(path, original["source"])
    assert imported["source"]["source_hash"] == hashlib.sha256(raw).hexdigest()
    assert imported["rows"] == original["rows"] and path.read_bytes() == raw


def test_csv_stable_key_does_not_merge_same_name_at_other_address(tmp_path):
    path = tmp_path / "registry.csv"
    path.write_text("name,type,district,address\n동일이름,kindergarten,북구,광주광역시 북구 첫길 1\n동일이름,kindergarten,북구,광주광역시 북구 다른길 2\n", encoding="utf-8")
    imported = load_official_file(path, payload()["source"])
    assert imported["rows"][0]["external_key"] != imported["rows"][1]["external_key"]
    assert validate_registry_import(imported)["accepted_rows"] == 2


def test_demo_file_cannot_be_promoted_to_live():
    data = payload()
    data["source"]["provider"] = "demo-source"
    with pytest.raises(ProviderError):
        validate_registry_import(data)


def test_neis_unknown_school_kind_fails_without_claiming_full_coverage():
    rows = [{"ATPT_OFCDC_SC_CODE": "F10", "SD_SCHUL_CODE": "123", "SCHUL_NM": "검증학교",
             "ORG_RDNMA": "광주광역시 북구 검증로 1", "SCHUL_KND_SC_NM": "새로운학교유형"}]
    with pytest.raises(ProviderError) as caught:
        neis_registry(rows)
    assert caught.value.code == "coverage_incomplete"
