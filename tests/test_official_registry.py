"""실제 확보한 명부의 재검증과 변조·잘못된 재승인에 대한 방어를 검사한다."""

import hashlib
import json
import shutil
from pathlib import Path
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from bs4 import BeautifulSoup

import app.official_registry as official
from app.domain import ApiError
from app.registry import validate_registry_import
from app.providers import ProviderError

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs/evidence/non-school-registry.json"
ASSETS = ROOT / "assets/registry"


def test_public_copies_redact_only_unneeded_fields_and_report_capture_provenance():
    """정제 사본을 원본과 구분하며 기관 행 검증과 취득 시각을 유지한다."""
    manifest = json.loads(MANIFEST.read_text())
    entries = {Path(entry["path"]).name: entry for entry in manifest["artifacts"]}
    directory = BeautifulSoup((ASSETS / "gen-kindergarten-directory.html").read_bytes(), "html.parser")
    csrf = directory.select('input[name="CSRFToken"]')
    assert len(csrf) == 2 and all(field["value"] == "[REDACTED_CSRF]" for field in csrf)
    profile = BeautifulSoup((ASSETS / "hana-summary.html").read_bytes(), "html.parser")
    director = next(th for th in profile.find_all("th") if th.get_text(strip=True) == "원장명")
    assert director.find_next_sibling("td").get_text(strip=True) == "[비식별 처리]"
    for filename, import_file, expected_rows in [
        ("gen-kindergarten-directory.html", official.IMPORT_FILES[0], 256),
        ("hana-summary.html", official.IMPORT_FILES[3], 1),
    ]:
        entry = entries[filename]
        assert entry["public_copy"]["source_rows_unchanged"] is True
        assert entry["public_copy"]["original_sha256"] != entry["sha256"]
        result = official.load_official_registry(MANIFEST, import_file, artifact_dir=ASSETS)
        assert len(result.registry["rows"]) == expected_rows
        assert result.provenance["reviewed_at"] == manifest["checked_at"]
        assert result.provenance["network_refetched"] is False
        assert result.provenance["sanitized_artifacts"] == [
            {"filename": filename, "public_copy_sha256": entry["sha256"], **entry["public_copy"]}
        ]


def _university_rows():
    """이미 검토한 공식 HTML의 18개 열을 다운로드 JSON 열로 옮긴다."""
    soup = BeautifulSoup((ASSETS / "universities-gwangju.bin").read_bytes(), "html.parser")
    table = next(table for table in soup.select("table")
                 if [th.get_text(" ", strip=True) for th in table.select("th")][:1] == ["학교명"])
    return [dict(zip(official.HigherEducationProvider.COLUMNS,
                     [td.get_text(" ", strip=True) for td in tr.select("td")], strict=True))
            for tr in table.select("tr") if tr.select("td")]


class _DownloadGateway:
    """공식 다운로드 총건수와 각 페이지의 계약을 독립적으로 반환한다."""

    def __init__(self, rows, mutate=None):
        self.rows, self.mutate, self.calls = rows, mutate, []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url == official.HigherEducationProvider.HEADER_URL:
            body = {"fileName": "전국대학및전문대학정보표준데이터", "totalCount": len(self.rows),
                    "tableVO": {"colNmList": list(official.HigherEducationProvider.COLUMNS),
                                "svcTableNm": "tn_pubr_public_univ_info_svc"}}
        else:
            page = kwargs["params"]["page"]
            body = deepcopy(self.rows[(page - 1) * 1000:page * 1000])
        if self.mutate:
            body = self.mutate(url, body)
        return httpx.Response(200, json=body, request=httpx.Request(method, url))


@pytest.mark.asyncio
async def test_live_download_preserves_existing_campus_keys_and_reference_year():
    """수집일을 기준일로 바꾸지 않고 기존 18개 기관 식별자를 유지한다."""
    provider = official.HigherEducationProvider(SimpleNamespace(), gateway=_DownloadGateway(_university_rows()))
    result = await provider.registry()
    original = official.load_official_registry(MANIFEST, official.IMPORT_FILES[1], artifact_dir=ASSETS)
    assert len(result.registry["rows"]) == 18 and result.registry["source"]["coverage_complete"]
    assert {row["external_key"] for row in result.registry["rows"]} == {
        row["external_key"] for row in original.registry["rows"]}
    assert result.provenance["reference_year"] == "2025"
    assert result.registry["source"]["as_of"] == "2026-03-18"
    assert result.provenance["by_type"] == {"junior_college": 7, "university": 11}
    assert result.provenance["national_raw_rows"] == 79
    assert validate_registry_import(result.registry)["accepted_rows"] == 18


