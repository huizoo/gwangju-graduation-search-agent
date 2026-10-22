"""실행 로더의 환경 우선순위·비밀 분리·재개 해시를 검사한다."""

import json
from pathlib import Path
import shutil

import pytest
from dotenv import dotenv_values

from app.config import ROOT, ConfigurationError, load_settings
from app.configuration import can_resume_configuration


@pytest.fixture
def root(tmp_path):
    """실제 .env를 읽지 않는 격리 설정 폴더를 만든다."""
    shutil.copytree(ROOT / "config", tmp_path / "config")
    target = tmp_path / "app"
    target.mkdir(parents=True)
    shutil.copy(ROOT / "app/domain.schema.json", target)
    return tmp_path


def test_precedence_blank_false_secret_and_hash(root):
    """명시적 false와 빈값을 구분하고 키·UI 변경은 재개 해시에서 제외한다."""
    (root / ".env").write_text("LLM_BACKEND=openai\nOPENAI_API_KEY=private-sentinel\nALLOW_EXTERNAL_DOCUMENT_TEXT=false\n")
    settings = load_settings(root, {"APP_PORT": "8700", "OPENAI_FAST_MODEL": ""})
    assert settings.data["app"]["llm_backend"] == "openai"
    assert settings.data["external_transfer"]["document_text_allowed"] is False
    assert settings.data["models"]["openai_quality_model"] == settings.data["models"]["openai_fast_model"]
    assert "private-sentinel" not in repr(settings) + json.dumps(settings.data) + json.dumps(settings.connection_summary())
    assert settings.secrets["openai_api_key"] == "private-sentinel"
    old_hash = settings.source_config_hash
    settings.secrets["openai_api_key"] = "changed"
    settings.data["app"]["port"] += 1
    assert old_hash == settings.source_config_hash
    settings.data["models"]["openai_fast_model"] = "other"
    assert old_hash != settings.source_config_hash


def test_key_does_not_choose_backend(root):
    """키 입력만으로 모델 선택이나 원문 외부 전송을 바꾸지 않는다."""
    (root / ".env").write_text("OPENAI_API_KEY=private\n")
    config = load_settings(root, {})
    assert config.data["app"]["llm_backend"] == "ollama"
    assert not config.data["external_transfer"]["document_text_allowed"]


def test_example_matches_user_setup_and_keeps_secrets_blank():
    """입력 양식은 필요한 항목만 표시하고 비밀값이나 선택적 기본값을 복제하지 않는다."""
    mapping = json.loads((ROOT / "config/env-map.json").read_text())
    example = dotenv_values(ROOT / ".env.example", interpolate=False)
    assert set(example) == {key for key, spec in mapping.items() if spec["user_setup"]}
    assert all(example[key] == "" for key in example if mapping[key].get("secret"))
    assert not {"OLLAMA_BASE_URL", "OLLAMA_TEXT_MODEL", "LANGSMITH_TRACING", "LANGSMITH_PROJECT"} & set(example)


def test_ollama_defaults_and_existing_overrides(root):
    """공유 기본값을 사용하면서 기존 로컬 설정과 환경 우선순위를 보존한다."""
    defaults = load_settings(root, {})
    assert defaults.data["models"]["ollama_base_url"] == "http://127.0.0.1:11434"
    assert defaults.data["models"]["ollama_text_model"] == "qwen3:8b"
    (root / ".env").write_text(
        "OLLAMA_BASE_URL=http://127.0.0.1:11435\nOLLAMA_TEXT_MODEL=local-model:test\n"
    )
    local = load_settings(root, {})
    assert local.data["models"]["ollama_base_url"] == "http://127.0.0.1:11435"
    assert local.data["models"]["ollama_text_model"] == "local-model:test"
    process = load_settings(root, {
        "OLLAMA_BASE_URL": "http://127.0.0.1:11436", "OLLAMA_TEXT_MODEL": "process-model:test",
    })
    assert process.data["models"]["ollama_base_url"] == "http://127.0.0.1:11436"
    assert process.data["models"]["ollama_text_model"] == "process-model:test"


