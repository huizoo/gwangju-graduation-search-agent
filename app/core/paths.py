"""모듈 배치와 독립적으로 프로젝트 데이터와 공통 화면 자원을 찾는다."""

from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
ROOT = APP_DIR.parent
STATIC_DIR = APP_DIR / "static"
