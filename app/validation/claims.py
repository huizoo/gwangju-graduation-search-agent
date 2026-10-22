"""모델의 후보를 원문 역할·학교·학년도에 연결하는 독립 검증."""

import json
import re
from datetime import date, timedelta
from urllib.parse import urlsplit

from app.integrations.providers import official_channel
from app.validation.identity import ceremony_term


def _quotes(evidence: list, document: dict) -> list[str] | None:
    """실제 연속 인용만 반환한다.

    Args:
        evidence: segment ID와 인용 문자열 목록.
        document: 불변 공개 원문.
    Returns:
        인용 목록. 하나라도 원문에 없으면 None.
    """
    segments = {s["id"]: s["text"] for s in document.get("segments", [])}
    if not evidence:
        return None
    if not all(e.get("quote") and e["quote"] in segments.get(e.get("segment_id"), "") for e in evidence):
        return None
    return [e["quote"] for e in evidence]


def explicit_days(text: str) -> list[str]:
    """원문에 명시된 날짜와 같은 달의 짧은 행사 기간을 읽는다.

    Args:
        text: 하나의 날짜 역할 구간.
    Returns:
        정렬된 실제 날짜. 잘못된 달력 날짜는 포함하지 않는다.
    """
    matches = list(re.finditer(r"(?<!\d)(20\d{2})\s*(?:년|[./-])\s*(\d{1,2})\s*(?:월|[./-])\s*(\d{1,2})\s*일?\.?", text))
    days = set()
    for match in matches:
        try:
            first = date(*map(int, match.groups()))
        except ValueError:
            continue
        days.add(first.isoformat())
        tail = text[match.end():]
        short = re.match(r"\s*(?:\([월화수목금토일]\))?\s*(?:~|～|–|부터)\s*(?:(\d{1,2})\s*(?:월|[./-])\s*)?(\d{1,2})\s*(?:일|[.(\s]|$)", tail)
        if short:
            try:
                last = date(first.year, int(short[1] or first.month), int(short[2]))
            except ValueError:
                continue
            if 0 <= (last - first).days <= 30:
                days.update((first + timedelta(days=n)).isoformat() for n in range((last - first).days + 1))
    # 양 끝에 연도가 쓰인 범위도 같은 역할 구간에 있을 때만 확장한다.
    if len(matches) == 2 and re.search(r"~|～|–|부터", text[matches[0].end():matches[1].start()]):
        parsed = sorted(days)
        if len(parsed) == 2:
            first, last = map(date.fromisoformat, parsed)
            if (last - first).days <= 30:
                days.update((first + timedelta(days=n)).isoformat() for n in range((last-first).days + 1))
    return sorted(days)


def _ceremony_block(candidate: dict, document: dict, institution: dict, season: dict) -> str | None:
    """인용의 제목·행사·학교·학년도 관계를 먼저 제한한다.

    Args:
        candidate: 검증할 미승인 후보.
        document: 읽기 상태와 원문 분절.
        institution: 공식 채널을 확인한 대상 학교.
        season: 실제 행사일의 고정 날짜 범위.
    Returns:
        해당 본식의 연속 문맥. 관계를 확인하지 못하면 None.
    """
    contexts = _quotes(candidate.get("context_evidence", []), document)
    if not contexts or len(contexts) != 1:
        return None
    block = contexts[0]
    if (len(block) > 4000 or document.get("status") != "ok" or document.get("partial")
            or document.get("authority") != "official"
            or document.get("reading", {}).get("institution_match") != "matched"
            or not official_channel(institution, document.get("display_url", ""))):
        return None
    if candidate.get("is_change_notice"):
        return None
    campus = candidate.get("campus_label")
    if campus and campus != institution.get("campus_label") and campus not in block:
        return None
    sequence = re.search(r"제\s*(\d+)\s*회", candidate.get("event_title", ""))
    if sequence and not re.search(rf"제\s*{sequence[1]}\s*회", block):
        return None
    heading = re.search(r"(?:졸업식|학위\s*수여식)", block)
    if not heading:
        return None
    around = block[max(0, heading.start()-100):heading.end()+35]
    if re.search(r"병설|부설유치원|어린이집|예행|리허설|준비\s*설명회|설명회|졸업여행", around):
        return None
    # 같은 공식 사이트에 실린 다른 학교 행사도 대상 학교의 근거가 아니다.
    names = re.findall(r"[가-힣A-Za-z]+(?:초등학교|중학교|고등학교|대학교|대학원|유치원)", around)
    own = institution.get("name", "").replace(" ", "")
    if any(name not in own and name not in {"대학교", "대학원"} and own not in name
           for name in names):
        return None
    if institution.get("type") in {"university", "junior_college"}:
        title_area = block[:heading.end()+30]
        if re.search(r"(?:[가-힣]+학과|[가-힣]+학부|단과대학)\s*(?:졸업식|학위\s*수여식)", title_area):
            return None
        source_term = ceremony_term({"title": title_area})
        if source_term != ceremony_term(candidate):
            return None
    years = set(re.findall(r"(20\d{2})\s*학년도", block))
    year = candidate.get("academic_year")
    if years and years != {str(year)}:
        return None
    # 명시되지 않은 학년도를 달력 날짜에서 채우지 않는다. 지연 행사의 학년도도 원문대로다.
    if not years and year is not None:
        return None
    if not years:
        days = explicit_days(" ".join(_quotes(candidate.get("date_evidence", []), document) or []))
        if (candidate.get("season_alignment") != "source_context" or not days
                or not any(season["search_start"] <= day <= season["search_end"] for day in days)):
            return None
    elif candidate.get("season_alignment") not in {"explicit", "source_context"}:
        return None
    return block


