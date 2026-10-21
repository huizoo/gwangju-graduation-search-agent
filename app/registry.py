"""공식 목록 파일과 NEIS 원본을 유형·지역·식별자별로 검증한다."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from copy import deepcopy
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from .domain import VALIDATORS
from .providers import ProviderError, canonical

DISTRICTS = {"동구", "서구", "남구", "북구", "광산구"}
TYPES = {"childcare", "kindergarten", "elementary", "middle", "high",
         "special_school", "other_school", "university", "junior_college"}
NEIS_TYPES = {"초등학교": "elementary", "중학교": "middle", "고등학교": "high",
              "특수학교": "special_school", "각종학교": "other_school",
              "고등기술학교": "other_school", "고등공민학교": "other_school",
              "방송통신중학교": "other_school", "방송통신고등학교": "other_school",
              "외국인학교": "other_school", "각종학교(초)": "other_school",
              "각종학교(중)": "other_school", "각종학교(고)": "other_school"}


def normalize_homepage(value) -> str | None:
    """명부의 null을 문자열 URL로 바꾸지 않고 공개 주소 형식만 보존한다.

    Args:
        value: 공식 명부의 홈페이지 값.
    Returns:
        HTTP(S) URL 또는 실제 주소가 없으면 None.
    """
    if not isinstance(value, str) or value.strip().lower() in {"", "none", "null", "nan", "-"}:
        return None
    raw = value.strip()
    raw = raw if raw.startswith(("http://", "https://")) else "https://" + raw
    try:
        parsed = urlsplit(raw)
        if not parsed.hostname or "." not in parsed.hostname or parsed.username or parsed.password:
            return None
        if any(char.isspace() or ord(char) < 32 for char in raw):
            return None
        return raw
    except ValueError:
        return None


def _validator(name: str):
    """동봉한 4.0 계약의 지정 정의를 검사하는 검증기를 반환한다."""
    return VALIDATORS[name]


def district_for(address: str) -> str | None:
    """경기도 광주시와 구분하여 광주광역시 주소에서 다섯 구를 얻는다."""
    # 2026-10-20 실제 F10 원문과 공식 교육청 주소에서 확인한 새 표기를 보존한다.
    match = re.match(r"^\s*(?:광주(?:광역시)?|전남광주통합특별시)\s+(동구|서구|남구|북구|광산구)(?:\s|$)", address)
    return match.group(1) if match else None


def validate_registry_import(payload: dict, live=True) -> dict:
    """공식 목록의 형식·범위·중복·완전성 주장을 검증한다.

    Args:
        payload: RegistryImport 계약의 출처와 정규화 행.
        live: 가상 자료원 이름과 예제 도메인을 금지할지 여부.
    Returns:
        허용 행과 제외 사유를 구분한 미리보기. 원본 객체는 수정하지 않는다.
    Raises:
        ProviderError: 출처 형식, 범위 또는 전체 건수 주장이 잘못된 경우.
    """
    try:
        _validator("RegistryImport").validate(payload)
    except Exception:
        raise ProviderError("parse_failed", "기관 목록이 RegistryImport 4.0 계약과 일치하지 않습니다.", "registry") from None
    source = payload["source"]
    hostname = urlsplit(source["official_url"]).hostname or ""
    if live and (source["provider"].lower().startswith(("demo", "fixture", "fake"))
                 or hostname.endswith((".example", ".invalid", ".test")) or hostname == "localhost"):
        raise ProviderError("source_unverified", "가상·시험 목록은 live 기관 목록으로 적재할 수 없습니다.", "registry")
    if not re.fullmatch(r"[a-f0-9]{64}", source["source_hash"]):
        raise ProviderError("parse_failed", "공식 원본 파일의 SHA-256 해시가 필요합니다.", "registry")
    accepted, rejected, seen = [], [], set()
    for number, row in enumerate(payload["rows"], 1):
        reasons = []
        key = row["external_key"]
        if key in seen:
            reasons.append("duplicate_key")
        seen.add(key)
        district = district_for(row["address"])
        if district is None or district != row["district"]:
            reasons.append("outside_region_or_district_mismatch")
        if row["district"] not in source["scope_districts"] or row["type"] not in source["scope_types"]:
            reasons.append("outside_declared_scope")
        if row["type"] in {"university", "junior_college"} and not row.get("campus_label"):
            reasons.append("campus_identity_required")
        if row["operation_status"] in {"closed", "suspended"} and not row.get("operation_evidence_url"):
            reasons.append("operation_evidence_required")
        if reasons:
            rejected.append({"row": number, "external_key": key, "reasons": reasons})
        else:
            accepted.append(deepcopy(row))
    normalized = deepcopy(payload)
    normalized["rows"] = accepted
    complete = source["coverage_complete"]
    expected = source["expected_total"]
    if complete and (source["as_of"] is None or not source["completeness_note"].strip()
                     or rejected or (expected is not None and expected != len(accepted))):
        raise ProviderError("coverage_incomplete", "전체 목록 주장과 자료 기준·행 수·제외 행이 일치하지 않습니다.", "registry")
    return {"payload": normalized, "accepted_rows": len(accepted), "rejected_rows": rejected,
            "coverage_complete": complete, "source_verified": False,
            "note": "형식·범위 검사 통과는 출처의 실제 공식성·최신 전체 명단 대조 완료를 뜻하지 않습니다."}


def load_official_file(path: Path, source: dict, column_map: dict | None = None) -> dict:
    """공식 JSON 또는 UTF-8 CSV를 공통 목록 적재 계약으로 변환한다.

    Args:
        path: 사용자가 작업 폴더에 준비한 공식 원본 파일.
        source: 출처 URL, 기준일, 유형·구 범위와 완전성 근거.
        column_map: CSV 정규 필드명에서 원본 열 이름으로 가는 명시적 매핑.
    Returns:
        실제 원본 해시를 포함한 RegistryImport. 이름만으로 자료원을 합치지 않는다.
    Raises:
        ProviderError: 미지원 형식, 잘못된 인코딩 또는 필수 값 누락.
    """
    raw = path.read_bytes()
    metadata = deepcopy(source)
    metadata["source_hash"] = hashlib.sha256(raw).hexdigest()
    try:
        if path.suffix.lower() == ".json":
            content = json.loads(raw.decode("utf-8-sig"))
            rows = content["rows"] if isinstance(content, dict) else content
        elif path.suffix.lower() == ".csv":
            mapping = column_map or {}
            rows = [{field: row.get(original) for field, original in mapping.items()}
                    if mapping else dict(row) for row in csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))]
            for row in rows:
                for key in ("campus_label", "phone", "homepage_url", "operation_effective_date", "operation_evidence_url"):
                    row[key] = row.get(key) or None
                row["operation_status"] = row.get("operation_status") or "unknown"
                if not row.get("external_key"):
                    # 공식 코드가 없는 파일의 행은 자료원·유형·이름·주소 범위에서만 안정적이다.
                    identity = [metadata["provider"], row.get("type"), row.get("name"), row.get("address"), row.get("campus_label")]
                    row["external_key"] = "file:" + hashlib.sha256(canonical(identity).encode()).hexdigest()
        else:
            raise ValueError
        payload = {"schema_version": "4.0", "source": metadata, "rows": rows}
        _validator("RegistryImport").validate(payload)
        return payload
    except Exception:
        raise ProviderError("parse_failed", "공식 파일 형식과 열 매핑을 확인하세요. JSON 또는 UTF-8 CSV를 지원합니다.", "registry") from None


def neis_registry(rows: list[dict]) -> dict:
    """인증 조회의 광주 학교 정형 행을 손실 없이 공식 적재 형식으로 만든다."""
    output = []
    for row in rows:
        address = str(row.get("ORG_RDNMA", "")).strip()
        district = district_for(address)
        institution_type = NEIS_TYPES.get(row.get("SCHUL_KND_SC_NM"))
        if row.get("ATPT_OFCDC_SC_CODE") != "F10" or district is None or institution_type is None:
            raise ProviderError("coverage_incomplete", "NEIS 학교 유형·광주 주소 매핑을 확인할 행이 있습니다.", "registry")
        code, name = str(row.get("SD_SCHUL_CODE", "")), str(row.get("SCHUL_NM", "")).strip()
        if not code.isdigit() or not name:
            raise ProviderError("provider_error", "NEIS 필수 학교 코드·이름이 없습니다.", "registry")
        homepage = normalize_homepage(row.get("HMPG_ADRES"))
        output.append({"external_key": f"F10:{code}", "name": name, "type": institution_type,
            "district": district, "address": address, "campus_label": None,
            "phone": row.get("ORG_TELNO") or None, "homepage_url": homepage,
            "operation_status": "unknown", "operation_effective_date": None, "operation_evidence_url": None})
    return {"schema_version": "4.0", "source": {"provider": "neis", "source_name": "NEIS 광주 학교기본정보",
        "official_url": "https://open.neis.go.kr/hub/schoolInfo?ATPT_OFCDC_SC_CODE=F10&Type=json",
        "as_of": date.today().isoformat(), "scope_types": sorted(set(NEIS_TYPES.values())),
        "scope_districts": sorted(DISTRICTS), "coverage_complete": True,
        "completeness_note": "인증 schoolInfo F10 요청의 list_total_count와 모든 페이지 행 수 대조. 다른 유형 목록의 완전성은 포함하지 않음.",
        "expected_total": len(rows), "source_hash": hashlib.sha256(canonical(rows).encode()).hexdigest()}, "rows": output}


async def sync_registry(settings, provider: str, meter=None) -> dict:
    """공식 학교 API와 대학 표준자료의 전체 페이지를 갱신한다.

    Args:
        settings: 확정된 실행 설정.
        provider: 목록 자료원 ID.
        meter: 영속 실행 예산 예약기.
    Returns:
        인증된 공식 API에서 읽은 RegistryImport.
    Raises:
        ProviderError: 키 누락, 미지원 API 또는 응답 무결성 실패.
    """
    from .providers import HttpGateway, NeisProvider
    if provider not in {"neis", "higher_education"}:
        raise ProviderError("configuration_required",
            "이 자료원의 승인된 API 연결은 준비되지 않았습니다. 공식 JSON·UTF-8 CSV 원본과 출처 URL·기준일·범위를 준비해 목록 가져오기를 이용하세요.",
            provider)
    gateway = HttpGateway(settings, meter)
    try:
        if provider == "higher_education":
            from .official_registry import HigherEducationProvider
            verified = await HigherEducationProvider(settings, meter, gateway).registry()
            validate_registry_import(verified.registry)
            return {"registry": verified.registry, "_verified_registry": verified}
        result = await NeisProvider(settings, meter, gateway).school_registry()
        validate_registry_import(result)
        return result
    finally:
        await gateway.close()
