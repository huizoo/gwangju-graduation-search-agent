#!/usr/bin/env python3
"""앱을 종료한 뒤 학교 명부를 보존하는 조사 기록 초기화를 실행한다."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import ConfigurationError, load_settings
from app.records.reset_data import ResetError, reset_research_data


def main() -> int:
    """확인 옵션을 검사하고 현재 설정의 저장소만 초기화한다.

    Returns:
        성공 0, 초기화하지 못한 경우 1.
    """
    parser = argparse.ArgumentParser(
        description="학교 목록과 설정을 남기고 시즌·수기·조사 기록을 전체 백업 후 초기화합니다."
    )
    confirmation = parser.add_mutually_exclusive_group()
    confirmation.add_argument("--confirm-reset", action="store_true", help="조사 기록 전체 초기화를 확인합니다.")
    confirmation.add_argument("--interactive", action="store_true", help="초기화 범위를 안내하고 직접 확인받습니다.")
    parser.add_argument("--keep-seasons", action="store_true", help="저장된 기간과 현재 선택을 유지합니다.")
    args = parser.parse_args()
    if not args.confirm_reset and not args.interactive:
        parser.error("초기화하려면 앱을 종료하고 --confirm-reset 옵션을 지정해 주세요.")
    if args.interactive:
        print("조사 기록 초기화")
        print("먼저 앱 화면의 '프로그램 종료'를 눌러 주세요.")
        print("모든 시즌의 일정·인원·시간 찾기·직접 기록·근거·작업 이력을 백업한 뒤 비웁니다.")
        print("기존 자동 저장 HTML은 백업 폴더로 옮깁니다. 따로 내려받은 HTML은 남습니다.")
        print("학교 목록과 설정은 유지합니다.")
        print("저장된 기간도 유지합니다." if args.keep_seasons else "저장된 기간도 지웁니다.")
        try:
            answer = input("계속하려면 '초기화'를 입력하세요. 그 외 입력은 취소합니다: ").strip()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer != "초기화":
            print("\n취소했습니다. 기록을 변경하지 않았습니다.")
            return 0
    try:
        settings = load_settings()
        result = reset_research_data(settings.data_dir, confirmed=True, keep_seasons=args.keep_seasons)
    except (ResetError, ConfigurationError, OSError, sqlite3.Error) as error:
        # 설정과 SQLite 예외에는 입력값이 섞일 수 있어 임의 예외 내용을 출력하지 않는다.
        message = str(error) if isinstance(error, ResetError) else "파일·DB·설정 처리에 실패했습니다. 원본과 백업을 확인해 주세요."
        print(message, file=sys.stderr)
        return 1
    if args.interactive:
        print("초기화를 완료했습니다.")
        print(f"백업 위치: {result['backup_directory']}")
        print("start.command로 앱을 다시 실행해 조사할 수 있습니다.")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