@pytest.mark.asyncio
async def test_download_all_pages_before_filtering_gwangju():
    """광주 행이 뒤쪽 페이지에 있어도 모든 공식 페이지를 조회한다."""
    rows = _university_rows()
    foreign = {**rows[0], "CTPV_NM": "서울특별시", "LCTN_ROAD_NM_ADDR": "서울특별시 강남구 학교로 1"}
    gateway = _DownloadGateway([{**foreign, "SCHL_NM": str(index)} for index in range(1000)] + rows)
    result = await official.HigherEducationProvider(SimpleNamespace(), gateway=gateway).registry()
    assert result.provenance["national_raw_rows"] == 1079 and len(result.registry["rows"]) == 18
    assert [call[2]["params"].get("page") for call in gateway.calls] == [None, 1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["short_page", "columns", "future_date", "duplicate", "missing_campus", "other_kind"])
async def test_live_download_cannot_claim_complete_when_contract_or_rows_disagree(case):
    rows = _university_rows()
    selected = next(row for row in rows if row["UNIV_SE_NM"] == "대학")
    if case == "future_date":
        for row in rows:
            row["CRTR_YMD"] = "2100-01-01"
    if case == "duplicate":
        rows.append(deepcopy(selected))
    if case == "missing_campus":
        selected["MAINBRANCH_NM"] = ""
    if case == "other_kind":
        selected["UNIV_SE_NM"] = "알 수 없는 유형"

    def mutate(url, body):
        if case == "short_page" and url == official.HigherEducationProvider.DATA_URL:
            return body[:-1]
        if case == "columns" and url == official.HigherEducationProvider.HEADER_URL:
            body["tableVO"]["colNmList"] = ["UNTRUSTED"]
        return body
    provider = official.HigherEducationProvider(SimpleNamespace(), gateway=_DownloadGateway(rows, mutate))
    with pytest.raises(ProviderError, match="전체 건수"):
        await provider.registry()


def _write_json(path, value):
    """검사 중 수정한 데이터를 검토 가능한 UTF-8 JSON으로 기록한다."""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def bundle(tmp_path):
    """원본을 보존하며 변조 검사에 사용할 독립 사본을 만든다."""
    manifest = tmp_path / "manifest.json"
    shutil.copyfile(MANIFEST, manifest)
    assets = tmp_path / "assets"
    shutil.copytree(ASSETS, assets)
    return manifest, assets


def _repin_for_negative_test(monkeypatch, bundle, changed_files=()):
    """잘못 검토된 미래 번들도 원문 교차 검사로 거절되는지 시험한다.

    운영 API에는 해시 재승인 인자가 없으며 이 우회는 테스트 monkeypatch에만 존재한다.
    """
    manifest, assets = bundle
    content = json.loads(manifest.read_text())
    for entry in content["artifacts"]:
        if Path(entry["path"]).name in changed_files:
            raw = (assets / Path(entry["path"]).name).read_bytes()
            entry.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    _write_json(manifest, content)
    monkeypatch.setattr(official, "APPROVED_MANIFEST_SHA256", frozenset({
        hashlib.sha256(manifest.read_bytes()).hexdigest(),
    }))


@pytest.mark.parametrize("name,rows,channels,as_of", [
    (official.IMPORT_FILES[0], 256, 169, None),
    (official.IMPORT_FILES[1], 18, 18, "2026-03-18"),
    (official.IMPORT_FILES[2], 250, 0, None),
    (official.IMPORT_FILES[3], 1, 1, None),
])
def test_real_reviewed_bundle_matches_raw_rows_and_existing_import_policy(name, rows, channels, as_of):
    result = official.load_official_registry(MANIFEST, name, artifact_dir=ASSETS)
    assert len(result.registry["rows"]) == rows
    assert len(result.authorities) == channels
    assert result.registry["source"]["as_of"] == as_of
    assert result.registry["source"]["coverage_complete"] is False
    assert result.provenance["network_refetched"] is False
    assert result.provenance["publication_approved"] is False
    checked = validate_registry_import(result.registry)
    assert checked["accepted_rows"] == rows and not checked["rejected_rows"]
    if name == official.IMPORT_FILES[0]:
        assert result.provenance["statistics"]["by_district"] == {
            "동구": 17, "서구": 46, "남구": 38, "북구": 80, "광산구": 75,
        }
        assert result.registry["source"]["expected_total"] == 256
        assert "자료 기준일 미상" in result.registry["source"]["completeness_note"]
        original = json.loads((ASSETS / name).read_text())
        assert original["source"]["coverage_complete"] is True
        assert any(row["external_key"] == "F10001506" for row in result.registry["rows"])
    for evidence in result.authorities:
        matched = next(row for row in result.registry["rows"] if row["external_key"] == evidence["external_key"])
        assert matched["homepage_url"] == evidence["channel"]["url"]
        assert evidence["channel"]["verified_at"] == result.provenance["reviewed_at"]
        assert evidence["source_sha256"] == result.registry["source"]["source_hash"]


