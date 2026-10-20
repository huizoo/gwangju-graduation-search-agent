"""검토된 공식 명부 묶음의 원문·정규 행·홈페이지 관계를 다시 검증한다.

공식 URL과 해시를 사용자가 함께 제출하는 것만으로 공식성이 증명되지는 않는다.
이 모듈은 코드 검토로 승인한 manifest 해시를 신뢰 기준으로 사용한다. HTTP를
재요청하지 않으므로 취득 당시의 관계만 증명하며 현재 운영·행사·소유권은 보증하지
않는다. 새 자료는 원문 출처를 다시 검토하고 승인 해시를 갱신해야 한다.
"""

from __future__ import annotations

import csv
import hashlib
import io
import ipaddress
import json
import re
import zipfile
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from .domain import ApiError, validate
from .registry import district_for

# 클라이언트가 pin을 지정하는 인자는 두지 않는다. 이 값 변경은 원문 재검토가 필요하다.
APPROVED_MANIFEST_SHA256 = frozenset({
    "6558f3481957260bfd3aab9574a53b8113de59e7e4b9cf5f0d01436c97f74741",
})
DISTRICTS = ("동구", "서구", "남구", "북구", "광산구")
GEN_URL = "https://www.gen.go.kr/s_school/index.php?mode=search1&s_div2=1"
GEN_FILE_URL = "https://www.gen.go.kr/s_school/index.php?mode=excel1&s_div1=&s_div2=1&s_div3=&s_div4=&s_name="
UNIVERSITY_URL = "https://www.data.go.kr/data/15107736/standard.do"
UNIVERSITY_FILE_URL = "https://www.data.go.kr/tcs/dss/selectStdDataDetailView.do?publicDataPk=15107736&colCondition=CTPV_NM&searchKeyword1=%EA%B4%91%EC%A3%BC%EA%B4%91%EC%97%AD%EC%8B%9C"
CHILDREN_URL = "https://www.data.go.kr/data/15135134/fileData.do"
CHILDREN_FILE_URL = "https://www.data.go.kr/cmm/cmm/fileDownload.do?atchFileId=FILE_000000007687446&fileDetailSn=1"
HANA_URL = "https://info.childcare.go.kr/info_html5/pnis/search/preview/SummaryInfoSlPu.jsp?flag=YJ&STCODE_POP=29110000084&CRNAMETITLE=&loginFlag="
IMPORT_FILES = (
    "kindergarten-edoffice-registry.json",
    "universities-registry.json",
    "childcare-welfare-partial-registry.json",
    "childcare-representative-registry.json",
)
SOURCE_PROFILES = {
    IMPORT_FILES[0]: ("gwangju_education_directory", GEN_URL, "gen-kindergarten-directory-original.bin", ("kindergarten",)),
    IMPORT_FILES[1]: ("kcue_standard_public_file", UNIVERSITY_URL, "universities-gwangju.bin", ("university", "junior_college")),
    IMPORT_FILES[2]: ("gwangju_welfare_public_file", CHILDREN_URL, "children-file-1.csv", ("childcare",)),
    IMPORT_FILES[3]: ("childcare_public_disclosure", HANA_URL, "hana-summary.html", ("childcare",)),
}
RAW_URLS = {
    "gen-kindergarten-directory.html": GEN_URL,
    "gen-kindergarten-directory-original.bin": GEN_FILE_URL,
    "universities-gwangju.bin": UNIVERSITY_FILE_URL,
    "children-20260921-original.bin": CHILDREN_FILE_URL,
    "children-file-1.csv": None,
    "hana-summary.html": HANA_URL,
}


@dataclass(frozen=True)
class VerifiedOfficialRegistry:
    """서버만 생성하는 목록과 취득 당시 홈페이지 관계를 담는다.

    Attributes:
        registry: 일반 RegistryImport 미리보기·적재에 전달할 검증된 목록.
        authorities: 외부 기관 코드별 OfficialChannel 및 원문 행 근거.
        provenance: 승인 manifest, 원본 해시, 검증 범위와 한계.
    """

    registry: dict
    authorities: tuple[dict, ...]
    provenance: dict


