"""실제 Git index·tree와 실행 경로에서 공개 안전 검사의 경계를 검증한다."""

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


PRIVATE_MARKER = b"-----BEGIN " + b"PRIVATE KEY-----"


SPEC = importlib.util.spec_from_file_location(
    "publication_safety", Path(__file__).resolve().parents[1] / "scripts/check_publication_safety.py")
GUARD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GUARD
SPEC.loader.exec_module(GUARD)


def git(root, *args):
    """격리 저장소만 조작하며 사용자 Git 설정을 변경하지 않는다."""
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True).stdout


@pytest.fixture
def repo(tmp_path):
    """사용자 데이터와 분리된 빈 Git 저장소를 만든다."""
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "검사")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    return tmp_path


def stage(repo, name, content):
    """파일을 만들고 원래 이름 그대로 index에 추가한다."""
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    git(repo, "add", "--", name)


def test_reads_staged_blob_not_clean_working_file(repo):
    """커밋에는 비밀이 남고 작업 파일만 정제한 경우를 차단한다."""
    secret = b"sk-" + b"a" * 35
    stage(repo, "fixture.txt", secret)
    (repo / "fixture.txt").write_text("redacted")
    findings, count = GUARD.check(repo, "staged")
    assert count == 1 and [item.category for item in findings] == ["api-key"]
    assert secret.decode() not in json.dumps([item.__dict__ for item in findings])


def test_clean_index_does_not_read_unstaged_secret(repo):
    """아직 추가하지 않은 로컬 수정은 공개 대상과 혼동하지 않는다."""
    stage(repo, "guide.txt", b"public")
    (repo / "guide.txt").write_text("sk-" + "a" * 35)
    assert GUARD.check(repo, "staged") == ([], 1)


@pytest.mark.parametrize("path,category", [
    ("output/schedule.html", "runtime-output"), ("runtime/result.json", "runtime-output"),
    ("tests/fixtures/raw.sqlite3", "database-file"), ("subdir/backups/manual.json", "private-archive"),
    (".env", "secret-file"), ("local.env", "secret-file"), ("config/server.key", "secret-file"),
])
def test_force_added_private_paths_are_rejected(repo, path, category):
    """gitignore를 우회해 추가한 실행 자료도 내용과 무관하게 차단한다."""
    stage(repo, path, b"ordinary bytes")
    assert category in {item.category for item in GUARD.check(repo, "staged")[0]}


def test_safe_examples_public_school_data_and_empty_csrf_are_allowed(repo):
    """빈 설정 예제와 공개 학교 주소·전화는 무차별 개인정보 검사로 막지 않는다."""
    stage(repo, ".env.example", b"OPENAI_API_KEY=\n")
    stage(repo, "assets/school.html", '<p>광주광역시 학교로 1, 062-123-4567</p><input type="hidden" name="csrf_token" value="">'.encode())
    assert GUARD.check(repo, "tracked") == ([], 2)


def test_html_token_is_detected_in_binary_extension(repo):
    """원본 BIN이나 여러 줄 HTML의 hidden 보안값도 확장자로 제외하지 않는다."""
    stage(repo, "assets/original.bin", b'<input\n name="csrf_token" type="hidden"\n value="secret-session">')
    findings, _ = GUARD.check(repo, "tracked")
    assert [(item.line, item.category) for item in findings] == [(1, "html-security-token")]


def test_local_key_match_without_printing_secret(repo):
    """알려진 접두사가 없는 실제 환경 키도 로컬 검사에서 탐지한다."""
    secret = "nonstandard-credential-12345"
    (repo / ".env").write_text(f"SCHOOLINFO_API_KEY='{secret}' # local only\n")
    stage(repo, "log.txt", f"request={secret}".encode())
    findings, _ = GUARD.check(repo, "staged")
    assert [item.category for item in findings] == ["local-secret"]
    assert secret not in json.dumps([item.__dict__ for item in findings])


def test_symlink_is_rejected_without_reading_external_target(repo, tmp_path):
    """Git의 symlink 내용만 취급하고 가리키는 파일은 읽지 않는다."""
    (repo / "external").symlink_to("/not-existing-private-file")
    git(repo, "add", "external")
    findings, _ = GUARD.check(repo, "tracked")
    assert [item.category for item in findings] == ["unscanned-link"]