def test_user_manifest_with_own_hash_and_official_flag_is_not_a_trust_anchor(bundle):
    manifest, assets = bundle
    content = json.loads(manifest.read_text())
    content["official"] = True
    _write_json(manifest, content)
    with pytest.raises(ApiError, match="서버가 승인"):
        official.load_official_registry(manifest, official.IMPORT_FILES[0], artifact_dir=assets)


@pytest.mark.parametrize("filename", [
    "gen-kindergarten-directory-original.bin",
    "kindergarten-edoffice-registry.json",
    "homepage-authority-provenance.json",
])
def test_changed_raw_normalized_or_sidecar_file_fails_hash(bundle, filename):
    manifest, assets = bundle
    with (assets / filename).open("ab") as stream:
        stream.write(b" ")
    with pytest.raises(ApiError, match="SHA-256"):
        official.load_official_registry(manifest, official.IMPORT_FILES[0], artifact_dir=assets)


def test_file_symlink_outside_selected_bundle_is_rejected(bundle, tmp_path):
    manifest, assets = bundle
    name = official.IMPORT_FILES[0]
    elsewhere = tmp_path / "copied.json"
    shutil.copyfile(assets / name, elsewhere)
    (assets / name).unlink()
    (assets / name).symlink_to(elsewhere)
    with pytest.raises(ApiError, match="지정 폴더"):
        official.load_official_registry(manifest, name, artifact_dir=assets)


@pytest.mark.parametrize("name", ["../universities-registry.json", "kindergarten-welfare-partial-registry.json", "uploaded.json"])
def test_only_four_server_selected_bundle_names_are_supported(name):
    with pytest.raises(ApiError, match="서버에서 제공"):
        official.load_official_registry(MANIFEST, name, artifact_dir=ASSETS)


def test_reapproved_manifest_path_traversal_still_fails(bundle, monkeypatch):
    manifest, assets = bundle
    content = json.loads(manifest.read_text())
    content["artifacts"][0]["path"] = "runtime/non-school-registry/../secret"
    _write_json(manifest, content)
    _repin_for_negative_test(monkeypatch, bundle)
    with pytest.raises(ApiError, match="경로"):
        official.load_official_registry(manifest, official.IMPORT_FILES[0], artifact_dir=assets)


def test_reapproved_manifest_with_lookalike_official_host_still_fails(bundle, monkeypatch):
    manifest, assets = bundle
    content = json.loads(manifest.read_text())
    next(entry for entry in content["artifacts"] if entry["path"].endswith("gen-kindergarten-directory-original.bin"))[
        "source_url"] = "https://www.gen.go.kr.attacker.invalid/s_school/index.php"
    _write_json(manifest, content)
    _repin_for_negative_test(monkeypatch, bundle)
    with pytest.raises(ApiError, match="취득 URL"):
        official.load_official_registry(manifest, official.IMPORT_FILES[0], artifact_dir=assets)


@pytest.mark.parametrize("field,value", [
    ("homepage_url", "https://attacker.invalid/"),
    ("external_key", "F999999999"),
    ("name", "다른 유치원"),
    ("operation_status", "active"),
])
def test_reapproved_normalized_fields_cannot_disagree_with_raw(bundle, monkeypatch, field, value):
    manifest, assets = bundle
    name = official.IMPORT_FILES[0]
    content = json.loads((assets / name).read_text())
    content["rows"][0][field] = value
    _write_json(assets / name, content)
    _repin_for_negative_test(monkeypatch, bundle, [name])
    with pytest.raises(ApiError, match="공식 원문의|계약 조건"):
        official.load_official_registry(manifest, name, artifact_dir=assets)


