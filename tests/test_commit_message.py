"""실제 Git hook으로 영어 전용 제목 거절과 한국어 제목 허용을 검사한다."""

import importlib.util
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("commit_message", ROOT / "scripts/check_commit_message.py")
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


@pytest.mark.parametrize("message,expected", [
    ("feat: improve export", False),
    ("feat: improve export\n\n본문만 한국어로 작성", False),
    ("", False),
    ("개선: HTML 일정표 파일명 통일", True),
    ("fix: SQLite 저장 오류 수정\n\nTechnical details are allowed.", True),
])
def test_only_title_requires_korean(message, expected):
    """기술명과 본문 언어를 제한하지 않고 제목의 설명만 확인한다."""
    assert CHECKER.validate_message(message) is expected


def test_real_hook_rejects_english_and_accepts_korean(tmp_path):
    """격리 저장소에서 실제 커밋을 거절하거나 허용하고 주석 우회를 막는다."""
    def git(*args, check=True):
        return subprocess.run(["git", "-C", str(tmp_path), *args], capture_output=True, check=check)

    git("init", "-q")
    git("config", "user.name", "검사")
    git("config", "user.email", "test@example.invalid")
    git("config", "core.hooksPath", ".githooks")
    (tmp_path / ".githooks").mkdir()
    (tmp_path / "scripts").mkdir()
    shutil.copy2(ROOT / ".githooks/commit-msg", tmp_path / ".githooks/commit-msg")
    shutil.copy2(ROOT / "scripts/check_commit_message.py", tmp_path / "scripts/check_commit_message.py")
    shutil.copy2(ROOT / "scripts/check_publication_safety.py", tmp_path / "scripts/check_publication_safety.py")
    (tmp_path / "sample.txt").write_text("public sample")
    git("add", "sample.txt")
    rejected = git("commit", "-m", "feat: add sample", check=False)
    assert rejected.returncode != 0
    assert "한국어" in rejected.stderr.decode()
    # 사용자 지정 주석 문자를 사용해도 템플릿 안내는 제목으로 인정하지 않는다.
    git("config", "core.commentChar", ";")
    message = tmp_path / "message.txt"
    message.write_text("; 한국어 템플릿 안내\nfeat: add sample\n", encoding="utf-8")
    rejected = git("commit", "-F", "message.txt", check=False)
    assert rejected.returncode != 0
    secret = "sk-" + "A" * 35
    rejected = git("commit", "-m", "개선: 설정 추가", "-m", secret, check=False)
    assert rejected.returncode != 0 and b"api-key" in rejected.stderr
    assert secret.encode() not in rejected.stderr
    local_key = "nonstandard-local-secret-13579"
    (tmp_path / ".env").write_text(f"SCHOOLINFO_API_KEY={local_key}\n")
    rejected = git("commit", "-m", "개선: 설정 추가", "-m", local_key, check=False)
    assert rejected.returncode != 0 and b"local-secret" in rejected.stderr
    assert local_key.encode() not in rejected.stderr
    git("commit", "-m", "개선: HTML 검사용 자료 추가")
    assert git("log", "-1", "--format=%s").stdout.decode().strip() == "개선: HTML 검사용 자료 추가"
