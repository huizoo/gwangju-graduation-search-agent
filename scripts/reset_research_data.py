#!/usr/bin/env python3
"""앱을 종료한 뒤 학교 명부를 보존하는 조사 기록 초기화를 실행한다."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import ConfigurationError, load_settings
from app.reset_data import ResetError, reset_research_data


def main() -> int:
    """확인 옵션을 검사하고 현재 설정의 저장소만 초기화한다.

    Returns:
        성공 0, 초기화하지 못한 경우 1.
    """
    parser = argparse.ArgumentParser(
        description="학교 목록과 설정을 남기고 시즌·수기·조사 기록을 전체 백업 후 초기화합니다."
    )
    parser.add_argument("--confirm-reset", action="store_true", help="조사 기록 전체 초기화를 확인합니다.")
    args = parser.parse_args()
    if not args.confirm_reset:
        parser.error("초기화하려면 앱을 종료하고 --confirm-reset 옵션을 지정해 주세요.")
    try:
        settings = load_settings()
        result = reset_research_data(settings.data_dir, confirmed=True)
    except (ResetError, ConfigurationError, OSError, sqlite3.Error) as error:
        # 설정과 SQLite 예외에는 입력값이 섞일 수 있어 임의 예외 내용을 출력하지 않는다.
        message = str(error) if isinstance(error, ResetError) else "파일·DB·설정 처리에 실패했습니다. 원본과 백업을 확인해 주세요."
        print(message, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
