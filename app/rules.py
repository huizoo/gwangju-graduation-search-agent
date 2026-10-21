"""모델 동의와 분리된, 버전이 고정된 정형·명시 공지 원문 검사."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date

from .providers import canonical, neis_identifier, neis_unit, official_channel, utcnow
from .identity import same_event
from .periods import fact_season, within_period

REGISTERED_RULES = {"neis_ceremony_row": "1.3.0", "explicit_notice": "1.4.0",
                    "academic_calendar_row": "1.1.0", "grounded_notice": "1.2.0"}
NEIS_EVENTS = {"졸업식": "graduation_ceremony", "졸업식 및 종업식": "graduation_and_completion",
               "졸업식 및 수료식": "graduation_and_completion", "졸업식/종업식": "graduation_and_completion",
               "졸업식,종업식": "graduation_and_completion", "종업식 및 졸업식": "graduation_and_completion",
               "졸업식(종업식)": "graduation_and_completion"}
EVIDENCE_FIELD = {"event_date": "date_evidence", "start_time": "time_evidence",
                  "venue": "venue_evidence", "ceremony_status": "context_evidence"}
_CHANGE_ROW = re.compile(
    r"(?P<line>\(변경\s*전\)\s*(?P<before>\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2})"
    r"\.?\s*(?:\([월화수목금토일]\))?\s*→\s*\(변경\s*후\)\s*"
    r"(?P<after>\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2})\.?\s*(?:\([월화수목금토일]\))?)"
)
_NEGATIVE = re.compile(
    r"(?:올해\s*)?(?:(?:개최|실시)(?:를)?\s*)?"
    r"(?P<status>취소(?:합니다|되었습니다|됐습니다|\s*안내)?|미실시(?:합니다|\s*안내)?|미개최(?:합니다|\s*안내)?|"
    r"(?:개최|실시)하지\s*않(?:습니다|기로\s*했습니다)|연기(?:합니다|되었습니다|됐습니다|\s*안내)?)\s*[.!。]?"
)


def _evidence(segment_id, quote):
    """원문 연속 인용을 계약 Evidence로 반환한다."""
    return {"kind": "literal", "segment_id": segment_id, "quote": quote, "bbox": None}


def _candidate(title, event_type, academic_year, day, segment_id, context, date_quote, time_value=None, time_quote=None):
    """입장 시각 등 누락값을 보완하지 않은 후보를 구성한다."""
    return {
        "event_title": title, "event_type": event_type, "event_key_hint": "main", "audience": "졸업 대상 학생",
        "campus_label": None, "academic_year": academic_year, "season_alignment": "explicit",
        "ceremony_status": "scheduled", "event_date": {"precision": "day", "date": day, "month": None},
        "start_time": time_value, "time_role": "ceremony_start" if time_value else None, "venue": None,
        "context_evidence": [_evidence(segment_id, context)],
        "date_evidence": [_evidence(segment_id, date_quote)],
        "time_evidence": [_evidence(segment_id, time_quote)] if time_value else [], "venue_evidence": [],
        "is_change_notice": False, "supersedes_text": None,
    }


def _neis_candidate(document: dict, season: dict) -> dict | None:
    """정확히 등록한 졸업 행사명과 실제 날짜가 있는 API 한 행만 매핑한다."""
    if document["source_kind"] != "official_api" or len(document["segments"]) != 1:
        return None
    segment = document["segments"][0]
    try:
        row = json.loads(segment["text"])
        unit = neis_unit(row)
        if not unit["course"]:
            return None
        name = re.sub(r"\s+", " ", row["EVENT_NM"]).strip()
        event_type = NEIS_EVENTS.get(name)
        if event_type is None or int(row["AY"]) != season["academic_year"]:
            return None
        raw_date = str(row["AA_YMD"])
        if not re.fullmatch(r"\d{8}", raw_date):
            return None
        day = date(int(raw_date[:4]), int(raw_date[4:6]), int(raw_date[6:])).isoformat()
        if not season["search_start"] <= day <= season["search_end"]:
            return None
        content = str(row.get("EVENT_CNTNT", "")).strip()
        status_text = re.sub(r"^(?:졸업식|학위수여식)(?:은|는|이|을)?\s*", "", content)
        negative = _NEGATIVE.fullmatch(status_text)
        if not negative and re.search(r"취소|미개최|미실시|하지\s*않|연기|예행|리허설|졸업여행", content):
            return None
        candidate = _candidate(name, event_type, int(row["AY"]), day, segment["id"], segment["text"], raw_date)
        candidate.update(event_key_hint=unit["key"], audience=unit["audience"])
        if negative:
            candidate.update(ceremony_status="unclear" if "연기" in negative["status"] else "cancelled" if "취소" in negative["status"] else "not_held",
                             event_date={"precision": "unknown", "date": None, "month": None}, date_evidence=[])
        return candidate
    except (KeyError, ValueError, TypeError):
        return None


def _notice_subject(line: str, institution: dict, season: dict):
    """기관명이 행사의 주어인 명시 문장만 반환한다.

    Args:
        line: 한 문장.
        institution: 대상 기관.
        season: 조사 학년도.
    Returns:
        행사 이름 뒤 부분 또는 연결을 확인할 수 없으면 None.
    """
    if re.findall(r"(\d{4})\s*학년도", line) != [str(season["academic_year"])]:
        return None
    # '초등학교 병설유치원 졸업식'과 '졸업식 준비 설명회'는 다른 명사구다.
    subject = re.search(re.escape(institution["name"]) +
        r"\s*(?:\d{4}\s*학년도\s*)?(?:(?:전기|후기)\s*)?(?:제\s*\d+\s*회\s*)?(졸업식|학위수여식)(?:은|는|이|을)?(?=\s|[:：]|$)", line)
    return line[subject.end():].strip() if subject else None


def _parse_negative(line, institution, season, segment_id):
    """같은 기관·학년도의 행사 자체에 붙은 부정 서술을 읽는다."""
    tail = _notice_subject(line, institution, season)
    if tail is None:
        return None
    tail = re.sub(r"^\d{4}년\s*\d{1,2}월\s*\d{1,2}일(?:\([월화수목금토일]\))?\s*(?:예정이었으나\s*)?", "", tail)
    negative = _NEGATIVE.fullmatch(tail)
    if not negative:
        return None
    sequence = re.search(r"제\s*(\d+)\s*회", line)
    term = re.search(r"(?:전기|후기)(?=\s*(?:제\s*\d+\s*회\s*)?(?:졸업식|학위수여식))", line)
    title = f"{season['academic_year']}학년도 " + (term[0] + " " if term else "") + (f"제{sequence[1]}회 " if sequence else "") + ("학위수여식" if "학위수여식" in line else "졸업식")
    candidate = _candidate(title,
        "degree_award_ceremony" if "학위수여식" in line else "graduation_ceremony",
        season["academic_year"], None, segment_id, line, None)
    candidate.update(ceremony_status="unclear" if "연기" in negative["status"] else "cancelled" if "취소" in negative["status"] else "not_held",
                     event_date={"precision": "unknown", "date": None, "month": None}, date_evidence=[])
    return candidate


def _parse_notice_line(line: str, institution: dict, season: dict, segment_id: str) -> dict | None:
    """한 문장에 연결된 기관·학년도·졸업식·행사일을 좁게 인식한다."""
    if len(line) > 1200:
        return None
    tail = _notice_subject(line, institution, season)
    if tail is None or re.search(r"취소|미개최|미실시|하지\s*않|연기|변경|지난|작년|지난해", tail):
        return None
    academic = re.findall(r"(\d{4})\s*학년도", line)
    dates = list(re.finditer(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일", line))
    if academic != [str(season["academic_year"])] or len(dates) != 1:
        return None
    matched = dates[0]
    # 날짜 앞에는 행사 조사·일시 연결어만 허용한다. 다른 행사 명사를 건너뛰지 않는다.
    before_date = tail.split(matched.group(0), 1)[0]
    if not re.fullmatch(r"(?:(?:개최\s*)?안내|일시)?\s*[:：]?\s*", before_date):
        return None
    try:
        day = date(*map(int, matched.groups())).isoformat()
    except ValueError:
        return None
    if not season["search_start"] <= day <= season["search_end"]:
        return None
    time_value = time_quote = None
    time_matches = list(re.finditer(
        r"(?P<ampm>오전|오후)?\s*(?P<hour>\d{1,2})(?:시(?:\s*(?P<minute>\d{1,2})분)?|:(?P<colon>\d{2}))\s*(?:정각)?(?:에|부터)?\s*(?:졸업식(?:이|을)?\s*)?(?:시작|개식)",
        line,
    ))
    valid_times = []
    for tm in time_matches:
        prefix = line[matched.end():tm.start()].strip()
        explicit_subject = re.search(r"(?:졸업식|본식)(?:은|는|이)?\s*$", prefix)
        hour, minute = int(tm["hour"]), int(tm["minute"] or tm["colon"] or "0")
        # 마지막 역할이 입장·가운이면 '시작'이라는 동사만으로 행사 시작으로 바꾸지 않는다.
        if (explicit_subject or re.fullmatch(r"(?:\([월화수목금토일]\))?\s*[,，:]?\s*", prefix)) and minute < 60 and (
            (tm["ampm"] is not None and 1 <= hour <= 12) or (tm["ampm"] is None and 0 <= hour <= 23)
        ):
            if tm["ampm"]:
                hour = hour % 12 + (12 if tm["ampm"] == "오후" else 0)
            valid_times.append((f"{hour:02d}:{minute:02d}", tm.group(0)))
    if len(valid_times) == 1:
        time_value, time_quote = valid_times[0]
    sequence = re.search(r"제\s*(\d+)\s*회", line)
    degree = "학위수여식" in line
    term = re.search(r"(?:전기|후기)(?=\s*(?:제\s*\d+\s*회\s*)?(?:졸업식|학위수여식))", line)
    title = f"{season['academic_year']}학년도 " + (term[0] + " " if term else "") + (f"제{sequence[1]}회 " if sequence else "") + ("학위수여식" if degree else "졸업식")
    return _candidate(title, "degree_award_ceremony" if degree else "graduation_ceremony",
        season["academic_year"], day, segment_id, line, matched.group(0), time_value, time_quote)


def _parse_notice_section(text: str, institution: dict, season: dict, segment_id: str) -> list[dict]:
    """제목 뒤의 명시적 행사개요 구간에서 같은 행사의 일시·장소만 읽는다.

    Args:
        text: 하나의 원문 segment.
        institution: 대상 기관. 행사개요 구간에도 기관명이 있어야 한다.
        season: 목표 학년도와 기간.
        segment_id: 인용할 원문 구간 ID.
    Returns:
        연도·행사 제목·구간 경계·일시 역할을 모두 확인한 후보 목록.
    """
    pattern = re.compile(
        r"(?P<context>(?P<year>\d{4})\s*학년도\s+(?P<ceremony>학위수여식|졸업식)[^\[\]]{0,400}"
        r"\[행사개요\](?P<body>[^\[\]]{1,1500}))"
    )
    results = []
    for match in pattern.finditer(text):
        body = match["body"]
        if institution["name"] not in body or int(match["year"]) != season["academic_year"]:
            continue
        years = set(re.findall(r"(\d{4})\s*학년도", match["context"]))
        if years != {str(season["academic_year"])} or re.search(r"취소|미실시|미개최|하지\s*않|연기|예행|리허설|가운|학위복|입장", body):
            continue
        timing = re.search(r"(?:^|\n)\s*1\.\s*일시\s*:\s*(?P<timing>[\s\S]*?)(?=\n\s*2\.\s*장소\s*:)", body)
        if timing is None:
            continue
        dates = list(re.finditer(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일", timing["timing"]))
        if len(dates) != 1:
            continue
        raw_date = dates[0]
        try:
            day = date(*map(int, raw_date.groups())).isoformat()
        except ValueError:
            continue
        if not season["search_start"] <= day <= season["search_end"]:
            continue
        after_date = timing["timing"][raw_date.end():]
        # '행사개요 > 일시'의 단일 시간 범위만 실제 행사 시작과 종료로 해석한다.
        time_range = re.fullmatch(
            r"\s*(?:\([월화수목금토일]\))?\s*([01]?\d|2[0-3]):([0-5]\d)\s*[-~～]\s*([01]?\d|2[0-3]):([0-5]\d)\s*(?:\(예정\))?\s*",
            after_date,
        )
        if time_range is None:
            continue
        hour, minute, end_hour, end_minute = map(int, time_range.groups())
        if (hour, minute) >= (end_hour, end_minute):
            continue
        kind = "degree_award_ceremony" if match["ceremony"] == "학위수여식" else "graduation_ceremony"
        candidate = _candidate(
            f"{match['year']}학년도 {match['ceremony']}", kind, int(match["year"]), day,
            segment_id, match["context"], raw_date.group(0), f"{hour:02d}:{minute:02d}", timing.group(0),
        )
        place = re.search(r"(?:^|\n)\s*2\.\s*장소\s*:\s*([^\n]+)", body)
        if place:
            candidate["venue"] = place.group(1).strip()
            candidate["venue_evidence"] = [_evidence(segment_id, place.group(0))]
        results.append(candidate)
    return results


def _parse_calendar_change(text: str, season: dict, segment_id: str) -> list[dict]:
    """학사력의 학부·회차·변경 전후 날짜가 명시된 한 행사 행만 읽는다.

    Args:
        text: 실제 원문 구간. 기관 관계는 별도 공식 채널 검사에서 확인한다.
        season: 대상 학년도와 전체 조사 기간.
        segment_id: 인용 구간 식별자.
    Returns:
        명시된 새 날짜 후보. 시작 시각과 변경 관계의 이전 관측은 추측하지 않는다.
    """
    headers = list(re.finditer(r"(\d{4})\s*학년도\s*(학부|대학원)?\s*학사력\s*변경", text))
    if not headers or {match[1] for match in headers} != {str(season["academic_year"])}:
        return []
    groups = {match[2] for match in headers if match[2]}
    if len(groups) != 1:
        return []
    group = groups.pop()
    pattern = re.compile(
        r"(?:^|\n)\s*-\s*(?P<title>제\s*\d+\s*회\s*(?P<kind>학위수여식|졸업식))\s*\n"
        r"(?P<row>[^\n]+)"
    )
    result = []
    for matched in pattern.finditer(text):
        change = _CHANGE_ROW.fullmatch(matched["row"].strip())
        if change is None:
            continue
        try:
            before, after = [date(*map(int, re.findall(r"\d+", change[key]))).isoformat()
                             for key in ("before", "after")]
        except ValueError:
            continue
        if before == after or not all(season["search_start"] <= day <= season["search_end"] for day in (before, after)):
            continue
        title = re.sub(r"\s+", "", matched["title"]).replace(matched["kind"], " " + matched["kind"])
        # 회차와 학부/대학원을 분리하여 다른 학과·회차의 과거값을 대체하지 않는다.
        candidate = _candidate(title, "degree_award_ceremony" if matched["kind"] == "학위수여식" else "graduation_ceremony",
                               season["academic_year"], after, segment_id, matched.group(0).strip(),
                               change["after"])
        candidate.update(event_key_hint=f"academic-calendar:{title}:{group}", audience=f"{group} 졸업 대상 학생",
                         is_change_notice=True, supersedes_text=change["line"])
        result.append(candidate)
    return result


def _extract_for_year(document: dict, institution: dict, season: dict) -> list[dict]:
    """검증 가능한 정형 매핑을 먼저 적용하여 불필요한 모델 호출을 줄인다.

    Args:
        document: 읽은 문서 버전.
        institution: 대상 기관.
        season: 목표 학년도와 기간.
    Returns:
        정형 또는 좁은 명시 공지 문법으로 추출한 후보. 최종 승인은 별도다.
    """
    if document["status"] != "ok":
        return []
    if document["source_kind"] == "official_api":
        candidate = _neis_candidate(document, season)
        return [candidate] if candidate else []
    results = []
    for segment in document["segments"]:
        results.extend(_parse_calendar_change(segment["text"], season, segment["id"]))
        results.extend(_parse_notice_section(segment["text"], institution, season, segment["id"]))
        # 표 전체나 다른 줄의 시간을 임의 결합하지 않고 하나의 명시 문장만 검사한다.
        lines = re.split(r"\n|(?<=[.!?。])\s+(?=[가-힣A-Za-z])", segment["text"])
        for line in lines:
            candidate = (_parse_negative(line.strip(), institution, season, segment["id"])
                         or _parse_notice_line(line.strip(), institution, season, segment["id"]))
            if candidate:
                results.append(candidate)
    return results


def extract_candidates(document: dict, institution: dict, season: dict) -> list[dict]:
    """사용자 기간 안의 행사에서 원문 학년도별 후보를 독립적으로 읽는다.

    Args:
        document: 읽은 불변 원문.
        institution: 대상 기관과 학교급.
        season: 양끝을 포함하는 실제 행사 날짜 범위.
    Returns:
        원문 학년도를 유지한 후보. 대학의 학년도는 날짜에서 역산하지 않는다.
    """
    years = {int(year) for segment in document.get("segments", [])
             for year in re.findall(r"(20\d{2})\s*학년도", segment["text"])}
    if document.get("source_kind") == "official_api" and len(document.get("segments", [])) == 1:
        try:
            raw_year = json.loads(document["segments"][0]["text"]).get("AY")
            if re.fullmatch(r"20\d{2}", str(raw_year)):
                years.add(int(raw_year))
        except (ValueError, TypeError, AttributeError):
            pass
    # 연기·조기 개최를 허용하되 원문에 없는 학년도를 달력 날짜로 생성하지 않는다.
    return [candidate for year in sorted(years)
            for candidate in _extract_for_year(document, institution, fact_season(season, year))]


def _present(evidence, document):
    """모든 인용이 같은 불변 문서의 실제 연속 문자열인지 검사한다."""
    segments = {item["id"]: item for item in document["segments"]}
    return bool(evidence) and all(
        item.get("kind") == "literal" and item.get("segment_id") in segments and item.get("quote")
        and item["quote"] in segments[item["segment_id"]]["text"] for item in evidence
    )


def value_check(field: str, value, observation: dict, document: dict, event: dict, season: dict, *, institution=None) -> dict:
    """원문·관측·기관·행사·시즌·정규값을 서버에서 다시 검사한다.

    Args:
        field: 확인할 계약 필드.
        value: 확인할 정규값.
        observation: 저장할 관측과 후보.
        document: 관측에 연결한 불변 원문.
        event: 서버가 묶은 행사 식별 정보.
        season: 저장된 시즌.
        institution: 공식 채널과 이름을 가진 기관.
    Returns:
        지원·모호·기각 결과와 정확한 검사 근거를 담은 ValueCheck.
    """
    institution = institution or event.get("institution", {})
    checks = {key: "unknown" for key in ("institution", "content", "season", "event_identity",
                                         "literal_presence", "normalized_value", "time_role")}
    checks["time_role"] = "not_applicable" if field != "start_time" else "unknown"
    candidate = observation["candidate"]
    rule = ("neis_ceremony_row" if document.get("source_kind") == "official_api"
            else "academic_calendar_row" if candidate.get("is_change_notice") else "explicit_notice")
    result = {"status": "ambiguous", "rule_id": rule, "checks": checks,
        "evidence_ids": [observation["id"]], "document_ids": [document["id"]], "reason_codes": [],
        "checked_at": utcnow(), "policy_version": "4.0", "rule_version": REGISTERED_RULES[rule],
        "field": field, "document_hashes": {document["id"]: document["content_hash"]} if document.get("content_hash") else {}}
    linked = (observation.get("document_id") == document["id"]
              and observation.get("institution_id") == institution.get("id") == document.get("institution_id")
              and observation.get("event_id") == event.get("id")
              and observation.get("season_id") == season.get("id"))
    if not linked:
        checks["institution"] = "fail"
        result.update(status="rejected", reason_codes=["evidence_link_invalid"])
        return result
    checks["content"] = "pass" if (document.get("status") == "ok" and not document.get("partial")
        and document.get("authority") == "official" and document.get("authority_evidence")
        and document.get("reading", {}).get("content_match") == "expected") else "fail"
    checks["institution"] = "pass" if document.get("reading", {}).get("institution_match") == "matched" else "unknown"
    year = candidate.get("academic_year")
    allowed_year = type(year) is int
    candidate_day = candidate.get("event_date", {})
    period_match = candidate_day.get("precision") == "unknown" or within_period(candidate_day, season)
    checks["season"] = "pass" if allowed_year and period_match and candidate.get("season_alignment") == "explicit" else "fail"
    checks["event_identity"] = "pass" if (
        same_event(candidate, event, institution) and candidate.get("event_type") in
        {"graduation_ceremony", "graduation_and_completion", "degree_award_ceremony"}
        and (not candidate.get("is_change_notice") or rule == "academic_calendar_row")
    ) else "fail"
    evidence = candidate.get("context_evidence", []) + candidate.get(EVIDENCE_FIELD.get(field, ""), [])
    checks["literal_presence"] = "pass" if _present(evidence, document) else "fail"
    matched_candidates = []
    if checks["content"] == "pass" and checks["institution"] == "pass":
        matched_candidates = extract_candidates(document, institution, season)
    if rule == "neis_ceremony_row":
        try:
            row = json.loads(document["segments"][0]["text"])
            ids = neis_identifier(event.get("_identifiers", institution.get("identifiers", [])))
            actual = (str(row["ATPT_OFCDC_SC_CODE"]), str(row["SD_SCHUL_CODE"]))
            # 공식 API의 원문도 요청 기관의 공식 복합 코드와 연결되어야 한다.
            checks["institution"] = "pass" if ids is not None and ids == actual and row.get("SCHUL_NM") == institution.get("name") else "fail"
            checks["content"] = "pass" if checks["content"] == "pass" and (
                document["content_hash"] == hashlib.sha256(canonical(row).encode()).hexdigest()
                and document["authority_evidence"]["channel_url"] == "https://open.neis.go.kr/hub"
            ) else "fail"
        except (KeyError, ValueError, IndexError, TypeError):
            checks["content"] = "fail"
    elif not official_channel(institution, document.get("acquisition", {}).get("final_url") or document["display_url"]):
        checks["content"] = "fail"
    for parsed in matched_candidates:
        # 후보가 인용한 문맥과 정형 파서가 해석한 문맥을 동일하게 묶는다.
        same_context = any(
            original["segment_id"] == parsed["context_evidence"][0]["segment_id"]
            and original["quote"] == parsed["context_evidence"][0]["quote"]
            for original in candidate["context_evidence"]
        )
        if (not same_context or parsed["event_date"] != candidate["event_date"] or
                any(parsed[key] != candidate.get(key) for key in
                    ("event_type", "audience", "campus_label", "academic_year", "is_change_notice", "supersedes_text"))):
            continue
        if rule == "academic_calendar_row" and parsed["event_title"] != candidate.get("event_title"):
            continue
        if field in parsed and parsed[field] == value and candidate.get(field) == value and value is not None:
            checks["normalized_value"] = "pass"
            if field == "start_time":
                checks["time_role"] = "pass" if parsed["time_role"] == candidate.get("time_role") == "ceremony_start" else "fail"
            break
    if (checks["normalized_value"] == "unknown" and rule == "explicit_notice"
            and checks["content"] == checks["institution"] == checks["literal_presence"] == checks["event_identity"] == "pass"):
        from .claims import grounded_claim
        # 모델을 파서와 재대결시키지 않고 같은 행사 구간의 역할·연도·값을 독립 검사한다.
        if grounded_claim(field, value, candidate, document, institution, season):
            checks.update(normalized_value="pass", season="pass")
            if field == "start_time":
                checks["time_role"] = "pass"
            result.update(rule_id="grounded_notice", rule_version=REGISTERED_RULES["grounded_notice"])
    if rule == "explicit_notice" and candidate.get("ceremony_status") == "scheduled":
        from .claims import ceremony_negatives
        # 한 줄 파서가 원래 분절의 후속 취소 문장을 생략해도 정상 일정으로 승인하지 않는다.
        if ceremony_negatives(candidate, document, institution):
            checks["normalized_value"] = "fail"
            result["reason_codes"].append("event_unclear")
    if checks["normalized_value"] == "unknown":
        result["reason_codes"].append("value_not_supported")
    if checks["literal_presence"] == "fail":
        result["reason_codes"].append("quote_absent")
    if checks["institution"] != "pass":
        result["reason_codes"].append("wrong_source")
    if checks["event_identity"] == "fail":
        result["reason_codes"].append("event_unclear")
    if checks["season"] == "fail":
        result["reason_codes"].append("season_unclear")
    if checks["content"] == "fail":
        result["reason_codes"].append("content_mismatch")
    if all(value in {"pass", "not_applicable"} for value in checks.values()):
        result["status"] = "supported"
    elif "fail" in checks.values():
        result["status"] = "rejected"
    else:
        result["reason_codes"].append("quote_support_ambiguous")
    return result


def supersession_edges(field, supported, documents, event, season) -> list[dict]:
    """검증된 변경 행의 이전값과 같은 행사 관측만 대체 관계로 연결한다.

    Args:
        field: 현재 평가할 필드. 현재 지원 범위는 날짜다.
        supported: 원문·공식성·활성 규칙 검사를 통과한 관측과 자동 평가 쌍.
        documents: 같은 저장 snapshot의 문서 사전.
        event: 같은 기관·회차·대상·시즌의 행사.
        season: 해당 행사의 학년도와 기간.
    Returns:
        순환하는 변경 주장을 제외한 방향 있는 관계 목록. 원래 관측은 삭제하지 않는다.
    """
    if field != "event_date":
        return []
    edges = []
    for new, automatic in supported:
        candidate = new["candidate"]
        if not candidate.get("is_change_notice"):
            continue
        proof = automatic.get("value_check") or {}
        document = documents.get(new["document_id"], {})
        if (proof.get("rule_id") != "academic_calendar_row"
                or proof.get("rule_version") != REGISTERED_RULES["academic_calendar_row"]
                or not verify(field, candidate[field], new, document, event, season)):
            continue
        change = _CHANGE_ROW.fullmatch(candidate.get("supersedes_text") or "")
        if change is None:
            continue
        try:
            before = date(*map(int, re.findall(r"\d+", change["before"]))).isoformat()
        except ValueError:
            continue
        for old, old_automatic in supported:
            old_candidate = old["candidate"]
            if (old["id"] == new["id"] or old_candidate.get("event_date", {}).get("date") != before
                    or any(old.get(key) != new.get(key) for key in ("institution_id", "season_id", "event_id"))
                    or any(old_candidate.get(key) != candidate.get(key) for key in
                           ("event_key_hint", "event_title", "event_type", "audience", "campus_label"))
                    or not verify(field, old_automatic["value"], old, documents.get(old["document_id"], {}), event, season)):
                continue
            edges.append({"old_id": old["id"], "new_id": new["id"], "field": field,
                          "source_document_id": new["document_id"], "rule_id": proof["rule_id"],
                          "rule_version": proof["rule_version"]})
    adjacency = {}
    for edge in edges:
        adjacency.setdefault(edge["old_id"], set()).add(edge["new_id"])

    def reaches(start, target):
        """변경 관계가 출발 관측으로 되돌아오는지 확인한다."""
        pending, seen = [start], set()
        while pending:
            node = pending.pop()
            if node == target:
                return True
            if node not in seen:
                seen.add(node)
                pending.extend(adjacency.get(node, ()))
        return False

    # 게시 순서를 추측하지 않고 A→B와 B→A는 충돌로 남겨 재확인을 요구한다.
    return [edge for edge in edges if not reaches(edge["new_id"], edge["old_id"])]


def verify(field, value, observation, document, event, season, *, institution=None) -> bool:
    """게시 단계에서 같은 버전의 실제 검사를 반복해 승인 위조를 거부한다.

    Args:
        field: 확인할 필드.
        value: 저장 평가의 정규값.
        observation: 연결한 관측.
        document: 저장 원문 버전.
        event: 대상 행사.
        season: 대상 시즌.
        institution: 기관 정보. 생략 시 행사에서 가져온다.
    Returns:
        모든 필수 실제 검사가 통과했을 때만 True.
    """
    try:
        return value_check(field, value, observation, document, event, season, institution=institution)["status"] == "supported"
    except (KeyError, TypeError, ValueError, IndexError):
        return False