def _field_quotes(candidate: dict, name: str, document: dict) -> list[str] | None:
    """개별 값이 같은 행사 문맥 내부에 실제 포함되는지 검사한다.

    Args:
        candidate: 문맥 인용을 가진 후보.
        name: 검사할 필드의 evidence 속성명.
        document: 원문 분절.
    Returns:
        같은 분절·문맥의 연속 인용 또는 None.
    """
    context = candidate["context_evidence"][0]
    entries = candidate.get(name, [])
    quotes = _quotes(entries, document)
    if not quotes or any(e["segment_id"] != context["segment_id"] or e["quote"] not in context["quote"] for e in entries):
        return None
    return quotes


def _role_line(block: str, quote: str) -> str:
    """인용이 속한 행을 읽어 날짜·시각 앞뒤의 역할을 검사한다.

    Args:
        block: 검증된 행사 문맥.
        quote: 문맥에 존재하는 필드 인용.
    Returns:
        인용의 앞뒤 행 경계를 포함한 원문.
    """
    start = block.find(quote)
    left = block.rfind("\n", 0, start) + 1
    right = block.find("\n", start + len(quote))
    return block[left:right if right >= 0 else len(block)]


_CEREMONY = re.compile(r"졸업식|학위\s*수여식|본식")
_AUXILIARY = re.compile(
    r"학위복|가운|대여|반납|학위증|졸업장\s*수령|기념촬영|사진촬영|식전|공연|리허설|예행|설명회|입장|집합"
)
_OTHER_EVENT = re.compile(r"입학식|종업식|수료식|세미나|간담회|워크숍|졸업여행")


def _subject_lines(text: str, candidate: dict, institution: dict) -> list[tuple[int, int, str | None, str]]:
    """원문의 제목이 뒤따르는 일시·상태 행에 적용되는 범위를 추적한다.

    Args:
        text: 모델이 자르기 전의 같은 원문 분절.
        candidate: 학년도·회차·캠퍼스가 있는 대상 본식.
        institution: 대상 기관.
    Returns:
        행의 시작·끝 위치, 행사 역할과 원문. 다른 행사의 제목을 만나면 관계를 전환한다.
    """
    role, year, owner, offset = None, None, True, 0
    result = []
    own = institution.get("name", "").replace(" ", "")
    sequence = re.search(r"제\s*(\d+)\s*회", candidate.get("event_title", ""))
    term = ceremony_term(candidate)
    for line in text.splitlines(keepends=True):
        years = set(re.findall(r"(20\d{2})\s*학년도", line))
        if years:
            year = next(iter(years)) if len(years) == 1 else "ambiguous"
        names = re.findall(r"[가-힣A-Za-z]+(?:초등학교|중학교|고등학교|대학교|대학원|유치원)", line)
        names = [name for name in names if name not in {"대학교", "대학원"}]
        if names:
            owner = all(name == own for name in names)
        main = list(_CEREMONY.finditer(line))
        auxiliary = list(_AUXILIARY.finditer(line))
        if auxiliary and (not main or auxiliary[-1].start() > main[-1].start()):
            role = "auxiliary"
        elif main:
            source_sequence = re.search(r"제\s*(\d+)\s*회", line)
            source_term = ceremony_term({"title": line})
            campus = candidate.get("campus_label")
            campuses = re.findall(r"[가-힣A-Za-z0-9]+캠퍼스", line)
            same = (owner and (year is None or year == str(candidate.get("academic_year")))
                    and not (sequence and source_sequence and sequence[1] != source_sequence[1])
                    and not (term and source_term and term != source_term)
                    and not (campus and campuses and campus not in campuses))
            role = "ceremony" if same else "other"
        elif _OTHER_EVENT.search(line) or (names and not owner):
            role = "other"
        # 날짜·시간·장소 같은 속성 행은 가장 가까운 행사 제목의 역할을 이어받는다.
        result.append((offset, offset + len(line), role, line.rstrip("\r\n")))
        offset += len(line)
    return result


