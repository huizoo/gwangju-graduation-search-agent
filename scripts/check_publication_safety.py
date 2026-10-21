"""Git에 들어가는 실제 blob에서 비밀값과 실행 산출물의 재유입을 막는다.

표준 라이브러리만 사용한다. 검사 결과에는 탐지한 값이나 원문을 출력하지 않는다.
허용 목록은 파일·분류·해당 줄 해시를 모두 고정하며 실키 예외는 허용하지 않는다.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = "config/publication-safety.json"
DATA_MANIFEST = "config/publication-data-manifest.json"
DATA_ROOTS = ("assets/registry/", "docs/evidence/", "tests/fixtures/", "reference/handoff/evidence/")
DATA_SUFFIXES = {".html", ".htm", ".bin", ".csv", ".json", ".jsonl", ".ndjson", ".png",
                 ".jpg", ".jpeg", ".webp", ".gif", ".pdf", ".txt", ".xml"}
MAX_BYTES = 20 * 1024 * 1024
RUNTIME_ROOTS = {"data", "runtime", ".runtime", "output", "cache", ".cache", "logs",
                 "backups", "exports", "archives", "artifacts", "tmp", ".tmp"}
PATTERNS = {
    "private-key": re.compile(rb"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    "api-key": re.compile(rb"(?<![A-Za-z0-9])(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}|tvly-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[A-Z0-9]{16})(?![A-Za-z0-9])"),
    "personal-home": re.compile(rb"(?:/Users/|/home/)[A-Za-z0-9._-]+/|[A-Za-z]:[\\/]Users[\\/][A-Za-z0-9._-]+[\\/]"),
}
SECRET_NAME = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)(?:_|$)", re.I)
PLACEHOLDER = re.compile(r"^(?:your[_-].*|example.*|placeholder.*|changeme|<.*>|\$\{.*\})$", re.I)


@dataclass(frozen=True)
class Finding:
    """원문 없이 공개할 수 있는 실패 위치를 나타낸다."""

    path: str
    line: int
    category: str
    line_sha256: str = ""


def git(root: Path, *arguments: str) -> bytes:
    """셸 해석 없이 프로젝트 Git의 결과만 읽는다.

    Args:
        root: 검사할 Git 저장소.
        arguments: 독립 인자로 전달할 Git 명령.
    Returns:
        Git 표준 출력 바이트.
    Raises:
        RuntimeError: Git 명령이 실패한 경우. 비밀값이 섞일 수 있는 stderr는 숨긴다.
    """
    result = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True)
    if result.returncode:
        raise RuntimeError("Git 대상을 읽을 수 없습니다.")
    return result.stdout


def blob(root: Path, oid: str) -> bytes:
    """검증한 객체 ID의 blob만 읽어 경로 해석과 symlink 추적을 피한다."""
    if not re.fullmatch(r"[0-9a-f]{40,64}", oid):
        raise ValueError("올바르지 않은 Git 객체 ID입니다.")
    if int(git(root, "cat-file", "-s", oid)) > MAX_BYTES:
        raise OverflowError("검토가 필요한 큰 파일입니다.")
    return git(root, "cat-file", "blob", oid)


def entries(root: Path, mode: str, tree: str | None = None) -> list[tuple[str, str, str]]:
    """작업 파일 대신 index 또는 고정 tree의 경로·mode·blob을 반환한다.

    Args:
        root: 저장소 경로.
        mode: staged, tracked, tree 중 하나.
        tree: tree 모드에서 검사할 커밋 또는 tree 참조.
    Returns:
        경로, Git 파일 모드, 객체 ID의 목록.
    Raises:
        RuntimeError: 충돌 중이거나 읽을 수 없는 Git 상태인 경우.
    """
    selected = None
    if mode == "staged":
        selected = set(git(root, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split(b"\0"))
    if mode == "tree":
        oid = git(root, "rev-parse", "--verify", "--end-of-options", f"{tree or 'HEAD'}^{{tree}}").decode().strip()
        raw = git(root, "ls-tree", "-r", "-z", oid)
    else:
        raw = git(root, "ls-files", "--stage", "-z")
    result = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, path = record.split(b"\t", 1)
        if selected is not None and path not in selected:
            continue
        parts = metadata.decode("ascii").split()
        if mode == "tree":
            file_mode, _, oid = parts
        else:
            file_mode, oid, stage = parts
            if stage != "0":
                raise RuntimeError("병합 충돌을 해결한 뒤 검사해 주세요.")
        result.append((path.decode("utf-8", "surrogateescape"), file_mode, oid))
    return result


def forbidden_path(path: str) -> str | None:
    """실행 원본과 비밀 설정의 저장 경로를 검사한다."""
    parts = PurePosixPath(path).parts
    name = parts[-1].lower()
    example = name.endswith((".example", ".sample", ".template"))
    if parts[0] in RUNTIME_ROOTS:
        return "runtime-output"
    if ((name == ".env" or name.startswith(".env.") or name.endswith(".env")) and not example
            or name.endswith((".pem", ".key", ".p12", ".pfx"))):
        return "secret-file"
    if re.search(r"\.(?:db|sqlite|sqlite3)(?:-(?:wal|shm|journal))?$", name):
        return "database-file"
    if any(part.lower() in {"backups", "archives", "secrets", ".secrets"} for part in parts[:-1]):
        return "private-archive"
    return None


def local_secrets(root: Path) -> list[bytes]:
    """현재 로컬 환경 파일의 충분히 긴 비밀값을 메모리에서만 읽는다.

    Args:
        root: .env 파일을 찾을 프로젝트 루트.
    Returns:
        출력하거나 파일로 저장하지 않는 비밀값 바이트 목록.
    """
    values = set()
    paths = [root / ".env", root / "local.env", *root.glob(".env.*")]
    for path in paths:
        if path.is_symlink() or not path.is_file() or path.name.endswith((".example", ".sample", ".template")):
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            key, separator, value = line.removeprefix("export ").partition("=")
            if not separator or not SECRET_NAME.search(key.strip()):
                continue
            value = value.strip()
            try:
                parsed = shlex.split(value, comments=True, posix=True)
            except ValueError:
                continue
            value = parsed[0] if len(parsed) == 1 else ""
            if len(value) >= 8 and not PLACEHOLDER.fullmatch(value):
                values.add(value.encode())
    return sorted(values)


class TokenInputs(HTMLParser):
    """수집 HTML의 실제 hidden/meta 보안값만 찾아 일반 안내 문구와 구분한다."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines: list[int] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        name = attributes.get("name") or attributes.get("id") or ""
        token_name = re.search(r"csrf|xsrf|token|nonce", name, re.I)
        if tag == "input" and attributes.get("type", "").lower() == "hidden" and token_name:
            value = attributes.get("value") or ""
            # 원문 정제 시 삽입한 이 고정 표식만 예외로 인정한다. 접두·부분 일치는 허용하지 않는다.
            if value.strip() and value != "[REDACTED_CSRF]":
                self.lines.append(self.getpos()[0])
        if tag == "meta" and token_name:
            value = attributes.get("content") or ""
            if value.strip() and value != "[REDACTED_CSRF]":
                self.lines.append(self.getpos()[0])