def test_langsmith_key_alone_is_private_and_does_not_enable_tracing(root):
    """추적 키만 입력하면 기존 동작을 유지하고 공개 설정에 키를 남기지 않는다."""
    baseline = load_settings(root, {})
    (root / ".env").write_text("LANGSMITH_API_KEY=private-tracing-sentinel\n")
    settings = load_settings(root, {})
    assert settings.data["tracing"] == {"enabled": False, "project": "graduation-agent"}
    assert settings.secrets["langsmith_api_key"] == "private-tracing-sentinel"
    assert "private-tracing-sentinel" not in (
        repr(settings) + json.dumps(settings.data) + json.dumps(settings.connection_summary())
    )
    assert settings.connection_summary() == baseline.connection_summary()
    assert settings.source_config_hash == baseline.source_config_hash


@pytest.mark.parametrize("key", ["", "LANGSMITH_API_KEY=private-tracing-sentinel\n"])
def test_optional_tracing_settings_preserve_loading_and_resume(root, key):
    """추적 키 누락은 앱 로드를 막지 않고 추적 변경은 수집 작업 재개를 막지 않는다."""
    baseline = load_settings(root, {})
    (root / ".env").write_text(
        key + "LANGSMITH_TRACING=true\nLANGSMITH_PROJECT=local-tracing-project\n"
    )
    settings = load_settings(root, {})
    assert settings.data["tracing"] == {"enabled": True, "project": "local-tracing-project"}
    assert bool(settings.secrets.get("langsmith_api_key")) == bool(key)
    assert settings.connection_summary() == baseline.connection_summary()
    assert can_resume_configuration(
        {"source_config_hash": baseline.source_config_hash}, settings.data,
    )
    disabled = load_settings(root, {
        "LANGSMITH_TRACING": "false", "LANGSMITH_PROJECT": "another-tracing-project",
    })
    assert disabled.data["tracing"] == {"enabled": False, "project": "another-tracing-project"}
    assert disabled.source_config_hash == settings.source_config_hash


@pytest.mark.parametrize(("field", "value", "error_code"), [
    ("enabled", "private-tracing-sentinel", "LANGSMITH_TRACING_INVALID"),
    ("enabled", 1, "LANGSMITH_TRACING_INVALID"),
    ("enabled", None, "LANGSMITH_TRACING_INVALID"),
    ("project", "", "LANGSMITH_PROJECT_INVALID"),
    ("project", " \t", "LANGSMITH_PROJECT_INVALID"),
    ("project", ["private-tracing-sentinel"], "LANGSMITH_PROJECT_INVALID"),
    ("project", 1, "LANGSMITH_PROJECT_INVALID"),
    ("project", None, "LANGSMITH_PROJECT_INVALID"),
])
def test_invalid_tracing_defaults_do_not_leak_values(root, field, value, error_code):
    """환경 변수뿐 아니라 파일의 추적 설정도 입력 원문 없이 타입을 검사한다."""
    defaults_path = root / "config/defaults.json"
    defaults = json.loads(defaults_path.read_text())
    defaults["tracing"][field] = value
    defaults_path.write_text(json.dumps(defaults))
    with pytest.raises(ConfigurationError) as error:
        load_settings(root, {})
    assert str(error.value) == error_code


@pytest.mark.parametrize("content", [
    "UNKNOWN_KEY=secret", "MAX_CONCURRENT_INSTITUTIONS=0", "APP_HOST=0.0.0.0",
    "APP_MODE=live\nLLM_BACKEND=fake", "AUTO_OPEN_BROWSER=maybe",
    "LANGSMITH_TRACING=private-tracing-sentinel",
])
def test_invalid_settings_do_not_leak_values(root, content):
    """잘못된 입력은 비밀값을 표시하지 않는 설정 오류로 거절한다."""
    (root / ".env").write_text(content)
    with pytest.raises(ConfigurationError) as error:
        load_settings(root, {})
    assert "secret" not in str(error.value)
    assert "private-tracing-sentinel" not in str(error.value)


def test_demo_data_is_separate(root):
    """가상 데이터와 실제 기록의 파일 경로를 격리한다."""
    live = load_settings(root, {})
    demo = load_settings(root, {"APP_MODE": "demo", "LLM_BACKEND": "fake"})
    assert live.data_dir != demo.data_dir
    assert demo.data_dir.name == "demo"
