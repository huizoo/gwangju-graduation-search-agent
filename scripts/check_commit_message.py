"""커밋 제목에 한국어가 포함되는지만 확인하며 본문 언어는 강제하지 않는다."""

import argparse
from pathlib import Path
import re
import subprocess
import sys


KOREAN = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ]")


def validate_message(message: str) -> bool:
    """정리된 커밋 메시지의 첫 제목 줄에 한국어가 있는지 검사한다.

    Args:
        message: Git 주석과 앞뒤 공백을 정리한 메시지.
    Returns:
        제목에 한글이 하나 이상 포함되면 True. 기술명·접두사의 영어는 허용한다.
    """
    lines = message.strip().splitlines()
    return bool(lines and KOREAN.search(lines[0]))


def main() -> int:
    """Git이 전달한 메시지 파일을 읽고 영어 전용 제목을 거절한다.

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
    if not validate_message(cleaned):
        print("커밋 제목에 한국어로 변경 내용을 적어 주세요. 기술명과 영어 접두사는 함께 사용할 수 있습니다.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