def test_tree_uses_committed_contents_and_staged_delete_is_not_scanned(repo):
    """이후 삭제한 파일도 지정 커밋 검사에는 보이며 삭제 자체는 안전하다."""
    stage(repo, "old.txt", PRIVATE_MARKER)
    git(repo, "commit", "-qm", "검사 자료")
    git(repo, "rm", "old.txt")
    assert GUARD.check(repo, "staged") == ([], 0)
    assert GUARD.check(repo, "tree", "HEAD")[0][0].category == "private-key"


def test_exact_home_exception_does_not_allow_other_lines_or_secrets(repo):
    """허용은 정확한 줄에만 적용하고 API 키를 예외로 만들지 못한다."""
    home = ("/" + "Users" + "/fixture_user/").encode()
    stage(repo, "reference/example.txt", home + b"old\n" + home + b"new")
    config = {"allow": [{"path": "reference/example.txt", "category": "personal-home",
                          "line_sha256": hashlib.sha256(home + b"old").hexdigest(),
                          "reason": "변경할 수 없는 외부 계약의 고정 검사 예시"}]}
    stage(repo, GUARD.CONFIG_PATH, json.dumps(config).encode())
    findings, _ = GUARD.check(repo, "tracked")
    assert len(findings) == 1 and findings[0].line == 2
    config["allow"][0]["category"] = "api-key"
    stage(repo, GUARD.CONFIG_PATH, json.dumps(config).encode())
    with pytest.raises(ValueError):
        GUARD.check(repo, "tracked")


def test_unstaged_allowlist_cannot_bypass_index(repo):
    """검사 설정도 index에서 읽으므로 로컬 파일만 고쳐 우회할 수 없다."""
    content = ("/" + "Users" + "/fixture_user/file").encode()
    stage(repo, "guide.md", content)
    (repo / "config").mkdir()
    (repo / GUARD.CONFIG_PATH).write_text(json.dumps({"allow": [{
        "path": "guide.md", "category": "personal-home", "line_sha256": hashlib.sha256(content).hexdigest(),
        "reason": "작업 파일만 바꾼 승인 시도"}]}))
    assert GUARD.check(repo, "staged")[0][0].category == "personal-home"


def test_newline_filename_is_handled_without_shell_or_log_injection(repo, capsys, monkeypatch):
    """특수문자 이름은 셸 실행하지 않고 JSON으로 이스케이프해 출력한다."""
    stage(repo, "$(touch unexpected)\nfile.txt", PRIVATE_MARKER)
    monkeypatch.setattr(GUARD, "ROOT", repo)
    monkeypatch.setattr(sys, "argv", ["check", "--staged"])
    assert GUARD.main() == 1
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2 and "\\n" in lines[0]
    assert not (repo / "unexpected").exists()


def test_reviewed_data_requires_exact_index_manifest(repo):
    """새 원본·변경된 스크린샷은 자동 탐지와 별도로 검토 등록을 요구한다."""
    content = b"sanitized public source"
    path = "assets/registry/school.bin"
    stage(repo, path, content)
    assert GUARD.check(repo, "tracked")[0][0].category == "unreviewed-data"
    manifest = {"version": 1, "files": {path: {
        "sha256": hashlib.sha256(content).hexdigest(), "purpose": "공개기관명부의 정제 원본"}}}
    stage(repo, GUARD.DATA_MANIFEST, json.dumps(manifest).encode())
    assert GUARD.check(repo, "tracked")[0] == []
    stage(repo, path, content + b"new personal detail")
    assert GUARD.check(repo, "tracked")[0][0].category == "unreviewed-data"


def test_manifest_cannot_approve_secret_or_empty_purpose(repo):
    """해시 승인은 실키 검사를 우회하지 않으며 공개 목적이 필요하다."""
    content = PRIVATE_MARKER
    path = "docs/evidence/capture.txt"
    stage(repo, path, content)
    manifest = {"version": 1, "files": {path: {
        "sha256": hashlib.sha256(content).hexdigest(), "purpose": "검사 증빙"}}}
    stage(repo, GUARD.DATA_MANIFEST, json.dumps(manifest).encode())
    assert [item.category for item in GUARD.check(repo, "tracked")[0]] == ["private-key"]
    manifest["files"][path]["purpose"] = ""
    stage(repo, GUARD.DATA_MANIFEST, json.dumps(manifest).encode())
    with pytest.raises(ValueError):
        GUARD.check(repo, "tracked")


