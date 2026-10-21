"""공시 인원의 학교·학년도·학년 관계와 키 없는 공개 표 경로를 검사한다."""

import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.domain import validate
from app.claims import validate_count
from app.providers_student import API_URL, GRADE_URL, PROFILE_URL, StudentProvider


INSTITUTION = {
    "id": str(uuid4()), "name": "건국초등학교", "type": "elementary", "district": "북구",
    "address": "전남광주통합특별시 북구 첨단연신로108번길 86", "official_channels": [],
}
IDENTIFIERS = [{"provider": "neis", "external_key": "F10:7392163"}]
PROFILE = {
    "SCHUL_CODE": "S050001287", "SCHUL_NM": "건국초등학교", "SCHUL_KND_SC_CODE": "02",
    "SCHUL_RDNMA": "광주광역시 북구 첨단연신로108번길 86", "ADRCD_CD": "1230013000",
}
ROW = {
    "SCHUL_CODE": "S050001287", "SCHUL_NM": "건국초등학교", "SCHUL_KND_SC_CODE": "02",
    "ADRCD_CD": "1230013000", "PBAN_EXCP_YN": "N", "COL_S6": 188, "COL_S_SUM": 971,
}
UUID = "fe880c33-60f2-4b15-bc82-a11bc022cdc2"


class Gateway:
    """고정 공식 응답과 요청 인자를 보관하는 네트워크 대체체."""

    meter = None

    def __init__(self, handler):
        self.handler, self.calls = handler, []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, deepcopy(kwargs)))
        data = self.handler(method, url, kwargs)
        if isinstance(data, dict):
            return httpx.Response(200, json=data, request=httpx.Request(method, url))
        return httpx.Response(200, text=data, request=httpx.Request(method, url))


def api_provider(profile=None, row=None):
    """학교기본정보와 학년 인원을 서로 다른 원문으로 제공한다."""
    profile = PROFILE if profile is None else profile
    row = ROW if row is None else row

    def handler(_method, url, args):
        if url != API_URL:
            return "<html>접근 실패</html>"
        selected = profile if args["params"]["apiType"] == "0" else row
        return {"resultCode": "success", "list": selected if isinstance(selected, list) else [selected]}
    gateway = Gateway(handler)
    provider = StudentProvider(SimpleNamespace(secrets={"schoolinfo_api_key": "not-a-real-secret"}), gateway=gateway)
    return provider, gateway


@pytest.mark.asyncio
async def test_api_matches_year_grade_address_and_district_without_disclosing_key():
    provider, gateway = api_provider()
    result = await provider.lookup(INSTITUTION, {"academic_year": 2026}, IDENTIFIERS)
    validate("StudentResult", result)
    assert result["status"] == "ok" and result["counts"][0]["value"] == 188
    assert result["counts"][0]["value"] != ROW["COL_S_SUM"]
    assert result["counts"][0]["academic_year"] == 2026
    assert result["counts"][0]["source_field"] == "COL_S6"
    assert len(gateway.calls) == 2
    assert all(call[2]["params"]["sggCode"] == "12300" for call in gateway.calls)
    assert gateway.calls[1][2]["params"]["pbanYr"] == "2026"
    assert "not-a-real-secret" not in json.dumps(result)
    metadata = json.loads(result["documents"][0]["segments"][0]["text"])
    assert metadata["identity"]["school_code"] == PROFILE["SCHUL_CODE"]
    assert validate_count(result["counts"][0], result["documents"][0], INSTITUTION, {"academic_year": 2026})["allowed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_field,value", [
    ("SCHUL_NM", "건국초등학교병설유치원"), ("SCHUL_RDNMA", "광주광역시 북구 다른길 86"),
    ("SCHUL_KND_SC_CODE", "03"), ("ADRCD_CD", "1233013000"),
])
async def test_api_identity_cannot_be_joined_by_name_alone(profile_field, value):
    profile = {**PROFILE, profile_field: value}
    provider, _ = api_provider(profile=profile)
    result = await provider.lookup(INSTITUTION, {"academic_year": 2026}, IDENTIFIERS)
    assert result["counts"] == [] and "schoolinfo_api_wrong_source" in result["reason_codes"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value,reason", [
    ("SCHUL_CODE", "another-school", "not_found"),
    ("SCHUL_NM", "다른초등학교", "wrong_source"),
    ("SCHUL_KND_SC_CODE", "04", "wrong_source"),
    ("ADRCD_CD", "1233013000", "wrong_source"),
    ("PBAN_EXCP_YN", "Y", "not_disclosed"),
    ("COL_S6", None, "not_found"),
    ("COL_S6", "188(5)", "not_found"),
    ("COL_S6", 188.2, "not_found"),
])
async def test_wrong_school_excluded_or_noninteger_count_is_not_published(field, value, reason):
    provider, _ = api_provider(row={**ROW, field: value})
    result = await provider.lookup(INSTITUTION, {"academic_year": 2026}, IDENTIFIERS)
    assert result["counts"] == [] and f"schoolinfo_api_{reason}" in result["reason_codes"]


