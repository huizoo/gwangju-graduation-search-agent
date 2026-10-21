"""영어 유형 접두사와 한국어 설명으로 된 커밋 제목 및 메시지의 비밀값 노출을 검사한다."""

import argparse
from pathlib import Path
import re
import subprocess
import sys


sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_publication_safety import local_secrets, scan_content


KOREAN = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ]")
TITLE = re.compile(r"(?:feat|fix|docs|test|refactor|chore|build|ci|perf|style|revert): (\S.*)")


def validate_message(message: str) -> bool:
    """첫 제목 줄이 허용된 영어 유형과 한국어 설명으로 구성되는지 검사한다.

    Args:
        message: Git 주석과 앞뒤 공백을 정리한 메시지.
    Returns:
        제목이 ``type: 한국어 설명`` 형식이면 True. 본문 언어는 제한하지 않는다.
    """
    lines = message.strip().splitlines()
    title = TITLE.fullmatch(lines[0]) if lines else None
    return bool(title and KOREAN.search(title.group(1)))


def main() -> int:
    """Git 메시지 파일의 제목 형식과 비밀값을 검사해 부적합한 커밋을 거절한다.

    Returns:
        통과하면 0, 제목이 기준에 맞지 않으면 1, 읽기·정리 실패는 2.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("message_file", type=Path)
    args = parser.parse_args()
    try:
        raw = args.message_file.read_bytes()
        # Git 자체의 주석 규칙을 사용해 템플릿의 한국어 안내가 제목으로 인정되지 않게 한다.
        cleaned = subprocess.run(["git", "stripspace", "--strip-comments"], input=raw,
                                 capture_output=True, check=True).stdout.decode("utf-8")
    except (OSError, UnicodeError, subprocess.CalledProcessError):
        print("커밋 메시지를 확인하지 못했습니다. 메시지 파일과 Git 상태를 확인해 주세요.", file=sys.stderr)
        return 2
    try:
        findings = scan_content("(commit-message)", cleaned.encode("utf-8"),
                                local_secrets(Path(__file__).resolve().parents[1]))
    except (OSError, ValueError):
        print("커밋 메시지의 비밀값 검사를 완료하지 못했습니다.", file=sys.stderr)
        return 2
    if findings:
        categories = ", ".join(sorted({item.category for item in findings}))
        print(f"커밋 메시지에서 공개할 수 없는 항목이 발견되었습니다: {categories}", file=sys.stderr)
        return 1
    if not validate_message(cleaned):
        print("커밋 제목은 'type: 한국어 설명' 형식으로 적어 주세요. 예: docs: 사용 방법 정리\n"
              "허용 유형: feat, fix, docs, test, refactor, chore, build, ci, perf, style, revert",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