def test_push_checks_committed_target_not_current_index_and_skips_deletion(repo):
    """커밋 이후 작업 파일을 정제해도 전송 대상의 비밀값을 차단한다."""
    stage(repo, "sample.txt", PRIVATE_MARKER)
    git(repo, "commit", "-qm", "검사용 커밋")
    oid = git(repo, "rev-parse", "HEAD").decode().strip()
    stage(repo, "sample.txt", b"safe working file")
    zeros = "0" * 40
    found, count = GUARD.check_push(repo, f"refs/heads/main {oid} refs/heads/main {zeros}\n")
    assert count >= 1 and found[0].category == "private-key"
    assert GUARD.check_push(repo, f"(delete) {zeros} refs/heads/old {oid}\n") == ([], 0)


def test_deleted_intermediate_secret_is_caught_before_push(repo):
    """최신 tree가 깨끗해도 중간 커밋에 추가 후 삭제한 비밀 파일을 막는다."""
    stage(repo, "guide.md", b"public")
    git(repo, "commit", "-qm", "공개 기준")
    base = git(repo, "rev-parse", "HEAD").decode().strip()
    stage(repo, ".env", b"PRIVATE_VALUE=not-a-known-key")
    git(repo, "commit", "-qm", "잘못 추가")
    git(repo, "rm", ".env")
    git(repo, "commit", "-qm", "뒤늦게 삭제")
    tip = git(repo, "rev-parse", "HEAD").decode().strip()
    assert GUARD.check(repo, "tree", tip)[0] == []
    found, _ = GUARD.check_push(repo, f"refs/heads/main {tip} refs/heads/main {base}\n")
    assert any(item.category == "secret-file" for item in found)
    # 원격 기준 커밋을 로컬에서 찾지 못해도 전체 도달 이력을 검사한다.
    found, _ = GUARD.check_history(repo, tip, "a" * 40)
    assert any(item.category == "secret-file" for item in found)


def test_old_data_without_manifest_requires_exact_tip_history_approval(repo):
    """명부가 없던 과거 자료는 최신 명부의 정확한 과거 승인으로만 허용한다."""
    path, content = "assets/registry/old.json", b'{"public": true}'
    stage(repo, path, content)
    git(repo, "commit", "-qm", "예전 공개 명부")
    assert GUARD.check_history(repo, "HEAD")[0][0].category == "unreviewed-data"
    git(repo, "rm", path)
    manifest = {"version": 1, "files": {}, "historical_files": {path: [{
        "sha256": hashlib.sha256(content).hexdigest(), "purpose": "검토한 예전 공개 명부"}]}}
    stage(repo, GUARD.DATA_MANIFEST, json.dumps(manifest).encode())
    git(repo, "commit", "-qm", "검토한 과거 자료 등록")
    assert GUARD.check_history(repo, "HEAD")[0] == []
    stage(repo, path, content)
    # 현재 파일에는 과거 승인만으로 게시를 허용하지 않는다.
    assert GUARD.check(repo, "tracked")[0][0].category == "unreviewed-data"


def test_shallow_history_fails_closed(repo, monkeypatch):
    """이력이 부족하면 중간 비밀이 없다고 잘못 판정하지 않는다."""
    real_git = GUARD.git
    monkeypatch.setattr(GUARD, "git", lambda root, *args:
                        b"true" if args == ("rev-parse", "--is-shallow-repository") else real_git(root, *args))
    with pytest.raises(RuntimeError, match="전체 이력"):
        GUARD.check_history(repo, "HEAD")


def test_real_precommit_hook_blocks_index_secret(repo):
    """실제 Git hook에서도 정제된 작업 파일 뒤에 남은 index 비밀을 거절한다."""
    import shutil
    source = Path(__file__).resolve().parents[1]
    (repo / "scripts").mkdir()
    (repo / ".githooks").mkdir()
    shutil.copy2(source / "scripts/check_publication_safety.py", repo / "scripts/check_publication_safety.py")
    shutil.copy2(source / ".githooks/pre-commit", repo / ".githooks/pre-commit")
    git(repo, "config", "core.hooksPath", ".githooks")
    stage(repo, "sample.txt", PRIVATE_MARKER)
    (repo / "sample.txt").write_text("clean working content")
    result = subprocess.run(["git", "-C", str(repo), "commit", "-qm", "거절 대상"], capture_output=True)
    assert result.returncode != 0
    assert b"private-key" in result.stdout + result.stderr
    assert PRIVATE_MARKER not in result.stdout + result.stderr


