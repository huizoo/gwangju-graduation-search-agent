"""자료원과 날짜 대신 기관·과정·회차·캠퍼스로 행사 관계를 비교한다."""

import re


def academic_year(value: dict) -> int | None:
    """날짜 대신 저장 메타데이터와 명시한 학년도만 읽는다.

    Args:
        value: 후보 또는 행사 메타데이터.
    Returns:
        유일하게 명시한 학년도. 정보가 없거나 모순되면 None.
    """
    title = value.get("event_title", value.get("title", ""))
    years = {int(year) for year in re.findall(r"(20\d{2})\s*학년도", title)}
    explicit = value.get("academic_year")
    if isinstance(explicit, int) and not isinstance(explicit, bool):
        years.add(explicit)
    return next(iter(years)) if len(years) == 1 else None


def _terms(value: dict) -> set[str]:
    """같은 후보가 제시한 제목·문맥의 전후기 표현을 함께 대조한다."""
    terms = set()
    if value.get("ceremony_term") in {"전기", "후기"}:
        terms.add(value["ceremony_term"])
    text = value.get("event_title", value.get("title", ""))
    text += "\n" + "\n".join(item.get("quote", "") for item in value.get("context_evidence", []))
    terms.update(re.findall(
        r"(?:(?<![가-힣])|(?<=학년도))(전기|후기)(?=\s|학위\s*수여식|졸업식|$)", text))
    return terms


def ceremony_term(value: dict) -> str | None:
    """행사 문맥에 명시한 전기·후기만 구분한다.

    Args:
        value: 후보 또는 행사 메타데이터.
    Returns:
        전기 또는 후기. 날짜로 추정하거나 서로 다른 표현을 합치지 않는다.
    """
    terms = _terms(value)
    return next(iter(terms)) if len(terms) == 1 else None


def scope(value: dict, institution: dict) -> tuple:
    """출처마다 다른 일반 졸업 대상 표기를 제한적으로 정규화한다.

    Args:
        value: Candidate 또는 행사 메타데이터.
        institution: 공식 기관 종류와 이름.
    Returns:
        행사 종류, 과정, 캠퍼스, 명시적 회차·학년도·전후기의 비교 키.
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
    if institution.get("type") in {"university", "junior_college"}:
        if kind in {"graduation_ceremony", "degree_award_ceremony"}:
            kind = "degree_award_ceremony"
        # 한 대표 학위수여식의 학위 과정 표기는 행사 식별자가 아니다.
        if audience in {"", "졸업 대상 학생", "학부", "대학원", "학부 및 대학원",
                        "학부 졸업 대상 학생", "대학원 졸업 대상 학생", "학부 졸업생", "대학원 졸업생",
                        "학사·석사·박사", "학사", "석사", "박사"}:
            audience = "대표 졸업식"
    title = value.get("event_title", value.get("title", ""))
    sequence = re.search(r"제\s*(\d+)\s*회", title)
    return (kind, audience, value.get("campus_label") or institution.get("campus_label"),
            sequence[1] if sequence else None, academic_year(value), ceremony_term(value))


def same_event(left: dict, right: dict, institution: dict) -> bool:
    """날짜 차이를 충돌로 남기면서 다른 대상·회차는 합치지 않는다.

    Args:
        left: 비교할 첫 후보 또는 행사.
        right: 비교할 두 번째 후보 또는 행사.
        institution: 동일 기관임을 이미 확인한 기관.
    Returns:
        학년도·대상·캠퍼스가 같고 명시적 회차와 전후기가 모순되지 않는지 여부.
    """
    a, b = scope(left, institution), scope(right, institution)
    if len(_terms(left)) > 1 or len(_terms(right)) > 1:
        return False
    for value, found in ((left, a[4]), (right, b[4])):
        title = value.get("event_title", value.get("title", ""))
        if found is None and (value.get("academic_year") is not None
                              or len(set(re.findall(r"(20\d{2})\s*학년도", title))) > 1):
            return False
    linked = (left.get("event_key_hint") or left.get("event_key")) == (
        right.get("event_key_hint") or right.get("event_key"))
    linked = linked and bool(left.get("event_key_hint") or left.get("event_key"))
    # 미상 학년도를 모든 연도의 행사에 대응시키면 겹친 시즌의 수기가 오염된다.
    years_match = a[4] == b[4] or (linked and (a[4] is None or b[4] is None))
    return years_match and compatible_scope(left, right, institution)


def compatible_scope(left: dict, right: dict, institution: dict) -> bool:
    """학년도 미상 후보의 추가 관계 검사를 위해 명시한 행사 범위만 비교한다.

    Args:
        left: 새 후보.
        right: 기존 행사.
        institution: 같은 기관임을 확인한 기관.
    Returns:
        과정·캠퍼스·명시 회차·전후기가 모순되지 않는지 여부. 동일 행사 확정은 아니다.
    """
    a, b = scope(left, institution), scope(right, institution)
    return (len(_terms(left)) <= 1 and len(_terms(right)) <= 1 and a[:3] == b[:3]
            and (a[3] is None or b[3] is None or a[3] == b[3])
            and (a[5] is None or b[5] is None or a[5] == b[5]))


def same_dates(left: dict | None, right: dict | None) -> bool:
    """한 날짜와 같은 하루의 목록 표기를 동일한 행사일 집합으로 비교한다.

    Args:
        left: 검증한 기존 DateValue.
        right: 검증한 후보 DateValue.
    Returns:
        둘 다 일 단위이며 실제 날짜 목록이 같은지 여부.
    """
    if not left or not right or left.get("precision") != "day" or right.get("precision") != "day":
        return False
    return bool(left.get("date") and right.get("date")) and set(
        left.get("dates") or [left["date"]]) == set(right.get("dates") or [right["date"]])