def _source_context(candidate: dict, document: dict, institution: dict):
    """문맥 인용의 원래 분절과 행 관계를 반환한다.

    Args:
        candidate: 원문의 연속 문맥을 인용한 후보.
        document: 해당 인용의 원본 분절을 가진 문서.
        institution: 대상 기관.
    Returns:
        잘라낸 제목·부정까지 보존한 분절 원문과 행사별 행 관계.
    """
    evidence = candidate.get("context_evidence", [])
    if len(evidence) != 1:
        return "", []
    context = evidence[0]
    text = next((s["text"] for s in document.get("segments", [])
                 if s["id"] == context.get("segment_id")), "")
    if not context.get("quote") or context["quote"] not in text:
        return "", []
    return text, _subject_lines(text, candidate, institution)


def ceremony_negatives(candidate: dict, document: dict, institution: dict) -> set[str]:
    """본식에 연결된 부정 서술만 읽고 다른 행사 취소는 분리한다.

    Args:
        candidate: 확인 대상 본식과 실제 문맥 인용.
        document: 모델 인용보다 넓은 원래 분절을 가진 문서.
        institution: 해당 본식의 기관.
    Returns:
        본식에 명시된 취소·미실시·연기 상태. 이중 부정이나 조건부 문장은 확정하지 않는다.
    """
    from app.validation.rules import _NEGATIVE
    _, lines = _source_context(candidate, document, institution)
    found = set()
    for _, _, role, line in lines:
        if role != "ceremony":
            continue
        negative = _NEGATIVE.search(line)
        if not negative or line[negative.end():].strip(" \t.!。"):
            continue
        before = line[:negative.start()]
        if re.search(r"(?:경우|이라면|하면|여부|가능성|일\s*수|예정|검토)\s*$|(?:우천|비상)\s*시\s*$", before):
            continue
        status = negative["status"]
        found.add("cancelled" if "취소" in status else "unclear" if "연기" in status else "not_held")
    return found


def _ceremony_field(candidate: dict, document: dict, institution: dict, quotes: list[str]) -> bool:
    """인용한 속성 행이 부대 일정 제목을 건너뛰지 않고 본식에 연결되는지 검사한다.

    Args:
        candidate: 본식의 문맥 인용을 가진 후보.
        document: 원본 분절을 가진 문서.
        institution: 대상 기관.
        quotes: 날짜 또는 시각의 실제 연속 인용.
    Returns:
        모든 속성 행이 본식 제목의 범위 안에 있는지 여부.
    """
    text, lines = _source_context(candidate, document, institution)
    for quote in quotes:
        start = text.find(quote)
        if start < 0 or text.find(quote, start + 1) >= 0:
            return False
        roles = {role for left, right, role, _ in lines if left < start + len(quote) and right > start}
        if roles != {"ceremony"}:
            return False
    return True


