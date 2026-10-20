"""자료원과 날짜 대신 기관·과정·회차·캠퍼스로 행사 관계를 비교한다."""

import re


def scope(value: dict, institution: dict) -> tuple:
    """출처마다 다른 일반 졸업 대상 표기를 제한적으로 정규화한다.

    Args:
        value: Candidate 또는 행사 메타데이터.
        institution: 공식 기관 종류와 이름.
    Returns:
        행사 종류, 대상 과정, 캠퍼스, 명시적 회차의 비교 키.
    """
    kind = value.get("event_type")
    audience = (value.get("audience") or "").strip()
    regular = {"elementary": "초등학교 6학년", "middle": "중학교 3학년", "high": "고등학교 3학년"}
    default = regular.get(institution.get("type"))
    if default:
        if audience in {"", "졸업 대상 학생"}:
            audience = default
        if audience.endswith(" · 주간"):
            audience = audience.removesuffix(" · 주간")
        if audience == default and kind == "graduation_and_completion":
            kind = "graduation_ceremony"
    title = value.get("event_title", value.get("title", ""))
    sequence = re.search(r"제\s*(\d+)\s*회", title)
    return kind, audience, value.get("campus_label") or institution.get("campus_label"), sequence[1] if sequence else None


def same_event(left: dict, right: dict, institution: dict) -> bool:
    """날짜 차이를 충돌로 남기면서 다른 대상·회차는 합치지 않는다.

    Args:
        left: 비교할 첫 후보 또는 행사.
        right: 비교할 두 번째 후보 또는 행사.
        institution: 동일 기관임을 이미 확인한 기관.
    Returns:
        대상과 캠퍼스가 같고 명시적 회차가 모순되지 않는지 여부.
    """
    a, b = scope(left, institution), scope(right, institution)
    return a[:3] == b[:3] and (a[3] is None or b[3] is None or a[3] == b[3])
