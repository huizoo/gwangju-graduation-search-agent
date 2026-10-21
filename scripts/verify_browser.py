"""현재 화면의 합성 회귀검사를 전용 브라우저에서 순차 실행한다."""

import argparse
import importlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    """선택한 검사만 실행하며 각 검사의 브라우저 종료 확인 후 다음으로 이동한다.

    Raises:
        Exception: 화면 검사나 전용 브라우저 수명 정리가 실패한 경우.
    """
    suites = ["periods", "checkpoint", "feedback", "first_use", "download"]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=suites + ["all"], default="all", nargs="?")
    selected = parser.parse_args().suite
    # 하위 검사기의 자체 인자 해석에 상위 선택 인자를 전달하지 않는다.
    sys.argv = [sys.argv[0]]
    for name in suites if selected == "all" else [selected]:
        importlib.import_module(f"checks.browser.{name}").main()


if __name__ == "__main__":
    main()
