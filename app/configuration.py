"""설정 우선순위와 비밀 제외 작업 해시를 계산한다.

4.0 인계 사양의 순수 병합 함수를 실행 로더에서 재사용한다.
"""

from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Mapping


_LLM_BACKENDS = frozenset({"ollama", "openai", "fake"})
_TRANSFER_FIELDS = frozenset({"document_text_allowed", "image_allowed"})


class ConfigurationError(ValueError):
    """입력값을 노출하지 않고 설정 키와 오류 종류만 전달하는 예외."""


def _is_unset(value: Any) -> bool:
    """미입력 값을 구분하되 명시적인 False와 0은 보존한다."""
    return value is None or (isinstance(value, str) and not value.strip())


def _get_path(values: Mapping[str, Any], path: str) -> Any:
    """점으로 구분한 기존 경로의 값을 읽는다."""
    current = values
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            raise KeyError(path)
        current = current[part]
    return current


def _set_path(values: dict[str, Any], path: str, value: Any) -> None:
    """검사된 경로에 값을 넣고 필요한 비밀값 사전만 생성한다."""
    parts = path.split(".")
    current = values
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def _parse_value(key: str, value: Any, specification: Mapping[str, Any]) -> Any:
    """허용된 타입과 열거값을 검사하며 오류에 입력값을 포함하지 않는다."""
    expected = specification["type"]
    if isinstance(value, str):
        value = value.strip()
    if expected == "string":
        valid = isinstance(value, str)
    elif expected == "integer":
        valid = type(value) is int
        if isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]+", value):
            value = int(value)
            valid = True
    elif expected == "boolean":
        valid = type(value) is bool
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            value = value.lower() == "true"
            valid = True
    else:
        raise ConfigurationError(f"MAPPING_TYPE_INVALID: {key}")
    if not valid:
        raise ConfigurationError(f"VALUE_TYPE_INVALID: {key}")
    if "allowed" in specification and value not in specification["allowed"]:
        raise ConfigurationError(f"VALUE_NOT_ALLOWED: {key}")
    return value


def _llm_profiles(defaults: Mapping[str, Any]) -> Mapping[str, Mapping[str, bool]]:
    """공개 문서 전송 권한만 가진 제공자별 프로필을 검사한다.

    Args:
        defaults: 비밀값이 없는 기본 설정.
    Returns:
        지원하는 세 백엔드의 불리언 권한 프로필.
    Raises:
        ConfigurationError: 프로필에 누락·미등록 항목·다른 타입이 있는 경우.
    """
    profiles = defaults.get("llm_profiles")
    if not isinstance(profiles, Mapping) or set(profiles) != _LLM_BACKENDS:
        raise ConfigurationError("LLM_PROFILES_INVALID")
    for profile in profiles.values():
        if (
            not isinstance(profile, Mapping)
            or set(profile) != _TRANSFER_FIELDS
            or any(type(value) is not bool for value in profile.values())
        ):
            raise ConfigurationError("LLM_PROFILE_PERMISSIONS_INVALID")
    return profiles