@pytest.mark.asyncio
async def test_duplicate_public_disclosure_rows_do_not_silently_choose_one():
    provider, _ = api_provider(row=[ROW, {**ROW, "COL_S6": 189}])
    result = await provider.lookup(INSTITUTION, {"academic_year": 2026}, IDENTIFIERS)
    assert not result["counts"] and "schoolinfo_api_source_conflict" in result["reason_codes"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,code", [("middle", "03"), ("high", "04")])
async def test_middle_and_high_use_third_grade_instead_of_school_total(kind, code):
    school = {**INSTITUTION, "type": kind, "name": "고려" + ("중학교" if kind == "middle" else "고등학교")}
    profile = {**PROFILE, "SCHUL_NM": school["name"], "SCHUL_KND_SC_CODE": code}
    row = {**ROW, "SCHUL_NM": school["name"], "SCHUL_KND_SC_CODE": code, "COL_S3": 312}
    provider, _ = api_provider(profile=profile, row=row)
    result = await provider.lookup(school, {"academic_year": 2026}, IDENTIFIERS)
    assert result["counts"][0]["value"] == 312 and result["counts"][0]["grade"] == 3


@pytest.mark.asyncio
async def test_university_requires_event_count_and_does_not_call_school_api():
    provider, gateway = api_provider()
    result = await provider.lookup({**INSTITUTION, "type": "university"}, {"academic_year": 2026}, [])
    assert result["counts"] == [] and gateway.calls == []


@pytest.mark.asyncio
async def test_explicit_zero_is_preserved_and_cached_rows_keep_actual_fetch_time(monkeypatch):
    """미공개와 공식 0명을 구분하고 캐시 읽기를 새 확인으로 표시하지 않는다."""
    times = iter(["2026-10-21T01:00:00Z", "2026-10-21T01:00:01Z"])
    monkeypatch.setattr("app.providers_student.utcnow", lambda: next(times))
    provider, gateway = api_provider(row={**ROW, "COL_S6": 0})
    first = await provider.lookup(INSTITUTION, {"academic_year": 2026}, IDENTIFIERS)
    again = await provider.lookup(INSTITUTION, {"academic_year": 2026}, IDENTIFIERS)
    assert first["counts"][0]["value"] == again["counts"][0]["value"] == 0
    assert first["documents"][0]["retrieved_at"] == again["documents"][0]["retrieved_at"] == "2026-10-21T01:00:01Z"
    assert len(gateway.calls) == 2


def public_provider(*, year=2026, neis_code="7392163", sum_value=188, sex_header="전체학생"):
    """공식 교육청→학교 코드→학년 표의 세 단계를 모사한다."""
    def handler(_method, url, _args):
        if "www.gen.go.kr" in url:
            values = ["건국초등학교", "https://geonkuk.gen.es.kr", "공립", "61085",
                      "광주광역시 북구 첨단연신로108번길 86", "", "", "",
                      "http://www.schoolinfo.go.kr/index.jsp?HG_CD=F100001287"]
            return "<table><tr>" + "".join(f"<td>{value}</td>" for value in values) + "</tr></table>"
        if url.startswith(PROFILE_URL):
            return (f"<html><title>건국초등학교 학교정보</title><input name='SHL_IDF_CD' value='{UUID}'>"
                    f'<script>var sdSchulCode = "{neis_code}";</script></html>')
        if url == GRADE_URL:
            headers = "".join(f"<th>{grade}학년</th>" for grade in range(1, 7))
            sexes = "".join(f"<th>{label}</th>" for label in ["남학생", "여학생", sex_header] * 6)
            values = "".join(f"<td>{value}</td>" for value in ([50, 50, 100] * 5 + [93, 95, sum_value]))
            return (f"<h2>건국초등학교 {year}학년도</h2><select id='select_trans_dt'>"
                    f"<option value='{year}2' selected>{year}</option></select>"
                    f"<table class='hidden_table'><caption>차트의 대체 텍스트로 1학년, 2학년, 3학년의 정보를 제공한다.</caption>"
                    f"<tr>{headers}</tr><tr>{sexes}</tr><tr>{values}</tr></table>")
        raise AssertionError(url)
    gateway = Gateway(handler)
    return StudentProvider(SimpleNamespace(secrets={}), gateway=gateway), gateway


@pytest.mark.asyncio
async def test_public_table_works_without_key_and_preserves_exact_school_and_year():
    provider, gateway = public_provider()
    result = await provider.lookup(INSTITUTION, {"academic_year": 2026}, IDENTIFIERS)
    validate("StudentResult", result)
    assert result["counts"][0]["value"] == 188 and result["counts"][0]["source"] == "schoolinfo_public"
    assert len(gateway.calls) == 3
    assert gateway.calls[-1][2]["data"]["SHL_IDF_CD"] == UUID
    assert validate_count(result["counts"][0], result["documents"][0], INSTITUTION, {"academic_year": 2026})["allowed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("options,reason", [
    ({"year": 2025}, "different_academic_year"),
    ({"neis_code": "7399999"}, "wrong_source"),
    ({"sum_value": 190}, "parse_failed"),
    ({"sex_header": "학급수"}, "unsupported_format"),
])
async def test_public_table_rejects_old_year_other_school_and_wrong_column(options, reason):
    provider, _ = public_provider(**options)
    result = await provider.lookup(INSTITUTION, {"academic_year": 2026}, IDENTIFIERS)
    assert result["counts"] == [] and reason in result["reason_codes"]


