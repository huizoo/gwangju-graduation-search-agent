"""사용자 조사 기간과 근거의 학년도를 분리하는 공통 날짜 규칙."""

from calendar import monthrange
from datetime import date

from app.core.domain import ApiError


def six_month_boundary(start: str) -> str:
    """말일을 보정한 달력상 6개월 뒤의 제외 경계를 구한다.

    Args:
        start: ISO 시작 날짜.
    Returns:
        종료일로 선택할 수 없는 첫 날짜.
    """
    day = date.fromisoformat(start)
    month_index = day.year * 12 + day.month - 1 + 6
    year, month = divmod(month_index, 12)
    month += 1
    return date(year, month, min(day.day, monthrange(year, month)[1])).isoformat()


def validate_period(start: str, end: str) -> None:
    """포함 범위의 순서와 6개월 미만 조건을 검사한다.

    Args:
        start: 찾을 행사의 첫 날짜.
        end: 찾을 행사의 마지막 날짜.
    Raises:
        ApiError: 날짜 형식, 순서 또는 달력상 길이가 잘못된 경우.
    """
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        if first.isoformat() != start or last.isoformat() != end:
            raise ValueError
        boundary = six_month_boundary(start)
    except (ValueError, TypeError):
        raise ApiError("invalid_date_range", "올바른 시작 날짜와 종료 날짜를 선택해 주세요.") from None
    if last < first:
        raise ApiError("invalid_date_range", "종료일은 시작일보다 빠를 수 없습니다.")
    if end >= boundary:
        raise ApiError("invalid_date_range", "조사 기간은 달력상 6개월보다 짧게 선택해 주세요.")


def fact_season(season: dict, academic_year: int | None) -> dict:
    """기간은 보존하면서 검증 대상 근거의 학년도를 전달한다.

    Args:
        season: 실제 취득 작업의 고정 기간.
        academic_year: 원문 또는 공식 자료에서 확인한 학년도.
    Returns:
        원래 기간·ID와 해당 근거의 학년도를 담은 독립 사전.
    """
    return {**season, "academic_year": academic_year}


def within_period(value: dict | str | None, season: dict) -> bool:
    """실제 행사일이 하나 이상 포함 범위와 겹치는지 검사한다.

    Args:
        value: DateValue, ISO 날짜 또는 미확인 값.
        season: 표시하거나 조사할 기간.
    Returns:
        적어도 하루가 기간 안에 있는지 여부.
    """
    if isinstance(value, str):
        days = [value]
    elif isinstance(value, dict) and value.get("precision") == "day":
        days = value.get("dates") or [value.get("date")]
    else:
        return False
    return any(day and season["search_start"] <= day <= season["search_end"] for day in days)
