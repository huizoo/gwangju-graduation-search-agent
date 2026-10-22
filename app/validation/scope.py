"""조회와 기본 조사에 공통으로 적용하는 학교 범위."""

TARGET_TYPES = frozenset({"elementary", "middle", "high", "university", "junior_college"})
RESEARCH_VERSION = "3.0"


def eligible_school(institution: dict, season: dict | None = None) -> bool:
    """광주 조사 대상 학교의 운영 범위를 확인한다.

    Args:
        institution: 공식 유형·지역·운영 상태가 있는 기관.
        season: 지정하면 조사 시작 전 공식 종료도 확인한다.
    Returns:
        전체 학교 조회와 조사 대상인지 여부.
    """
    return (
        institution.get("type") in TARGET_TYPES
        and institution.get("district") in {"동구", "서구", "남구", "북구", "광산구"}
        and not (season and institution.get("operation_status") == "closed_confirmed"
                 and institution.get("operation_effective_date")
                 and institution["operation_effective_date"] <= season["search_start"])
    )