def resolve_configuration(
    defaults: Mapping[str, Any],
    mapping: Mapping[str, Mapping[str, Any]],
    dotenv_values: Mapping[str, Any],
    process_values: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """기본값 위에 필요한 환경 설정만 적용한 독립적인 사본을 만든다.

    Args:
        defaults: 비밀값이 없는 기본 설정.
        mapping: 허용된 환경 변수의 대상 경로·타입·선택적 열거값.
        dotenv_values: 이미 파싱한 .env 항목. 빈 문자열과 None은 미입력이다.
        process_values: 프로세스 환경의 명시적 사본. 등록되지 않은 키는 무시한다.
    Returns:
        기본값 → 선택된 LLM 프로필 → 비어 있지 않은 .env → 비어 있지 않은
        프로세스 환경 순서의 설정. 백엔드는 같은 환경 우선순위로 먼저 결정한다.
        비밀값은 반환 객체의 secrets에만 존재한다. 반환 객체 전체를 저장·출력하면 안 된다.
    Raises:
        ConfigurationError: 알 수 없는 .env 키, 잘못된 매핑·타입·열거값이 있는 경우.
    """
    if "secrets" in defaults:
        raise ConfigurationError("SECRETS_NOT_ALLOWED_IN_DEFAULTS")
    profiles = _llm_profiles(defaults)
    result = deepcopy(dict(defaults))
    result["secrets"] = {}
    for key, specification in mapping.items():
        path = specification.get("path", "")
        if not isinstance(path, str) or not path or any(not part for part in path.split(".")):
            raise ConfigurationError(f"MAPPING_PATH_INVALID: {key}")
        is_secret = specification.get("secret", False)
        if type(is_secret) is not bool:
            raise ConfigurationError(f"MAPPING_SECRET_INVALID: {key}")
        # 비밀값을 일반 설정 경로에 놓으면 향후 설정 저장 과정에서 유출될 수 있다.
        if is_secret != path.startswith("secrets."):
            raise ConfigurationError(f"MAPPING_SECRET_PATH_INVALID: {key}")
        if not is_secret:
            try:
                _get_path(defaults, path)
            except KeyError:
                raise ConfigurationError(f"MAPPING_PATH_MISSING: {key}") from None
        if specification.get("type") not in {"string", "integer", "boolean"}:
            raise ConfigurationError(f"MAPPING_TYPE_INVALID: {key}")
    for key in dotenv_values:
        if key not in mapping:
            raise ConfigurationError(f"UNKNOWN_ENV_KEY: {key}")
    selector = mapping.get("LLM_BACKEND", {})
    if selector.get("path") != "app.llm_backend" or selector.get("type") != "string":
        raise ConfigurationError("LLM_SELECTOR_MAPPING_INVALID")
    layers = (dotenv_values, process_values or {})
    backend = _parse_value("LLM_BACKEND", _get_path(defaults, "app.llm_backend"), selector)
    for overrides in layers:
        if "LLM_BACKEND" in overrides and not _is_unset(overrides["LLM_BACKEND"]):
            backend = _parse_value("LLM_BACKEND", overrides["LLM_BACKEND"], selector)
    if backend not in _LLM_BACKENDS:
        raise ConfigurationError("VALUE_NOT_ALLOWED: LLM_BACKEND")
    # 권한을 먼저 정한 뒤 명시적 환경 값을 적용해 false 재정의를 보존한다.
    result["external_transfer"].update(deepcopy(dict(profiles[backend])))
    for overrides in layers:
        for key, value in overrides.items():
            if key not in mapping or _is_unset(value):
                continue
            specification = mapping[key]
            parsed = _parse_value(key, value, specification)
            _set_path(result, specification["path"], parsed)
    models = result.get("models", {})
    if _is_unset(models.get("openai_quality_model")):
        models["openai_quality_model"] = models.get("openai_fast_model", "")
    # 연결 정보가 없더라도 기존 기록 조회·수기 입력까지 금지하지 않는다.
    # 누락 점검은 실제로 해당 서비스를 사용하려는 단계에서 별도로 수행한다.
    return result


def missing_service_settings(config: Mapping[str, Any], service: str) -> list[str]:
    """서비스 호출에 필요한 미입력 설정의 환경 변수명만 반환한다.

    Args:
        config: resolve_configuration의 결과.
        service: neis, tavily, openai, ollama 중 점검할 서비스.
    Returns:
        비어 있는 연결 설정의 키 이름 목록. 빈 목록은 실제 연결 성공을 뜻하지 않는다.
        OpenAI 원문·이미지 전송 허용 여부는 이 점검과 별도의 기능 권한이다.
    Raises:
        ConfigurationError: 지원하지 않는 서비스 이름이 전달된 경우.
    """
    requirements = {
        "neis": [("NEIS_API_KEY", "secrets.neis_api_key")],
        "tavily": [("TAVILY_API_KEY", "secrets.tavily_api_key")],
        "openai": [
            ("OPENAI_API_KEY", "secrets.openai_api_key"),
            ("OPENAI_FAST_MODEL", "models.openai_fast_model"),
        ],
        "ollama": [
            ("OLLAMA_BASE_URL", "models.ollama_base_url"),
            ("OLLAMA_TEXT_MODEL", "models.ollama_text_model"),
        ],
    }
    if service not in requirements:
        raise ConfigurationError("UNKNOWN_SERVICE")
    missing = []
    for key, path in requirements[service]:
        try:
            value = _get_path(config, path)
        except KeyError:
            value = None
        if _is_unset(value):
            missing.append(key)
    return missing


def llm_connection_plan(config: Mapping[str, Any]) -> dict[str, Any]:
    """선택한 백엔드의 연결 계획만 비밀값 없이 반환한다.

    Args:
        config: resolve_configuration의 결과.
    Returns:
        백엔드·모델·선택적 로컬 주소·누락 키 이름. SDK 생성이나 연결 검사는 하지 않는다.
    Raises:
        ConfigurationError: 지원하지 않는 백엔드가 전달된 경우.
    """
    backend = config.get("app", {}).get("llm_backend")
    if not isinstance(backend, str) or backend not in _LLM_BACKENDS:
        raise ConfigurationError("VALUE_NOT_ALLOWED: LLM_BACKEND")
    models = config.get("models", {})
    if backend == "ollama":
        text_model = models.get("ollama_text_model")
        quality_model = text_model
        base_url = models.get("ollama_base_url")
    elif backend == "openai":
        text_model = models.get("openai_fast_model")
        quality_model = models.get("openai_quality_model")
        if _is_unset(quality_model):
            quality_model = text_model
        base_url = None
    else:
        text_model = quality_model = base_url = None
    return {
        "backend": backend,
        "text_model": text_model,
        "quality_model": quality_model,
        "base_url": base_url,
        "missing_settings": [] if backend == "fake" else missing_service_settings(config, backend),
    }


def _source_config_value(config: Mapping[str, Any], path: str, expected: type) -> Any:
    """해시 허용 경로의 타입을 검사하며 입력값은 오류에 포함하지 않는다."""
    try:
        value = _get_path(config, path)
    except KeyError:
        raise ConfigurationError(f"SOURCE_CONFIG_MISSING: {path}") from None
    if type(value) is not expected:
        raise ConfigurationError(f"SOURCE_CONFIG_TYPE_INVALID: {path}")
    return value


def source_configuration_hash(config: Mapping[str, Any]) -> str:
    """작업의 수집·게시 설정 허용 목록만 정규 JSON으로 해시한다.

    Args:
        config: resolve_configuration으로 결합한 설정.
    Returns:
        모드·LLM/검색 제공자, 선택한 모델/주소, 문서 도구 두 플래그, 검색 필터,
        전송 권한 두 플래그, 게시 정책/규칙, 이미지 후보 플래그의 SHA-256.
        Ollama 이미지 모델은 로컬 이미지 후보 활성 시에만 포함한다.
        비밀값·누락 인증키·비선택 모델·화면/추적 설정·시즌·실행 한도는 제외한다.
    Raises:
        ConfigurationError: 허용 경로가 없거나 예상 타입과 다른 경우.
    """
    plan = llm_connection_plan(config)
    selected = {key: plan[key] for key in ("backend", "text_model", "quality_model", "base_url")}
    for key, value in selected.items():
        expects_none = key != "backend" and (
            plan["backend"] == "fake" or (key == "base_url" and plan["backend"] == "openai")
        )
        if (expects_none and value is not None) or (not expects_none and not isinstance(value, str)):
            raise ConfigurationError(f"SOURCE_CONFIG_TYPE_INVALID: llm.{key}")
    image_candidates_enabled = _source_config_value(config, "models.image_candidates_enabled", bool)
    rule_ids = _source_config_value(config, "publication.auto_rule_ids", list)
    if any(type(rule_id) is not str for rule_id in rule_ids):
        raise ConfigurationError("SOURCE_CONFIG_TYPE_INVALID: publication.auto_rule_ids")
    projection = {
        "app": {
            key: _source_config_value(config, f"app.{key}", str)
            for key in ("mode", "llm_backend", "search_provider")
        },
        "llm": selected,
        "documents": {
            key: _source_config_value(config, f"documents.{key}", bool)
            for key in ("browser_fallback_enabled", "hwp_preview_enabled")
        },
        "search": {"domain_filter_enabled": _source_config_value(config, "search.domain_filter_enabled", bool)},
        "external_transfer": {
            key: _source_config_value(config, f"external_transfer.{key}", bool)
            for key in ("document_text_allowed", "image_allowed")
        },
        "publication": {
            "policy_version": _source_config_value(config, "publication.policy_version", str),
            "auto_rule_ids": rule_ids,
        },
        "models": {
            "image_candidates_enabled": image_candidates_enabled,
            "ollama_vision_model": (
                _source_config_value(config, "models.ollama_vision_model", str)
                if plan["backend"] == "ollama" and image_candidates_enabled
                else None
            ),
        },
    }
    canonical = json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def can_resume_configuration(scope_snapshot: Mapping[str, Any] | None, config: Mapping[str, Any]) -> bool:
    """저장된 설정 해시를 대조해 다른 설정으로 작업이 재개되지 않도록 한다.

    Args:
        scope_snapshot: 저장한 JobScopeSnapshot. 이전 형식이나 누락은 재개 불가다.
        config: resolve_configuration으로 결합한 현재 설정.
    Returns:
        해시가 일치할 때만 True. 인증키 준비와 연결 성공은 별도로 확인한다.
    Raises:
        ConfigurationError: 현재 설정의 해시 대상 경로가 유효하지 않은 경우.
    """
    if not isinstance(scope_snapshot, Mapping):
        return False
    stored = scope_snapshot.get("source_config_hash")
    if not isinstance(stored, str) or not re.fullmatch(r"[0-9a-f]{64}", stored):
        return False
    return stored == source_configuration_hash(config)