@pytest.mark.asyncio
@pytest.mark.parametrize("source,mutation", [
    ("api", "amount"), ("api", "year"), ("api", "type"), ("api", "excluded"),
    ("api", "address"), ("api", "district"), ("api", "raw_hash"),
    ("public", "amount"), ("public", "year"), ("public", "type"), ("public", "address"),
    ("public", "grade_values"), ("public", "school_code"), ("public", "selected_year"),
    ("public", "other_profile"), ("public", "directory"),
])
async def test_common_validator_independently_rejects_tampered_provider_results(source, mutation):
    """자료원이 후보를 만들었어도 공통 경로는 원문·학교·연도 관계를 다시 검사한다."""
    provider, _ = api_provider() if source == "api" else public_provider()
    result = await provider.lookup(INSTITUTION, {"academic_year": 2026}, IDENTIFIERS)
    document, claim = result["documents"][0], result["counts"][0]
    segments = {s["id"]: s for s in document["segments"]}
    meta = json.loads(segments["metadata"]["text"])
    if mutation == "amount":
        claim["value"] += 1
    elif mutation == "year":
        claim["academic_year"] = 2025
    elif mutation == "type":
        meta["identity"]["school_type"] = "04"
    elif mutation == "address":
        meta["identity"]["address"] = "광주광역시 북구 다른길 10"
    elif mutation == "raw_hash":
        document["content_hash"] = "0" * 64
    elif mutation in {"excluded", "district"}:
        row = json.loads(segments["row"]["text"])
        row["PBAN_EXCP_YN" if mutation == "excluded" else "ADRCD_CD"] = "Y" if mutation == "excluded" else "1233013000"
        segments["row"]["text"] = json.dumps(row, ensure_ascii=False)
        claim["evidence"][0]["quote"] = segments["row"]["text"]
    elif mutation == "grade_values":
        meta["grade_values"][-1] = 200
    elif mutation == "school_code":
        meta["identity"]["school_code"] = "F100099999"
    elif mutation == "selected_year":
        meta["selected_year"] = 2025
    elif mutation in {"other_profile", "directory"}:
        profile = json.loads(segments["profile"]["text"])
        if mutation == "other_profile":
            profile["neis_source"] = 'var sdSchulCode = "7399999"'
        else:
            profile["directory_cells"] = []
        segments["profile"]["text"] = json.dumps(profile, ensure_ascii=False)
    segments["metadata"]["text"] = json.dumps(meta, ensure_ascii=False)
    assert not validate_count(claim, document, INSTITUTION, {"academic_year": 2026})["allowed"]