def scan_content(path: str, content: bytes, secrets: list[bytes]) -> list[Finding]:
    """공개할 내용은 검사하되 탐지 값은 결과에서 제외한다."""
    lines = content.splitlines()
    found: set[tuple[int, str]] = set()
    for category, pattern in PATTERNS.items():
        for match in pattern.finditer(content):
            found.add((content.count(b"\n", 0, match.start()) + 1, category))
    for secret in secrets:
        position = content.find(secret)
        while position >= 0:
            found.add((content.count(b"\n", 0, position) + 1, "local-secret"))
            position = content.find(secret, position + len(secret))
    if b"<input" in content.lower() or b"<meta" in content.lower():
        parser = TokenInputs()
        parser.feed(content.decode("utf-8", "replace"))
        found.update((line, "html-security-token") for line in parser.lines)
    return [Finding(path, line, category, hashlib.sha256(lines[line - 1] if line <= len(lines) else b"").hexdigest())
            for line, category in sorted(found)]


def allowances(root: Path, all_entries: list[tuple[str, str, str]]) -> set[tuple[str, str, str]]:
    """검사 대상 자체의 승인 목록을 읽어 작업 폴더 변경으로 우회하지 못하게 한다."""
    config = next((entry for entry in all_entries if entry[0] == CONFIG_PATH), None)
    if config is None:
        return set()
    if config[1] != "100644":
        raise ValueError("승인 목록은 일반 JSON 파일이어야 합니다.")
    result = set()
    data = json.loads(blob(root, config[2]))
    if not isinstance(data, dict) or not isinstance(data.get("allow", []), list):
        raise ValueError("승인 목록은 allow 배열이 필요합니다.")
    for item in data.get("allow", []):
        if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
                or item.get("category") != "personal-home" or not isinstance(item.get("reason"), str)
                or not item["reason"].strip()
                or not re.fullmatch(r"[0-9a-f]{64}", item.get("line_sha256", ""))):
            raise ValueError("승인 목록은 개인 경로의 정확한 줄 해시와 사유만 허용합니다.")
        result.add((item["path"], item["category"], item["line_sha256"]))
    return result