def _calendar_days(candidate: dict, document: dict, season: dict) -> list[str]:
    """월별 달력의 연월 제목과 바로 뒤 행사 행을 같은 원문에서 연결한다.

    Args:
        candidate: 행사 문맥과 날짜의 연속 인용을 제출한 후보.
        document: 분절 경계를 보존한 공식 학사일정 원문.
        season: 실제 행사일의 고정 날짜 범위.
    Returns:
        단 하나의 행사 행에 연결된 날짜. 헤더·행 관계가 모호하면 빈 목록.
    """
    context = candidate["context_evidence"][0]
    segment = next((s["text"] for s in document["segments"] if s["id"] == context["segment_id"]), "")
    # 모델이 연도를 추정하지 않도록 실제 분절의 가장 가까운 연월 제목을 사용한다.
    offset = segment.find(context["quote"])
    if offset < 0 or segment.find(context["quote"], offset + 1) >= 0:
        return []
    date_quotes = _field_quotes(candidate, "date_evidence", document) or []
    row_pattern = r"(?m)^[ \t]*(\d{1,2})[./](\d{1,2})(?:\([월화수목금토일]\))?[ \t]*$"
    matches = []
    for row in re.finditer(row_pattern, context["quote"]):
        if not any(row[0].strip() in q or q.strip() in row[0].strip() for q in date_quotes):
            continue
        following = context["quote"][row.end():].lstrip("\n").split("\n", 1)[0].strip()
        if not re.search(r"(?:졸업식|학위\s*수여식)", following):
            continue
        if re.search(r"학위복|대여|반납|수령|기념촬영|식전|공연|예행|설명회|취소|미실시|미개최|실시하지|연기", following):
            continue
        years = set(re.findall(r"(20\d{2})\s*학년도", following))
        if years and years != {str(candidate.get("academic_year"))}:
            continue
        # 다른 행의 명칭·시각과 날짜를 결합하지 않고 바로 붙은 본식 한 행만 인정한다.
        headers = list(re.finditer(r"(?m)^[ \t]*(20\d{2})[./](\d{1,2})[ \t]*$", segment[:offset + row.start()]))
        if not headers or int(headers[-1][2]) != int(row[1]):
            continue
        try:
            matches.append(date(int(headers[-1][1]), int(row[1]), int(row[2])).isoformat())
        except ValueError:
            continue
    return matches if len(matches) == 1 else []


def _common_time_for_days(line: str, days: list[str], value: str) -> bool:
    """하나의 날짜 목록 바로 뒤 시각만 여러 날의 공통 시각으로 연결한다.

    Args:
        line: 시각 인용이 속한 하나의 실제 행사 행.
        days: 같은 문맥에서 별도로 검증한 전체 행사일.
        value: 확인하려는 본식 시작 시각.
    Returns:
        날짜 목록·공통 시작의 관계가 명시된 경우 True.
    """
    if "\n" in line or explicit_days(line) != days:
        return False
    date_pattern = r"(?<!\d)(20\d{2})\s*(?:년|[./-])\s*(\d{1,2})\s*(?:월|[./-])\s*(\d{1,2})\s*일?\.?"
    date_matches = list(re.finditer(date_pattern, line))
    clock_matches = list(re.finditer(
        r"(오전|오후)?\s*(\d{1,2})(?::([0-5]\d)|시(?:\s*([0-5]?\d)분)?)", line))
    if not date_matches or not clock_matches:
        return False
    first_clock = clock_matches[0]
    if first_clock.start() < date_matches[-1].end():
        # '20일 14시, 21일'처럼 특정 날짜 뒤의 시각은 목록 전체에 적용되지 않는다.
        return False
    hour = int(first_clock[2])
    if first_clock[1]:
        if not 1 <= hour <= 12:
            return False
        hour = hour % 12 + (12 if first_clock[1] == "오후" else 0)
    elif first_clock[3] is None and hour < 13:
        return False
    if f"{hour:02}:{int(first_clock[3] or first_clock[4] or 0):02}" != value:
        return False
    # 목록 안에는 날짜·요일·연결 기호만 허용한다. 별도 날짜의 역할 설명을 건너뛰지 않는다.
    phrase = line[date_matches[0].start():first_clock.start()]
    phrase = re.sub(date_pattern, "D", phrase)
    phrase = re.sub(r"\([월화수목금토일]\)", "", phrase)
    phrase = re.sub(r"(?<=[~～–-])\s*(?:\d{1,2}\s*(?:월|[./-])\s*)?\d{1,2}\s*일?\.?", "D", phrase)
    phrase = re.sub(r"\s+", "", phrase)
    if re.search(r"~|～|–|-|부터", phrase) and not re.search(r"공통|매일|양일", phrase):
        # 연속 기간 뒤의 시각은 첫날 시작일 수도 있으므로 매일이라는 관계를 별도로 요구한다.
        return False
    if not re.fullmatch(r"D(?:(?:,|·|ㆍ|및|와|과|~|～|–|-|부터)D(?:까지)?)*(?:공통|매일|양일)?[:：]?", phrase):
        return False
    if len(clock_matches) > 1:
        # 명시된 시작~종료 범위만 허용한다. 날짜별 다른 시각 목록은 공통 시작이 아니다.
        if len(clock_matches) != 2 or not re.fullmatch(
                r"\s*[-~～–]\s*", line[first_clock.end():clock_matches[1].start()]):
            return False
    suffix = line[clock_matches[-1].end():].strip()
    if not re.fullmatch(r"(?:(?:에|부터)\s*)?(?:(?:졸업식|본식)\s*)?(?:(?:시작|개식)(?:합니다|됩니다)?)?\s*(?:\(예정\))?[.!。]?", suffix):
        # 시각 뒤에 특정 일자·대상을 한정하는 나머지 문맥이 있으면 공통이라고 해석하지 않는다.
        return False
    return True