def _require(condition: bool, message: str) -> None:
    """검증 실패를 데이터 값이나 파일 내용을 노출하지 않는 오류로 바꾼다.

    Args:
        condition: 반드시 참이어야 하는 검증 결과.
        message: 실패 시 표시할 한국어 설명.
    Raises:
        ApiError: 검증 조건이 거짓인 경우.
    """
    if not condition:
        raise ApiError("official_registry_unverified", message)


def _sha256(raw: bytes) -> str:
    """원본 바이트의 비교용 SHA-256을 반환한다.

    Args:
        raw: 인코딩 변환 전의 원본 바이트.
    Returns:
        소문자 16진수 SHA-256.
    """
    return hashlib.sha256(raw).hexdigest()


def _json(raw: bytes):
    """중복 JSON 키와 비표준 숫자를 거절해 파서별 해석 차이를 없앤다.

    Args:
        raw: 해시를 확인한 JSON 바이트.
    Returns:
        중복 키가 없는 JSON 값.
    Raises:
        ApiError: 중복 키나 비표준 숫자가 있는 경우.
    """
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "공식 명부 JSON에 중복 키가 있습니다.")
            result[key] = value
        return result

    def invalid(_value):
        raise ApiError("official_registry_unverified", "공식 명부 JSON에 비표준 숫자가 있습니다.")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)


class _Bundle:
    """승인된 manifest의 파일만 지정 폴더에서 읽고 원본 바이트를 고정한다."""

    def __init__(self, manifest_path: Path, artifact_dir: Path | None):
        """승인 해시와 고정 폴더로 파일 읽기 범위를 제한한다.

        Args:
            manifest_path: 서버가 선택한 검토 기록 파일.
            artifact_dir: 원본을 보관한 서버 폴더 또는 기본 위치를 뜻하는 None.
        Raises:
            ApiError: 승인되지 않은 기록·경로·취득 시각인 경우.
        """
        raw = manifest_path.read_bytes()
        self.digest = _sha256(raw)
        _require(self.digest in APPROVED_MANIFEST_SHA256, "서버가 승인한 공식 명부 manifest가 아닙니다.")
        self.manifest = _json(raw)
        self.directory = (artifact_dir or manifest_path.resolve().parents[2] / "runtime/non-school-registry").resolve()
        self.entries = {}
        for entry in self.manifest["artifacts"]:
            path = Path(entry["path"])
            _require(path.parts[:2] == ("runtime", "non-school-registry") and len(path.parts) == 3,
                     "공식 명부 manifest의 파일 경로가 허용 범위를 벗어났습니다.")
            _require(path.name not in self.entries, "공식 명부 manifest에 파일이 중복되었습니다.")
            self.entries[path.name] = entry
        self.cache = {}
        checked = datetime.fromisoformat(self.manifest["checked_at"])
        _require(checked.tzinfo is not None, "공식 명부 취득 검토 시각에는 시간대가 필요합니다.")
        self.checked_at = self.manifest["checked_at"]

    def read(self, filename: str) -> bytes:
        """원본 출처·크기·해시·경로를 확인한 동일 바이트를 재사용한다.

        Args:
            filename: 승인 manifest에 있는 파일 이름.
        Returns:
            같은 검증 중에는 다시 읽지 않는 고정된 원본 바이트.
        Raises:
            ApiError: 파일이 변조되었거나 지정 범위 밖인 경우.
        """
        _require(filename in self.entries and Path(filename).name == filename, "승인된 공식 명부 파일이 아닙니다.")
        if filename in self.cache:
            return self.cache[filename]
        entry = self.entries[filename]
        _require(entry.get("source_url") == RAW_URLS.get(filename), "공식 원본의 취득 URL이 승인 범위와 다릅니다.")
        path = self.directory / filename
        _require(not path.is_symlink() and path.resolve().parent == self.directory,
                 "공식 명부 파일은 지정 폴더 안의 일반 파일이어야 합니다.")
        raw = path.read_bytes()
        _require(len(raw) == entry["bytes"] and _sha256(raw) == entry["sha256"],
                 "공식 명부 파일의 크기 또는 SHA-256이 승인 원본과 다릅니다.")
        self.cache[filename] = raw
        return raw