def reviewed_data(root: Path, all_entries: list[tuple[str, str, str]], include_history: bool = False) -> dict:
    """공개 검토를 마친 데이터의 해시와 목적을 같은 Git 스냅샷에서 읽는다.

    Args:
        root: 검사 저장소.
        all_entries: 검사 대상과 같은 index 또는 tree의 전체 항목.
        include_history: 명부가 없던 과거 snapshot용 정확한 이력 해시도 합칠지 여부.
    Returns:
        파일 경로별 승인 목록. 명부가 없으면 빈 값.
    Raises:
        ValueError: 검토 목록의 형식 또는 파일 모드가 잘못된 경우.
    """
    entry = next((item for item in all_entries if item[0] == DATA_MANIFEST), None)
    if entry is None:
        return {}
    if entry[1] != "100644":
        raise ValueError("검토 목록은 일반 JSON 파일이어야 합니다.")
    data = json.loads(blob(root, entry[2]))
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("files"), dict):
        raise ValueError("검토 목록 형식이 올바르지 않습니다.")
    history = data.get("historical_files", {})
    if not isinstance(history, dict) or any(not isinstance(items, list) for items in history.values()):
        raise ValueError("과거 검토 목록은 경로별 승인 배열이어야 합니다.")
    current = {path: [approval] for path, approval in data["files"].items()}
    for path, approvals in [*current.items(), *history.items()]:
        if not approvals:
            raise ValueError("과거 승인 배열은 비어 있을 수 없습니다.")
        for approval in approvals:
            if (not isinstance(approval, dict) or not isinstance(approval.get("purpose"), str)
                    or not approval["purpose"].strip()
                    or not re.fullmatch(r"[0-9a-f]{64}", approval.get("sha256", ""))):
                raise ValueError("각 공개 자료의 정확한 해시와 검토 목적이 필요합니다.")
    if include_history:
        for path, approvals in history.items():
            current.setdefault(path, []).extend(approvals)
    return current


def data_is_approved(approved: dict, path: str, digest: str) -> bool:
    """현재 또는 과거 승인 중 경로와 전체 파일 해시가 정확히 같은 항목만 인정한다."""
    return any(item["sha256"] == digest for item in approved.get(path, []))


