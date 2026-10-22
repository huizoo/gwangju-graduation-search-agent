"""브라우저와 같은 스크립트의 달력 기간 경계를 실행해 검사한다."""

from datetime import date, timedelta
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from app.core.domain import ApiError
from app.validation.periods import six_month_boundary, validate_period


@pytest.mark.parametrize("start,end,valid,max_end", [
    ("2027-01-01", "2027-01-01", True, "2027-06-30"),
    ("2027-01-01", "2026-12-31", False, "2027-06-30"),
    ("2027-01-01", "2027-06-30", True, "2027-06-30"),
    ("2027-01-01", "2027-07-01", False, "2027-06-30"),
    ("2023-08-31", "2024-02-28", True, "2024-02-28"),
    ("2023-08-31", "2024-02-29", False, "2024-02-28"),
    ("2024-08-31", "2025-02-27", True, "2025-02-27"),
    ("2024-08-31", "2025-02-28", False, "2025-02-27"),
    ("2024-02-29", "2024-08-28", True, "2024-08-28"),
    ("2024-02-29", "2024-08-29", False, "2024-08-28"),
    ("2025-12-01", "2026-03-31", True, "2026-05-31"),
    ("2025-02-30", "2025-03-01", False, None),
])
def test_browser_calendar_range_boundaries(start, end, valid, max_end):
    """UTC 달력 연산이 역전·포함 경계·말일·윤년을 일관되게 처리한다.

    Args:
        start: 행사 범위 시작일.
        end: 행사 범위 종료일.
        valid: 기대 허용 여부.
        max_end: 시작일에서 계산한 최종 선택일.
    """
    node = shutil.which("node")
    assert node, "브라우저 스크립트 경계 검사를 실행할 Node.js가 필요합니다."
    script = Path("app/static/schedule.js").read_text()
    result = subprocess.run([node, "-e", "global.window={};global.document={querySelectorAll:()=>[]};" +
        script + "\nconsole.log(JSON.stringify(window.seasonRangeBounds(" +
        json.dumps(start) + "," + json.dumps(end) + ")));"], text=True, capture_output=True, check=True)
    actual = json.loads(result.stdout)
    assert actual["valid"] is valid
    assert actual["maxEnd"] == max_end
    if valid:
        validate_period(start, end)
    else:
        with pytest.raises(ApiError):
            validate_period(start, end)
    if max_end:
        assert (date.fromisoformat(six_month_boundary(start)) - timedelta(days=1)).isoformat() == max_end