@pytest.mark.parametrize("field,value,match", [
    ("as_of", "2026-10-20", "자료 기준일"),
    ("expected_total", 255, "전체성"),
    ("coverage_complete", False, "전체성"),
    ("scope_districts", ["북구"], "범위"),
])
def test_reapproved_source_must_preserve_date_scope_and_roster_claim(bundle, monkeypatch, field, value, match):
    manifest, assets = bundle
    name = official.IMPORT_FILES[0]
    content = json.loads((assets / name).read_text())
    content["source"][field] = value
    _write_json(assets / name, content)
    _repin_for_negative_test(monkeypatch, bundle, [name])
    with pytest.raises(ApiError, match=match):
        official.load_official_registry(manifest, name, artifact_dir=assets)


def test_reapproved_kindergarten_page_cannot_disagree_on_district_counts(bundle, monkeypatch):
    manifest, assets = bundle
    name = "gen-kindergarten-directory.html"
    text = (assets / name).read_text()
    assert "북구(80)" in text
    (assets / name).write_text(text.replace("북구(80)", "북구(79)"), encoding="utf-8")
    _repin_for_negative_test(monkeypatch, bundle, [name])
    with pytest.raises(ApiError, match="소계"):
        official.load_official_registry(manifest, official.IMPORT_FILES[0], artifact_dir=assets)


def test_reapproved_sidecar_cannot_move_homepage_to_other_institution(bundle, monkeypatch):
    manifest, assets = bundle
    name = "homepage-authority-provenance.json"
    content = json.loads((assets / name).read_text())
    selected = [entry for entry in content if entry["import_file"] == official.IMPORT_FILES[0]]
    selected[0]["homepage_url"] = selected[1]["homepage_url"]
    selected[0]["institution_name"] = selected[1]["institution_name"]
    _write_json(assets / name, content)
    _repin_for_negative_test(monkeypatch, bundle, [name])
    with pytest.raises(ApiError, match="홈페이지 관계"):
        official.load_official_registry(manifest, official.IMPORT_FILES[0], artifact_dir=assets)


def test_reapproved_university_json_cannot_replace_source_date_with_acquisition_date(bundle, monkeypatch):
    manifest, assets = bundle
    name = official.IMPORT_FILES[1]
    content = json.loads((assets / name).read_text())
    content["source"]["as_of"] = "2026-10-20"
    _write_json(assets / name, content)
    _repin_for_negative_test(monkeypatch, bundle, [name])
    with pytest.raises(ApiError, match="자료 기준일"):
        official.load_official_registry(manifest, name, artifact_dir=assets)


def test_reapproved_childcare_csv_must_be_exact_member_of_original_zip(bundle, monkeypatch):
    manifest, assets = bundle
    filename, name = "children-file-1.csv", official.IMPORT_FILES[2]
    with (assets / filename).open("ab") as stream:
        stream.write(b"\r\n")
    content = json.loads((assets / name).read_text())
    content["source"]["source_hash"] = hashlib.sha256((assets / filename).read_bytes()).hexdigest()
    _write_json(assets / name, content)
    _repin_for_negative_test(monkeypatch, bundle, [filename, name])
    with pytest.raises(ApiError, match="ZIP 원문"):
        official.load_official_registry(manifest, name, artifact_dir=assets)


def test_university_excludes_graduate_rows_and_preserves_campuses_and_raw_homepage():
    result = official.load_official_registry(MANIFEST, official.IMPORT_FILES[1], artifact_dir=ASSETS)
    assert result.provenance["statistics"]["raw_rows"] == 79
    assert result.provenance["statistics"]["excluded_graduate_rows"] == 61
    assert sum(row["type"] == "university" for row in result.registry["rows"]) == 11
    assert sum(row["type"] == "junior_college" for row in result.registry["rows"]) == 7
    polytechnic = next(row for row in result.registry["rows"] if "폴리텍" in row["name"])
    assert polytechnic["type"] == "junior_college" and polytechnic["campus_label"] == "본교"
    assert polytechnic["homepage_url"] == "https://www.kopo.ac.kr/gwangju"
    assert len({row["external_key"] for row in result.registry["rows"]}) == 18


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "http://127.0.0.1", "http://[::1]/",
    "http://localhost/", "https://user:secret@school.go.kr/", "https://school.go.kr\n.attacker.com/",
])
def test_historical_homepage_relationship_never_allows_nonpublic_or_credential_urls(url):
    with pytest.raises(ApiError):
        official._homepage(url)