def needs_review(path: str) -> bool:
    """명부·수집원문·화면 증빙은 자동 개인정보 탐지와 별도로 사람이 검토한다."""
    return ((path.startswith(DATA_ROOTS) and PurePosixPath(path).suffix.lower() in DATA_SUFFIXES)
            or path.startswith("checks/") and path.endswith("results.json"))


def inspect_entries(root: Path, targets: list[tuple[str, str, str]], secrets: list[bytes],
                    allowed: set | None = None, approved: dict | None = None) -> list[Finding]:
    """현재 자료와 이력 blob에 같은 경로·내용 기준을 적용한다.

    Args:
        root: 검사 저장소.
        targets: 검사할 Git 파일 항목.
        secrets: 메모리에만 보관하는 로컬 비밀값.
        allowed: 정확한 개인 경로 예외. 이력 검사는 예외 없이 수행한다.
        approved: 현재 자료 검토 명부. None이면 과거 blob의 내용만 검사한다.
    Returns:
        값이 노출되지 않은 탐지 위치 목록.
    """
    findings = []
    for path, file_mode, oid in targets:
        category = forbidden_path(path)
        if category:
            findings.append(Finding(path, 0, category))
        if file_mode not in {"100644", "100755"}:
            findings.append(Finding(path, 0, "unscanned-link"))
            continue
        try:
            content = blob(root, oid)
        except OverflowError:
            findings.append(Finding(path, 0, "oversize-unscanned"))
            continue
        if approved is not None and needs_review(path):
            if not data_is_approved(approved, path, hashlib.sha256(content).hexdigest()):
                findings.append(Finding(path, 0, "unreviewed-data"))
        findings.extend(item for item in scan_content(path, content, secrets)
                        if (item.path, item.category, item.line_sha256) not in (allowed or set()))
    return findings


def check(root: Path, mode: str, tree: str | None = None) -> tuple[list[Finding], int]:
    """선택한 Git 스냅샷을 검사해 승인되지 않은 항목과 파일 수를 반환한다."""
    targets = entries(root, mode, tree)
    full = targets if mode != "staged" else entries(root, "tracked")
    findings = inspect_entries(root, targets, local_secrets(root), allowances(root, full), reviewed_data(root, full))
    return findings, len(targets)


def check_history(root: Path, tip: str, base: str | None = None) -> tuple[list[Finding], int]:
    """나중에 삭제한 비밀도 전송 이력에서 찾아 최신 파일만 검사하는 빈틈을 막는다.

    Args:
        root: 검사 저장소.
        tip: 검사할 마지막 커밋.
        base: 이미 원격에 있는 커밋. 로컬에 없으면 전체 도달 이력을 검사한다.
    Returns:
        이력 자료·메시지 탐지와 고유 경로·blob 수. 이전 명부가 없을 때만 tip의 과거 승인을 사용한다.
    Raises:
        RuntimeError: shallow 저장소라 전체 전송 이력을 확인할 수 없는 경우.
    """
    if git(root, "rev-parse", "--is-shallow-repository").strip() == b"true":
        raise RuntimeError("전체 이력이 필요합니다.")
    tip_oid = git(root, "rev-parse", "--verify", "--end-of-options", f"{tip}^{{commit}}").decode().strip()
    arguments = ["rev-list", tip_oid]
    if base and set(base) != {"0"}:
        try:
            base_oid = git(root, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}").decode().strip()
        except RuntimeError:
            base_oid = None
        if base_oid:
            arguments.append("^" + base_oid)
    fallback = reviewed_data(root, entries(root, "tree", tip_oid), include_history=True)
    secrets, targets, findings, hashes = local_secrets(root), set(), [], {}
    for commit in git(root, *arguments).decode().splitlines():
        snapshot = entries(root, "tree", commit)
        has_manifest = any(item[0] == DATA_MANIFEST for item in snapshot)
        approved = reviewed_data(root, snapshot) if has_manifest else fallback
        for path, mode, oid in snapshot:
            if not needs_review(path) or mode not in {"100644", "100755"}:
                continue
            try:
                if oid not in hashes:
                    hashes[oid] = hashlib.sha256(blob(root, oid)).hexdigest()
            except OverflowError:
                findings.append(Finding(path, 0, "oversize-unscanned"))
                continue
            if not data_is_approved(approved, path, hashes[oid]):
                findings.append(Finding(path, 0, "unreviewed-data"))
        targets.update(snapshot)
        message = git(root, "show", "-s", "--format=%B", commit)
        findings.extend(scan_content(f"(commit-message)/{commit}", message, secrets))
    ordered = sorted(targets)
    findings.extend(inspect_entries(root, ordered, secrets))
    return list(dict.fromkeys(findings)), len(ordered)