def _homepage(value: str | None) -> str | None:
    """원문 홈페이지의 스킴·호스트를 검사하되 현재 DNS나 소유권은 추정하지 않는다.

    Args:
        value: 공식 원문이 제시한 홈페이지 또는 None.
    Returns:
        형식을 확인한 원문 URL 또는 None.
    Raises:
        ApiError: 비공개 주소·인증 정보·위험한 스킴이 포함된 경우.
    """
    if not value:
        return None
    parts = urlsplit(value)
    host = parts.hostname or ""
    _require(not any(char.isspace() or ord(char) < 32 for char in value)
             and parts.scheme in {"http", "https"} and bool(host)
             and parts.username is None and parts.password is None
             and parts.port in {None, 80, 443} and host not in {"localhost", "localhost.localdomain"}
             and not host.endswith((".localhost", ".local", ".invalid", ".test", ".example")),
             "공식 명부의 홈페이지 URL이 공개 HTTP 주소 형식을 충족하지 않습니다.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        _require(address.is_global, "공식 명부의 홈페이지에 비공개 IP를 등록할 수 없습니다.")
    return value


def _key(source: str, kind: str, name: str, address: str) -> str:
    """행 순서와 무관한 출처·기관유형·이름·주소의 안정적인 키를 계산한다.

    Args:
        source: 공식 데이터셋 식별자.
        kind: 계약의 기관 유형.
        name: 원문 기관명.
        address: 원문 주소.
    Returns:
        행 위치가 바뀌어도 유지되는 SHA-256 식별자.
    """
    raw = json.dumps([source, kind, name, address], ensure_ascii=False, separators=(",", ":")).encode()
    return "sha256:" + _sha256(raw)


def _row(key: str, name: str, kind: str, address: str, phone=None, homepage=None, campus=None) -> dict:
    """원문에서 확인된 기관 속성만 계약으로 변환하고 운영상태 추정을 막는다.

    Args:
        key: 공식 코드 또는 안정적인 합성 식별자.
        name: 원문 기관명.
        kind: 계약의 기관 유형.
        address: 공개 주소.
        phone: 공개 업무 전화번호.
        homepage: 원문 홈페이지.
        campus: 원문 본교·분교 구분.
    Returns:
        운영상태가 unknown인 RegistryRow.
    Raises:
        ApiError: 광주 5개 구 밖의 주소나 잘못된 홈페이지인 경우.
    """
    district = district_for(address)
    _require(district in DISTRICTS, "공식 명부에 광주 5개 구 밖의 주소가 있습니다.")
    return {
        "external_key": key, "name": name, "type": kind, "district": district, "address": address,
        "campus_label": campus, "phone": phone or None, "homepage_url": _homepage(homepage),
        "operation_status": "unknown", "operation_effective_date": None, "operation_evidence_url": None,
    }


def _parse_kindergarten(bundle: _Bundle) -> tuple[list[dict], list[dict], dict]:
    """공식 코드와 이름 링크를 재추출하고 총수·5개 구 소계를 교차 검사한다.

    Args:
        bundle: 원본 바이트를 검증하는 승인 명부 묶음.
    Returns:
        정규 기관 행, 행별 근거, 구별 소계와 기준일.
    Raises:
        ApiError: 공식 코드·열 구성·구별 합계가 일치하지 않는 경우.
    """
    soup = BeautifulSoup(bundle.read("gen-kindergarten-directory-original.bin").decode("utf-8"), "html.parser")
    rows, proofs = [], []
    for number, tr in enumerate(soup.select("tr")[1:], 1):
        cells = tr.find_all("td")
        _require(len(cells) == 7, "유치원 원문 열 구성이 달라 재검토가 필요합니다.")
        disclosure = cells[6].find("a")["href"]
        parts = urlsplit(disclosure)
        _require(parts.hostname == "e-childschoolinfo.moe.go.kr" and parts.path == "/presch/preschSumry.do"
                 and parts.scheme in {"http", "https"}, "유치원 공식 코드의 공시 출처가 다릅니다.")
        codes = parse_qs(parts.query).get("pPresch", [])
        # 공식 명부의 운수유치원 코드는 숫자 8자리다. 자릿수를 임의 보정하지 않는다.
        _require(len(codes) == 1 and bool(re.fullmatch(r"[A-Z]\d{8,9}", codes[0])), "유치원 공식 코드가 올바르지 않습니다.")
        homepage = cells[0].find("a")["href"] or None
        rows.append(_row(codes[0], cells[0].get_text(strip=True), "kindergarten",
                         cells[3].get_text(" ", strip=True), cells[4].get_text(strip=True), homepage))
        proofs.append({"row_index_1_based": number, "official_disclosure_url": disclosure})
    directory = BeautifulSoup(bundle.read("gen-kindergarten-directory.html").decode("utf-8"), "html.parser")
    declared = {}
    for anchor in directory.find_all("a"):
        matched = re.fullmatch(r"(전체|동구|서구|남구|북구|광산구)\((\d+)\)", anchor.get_text(strip=True))
        if matched:
            label, count = matched.group(1), int(matched.group(2))
            _require(label not in declared or declared[label] == count, "유치원 화면의 소계가 서로 다릅니다.")
            declared[label] = count
    counts = {district: sum(row["district"] == district for row in rows) for district in DISTRICTS}
    _require(set(declared) == {"전체", *DISTRICTS} and declared["전체"] == len(rows)
             and all(declared[district] == counts[district] for district in DISTRICTS)
             and sum(counts.values()) == declared["전체"], "유치원 화면·원문 행수·구별 소계가 일치하지 않습니다.")
    return rows, proofs, {"directory_total": len(rows), "by_district": counts, "as_of": None}


def _parse_universities(bundle: _Bundle) -> tuple[list[dict], list[dict], dict]:
    """대학 표준표 원문을 읽어 대학원 행을 제외하고 자료 기준일을 대조한다.

    Args:
        bundle: 원본 바이트를 검증하는 승인 명부 묶음.
    Returns:
        정규 기관 행, 행별 근거, 원문·제외 행수와 기준일.
    Raises:
        ApiError: 열·지역·캠퍼스·자료 기준일이 불명확한 경우.
    """
    soup = BeautifulSoup(bundle.read("universities-gwangju.bin").decode("utf-8"), "html.parser")
    tables = [table for table in soup.find_all("table")
              if [cell.get_text(" ", strip=True) for cell in table.select("th")][:1] == ["학교명"]]
    _require(len(tables) == 1, "대학 원문에서 유일한 기관 표를 확인하지 못했습니다.")
    headers = [cell.get_text(" ", strip=True) for cell in tables[0].select("th")]
    _require(len(headers) == 18 and [headers[index] for index in (2, 3, 7, 8, 12, 13, 17)] ==
             ["본분교구분명", "대학구분명", "시도명", "소재지도로명주소", "홈페이지주소", "대표전화번호", "데이터기준일자"],
             "대학 원문의 열 구성이 달라 재검토가 필요합니다.")
    rows, proofs, as_ofs, raw_count, excluded = [], [], set(), 0, 0
    for tr in tables[0].select("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in tr.find_all("td")]
        if not cells:
            continue
        raw_count += 1
        _require(len(cells) == 18 and cells[7] == "광주광역시", "대학 원문이 광주 지역 필터와 다릅니다.")
        as_ofs.add(date.fromisoformat(cells[17]).isoformat())
        if cells[3] == "대학원":
            excluded += 1
            continue
        _require(cells[3] in {"대학", "전문대학"} and bool(cells[2]), "대학 구분·캠퍼스가 확인되지 않습니다.")
        kind = "university" if cells[3] == "대학" else "junior_college"
        homepage = cells[12] if cells[12].startswith(("http://", "https://")) else "https://" + cells[12] if cells[12] else None
        rows.append(_row(_key("data.go.kr:15107736", kind, cells[0], cells[8]), cells[0], kind,
                         cells[8], cells[13], homepage, cells[2]))
        proofs.append({"row_index_1_based": raw_count, "homepage_as_recorded": cells[12],
                       "original_university_category": cells[3], "original_school_category": cells[4],
                       "original_campus_label": cells[2], "data_as_of": cells[17]})
    _require(len(as_ofs) == 1, "대학 원문의 자료 기준일이 없거나 서로 다릅니다.")
    return rows, proofs, {"raw_rows": raw_count, "excluded_graduate_rows": excluded, "as_of": as_ofs.pop()}


def _parse_childcare(bundle: _Bundle) -> tuple[list[dict], list[dict], dict]:
    """공식 ZIP 안의 동일 CSV를 확인하고 이름·주소의 정확한 중복만 제거한다.

    Args:
        bundle: 원본 바이트를 검증하는 승인 명부 묶음.
    Returns:
        정규 기관 행, 행별 근거, 원문 행수·구별 소계와 미상 기준일.
    Raises:
        ApiError: CSV와 ZIP이 다르거나 열·지역 범위가 변한 경우.
    """
    raw = bundle.read("children-file-1.csv")
    archive = zipfile.ZipFile(io.BytesIO(bundle.read("children-20260921-original.bin")))
    with archive:
        members = [member for member in archive.infolist() if member.file_size == len(raw)]
        _require(any(archive.read(member) == raw for member in members), "어린이집 CSV가 승인된 공식 ZIP 원문과 다릅니다.")
    reader = csv.DictReader(io.StringIO(raw.decode("cp949")))
    _require(reader.fieldnames == ["보육시설명", "주소"], "어린이집 원문의 열 구성이 다릅니다.")
    rows, proofs, seen, raw_count = [], [], set(), 0
    for number, values in enumerate(reader, 1):
        raw_count += 1
        name, address = values["보육시설명"].strip(), values["주소"].strip()
        key = _key("data.go.kr:15135134", "childcare", name, address)
        if key in seen:
            continue
        seen.add(key)
        rows.append(_row(key, name, "childcare", address))
        proofs.append({"row_index_1_based": number})
    counts = {district: sum(row["district"] == district for row in rows) for district in DISTRICTS}
    _require(all(counts.values()), "어린이집 일부 목록의 5개 구 범위가 달라졌습니다.")
    return rows, proofs, {"raw_rows": raw_count, "by_district": counts, "as_of": None}


def _parse_childcare_profile(bundle: _Bundle) -> tuple[list[dict], list[dict], dict]:
    """기관 공시의 이름·주소·전화·홈페이지 열을 직접 읽는다.

    Args:
        bundle: 원본 바이트를 검증하는 승인 명부 묶음.
    Returns:
        단일 기관 행, 공시 페이지 근거, 미상 기준일.
    Raises:
        ApiError: 공시 기관 코드나 지역·홈페이지가 확인되지 않는 경우.
    """
    text = bundle.read("hana-summary.html").decode("utf-8")
    soup, values, homepage = BeautifulSoup(text, "html.parser"), {}, None
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["th", "td"], recursive=False)
        for index, cell in enumerate(cells[:-1]):
            if cell.name == "th" and cells[index + 1].name == "td":
                label, value = cell.get_text(" ", strip=True), cells[index + 1]
                values[label] = value.get_text(" ", strip=True)
                if label == "홈페이지":
                    homepage = value.find("a")["href"]
    code = parse_qs(urlsplit(HANA_URL).query)["STCODE_POP"][0]
    _require(code in text, "어린이집 공시 원문에서 승인된 기관 코드를 확인하지 못했습니다.")
    address = re.sub(r"^\(\d{5}\)\s*", "", values["주소"])
    row = _row(code, values["기관명"], "childcare", address, values["전화번호"], homepage)
    return [row], [{}], {"as_of": None}


def load_official_registry(manifest_path: Path, import_file: str, *, artifact_dir: Path | None = None) -> VerifiedOfficialRegistry:
    """서버가 선택한 승인 명부를 검증하여 적재 자료와 홈페이지 근거를 반환한다.

    Args:
        manifest_path: 서버 설정이 지정한 검토 완료 manifest. HTTP 요청의 경로를 전달하지 않는다.
        import_file: IMPORT_FILES 중 하나인 서버 제공 목록 식별자.
        artifact_dir: 원문·정규 파일의 고정 폴더. 생략하면 기존 runtime 저장 위치를 사용한다.
    Returns:
        RegistryImport, 기관별 OfficialChannel, 취득 당시 근거 및 현재성 한계.
    Raises:
        ApiError: 승인되지 않은 manifest, 원문 변조, 행 불일치, 범위·기준일·출처 검증 실패.
    Examples:
        >>> # API는 목록 식별자만 받고 경로는 서버가 고정한다.
        >>> # result = load_official_registry(manifest, IMPORT_FILES[0], artifact_dir=assets)
        >>> # preview = service.registry_preview(result.registry)
        >>> # commit 후 external_key로 기관을 찾아 result.authorities를 연결한다.
    """
    try:
        _require(import_file in IMPORT_FILES, "서버에서 제공하는 공식 명부 목록이 아닙니다.")
        bundle = _Bundle(Path(manifest_path), Path(artifact_dir) if artifact_dir else None)
        payload = validate("RegistryImport", _json(bundle.read(import_file)))
        source = payload["source"]
        provider, origin, raw_file, types = SOURCE_PROFILES[import_file]
        expected_districts = ["동구"] if import_file == IMPORT_FILES[3] else list(DISTRICTS)
        _require(source["provider"] == provider and source["official_url"] == origin
                 and source["scope_types"] == list(types) and source["scope_districts"] == expected_districts
                 and source["source_hash"] == _sha256(bundle.read(raw_file)),
                 "정규 명부의 출처·범위·원본 해시가 승인 근거와 다릅니다.")
        parser = {
            IMPORT_FILES[0]: _parse_kindergarten, IMPORT_FILES[1]: _parse_universities,
            IMPORT_FILES[2]: _parse_childcare, IMPORT_FILES[3]: _parse_childcare_profile,
        }[import_file]
        rows, proofs, statistics = parser(bundle)
        _require(rows and len({row["external_key"] for row in rows}) == len(rows),
                 "공식 원문의 기관 식별자가 없거나 중복되었습니다.")
        _require(payload["rows"] == rows, "정규 기관 정보가 공식 원문의 기관 코드·홈페이지·속성과 일치하지 않습니다.")
        _require(source["as_of"] == statistics["as_of"], "자료 기준일이 원문과 다릅니다. 취득일을 대신 사용할 수 없습니다.")
        if source["as_of"]:
            _require(date.fromisoformat(source["as_of"]) <= datetime.fromisoformat(bundle.checked_at).date(),
                     "자료 기준일이 취득 검토 시각보다 나중입니다.")
        is_kindergarten = import_file == IMPORT_FILES[0]
        _require(source["coverage_complete"] is is_kindergarten
                 and source["expected_total"] == (len(rows) if is_kindergarten else None),
                 "정규 명부의 전체성 주장과 원문 범위가 다릅니다.")
        declared_result = [result for result in bundle.manifest["results"] if result["file"] == import_file]
        _require(len(declared_result) == 1 and declared_result[0]["rows"] == len(rows), "검토 기록과 정규 행수가 다릅니다.")
        if "by_district" in declared_result[0]:
            _require(declared_result[0]["by_district"] == dict(Counter(row["district"] for row in rows)),
                     "검토 기록과 구별 기관수가 다릅니다.")
        if import_file == IMPORT_FILES[1]:
            expected = bundle.manifest["university_filter"]
            _require(statistics["raw_rows"] == expected["filtered_rows"]
                     and statistics["excluded_graduate_rows"] == expected["excluded_graduate_rows"]
                     and statistics["as_of"] == expected["data_as_of"], "대학 원문의 제외 범위·자료 기준일이 검토 기록과 다릅니다.")
        sidecar = _json(bundle.read("homepage-authority-provenance.json"))
        selected = [entry for entry in sidecar if entry.get("import_file") == import_file]
        _require(len(selected) == len(rows) and len({entry["external_key"] for entry in selected}) == len(rows),
                 "공식 홈페이지 근거의 기관별 연결이 누락되거나 중복되었습니다.")
        indexed = {entry["external_key"]: entry for entry in selected}
        authorities = []
        for row, proof in zip(rows, proofs, strict=True):
            evidence = indexed.get(row["external_key"], {})
            _require(evidence.get("institution_name") == row["name"] and evidence.get("homepage_url") == row["homepage_url"]
                     and evidence.get("source_origin") == origin and evidence.get("source_file") == raw_file
                     and evidence.get("source_sha256") == source["source_hash"]
                     and evidence.get("source_registration_not_publication_approval") is True
                     and all(evidence.get(key) == value for key, value in proof.items()),
                     "홈페이지 관계 근거가 원문의 기관 식별자·행·주소와 다릅니다.")
            if evidence.get("retrieval_url"):
                _require(evidence["retrieval_url"] == RAW_URLS[raw_file], "홈페이지 근거의 원문 취득 URL이 다릅니다.")
            if row["homepage_url"]:
                channel = validate("OfficialChannel", {
                    "url": row["homepage_url"], "verified_by_url": origin, "verified_at": bundle.checked_at,
                })
                authorities.append({
                    "provider": provider, "external_key": row["external_key"], "channel": channel,
                    "source_file": raw_file, "source_sha256": source["source_hash"], **proof,
                })
        # 기준일 미상인 명부의 256행 대조 성공을 현재 전체성 정책 통과로 격상하지 않는다.
        normalized = deepcopy(payload)
        if is_kindergarten:
            normalized["source"]["coverage_complete"] = False
            normalized["source"]["completeness_note"] += (
                " 서버 검증: 공식 화면·명부 행수와 5개 구 소계가 일치한다. "
                "원문 자료 기준일 미상으로 앱의 전체성 정책을 충족하지 않아 coverage_complete=false로 적재한다."
            )
        normalized = validate("RegistryImport", normalized)
        return VerifiedOfficialRegistry(normalized, tuple(authorities), {
            "manifest_sha256": bundle.digest, "source_sha256": source["source_hash"],
            "reviewed_at": bundle.checked_at, "source_as_of": source["as_of"],
            "authority_scope": "historical_official_directory_homepage_relation",
            "network_refetched": False, "publication_approved": False,
            "coverage_complete": normalized["source"]["coverage_complete"], "statistics": statistics,
            "limitations": [
                "승인된 취득 원문과 같은 바이트임을 확인했다. 현재 사이트 내용·소유권·기관 운영 상태는 재검증하지 않았다.",
                "공식 홈페이지 관계는 졸업식 날짜·실제 시작 시각의 확인 또는 게시 승인이 아니다.",
                "기관 주소 변경·새 목록은 원문 출처 검토와 서버 manifest 승인 갱신이 필요하다.",
            ],
        })
    except ApiError:
        raise
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError, zipfile.BadZipFile):
        raise ApiError("official_registry_unverified", "공식 명부 묶음이 없거나 원문 구조를 검증할 수 없습니다.") from None
