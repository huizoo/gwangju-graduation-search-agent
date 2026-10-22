"""파일 설정을 읽고 실행에 필요한 경로와 허용 범위를 검사한다."""

from copy import deepcopy
from dataclasses import dataclass, field
import ipaddress
import json
import os
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from dotenv import dotenv_values
from jsonschema import Draft202012Validator, FormatChecker

from .configuration import (
    ConfigurationError, llm_connection_plan, missing_service_settings,
    resolve_configuration, source_configuration_hash,
)

ROOT = Path(__file__).resolve().parents[1]


@dataclass(repr=False)
class Settings:
    """공유 설정과 메모리 전용 비밀을 분리한 실행 설정.

    Args:
        data: 직렬화할 수 있는 비밀 제외 설정.
        secrets: 외부 서비스 인증에만 사용하는 값.
        root: 패키지와 설정 파일의 기준 경로.
    """

    data: dict
    secrets: dict = field(repr=False)
    root: Path = ROOT

    def __repr__(self) -> str:
        """로그에 비밀값이 섞이지 않는 짧은 표현을 반환한다."""
        return f"Settings(mode={self.data['app']['mode']!r}, backend={self.data['app']['llm_backend']!r})"

    @property
    def data_dir(self) -> Path:
        """모드별로 격리한 지속 데이터 디렉터리를 반환한다."""
        base = self.data["app"]["data_dir"]
        path = Path(base).expanduser() if base else self.root / "data"
        if not path.is_absolute():
            path = self.root / path
        return path / self.data["app"]["mode"]

    @property
    def source_config_hash(self) -> str:
        """기존 작업의 재개 호환성을 확인할 설정 해시를 반환한다."""
        return source_configuration_hash(self.data)

    def connection_summary(self) -> dict:
        """연결 성공을 추정하지 않고 설정 유무만 비밀 없이 반환한다."""
        combined = {**self.data, "secrets": self.secrets}
        plan = llm_connection_plan(combined)
        return {
            "llm": plan,
            "neis_missing": missing_service_settings(combined, "neis"),
            "search_missing": (
                missing_service_settings(combined, "tavily")
                if self.data["app"]["search_provider"] == "tavily" else []
            ),
            "external_transfer": deepcopy(self.data["external_transfer"]),
            "mode": self.data["app"]["mode"],
        }


def validate_configuration(data: dict, root: Path = ROOT) -> None:
    """타입 검사 뒤 실제 운영 범위와 로컬 서버 경계를 확인한다.

    Args:
        data: 비밀을 제외한 병합 설정.
        root: 계약 파일을 찾을 프로젝트 경로.
    Raises:
        ConfigurationError: 한도·주소·모드·시즌 또는 추적 설정이 유효하지 않은 경우.
    """
    schema = json.loads((root / "app/domain.schema.json").read_text())
    validator = Draft202012Validator(
        {"$ref": "#/$defs/ExecutionLimits", "$defs": schema["$defs"]},
        format_checker=FormatChecker(),
    )
    errors = sorted(validator.iter_errors(data["execution"]), key=lambda e: str(e.path))
    if errors:
        key = ".".join(str(x) for x in errors[0].path)
        raise ConfigurationError(f"EXECUTION_LIMIT_INVALID: {key}")
    app = data["app"]
    try:
        local = ipaddress.ip_address(app["host"]).is_loopback
    except ValueError:
        local = False
    if not local:
        raise ConfigurationError("APP_HOST_MUST_BE_LOOPBACK")
    if type(app["port"]) is not int or not 1024 <= app["port"] <= 65535:
        raise ConfigurationError("APP_PORT_INVALID")
    if type(app["poll_interval_ms"]) is not int or app["poll_interval_ms"] < 250:
        raise ConfigurationError("POLL_INTERVAL_MS_INVALID")
    if app["timezone"] != "Asia/Seoul":
        raise ConfigurationError("APP_TIMEZONE_INVALID")
    if app["mode"] == "live" and app["llm_backend"] == "fake":
        raise ConfigurationError("FAKE_BACKEND_REQUIRES_DEMO")
    if data["publication"]["policy_version"] != "4.0":
        raise ConfigurationError("PUBLICATION_POLICY_UNSUPPORTED")
    if app["llm_backend"] == "ollama":
        url = urlsplit(data["models"]["ollama_base_url"])
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ConfigurationError("OLLAMA_BASE_URL_INVALID")
    tracing = data["tracing"]
    if type(tracing["enabled"]) is not bool:
        raise ConfigurationError("LANGSMITH_TRACING_INVALID")
    if not isinstance(tracing["project"], str) or not tracing["project"].strip():
        raise ConfigurationError("LANGSMITH_PROJECT_INVALID")


def load_settings(
    root: Path = ROOT, process_values: Mapping[str, str] | None = None,
    dotenv_path: Path | None = None,
) -> Settings:
    """기존 파일을 변경하지 않고 설정을 읽는다.

    Args:
        root: defaults와 env-map을 가진 프로젝트 경로.
        process_values: 테스트 시 격리할 프로세스 환경; 생략하면 현재 환경.
        dotenv_path: 기본 .env 대신 읽을 경로.
    Returns:
        비밀과 공유 설정이 분리된 Settings.
    Raises:
        ConfigurationError: 알 수 없는 .env 키나 잘못된 설정이 있는 경우.
    """
    root = Path(root)
    defaults = json.loads((root / "config/defaults.json").read_text())
    mapping = json.loads((root / "config/env-map.json").read_text())
    merged = resolve_configuration(
        defaults, mapping, dotenv_values(dotenv_path or root / ".env", interpolate=False),
        os.environ if process_values is None else process_values,
    )
    secrets = merged.pop("secrets")
    validate_configuration(merged, root)
    return Settings(merged, secrets, root)
