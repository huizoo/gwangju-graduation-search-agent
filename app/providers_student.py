"""학교알리미의 공시 학교 식별자와 학년별 재학생 수를 대조한다."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from bs4 import BeautifulSoup

from .providers import HttpGateway, ProviderError, canonical, empty_document, neis_identifier, reserve, utcnow

SCHOOLINFO_BASE = "https://www.schoolinfo.go.kr"
API_URL = SCHOOLINFO_BASE + "/openApi.do"
PROFILE_URL = SCHOOLINFO_BASE + "/ei/ss/Pneiss_b01_s0.do"
GRADE_URL = SCHOOLINFO_BASE + "/ei/pp/Pneipp_b03_s0p.do"
DISTRICT_CODES_URL = SCHOOLINFO_BASE + "/download/sido_sggCode.xlsx"
DISTRICT_CODES = {"동구": "12210", "서구": "12240", "남구": "12270", "북구": "12300", "광산구": "12330"}
SCHOOL_TYPES = {"elementary": ("02", 6, "2"), "middle": ("03", 3, "3"), "high": ("04", 3, "4")}


def _compact(value) -> str:
    """학교명·도로명 주소를 표기상 공백과 행정구역 명칭 변경에 한해 정규화한다."""
    value = str(value or "").replace("전남광주통합특별시", "광주광역시")
    return re.sub(r"\s+", "", value)


def _number(value) -> int | None:
    """빈칸·소수·불리언을 인원으로 추정하지 않고 공개 정수만 읽는다."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= 1_000_000 else None
    if isinstance(value, str) and re.fullmatch(r"\d{1,7}", value.strip()):
        return int(value) if int(value) <= 1_000_000 else None
    return None


def _decode(response) -> str:
    """공식 페이지의 실제 문자셋을 읽어 한글 식별자가 깨지지 않게 한다."""
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return response.content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ProviderError("parse_failed", "학교알리미 문서 인코딩을 읽을 수 없습니다.", "schoolinfo")


def _document(institution, url, metadata, segments, raw, verified_by, kind="official_api", checked_at=None):
    """요청 학년도·식별 근거와 원문 구간을 사실 승인 전 문서로 보존한다."""
    document = empty_document(institution, url)
    if checked_at:
        document.update(retrieved_at=checked_at, last_checked_at=checked_at)
    document.update(
        source_key=f"schoolinfo:{metadata['provider']}:{metadata['identity']['school_code']}:{metadata['academic_year']}",
        title=f"{institution['name']} {metadata['academic_year']}학년도 학년별·학급별 학생수",
        authority="official", source_kind=kind, content_type="application/json" if kind == "official_api" else "text/html",
        status="ok", content_hash=hashlib.sha256(raw).hexdigest(), partial=False,
        read_scope="공시 학년도·학교 식별 및 졸업학년 학생수",
        authority_evidence={"channel_url": SCHOOLINFO_BASE + "/", "verified_by_url": verified_by,
                            "verified_at": document["retrieved_at"]},
        segments=[{"id": "metadata", "locator_kind": "api_json_path",
                   "location": "서버가 대조한 요청·원문 식별 관계", "text": canonical(metadata), "unit_id": "school-year"}] + segments,
    )
    document["acquisition"].update(method="api" if kind == "official_api" else "http",
                                  transport_status="ok", http_status=200, final_url=url, tls_verified=True)
    document["reading"].update(method="api_json" if kind == "official_api" else "html_parser",
                              scope="full_text", content_match="expected", institution_match="matched")
    return document


def _claim(document, value, metadata, segment_id, quote):
    """원문의 정규 인원 후보를 공통 게시 검증에 넘긴다."""
    return {
        "document_id": document["id"], "value": value, "academic_year": metadata["academic_year"],
        "kind": "graduating_grade_enrollment", "grade": metadata["grade"], "unit": "persons",
        "event_key_hint": None, "event_date": None, "source": metadata["provider"],
        "source_school_code": metadata["identity"]["school_code"], "source_field": metadata["source_field"],
        "evidence": [{"segment_id": segment_id, "quote": quote}],
    }