def grounded_claim(field: str, value, candidate: dict, document: dict,
                   institution: dict, season: dict) -> bool:
    """추출 파서의 재발견 여부와 독립적으로 원문 후보의 관계를 확인한다.

    Args:
        field: 게시하려는 날짜·시각·개최 상태·장소.
        value: 모델이 정규화한 값.
        candidate: 원문 역할별 인용을 가진 후보.
        document: 읽기·공식 관계가 검증된 불변 문서.
        institution: 대상 학교.
        season: 요청한 실제 행사일 범위.
    Returns:
        명시한 값과 실제 본식의 관계를 모두 확인했는지 여부.
    """
    block = _ceremony_block(candidate, document, institution, season)
    if block is None:
        return False
    status = candidate.get("ceremony_status")
    if status != "scheduled" and field != "ceremony_status":
        return False
    negatives = ceremony_negatives(candidate, document, institution)
    if field == "ceremony_status" and status in {"cancelled", "not_held", "unclear"}:
        return value == status and negatives == {status}
    if negatives:
        return False
    dates = _field_quotes(candidate, "date_evidence", document)
    if not dates:
        return False
    lines = [_role_line(block, q) for q in dates]
    side_role = _AUXILIARY.pattern
    if any(re.search(side_role, line) for line in lines):
        # 같은 줄의 여러 행사 역할을 건너뛰어 날짜·시각을 결합하지 않는다.
        return False
    parsed_days = sorted({day for q in dates for day in explicit_days(q)})
    calendar_days = _calendar_days(candidate, document, season) if not parsed_days else []
    if not calendar_days and not all(re.search(r"일시|날짜|행사일|개최일|졸업식|학위\s*수여식", line) for line in lines):
        return False
    if not calendar_days and not _ceremony_field(candidate, document, institution, dates):
        return False
    parsed_days = parsed_days or calendar_days
    claim = candidate.get("event_date", {})
    expected = claim.get("dates", [claim.get("date")])
    if (claim.get("precision") != "day" or not parsed_days or expected != parsed_days
            or claim.get("date") != parsed_days[0]
            or not any(season["search_start"] <= day <= season["search_end"] for day in parsed_days)):
        return False
    if field == "ceremony_status":
        return value == status == "scheduled"
    if status != "scheduled":
        return False
    if field == "event_date":
        return value == claim
    if field == "start_time":
        times = _field_quotes(candidate, "time_evidence", document)
        if (not times or candidate.get("time_role") != "ceremony_start"
                or not _ceremony_field(candidate, document, institution, times)):
            return False
        found = set()
        for quote in times:
            line = _role_line(block, quote)
            if re.search(side_role, line) or not re.search(r"일시|시작|개식|본식|졸업식|학위\s*수여식", line):
                return False
            if len(parsed_days) > 1 and not _common_time_for_days(line, parsed_days, value):
                return False
            for m in re.finditer(r"(오전|오후)?\s*(\d{1,2})(?::([0-5]\d)|시(?:\s*([0-5]?\d)분)?)", quote):
                hour = int(m[2])
                if m[1]:
                    if not 1 <= hour <= 12:
                        return False
                    hour = hour % 12 + (12 if m[1] == "오후" else 0)
                elif m[3] is None and hour < 13:
                    return False
                if hour <= 23:
                    found.add(f"{hour:02}:{int(m[3] or m[4] or 0):02}")
        return found == {value} and candidate.get("start_time") == value
    if field == "venue":
        quotes = _field_quotes(candidate, "venue_evidence", document)
        return bool(quotes and any(value in q and re.search(r"장소|행사장", _role_line(block, q)) for q in quotes))
    return False