def test_oversize_blob_is_blocked_instead_of_silently_skipped(repo, monkeypatch):
    """자동 검사를 수행하지 못한 큰 자료는 성공으로 처리하지 않는다."""
    stage(repo, "large.bin", b"1234567890")
    monkeypatch.setattr(GUARD, "MAX_BYTES", 5)
    assert GUARD.check(repo, "staged")[0][0].category == "oversize-unscanned"


@pytest.mark.parametrize("remove_manifest", [False, True])
def test_unreviewed_intermediate_data_cannot_be_hidden_by_later_deletion(repo, remove_manifest):
    """새 원문을 커밋 후 지우거나 중간에 명부를 지워도 전송 검사를 통과하지 못한다."""
    manifest = {"version": 1, "files": {}}
    stage(repo, GUARD.DATA_MANIFEST, json.dumps(manifest).encode())
    git(repo, "commit", "-qm", "공개 검토 기준")
    base = git(repo, "rev-parse", "HEAD").decode().strip()
    if remove_manifest:
        git(repo, "rm", GUARD.DATA_MANIFEST)
    path, content = "docs/evidence/raw.json", b'{"unreviewed": "collected source"}'
    stage(repo, path, content)
    git(repo, "commit", "-qm", "미검토 원문 추가")
    git(repo, "rm", path)
    stage(repo, GUARD.DATA_MANIFEST, json.dumps(manifest).encode())
    git(repo, "commit", "-qm", "최신 파일에서 제거")
    tip = git(repo, "rev-parse", "HEAD").decode().strip()
    assert GUARD.check(repo, "tree", tip)[0] == []
    findings, _ = GUARD.check_push(repo, f"refs/heads/main {tip} refs/heads/main {base}\n")
    assert any(item.path == path and item.category == "unreviewed-data" for item in findings)
    if not remove_manifest:
        # 당시 명부가 존재했던 자료는 사후 historical_files 추가로 과거 누락을 우회할 수 없다.
        manifest["historical_files"] = {path: [{
            "sha256": hashlib.sha256(content).hexdigest(), "purpose": "사후 과거 승인 시도"}]}
        stage(repo, GUARD.DATA_MANIFEST, json.dumps(manifest).encode())
        git(repo, "commit", "-qm", "현재 명부에 과거 승인 추가")
        assert any(item.category == "unreviewed-data" for item in GUARD.check_history(repo, "HEAD", base)[0])


def test_history_scans_commit_message_without_echoing_secret(repo):
    """파일이 안전해도 커밋 본문에 남은 알려진 키와 로컬 키를 탐지한다."""
    api_secret = "sk-" + "B" * 35
    local_secret = "nonstandard-history-credential-123"
    (repo / ".env").write_text(f"SCHOOLINFO_API_KEY={local_secret}\n")
    stage(repo, "guide.md", b"public")
    git(repo, "commit", "-qm", "문서: 검사용 기록", "-m", f"{api_secret}\n{local_secret}")
    assert GUARD.check(repo, "tree", "HEAD")[0] == []
    findings, _ = GUARD.check_history(repo, "HEAD")
    assert {item.category for item in findings} == {"api-key", "local-secret"}
    assert all(item.path.startswith("(commit-message)/") for item in findings)
    output = json.dumps([item.__dict__ for item in findings])
    assert api_secret not in output and local_secret not in output


def test_history_approval_requires_exact_digest(repo):
    """과거 승인에 같은 경로가 있어도 다른 버전의 원문은 허용하지 않는다."""
    path = "docs/evidence/old.txt"
    stage(repo, path, b"unreviewed change")
    git(repo, "commit", "-qm", "이전 수집 원문")
    git(repo, "rm", path)
    stage(repo, GUARD.DATA_MANIFEST, json.dumps({"version": 1, "files": {}, "historical_files": {
        path: [{"sha256": hashlib.sha256(b"reviewed original").hexdigest(), "purpose": "검토한 원본"}]}}).encode())
    git(repo, "commit", "-qm", "검토한 원본만 등록")
    assert any(item.category == "unreviewed-data" for item in GUARD.check_history(repo, "HEAD")[0])