class StudentProvider:
    """해당 학년도 졸업학년 인원만 공식 공시에서 읽는다.

    Args:
        settings: 비밀값과 일반 설정을 분리한 실행 설정.
        meter: HTTP·문서 예산을 기록하는 실행 관리자.
        gateway: 이미 열어 둔 HTTP 연결. 없으면 이 자료원에서 생성한다.
    """

    def __init__(self, settings, meter=None, gateway=None):
        self.settings = settings
        self.gateway = gateway or HttpGateway(settings, meter)
        self._api_cache = {}
        self._api_checked_at = {}
        self._directory_cache = {}

    async def lookup(self, institution, season, identifiers) -> dict:
        """학교급·구·학교 식별·학년도를 검사해 인원 후보와 원문을 반환한다.

        Args:
            institution: 공식 목록에서 확인한 학교명·유형·주소.
            season: 확인할 행사 학년도가 명시된 내부 조회 문맥.
            identifiers: NEIS 등 기존 공식 학교 식별자.
        Returns:
            StudentResult. 인원 실패는 일정 날짜를 변경하지 않는다.
        """
        empty = {"status": "no_data", "reason_codes": [], "documents": [], "counts": []}
        # 사용자 시즌의 이름·시작연도 대신 실행기가 고정한 자료 학년도를 사용한다.
        if type(season.get("academic_year")) is not int:
            return {**empty, "status": "identity_unresolved", "reason_codes": ["academic_year_required"]}
        if institution.get("type") not in SCHOOL_TYPES:
            return {**empty, "reason_codes": ["ceremony_count_requires_notice"]}
        if institution.get("district") not in DISTRICT_CODES:
            return {**empty, "status": "identity_unresolved", "reason_codes": ["district_required"]}
        api_error = None
        if self.settings.secrets.get("schoolinfo_api_key"):
            try:
                return await self._api(institution, season, identifiers)
            except ProviderError as error:
                api_error = error.code
        try:
            result = await self._public(institution, season, identifiers)
            if api_error:
                result["reason_codes"].append("schoolinfo_api_" + api_error)
            return result
        except ProviderError as error:
            reasons = [error.code]
            if api_error:
                reasons.append("schoolinfo_api_" + api_error)
            elif not self.settings.secrets.get("schoolinfo_api_key"):
                reasons.append("schoolinfo_key_optional")
            return {**empty, "status": "provider_error", "reason_codes": reasons}

    async def _api_rows(self, params):
        """구·학교급별 공개 응답을 재사용하고 키를 원문 저장에서 분리한다."""
        cache_key = canonical(params)
        if cache_key in self._api_cache:
            return self._api_cache[cache_key]
        response = await self.gateway.request("GET", API_URL, params={
            **params, "apiKey": self.settings.secrets["schoolinfo_api_key"],
        })
        try:
            body = response.json()
            if not response.is_success or body.get("resultCode") != "success":
                raise ProviderError("provider_error", "학교알리미 API가 정상 자료를 반환하지 않았습니다.", "schoolinfo")
            rows = body["list"]
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise ProviderError("parse_failed", "학교알리미 API 응답 구조를 확인할 수 없습니다.", "schoolinfo") from None
        self._api_cache[cache_key] = rows
        self._api_checked_at[cache_key] = utcnow()
        return rows

    async def _api(self, institution, season, identifiers):
        """API0의 이름·주소·유형을 API09 공시 학교코드와 연결한다."""
        school_type, grade, _ = SCHOOL_TYPES[institution["type"]]
        district = DISTRICT_CODES[institution["district"]]
        base = {"sidoCode": "12", "sggCode": district, "schulKndCode": school_type}
        identity_query = {**base, "apiType": "0"}
        identities = await self._api_rows(identity_query)
        matches = [
            row for row in identities
            if _compact(row.get("SCHUL_NM")) == _compact(institution["name"])
            and row.get("SCHUL_KND_SC_CODE") == school_type
            and str(row.get("ADRCD_CD", "")).startswith(district)
            and _compact(row.get("SCHUL_RDNMA")) == _compact(institution.get("address"))
        ]
        if len(matches) != 1:
            raise ProviderError("wrong_source", "공시 학교코드를 이름·학교급·주소로 유일하게 연결하지 못했습니다.", "schoolinfo")
        identity = matches[0]
        query = {**base, "apiType": "09", "pbanYr": str(season["academic_year"])}
        rows = await self._api_rows(query)
        matches = [row for row in rows if row.get("SCHUL_CODE") == identity["SCHUL_CODE"]]
        if len(matches) != 1:
            raise ProviderError("not_found" if not matches else "source_conflict",
                                "해당 학교·학년도 공시 인원을 유일하게 확인하지 못했습니다.", "schoolinfo")
        row = matches[0]
        if (row.get("SCHUL_KND_SC_CODE") != school_type or _compact(row.get("SCHUL_NM")) != _compact(institution["name"])
                or not str(row.get("ADRCD_CD", "")).startswith(district)):
            raise ProviderError("wrong_source", "공시 인원의 학교·지역이 요청과 다릅니다.", "schoolinfo")
        if row.get("PBAN_EXCP_YN") != "N":
            raise ProviderError("not_disclosed", "공시 제외된 인원은 사용할 수 없습니다.", "schoolinfo")
        field = f"COL_S{grade}"
        value = _number(row.get(field))
        if value is None:
            raise ProviderError("not_found", "졸업학년 학생수 칸이 공개 정수가 아닙니다.", "schoolinfo")
        neis = neis_identifier(identifiers)
        metadata = {
            "provider": "schoolinfo_api09", "query": query, "academic_year": season["academic_year"],
            "grade": grade, "source_field": field, "unit": "persons",
            "identity": {"school_name": identity["SCHUL_NM"], "school_code": identity["SCHUL_CODE"],
                         "school_type": school_type, "address": identity["SCHUL_RDNMA"],
                         "neis_code": neis[1] if neis else None},
        }
        text, identity_text = canonical(row), canonical(identity)
        if max(len(text), len(identity_text)) > 4000:
            raise ProviderError("parse_failed", "학교알리미 한 행의 원문 크기가 계약을 초과했습니다.", "schoolinfo")
        await reserve(self.gateway.meter, "document_fetches")
        document = _document(institution, API_URL + "?" + urlencode(query), metadata, [
            {"id": "row", "locator_kind": "api_json_path", "location": "$.list[SCHUL_CODE]", "text": text, "unit_id": "school-year"},
            {"id": "identity", "locator_kind": "api_json_path", "location": "apiType=0 $.list[SCHUL_CODE]",
             "text": identity_text, "unit_id": "school-year"},
        ], canonical({"query": query, "identity": identity, "row": row}).encode(),
            API_URL + "?" + urlencode(identity_query), checked_at=self._api_checked_at[canonical(query)])
        return {"status": "ok", "reason_codes": [], "documents": [document],
                "counts": [_claim(document, value, metadata, "row", text)]}

    async def _public(self, institution, season, identifiers):
        """교육청 공시 링크와 NEIS 코드가 같은 공개 학년 표를 읽는다."""
        _, grade, directory_type = SCHOOL_TYPES[institution["type"]]
        neis = neis_identifier(identifiers)
        if not neis:
            raise ProviderError("wrong_source", "공개 공시의 NEIS 학교코드를 대조할 식별자가 없습니다.", "schoolinfo")
        directory_url = ("https://www.gen.go.kr/s_school/index.php?mode=excel1&s_div1=&s_div2="
                         + directory_type + "&s_div3=&s_div4=&s_name=")
        if directory_type not in self._directory_cache:
            response = await self.gateway.request("GET", directory_url)
            if not response.is_success:
                raise ProviderError("access_failed", "교육청 학교목록을 읽지 못했습니다.", "schoolinfo")
            self._directory_cache[directory_type] = _decode(response)
        directory = BeautifulSoup(self._directory_cache[directory_type], "html.parser")
        links, matched_cells = [], None
        for tr in directory.select("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in tr.find_all("td")]
            if len(cells) != 9 or _compact(cells[0]) != _compact(institution["name"]):
                continue
            if _compact(cells[4]) != _compact(institution.get("address")):
                continue
            url = urlsplit(cells[8])
            if url.hostname in {"schoolinfo.go.kr", "www.schoolinfo.go.kr"}:
                codes = parse_qs(url.query).get("HG_CD", [])
                if len(codes) == 1 and re.fullmatch(r"[A-Z]\d{9}", codes[0]):
                    links.append(codes[0])
                    matched_cells = cells
        if len(set(links)) != 1:
            raise ProviderError("not_found", "교육청 목록에서 해당 학교의 공시 링크를 유일하게 찾지 못했습니다.", "schoolinfo")
        hg_code = links[0]
        response = await self.gateway.request("GET", PROFILE_URL + "?" + urlencode({"HG_CD": hg_code}), public=True)
        profile_url = str(response.request.url)
        text = _decode(response)
        profile = BeautifulSoup(text, "html.parser")
        school_code = re.search(r'var\s+sdSchulCode\s*=\s*"(\d+)"', text)
        uuid_input = profile.find("input", attrs={"name": "SHL_IDF_CD"})
        if (not response.is_success or not school_code or school_code.group(1) != neis[1]
                or not profile.title or institution["name"] not in profile.title.get_text()
                or not uuid_input):
            raise ProviderError("wrong_source", "학교알리미 프로필의 학교명·NEIS 코드가 대상과 다릅니다.", "schoolinfo")
        school_uuid = uuid_input.get("value", "")
        if not re.fullmatch(r"[0-9a-f-]{36}", school_uuid):
            raise ProviderError("parse_failed", "학교알리미 공시 식별자 형식을 확인할 수 없습니다.", "schoolinfo")
        year = str(season["academic_year"])
        query = {"GS_HANGMOK_CD": "03", "GS_HANGMOK_NO": "3-가-3", "GS_HANGMOK_NM": "학년별·학급별 학생수",
                 "GS_BURYU_CD": "JG010", "JG_BURYU_CD": "JG030", "JG_HANGMOK_CD": "09", "JG_GUBUN": "1",
                 "JG_YEAR2": year, "HG_NM": institution["name"], "SHL_IDF_CD": school_uuid, "GS_TYPE": "Y",
                 "JG_YEAR": year, "SORT": "BR", "CHOSEN_JG_YEAR": year, "PRE_JG_YEAR": year, "LOAD_TYPE": "single"}
        await reserve(self.gateway.meter, "document_fetches")
        response = await self.gateway.request("POST", GRADE_URL, data=query)
        if not response.is_success:
            raise ProviderError("access_failed", "공개 학년별 학생수 표에 연결하지 못했습니다.", "schoolinfo")
        markup = _decode(response)
        soup = BeautifulSoup(markup, "html.parser")
        selected = soup.select_one("#select_trans_dt option[selected]")
        if not selected or not str(selected.get("value", "")).startswith(year):
            raise ProviderError("different_academic_year", "공개 공시가 요청한 학년도로 열리지 않았습니다.", "schoolinfo")
        academic_label = soup.find(string=re.compile(year + r"\s*학년도"))
        if institution["name"] not in soup.get_text(" ", strip=True) or not academic_label:
            raise ProviderError("wrong_source", "공개 표에서 학교와 학년도를 확인하지 못했습니다.", "schoolinfo")
        table = soup.select_one("table.hidden_table")
        headers = [cell.get_text(" ", strip=True) for cell in table.select("tr")[0].find_all("th")] if table else []
        sex_headers = [cell.get_text(" ", strip=True) for cell in table.select("tr")[1].find_all("th")] if table and len(table.select("tr")) > 1 else []
        values = [_number(cell.get_text(strip=True)) for cell in table.find_all("td")] if table else []
        if headers[:grade] != [f"{g}학년" for g in range(1, grade + 1)] or len(values) != len(headers) * 3:
            raise ProviderError("unsupported_format", "공개 학년별 학생수 표의 열 관계가 지원 형식과 다릅니다.", "schoolinfo")
        if sex_headers != ["남학생", "여학생", "전체학생"] * len(headers):
            raise ProviderError("unsupported_format", "학년별 남녀·전체 인원 열을 구분하지 못했습니다.", "schoolinfo")
        selected_values = values[(grade - 1) * 3:grade * 3]
        if any(value is None for value in selected_values) or sum(selected_values[:2]) != selected_values[2]:
            raise ProviderError("parse_failed", "졸업학년 인원과 남녀 소계가 일치하지 않습니다.", "schoolinfo")
        value = selected_values[2]
        metadata = {"provider": "schoolinfo_public", "academic_year": season["academic_year"], "grade": grade,
                    "source_field": f"grade_{grade}_total", "unit": "persons", "query": query,
                    "identity": {"school_name": institution["name"], "school_code": hg_code, "school_uuid": school_uuid,
                                 "school_type": SCHOOL_TYPES[institution["type"]][0], "address": institution["address"],
                                 "neis_code": neis[1]}, "selected_year": int(year), "table_academic_year": int(year),
                    "grade_headers": headers, "sex_headers": sex_headers, "grade_values": values}
        table_text = table.get_text(" ", strip=True)
        document = _document(institution, GRADE_URL + "?" + urlencode(query), metadata, [
            {"id": "table", "locator_kind": "html_text", "location": "table.hidden_table 학년별 전체학생 열",
             "text": table_text, "unit_id": "school-year"},
            {"id": "profile", "locator_kind": "html_text", "location": "공식 교육청 링크와 학교알리미 프로필",
             "text": canonical({"education_directory_url": directory_url, "schoolinfo_code": hg_code,
                                "school_name": institution["name"], "profile_url": profile_url,
                                "school_uuid": school_uuid, "neis_code": neis[1],
                                "directory_cells": matched_cells, "profile_title": profile.title.get_text(),
                                "neis_source": school_code.group(0), "school_uuid_source": str(uuid_input)}),
             "unit_id": "school-year"},
            {"id": "context", "locator_kind": "html_text", "location": "선택 공시년도와 표 학년도",
             "text": canonical({"selected_option": str(selected), "academic_year_label": str(academic_label)}),
             "unit_id": "school-year"},
        ], response.content, directory_url, "html")
        return {"status": "ok", "reason_codes": [], "documents": [document],
                "counts": [_claim(document, value, metadata, "table", table_text)]}
