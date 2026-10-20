"""4.0 JSON 계약을 런타임 입력·출력 검증에 함께 사용하는 모델이다."""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import RootModel, model_validator
from pydantic_core import core_schema

SCHEMA = json.loads(Path(__file__).with_name("domain.schema.json").read_text(encoding="utf-8"))
FIELDS = ("ceremony_status", "event_date", "start_time", "venue")
VALIDATORS = {name: Draft202012Validator({"$ref": f"#/$defs/{name}", "$defs": SCHEMA["$defs"]}, format_checker=FormatChecker()) for name in SCHEMA["$defs"]}


class ApiError(Exception):
    """HTTP와 CLI가 공통으로 표시할 도메인 오류를 보관한다."""

    def __init__(self, code: str, message: str, status: int = 422, details: dict | None = None):
        """오류를 구성한다.

        Args:
            code: 기계가 분류할 오류 코드.
            message: 사용자가 이해할 설명.
            status: HTTP 상태 코드.
            details: 계약에서 허용한 추가 오류 정보.
        """
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.details = details or {}
        self.status_code = status

    def as_dict(self) -> dict:
        """오류 응답을 반환한다.

        Returns:
            Error 계약 객체.
        """
        return {"code": self.code, "message": self.message, "retryable": self.status >= 500, "details": self.details}


def validate(name: str, data: Any) -> Any:
    """타입 강제 변환 없이 정확한 JSON 계약을 검사한다.

    Args:
        name: domain.schema.json 정의 이름.
        data: 검사할 JSON 값.
    Returns:
        입력 값의 독립 복사본.
    Raises:
        ApiError: 누락·잘못된 날짜·임의 추가 필드 등 계약 위반.
    """
    if isinstance(data, RootModel):
        data = data.root
    errors = sorted(VALIDATORS[name].iter_errors(data), key=lambda error: str(error.path))
    if errors:
        error = errors[0]
        path = ".".join(map(str, error.absolute_path)) or "$"
        raise ApiError("validation_error", f"{name}.{path}: {error.validator} 계약 조건을 충족하지 않습니다.", details={"fields": [path]})
    return copy.deepcopy(data)


class ContractModel(RootModel[Any]):
    """JSON Schema의 조건부 규칙까지 적용하는 Pydantic 경계 모델이다."""
    contract_name: ClassVar[str] = ""

    @model_validator(mode="before")
    @classmethod
    def validate_contract(cls, value: Any) -> Any:
        """입력 계약을 검증한다.

        Args:
            value: API 또는 Python에서 제출한 값.
        Returns:
            계약을 충족한 JSON 값.
        Raises:
            ValueError: Pydantic 오류로 변환할 계약 위반.
        """
        try:
            return validate(cls.contract_name, value)
        except ApiError as error:
            raise ValueError(error.message) from error

    @classmethod
    def __get_pydantic_json_schema__(cls, schema: core_schema.CoreSchema, handler: Any) -> dict:
        """원본 계약의 중첩 참조를 인라인화해 OpenAPI에 노출한다.

        Args:
            schema: Pydantic 내부 스키마.
            handler: Pydantic 스키마 처리기.
        Returns:
            런타임 검증과 동일한 JSON Schema.
        """
        def expand(value: Any) -> Any:
            if isinstance(value, list):
                return [expand(item) for item in value]
            if isinstance(value, dict):
                if "$ref" in value:
                    return expand(SCHEMA["$defs"][value["$ref"].split("/")[-1]])
                return {key: expand(item) for key, item in value.items()}
            return value
        return {"title": cls.contract_name, **expand(SCHEMA["$defs"][cls.contract_name])}


_MODELS: dict[str, type[ContractModel]] = {}


def model_for(name: str) -> type[ContractModel]:
    """명명된 계약에 대응하는 Pydantic 모델을 반환한다.

    Args:
        name: 계약 정의 이름.
    Returns:
        캐시된 RootModel 하위 클래스.
    """
    if name not in _MODELS:
        _MODELS[name] = type(name, (ContractModel,), {"contract_name": name, "__module__": __name__})
    return _MODELS[name]


def utcnow() -> str:
    """현재 UTC 기록 시각을 반환한다.

    Returns:
        timezone을 포함한 ISO 8601 문자열.
    """
    return datetime.now(timezone.utc).isoformat()


def normalize_instant(value: str | None) -> str | None:
    """명시된 시각을 UTC로 정규화한다.

    Args:
        value: timezone을 포함한 시각 또는 미확인 null.
    Returns:
        UTC 시각 또는 null.
    Raises:
        ApiError: timezone 없는 값.
    """
    if value is None:
        return None
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ApiError("validation_error", "확인 시각에는 시간대가 필요합니다.")
    return moment.astimezone(timezone.utc).isoformat()


def new_id() -> str:
    """서버 UUID를 생성한다.

    Returns:
        UUID4 문자열.
    """
    return str(uuid4())


def dumps(value: Any) -> str:
    """해시와 DB에 동일한 JSON 직렬화를 사용한다.

    Args:
        value: JSON 호환 값.
    Returns:
        키 순서를 고정한 JSON 문자열.
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value: Any) -> str:
    """변동 시각을 제외한 호출자 입력의 안정적인 해시를 만든다.

    Args:
        value: 식별할 JSON 값.
    Returns:
        SHA-256 hex 문자열.
    """
    return hashlib.sha256(dumps(value).encode("utf-8")).hexdigest()
