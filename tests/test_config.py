"""실행 로더의 환경 우선순위·비밀 분리·재개 해시를 검사한다."""

import json
from pathlib import Path
import shutil

import pytest

from app.config import ROOT, ConfigurationError, load_settings


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


@pytest.mark.parametrize("content", [
    "UNKNOWN_KEY=secret", "MAX_CONCURRENT_INSTITUTIONS=0", "APP_HOST=0.0.0.0",
    "APP_MODE=live\nLLM_BACKEND=fake", "AUTO_OPEN_BROWSER=maybe",
])
def test_invalid_settings_do_not_leak_values(root, content):
    """잘못된 입력은 비밀값을 표시하지 않는 설정 오류로 거절한다."""
    (root / ".env").write_text(content)
    with pytest.raises(ConfigurationError) as error:
        load_settings(root, {})
    assert "secret" not in str(error.value)


def test_demo_data_is_separate(root):
    """가상 데이터와 실제 기록의 파일 경로를 격리한다."""
    live = load_settings(root, {})
    demo = load_settings(root, {"APP_MODE": "demo", "LLM_BACKEND": "fake"})
    assert live.data_dir != demo.data_dir
    assert demo.data_dir.name == "demo"