def validate_count(claim: dict, document: dict, institution: dict, season: dict,
                   event: dict | None = None) -> dict:
    """학년별 학생 수 또는 대표 행사의 졸업인원을 독립적으로 검사한다.

    Args:
        claim: 학년도·종류·학년·단위·근거가 있는 인원 후보.
        document: 해당 후보의 공개 원문.
        institution: 공식 학교 식별자.
        season: 행사일 범위 또는 단독 자료 검증의 명시적 학년도.
        event: 인원을 연결할 행사와 원문 학년도. 초중고 자료 자체 검증에는 생략 가능하다.
    Returns:
        allowed와 채택하지 않은 이유. 미발견을 0명으로 바꾸지 않는다.
    """
    from app.core.domain import validate, ApiError
    try:
        validate("CountClaim", claim)
    except ApiError:
        return {"allowed": False, "reasons": ["count_contract_invalid"]}
    reasons = []
    quotes = _quotes(claim.get("evidence", []), document)
    if (not quotes or document.get("id") != claim["document_id"]
            or document.get("institution_id") != institution.get("id")
            or document.get("status") != "ok" or document.get("partial")):
        reasons.append("count_evidence_invalid")
    university = institution.get("type") in {"university", "junior_college"}
    event_year = (event or {}).get("academic_year")
    if ((event is not None and event_year != claim["academic_year"])
            or (event is None and "search_start" not in season and type(season.get("academic_year")) is int
                and claim["academic_year"] != season["academic_year"])):
        reasons.append("count_wrong_year")
    if university and event_year is None:
        reasons.append("count_event_year_unknown")
    grade = {"elementary": 6, "middle": 3, "high": 3}.get(institution.get("type"))
    if grade:
        if claim["kind"] != "graduating_grade_enrollment" or claim["grade"] != grade:
            reasons.append("count_wrong_cohort")
    elif institution.get("type") in {"university", "junior_college"}:
        if claim["kind"] != "ceremony_graduates" or claim.get("grade") is not None:
            reasons.append("count_wrong_kind")
        if not event or not claim.get("event_date"):
            reasons.append("count_event_required")
    else:
        reasons.append("count_school_type")
    if claim["source"] in {"schoolinfo_api09", "schoolinfo_public"}:
        try:
            import hashlib
            from bs4 import BeautifulSoup
            from urllib.parse import parse_qs
            from app.integrations.providers import canonical
            from app.integrations.providers_student import DISTRICT_CODES, SCHOOL_TYPES

            def address_key(value):
                return re.sub(r"\s+", "", str(value or "").replace("전남광주통합특별시", "광주광역시"))

            def count_integer(value):
                if isinstance(value, bool) or not isinstance(value, (int, str)):
                    raise ValueError
                if not re.fullmatch(r"\d{1,7}", str(value)) or not 0 <= int(value) <= 1_000_000:
                    raise ValueError
                return int(value)

            segments = {s["id"]: s["text"] for s in document["segments"]}
            meta = json.loads(segments["metadata"])
            identity = meta["identity"]
            query = meta["query"]
            field = f"COL_S{grade}"
            expected_type, expected_grade, directory_type = SCHOOL_TYPES[institution["type"]]
            district = DISTRICT_CODES[institution["district"]]
            document_url = urlsplit(document["display_url"])
            if not (meta["provider"] == claim["source"]
                    and meta["academic_year"] == claim["academic_year"]
                    and meta["grade"] == grade and meta["unit"] == "persons"
                    and grade == expected_grade
                    and identity["school_name"] == institution["name"]
                    and identity["school_type"] == expected_type
                    and address_key(identity["address"]) == address_key(institution["address"])
                    and document.get("authority") == "official"
                    and document_url.hostname == "www.schoolinfo.go.kr" and document_url.scheme == "https"
                    and document["reading"]["institution_match"] == "matched"
                    and document["authority_evidence"]["channel_url"] == "https://www.schoolinfo.go.kr/"
                    and claim["source_school_code"] == identity["school_code"]):
                raise ValueError
            if claim["source"] == "schoolinfo_api09":
                identity_row = json.loads(segments["identity"])
                row = json.loads(segments["row"])
                # 학년 합계를 학교 전체 인원·학급당 평균·다른 학교 행과 섞지 않는다.
                if (document_url.path != "/openApi.do" or query.get("apiType") != "09"
                        or int(query["pbanYr"]) != claim["academic_year"]
                        or query.get("sidoCode") != "12" or query.get("sggCode") != district
                        or query.get("schulKndCode") != expected_type
                        or claim["source_field"] != field or meta["source_field"] != field
                        or str(row["SCHUL_CODE"]) != identity["school_code"]
                        or str(identity_row["SCHUL_CODE"]) != identity["school_code"]
                        or row["SCHUL_NM"] != institution["name"] or identity_row["SCHUL_NM"] != institution["name"]
                        or row["SCHUL_KND_SC_CODE"] != expected_type or identity_row["SCHUL_KND_SC_CODE"] != expected_type
                        or not str(row["ADRCD_CD"]).startswith(district)
                        or not str(identity_row["ADRCD_CD"]).startswith(district)
                        or address_key(identity_row["SCHUL_RDNMA"]) != address_key(institution["address"])
                        or row.get("PBAN_EXCP_YN") != "N"
                        or count_integer(row[field]) != claim["value"]):
                    raise ValueError
                raw = canonical({"query": query, "identity": identity_row, "row": row}).encode()
                if hashlib.sha256(raw).hexdigest() != document["content_hash"]:
                    raise ValueError
            else:
                profile = json.loads(segments["profile"])
                context = json.loads(segments["context"])
                headers, sex_headers, values = meta["grade_headers"], meta["sex_headers"], meta["grade_values"]
                selected = BeautifulSoup(context["selected_option"], "html.parser").find("option", selected=True)
                directory_cells = profile["directory_cells"]
                directory_url = urlsplit(profile["education_directory_url"])
                directory_query = parse_qs(directory_url.query)
                disclosure_url = urlsplit(directory_cells[8])
                profile_url = urlsplit(profile["profile_url"])
                source_school_uuid = BeautifulSoup(profile["school_uuid_source"], "html.parser").find("input")
                if (document_url.path != "/ei/pp/Pneipp_b03_s0p.do"
                        or int(query["JG_YEAR"]) != claim["academic_year"]
                        or int(query["JG_YEAR2"]) != claim["academic_year"]
                        or query["HG_NM"] != institution["name"] or query["SHL_IDF_CD"] != identity["school_uuid"]
                        or meta["selected_year"] != claim["academic_year"] or meta["table_academic_year"] != claim["academic_year"]
                        or not selected or not str(selected.get("value", "")).startswith(str(claim["academic_year"]))
                        or not re.search(rf"{claim['academic_year']}\s*학년도", context["academic_year_label"])
                        or profile["schoolinfo_code"] != identity["school_code"]
                        or profile["school_name"] != institution["name"] or institution["name"] not in profile["profile_title"]
                        or profile["school_uuid"] != identity["school_uuid"] or profile["neis_code"] != identity["neis_code"]
                        or not re.fullmatch(r'var\s+sdSchulCode\s*=\s*"' + re.escape(identity["neis_code"]) + r'"', profile["neis_source"])
                        or not source_school_uuid or source_school_uuid.get("name") != "SHL_IDF_CD"
                        or source_school_uuid.get("value") != identity["school_uuid"]
                        or len(directory_cells) != 9 or directory_cells[0] != institution["name"]
                        or address_key(directory_cells[4]) != address_key(institution["address"])
                        or directory_url.hostname != "www.gen.go.kr" or directory_query.get("mode") != ["excel1"]
                        or directory_query.get("s_div2") != [directory_type]
                        or disclosure_url.hostname not in {"schoolinfo.go.kr", "www.schoolinfo.go.kr"}
                        or parse_qs(disclosure_url.query).get("HG_CD") != [identity["school_code"]]
                        or profile_url.hostname != "www.schoolinfo.go.kr" or profile_url.path != "/ei/ss/Pneiss_b01_s0.do"
                        or claim["source_field"] != f"grade_{grade}_total" or meta["source_field"] != claim["source_field"]
                        or headers[:grade] != [f"{g}학년" for g in range(1, grade+1)]
                        or sex_headers != ["남학생", "여학생", "전체학생"] * len(headers)
                        or len(values) != len(headers) * 3):
                    raise ValueError
                # 추출 메타데이터를 원문 표의 열 순서와 다시 대조한다.
                table_tokens = segments["table"].split()
                starts = [index for index in range(len(table_tokens) - len(headers) + 1)
                          if table_tokens[index:index + len(headers)] == headers]
                if len(starts) != 1:
                    raise ValueError
                table_tokens = table_tokens[starts[0]:]
                if table_tokens[:len(headers)] != headers or table_tokens[len(headers):len(headers)+len(sex_headers)] != sex_headers:
                    raise ValueError
                original_values = [count_integer(v) for v in table_tokens[len(headers)+len(sex_headers):]]
                if original_values != values:
                    raise ValueError
                male, female, total = [count_integer(v) for v in values[(grade-1)*3:grade*3]]
                if male + female != total or total != claim["value"]:
                    raise ValueError
        except (ValueError, KeyError, TypeError, IndexError, AttributeError):
            reasons.append("count_structured_mismatch")
    else:
        if (document.get("authority") != "official"
                or document.get("reading", {}).get("institution_match") != "matched"
                or not official_channel(institution, document.get("display_url", ""))):
            reasons.append("count_official_source_required")
        text = "\n".join(quotes or [])
        names = re.findall(r"[가-힣A-Za-z]+(?:초등학교|중학교|고등학교|대학교|대학원|유치원)", text)
        own = institution.get("name", "").replace(" ", "")
        if any(name not in own and name not in {"대학교", "대학원"} and own not in name for name in names):
            reasons.append("count_wrong_institution")
        if not re.search(rf"{claim['academic_year']}\s*학년도", text):
            reasons.append("count_year_not_explicit")
        n = re.escape(f"{claim['value']:,}").replace(",", ",?")
        if grade:
            matches = list(re.finditer(rf"{grade}\s*학년[^\n]{{0,150}}(?:학생\s*수|합계|계|총)\s*[:|：]?\s*{n}\s*명", text))
            if not matches:
                reasons.append("count_grade_relation_missing")
            linked_years = []
            for match in matches:
                left = text.rfind("\n", 0, match.start()) + 1
                right = text.find("\n", match.end())
                line = text[left:right if right >= 0 else len(text)]
                line_years = set(re.findall(r"(20\d{2})\s*학년도", line))
                headers = list(re.finditer(r"(20\d{2})\s*학년도", text[:match.start()]))
                linked_years.append(line_years if line_years else {headers[-1][1]} if headers else set())
            # 문서 어딘가의 학년도와 다른 표·행의 학생 수를 교차 결합하지 않는다.
            if not any(years == {str(claim["academic_year"])} for years in linked_years):
                reasons.append("count_grade_year_relation_missing")
        else:
            if (not re.search(r"졸업식|학위\s*수여식", text)
                    or re.search(r"[가-힣]+(?:학과|학부)\s*(?:졸업식|학위\s*수여식)", text)):
                reasons.append("count_representative_event_required")
            if not re.search(rf"(?:졸업생|학위\s*취득자|학위\s*수여자)[^\n]{{0,60}}(?:총\s*)?(?<![\d,]){n}(?!\d)\s*명", text):
                reasons.append("count_ceremony_relation_missing")
            if re.search(rf"[가-힣]+(?:학과|학부)[^\n]{{0,80}}(?<![\d,]){n}(?!\d)\s*명", text):
                reasons.append("count_department_only")
            count_lines = [line for line in text.splitlines()
                           if re.search(rf"(?:졸업생|학위\s*취득자|학위\s*수여자)[^\n]{{0,60}}(?<![\d,]){n}(?!\d)\s*명", line)]
            if any(set(re.findall(r"(20\d{2})\s*학년도", line)) - {str(claim["academic_year"])}
                   for line in count_lines):
                reasons.append("count_other_cohort_year")
            campus = (event or {}).get("campus_label") or institution.get("campus_label")
            if campus and campus not in text and (
                (event or {}).get("campus_label") not in {None, institution.get("campus_label")}
                or re.search(r"\S+캠퍼스", text)
            ):
                reasons.append("count_campus_unmatched")
            if ceremony_term(event or {}) != ceremony_term({"title": text}):
                reasons.append("count_term_unmatched")
            event_title = (event or {}).get("event_title", (event or {}).get("title", ""))
            sequence = re.search(r"제\s*(\d+)\s*회", event_title)
            source_sequences = set(re.findall(r"제\s*(\d+)\s*회\s*(?:졸업식|학위\s*수여식)", text))
            if sequence and source_sequences != {sequence[1]}:
                reasons.append("count_sequence_unmatched")
            if claim.get("event_date") not in explicit_days(text):
                reasons.append("count_ceremony_date_missing")
            event_day = (event or {}).get("event_date")
            event_days = event_day.get("dates", [event_day.get("date")]) if isinstance(event_day, dict) else [event_day]
            if isinstance(event_day, dict):
                event_day = event_day.get("date")
            if not event_day or claim.get("event_date") not in event_days:
                reasons.append("count_event_mismatch")
    return {"allowed": not reasons, "reasons": sorted(set(reasons))}