def check_push(root: Path, input_text: str) -> tuple[list[Finding], int]:
    """전송 ref의 최신 검토 명부와 중간 커밋에서 삭제된 내용까지 검사한다.

    Args:
        root: 저장소 경로.
        input_text: Git pre-push가 제공한 ref와 객체 ID 목록.
    Returns:
        최신 tree·전송 이력의 탐지 결과와 검사 파일 수.
    Raises:
        ValueError: Git 입력 형식이 올바르지 않은 경우.
    """
    findings, count, inspected = [], 0, set()
    for line in input_text.splitlines():
        fields = line.split()
        if (len(fields) != 4 or not re.fullmatch(r"[0-9a-f]{40,64}", fields[1])
                or not re.fullmatch(r"[0-9a-f]{40,64}", fields[3])):
            raise ValueError("pre-push 객체 목록 형식이 올바르지 않습니다.")
        oid, base = fields[1], fields[3]
        if set(oid) == {"0"} or (oid, base) in inspected:
            continue
        inspected.add((oid, base))
        current, current_count = check(root, "tree", oid)
        historical, historical_count = check_history(root, oid, base)
        findings.extend(current + historical)
        count += current_count + historical_count
    return list(dict.fromkeys(findings)), count


def main() -> int:
    """로컬 hook과 CI에서 같은 공개 기준을 실행한다."""
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--staged", action="store_true", help="커밋 예정 변경의 index blob 검사 (기본값)")
    group.add_argument("--tracked", action="store_true", help="index의 전체 추적 파일 검사")
    group.add_argument("--history", help="지정 커밋까지 이력 자료의 검토 승인·내용·커밋 메시지 검사")
    parser.add_argument("--base", help="--history 검사에서 제외할 이미 검토한 기준 커밋")
    group.add_argument("--pre-push", action="store_true", help="Git pre-push 전송 ref와 중간 이력 검사")
    group.add_argument("--tree", help="지정 커밋/tree의 전체 파일 검사")
    parser.add_argument("--json", action="store_true", help="탐지 값 없이 JSON 출력")
    args = parser.parse_args()
    mode = "tree" if args.tree else "tracked" if args.tracked else "staged"
    try:
        if args.base and not args.history:
            parser.error("--base는 --history와 함께 사용합니다.")
        if args.pre_push:
            findings, count = check_push(ROOT, sys.stdin.read())
        elif args.history:
            findings, count = check_history(ROOT, args.history, args.base)
        else:
            findings, count = check(ROOT, mode, args.tree)
    except (RuntimeError, ValueError, TypeError, OSError, KeyError, json.JSONDecodeError):
        print("공개 안전 검사에 실패했습니다. Git 상태·전체 이력(shallow 여부)·승인 목록 형식을 확인해 주세요.", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"status": "failed" if findings else "passed", "files": count,
                          "findings": [asdict(item) for item in findings]}, ensure_ascii=True, indent=2))
    else:
        for item in findings:
            print(f"{json.dumps(item.path, ensure_ascii=True)}:{item.line}: {item.category}")
        print(f"공개 안전 검사: {count}개 파일, {len(findings)}개 확인 필요")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
