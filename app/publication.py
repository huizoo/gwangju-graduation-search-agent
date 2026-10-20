"""저장 snapshot의 검증 근거로 일반·판매·HTML 공통 게시값을 계산한다.

인계 4.0 참조 정책을 바탕으로 운영 규칙 등록과 재검증 경계를 추가한다.
"""
from __future__ import annotations

from datetime import date, datetime
import re
from urllib.parse import urlsplit

POLICY_VERSION = "4.0"
FIELDS = ("ceremony_status", "event_date", "start_time", "venue")
EVENT_BLOCKERS = {"event_unclear", "event_match_required", "season_unclear", "cancelled_notice"}
FIELD_BLOCKERS = {"conflict", "manual_difference", "semantic_review_required"}
REQUIRED_CHECKS = ("institution", "content", "season", "event_identity", "literal_presence", "normalized_value")
GRADUATION_EVENTS = {"graduation_ceremony", "graduation_and_completion", "degree_award_ceremony"}
class RuleRegistry:
    """검증 구현과 명시적으로 활성화한 버전을 함께 보관한다."""

    def __init__(self):
        """비어 있는 운영 허용 목록을 만든다."""
        self._rules = {}

    def register(self, rule_id, version, verifier):
        """서버가 검토한 규칙을 등록한다.

        Args:
            rule_id: 계약의 규칙 ID.
            version: 시험한 구현 버전.
            verifier: 실제 원문·기관·행사·정규값을 다시 검사할 함수.
        """
        from app.domain import validate
        validate("RuleId", rule_id)
        self._rules[rule_id] = (version, verifier)

    def versions(self):
        """등록 버전을 반환한다.

        Returns:
            규칙 ID와 버전의 사본.
        """
        return {key: item[0] for key, item in self._rules.items()}

    def verify(self, rule_id, version, field, value, observation, document, event, season):
        """등록한 같은 버전으로 저장 근거를 다시 검사한다.

        Args:
            rule_id: 규칙 ID.
            version: 평가 시 규칙 버전.
            field: 검사할 필드.
            value: 검사할 정규값.
            observation: 저장 관측.
            document: 불변 원문 버전.
            event: 같은 snapshot의 행사.
            season: 같은 snapshot의 시즌.
        Returns:
            근거가 현재도 값을 뒷받침하면 True.
        """
        item = self._rules.get(rule_id)
        if not item or item[0] != version:
            return False
        return item[1](field, value, observation, document, event, season, institution=event["institution"]) is True


RULES = RuleRegistry()


def _instant(value):
    """시간대가 명시된 유효 시각만 해석한다.

    Args:
        value: 서버가 조립한 value 입력.
    Returns:
        해석한 시각 또는 None.
    """
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def _past(value, now):
    """확인시각이 판정시각보다 미래인지 검사한다.

    Args:
        value: 서버가 조립한 value 입력.
        now: 서버가 조립한 now 입력.
    Returns:
        유효한 과거·현재 시각이면 True.
    """
    parsed = _instant(value)
    return parsed is not None and parsed <= now


def _within(value, season):
    """날짜 정밀도를 유지해 시즌 범위 포함 여부를 검사한다.

    Args:
        value: 서버가 조립한 value 입력.
        season: 서버가 조립한 season 입력.
    Returns:
        시즌과 겹치는 유효 값이면 True.
    """
    if not isinstance(value, dict):
        return False
    try:
        if value.get("precision") == "day":
            day = date.fromisoformat(value["date"])
            return date.fromisoformat(season["search_start"]) <= day <= date.fromisoformat(season["search_end"])
        if value.get("precision") == "month":
            month = value["month"]
            return bool(re.fullmatch(r"\d{4}-\d{2}", month)) and season["search_start"][:7] <= month <= season["search_end"][:7]
    except (KeyError, TypeError, ValueError):
        pass
    return False


def _url_under(url, channel, require_https=True):
    """공식 채널의 HTTPS 호스트·경로 경계를 검사한다.

    Args:
        url: 서버가 조립한 url 입력.
        channel: 서버가 조립한 channel 입력.
        require_https: 실제 취득 경로에 TLS를 요구할지 여부.
    Returns:
        원문 주소가 해당 채널 내부이면 True.
    """
    try:
        source, allowed = urlsplit(url), urlsplit(channel)
        prefix = allowed.path.rstrip("/")
        return source.scheme in (("https",) if require_https else ("http", "https")) and allowed.scheme in ("http", "https") and source.netloc == allowed.netloc and (not prefix or source.path == prefix or source.path.startswith(prefix + "/"))
    except (AttributeError, TypeError, ValueError):
        return False


def _authority(document, institution, now):
    """저장한 공식 채널 관계와 원문 취득 경로를 대조한다.

    Args:
        document: 서버가 조립한 document 입력.
        institution: 서버가 조립한 institution 입력.
        now: 서버가 조립한 now 입력.
    Returns:
        기관·출처 관계가 입증되면 True.
    """
    authority = document.get("authority_evidence") or {}
    if document.get("authority") != "official" or not _past(authority.get("verified_at"), now):
        return False
    return any(
        channel.get("url") == authority.get("channel_url")
        and channel.get("verified_by_url") == authority.get("verified_by_url")
        and channel.get("verified_at") == authority.get("verified_at")
        and _url_under(document.get("display_url"), channel.get("url"), require_https=False)
        and _url_under((document.get("acquisition") or {}).get("final_url"), channel.get("url"))
        for channel in institution.get("official_channels", [])
    )


def _literal_support(candidate, field, document):
    """문맥과 필드 인용이 같은 행사 단위의 실제 원문인지 검사한다.

    Args:
        candidate: 서버가 조립한 candidate 입력.
        field: 서버가 조립한 field 입력.
        document: 서버가 조립한 document 입력.
    Returns:
        연결된 필드 인용문 또는 None.
    """
    names = {"ceremony_status": "context_evidence", "event_date": "date_evidence", "start_time": "time_evidence", "venue": "venue_evidence"}
    segments = {segment["id"]: segment for segment in document.get("segments", [])}
    context = candidate.get("context_evidence") or []
    field_evidence = candidate.get(names[field]) or []
    if not context or not field_evidence:
        return None
    units, quotes = set(), []
    for evidence in context + field_evidence:
        segment = segments.get(evidence.get("segment_id"), {})
        quote = evidence.get("quote")
        if evidence.get("kind") != "literal" or not quote or quote not in segment.get("text", "") or not segment.get("unit_id"):
            return None
        units.add(segment["unit_id"])
    for evidence in field_evidence:
        quotes.append(evidence["quote"])
    return " ".join(quotes) if len(units) == 1 else None


def _literal_value(field, value, quote, candidate):
    """참조 회귀시험에만 쓰는 정규화 함수이며 운영 등록 규칙이 아니다."""
    if field == "event_date":
        if not isinstance(value, dict):
            return False
        if value.get("precision") == "day":
            try:
                day = date.fromisoformat(value["date"])
            except (KeyError, TypeError, ValueError):
                return False
            patterns = (re.escape(day.isoformat()), rf"{day.year}\s*년\s*0?{day.month}\s*월\s*0?{day.day}\s*일")
            return any(re.search(pattern, quote) for pattern in patterns)
        if value.get("precision") == "month":
            try:
                year, month = map(int, value["month"].split("-"))
            except (KeyError, TypeError, ValueError):
                return False
            return bool(re.search(rf"{year}\s*년\s*0?{month}\s*월", quote) or value["month"] in quote)
        return False
    if field == "start_time":
        if candidate.get("time_role") != "ceremony_start" or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(value)):
            return False
        if not re.search(r"시작|개식|개회|ceremony\s+(?:starts?|begins?)", quote, re.I):
            return False
        hour, minute = map(int, value.split(":"))
        if value in quote:
            return True
        period = "오후" if hour >= 12 else "오전"
        clock = hour % 12 or 12
        minute_part = rf"\s*{minute}\s*분" if minute else r"(?:\s*0\s*분)?(?!\s*\d+\s*분)"
        return bool(re.search(rf"{period}\s*0?{clock}\s*시{minute_part}", quote))
    if field == "ceremony_status":
        if value == "scheduled":
            return bool(re.search(r"졸업식|학위수여|graduation", quote, re.I)) and not bool(re.search(r"취소|미실시|cancelled|canceled", quote, re.I))
        if value == "cancelled":
            return bool(re.search(r"취소|cancelled|canceled", quote, re.I))
        if value == "not_held":
            return bool(re.search(r"미실시|개최하지|not\s+held", quote, re.I))
        return False
    return isinstance(value, str) and bool(value.strip()) and value in quote


def _support(field, automatic, event, season, context, now):
    """등록 규칙·관측 연결·해시·공식 원문을 다시 검사한다.

    Args:
        field: 서버가 조립한 field 입력.
        automatic: 서버가 조립한 automatic 입력.
        event: 서버가 조립한 event 입력.
        season: 서버가 조립한 season 입력.
        context: 서버가 조립한 context 입력.
        now: 서버가 조립한 now 입력.
    Returns:
        지원 문서 목록과 거절 사유 목록.
    """
    proof = automatic.get("value_check") or {}
    rule = proof.get("rule_id")
    registered = context.get("registered_rules", {})
    if registered.get(rule) != proof.get("rule_version") or RULES.versions().get(rule) != proof.get("rule_version"):
        return [], ["rule_not_enabled"]
    if proof.get("policy_version") != POLICY_VERSION or proof.get("field") != field or proof.get("status") != "supported" or not _past(proof.get("checked_at"), now):
        return [], ["value_not_supported"]
    required = REQUIRED_CHECKS + (("time_role",) if field == "start_time" else ())
    if any(proof.get("checks", {}).get(name) != "pass" for name in required):
        return [], ["value_not_supported"]
    if field != "start_time" and proof.get("checks", {}).get("time_role") != "not_applicable":
        return [], ["value_not_supported"]
    evidence_ids = set(automatic.get("evidence_ids", [])) & set(proof.get("evidence_ids", []))
    documents, observations = context.get("documents", {}), context.get("observations", {})
    supports = []
    for observation_id in evidence_ids:
        observation = observations.get(observation_id, {})
        document_id = observation.get("document_id")
        document = documents.get(document_id, {})
        candidate = observation.get("candidate") or {}
        if (
            observation.get("disposition") != "accepted"
            or observation.get("institution_id") != event["institution"]["id"]
            or document.get("institution_id") != event["institution"]["id"]
            or observation.get("season_id") != season["id"]
            or observation.get("event_id") != event["id"]
            or candidate.get("academic_year") != season["academic_year"]
            or candidate.get("event_type") != event.get("event_type")
            or candidate.get("event_type") not in GRADUATION_EVENTS
            or candidate.get("event_key_hint") != event.get("event_key")
            or candidate.get("audience") != event.get("audience")
            or candidate.get("campus_label") != event.get("campus_label")
            or candidate.get("season_alignment") != "explicit"
            or candidate.get(field) != automatic.get("value")
            or document_id not in proof.get("document_ids", [])
            or not document.get("content_hash")
            or proof.get("document_hashes", {}).get(document_id) != document.get("content_hash")
            or not _past(observation.get("created_at"), now)
            or not _authority(document, event["institution"], now)
        ):
            continue
        acquired = document.get("acquisition") or {}
        reading = document.get("reading") or {}
        if document.get("status") != "ok" or acquired.get("transport_status") != "ok" or acquired.get("tls_verified") is not True:
            continue
        if reading.get("content_match") != "expected" or reading.get("institution_match") != "matched" or reading.get("scope") not in ("full_text", "rendered_dom"):
            continue
        quote = _literal_support(candidate, field, document)
        if quote is None or not RULES.verify(rule, proof.get("rule_version"), field, automatic.get("value"), observation, document, event, season):
            continue
        if field == "start_time" and (candidate.get("event_date") or {}).get("date") != automatic.get("bound_date"):
            continue
        supports.append(document)
    return supports, [] if supports else ["evidence_invalidated"]


def _permission(visible=False, allowed=False, confirmed=None, recheck="not_applicable", reasons=()):
    """필드의 과거 표시와 현재 사용 권한을 분리한다.

    Args:
        visible: 서버가 조립한 visible 입력.
        allowed: 서버가 조립한 allowed 입력.
        confirmed: 서버가 조립한 confirmed 입력.
        recheck: 서버가 조립한 recheck 입력.
        reasons: 서버가 조립한 reasons 입력.
    Returns:
        FieldPermission 객체.
    """
    return {"general_visible": visible, "current_allowed": allowed, "confirmed_at": confirmed, "recheck_status": recheck, "reason_codes": sorted(set(reasons))}


def _field(field, snapshot, event, season, context, now):
    """수기 보호·충돌·재확인 상태를 적용해 필드값을 판정한다.

    Args:
        field: 서버가 조립한 field 입력.
        snapshot: 서버가 조립한 snapshot 입력.
        event: 서버가 조립한 event 입력.
        season: 서버가 조립한 season 입력.
        context: 서버가 조립한 context 입력.
        now: 서버가 조립한 now 입력.
    Returns:
        값, 연결 날짜, 필드별 권한.
    """
    automatic = snapshot.get("automatic") or {}
    manual = snapshot.get("manual")
    reasons = set(snapshot.get("reason_codes", [])) & FIELD_BLOCKERS
    conflict = snapshot.get("review_required") or snapshot.get("has_web_difference") or bool(reasons)
    if manual is not None:
        if manual.get("mode") != "set" or manual.get("basis") not in ("phone_confirmed", "document_confirmed") or not _past(manual.get("confirmed_at"), now):
            return None, None, _permission(reasons=["confirmation_time_missing" if not manual.get("confirmed_at") else "manual_confirmation_invalid"])
        if conflict:
            detected = _instant(automatic.get("conflict_detected_at"))
            if not str(manual.get("note") or "").strip() or not automatic.get("fingerprint") or manual.get("resolved_fingerprint") != automatic.get("fingerprint") or detected is None or detected > now or _instant(manual["confirmed_at"]) < detected:
                return None, None, _permission(reasons=["manual_difference"])
        value = manual.get("value")
        if value is None or (field == "event_date" and not _within(value, season)):
            return None, None, _permission(reasons=["value_not_supported"])
        if manual.get("basis") == "document_confirmed":
            document_id = manual.get("source_document_id")
            documents = context.get("documents", {})
            if document_id:
                selected = documents.get(document_id)
                if not selected or selected.get("institution_id") != event["institution"]["id"] or (manual.get("source_url") and selected.get("display_url") != manual["source_url"]):
                    return None, None, _permission(reasons=["evidence_link_invalid"])
            else:
                versions = [d for d in documents.values() if manual.get("source_url") and d.get("display_url") == manual["source_url"]
                            and d.get("institution_id") == event["institution"]["id"] and _instant(d.get("retrieved_at")) is not None
                            and _instant(d["retrieved_at"]) <= _instant(manual["confirmed_at"])]
                selected = max(versions, key=lambda d: (_instant(d["retrieved_at"]), d["id"]), default=None)
            # 종이와 전화 확인은 웹 연결 없이도 독립적인 사람 확인으로 남긴다.
            if selected:
                if selected.get("status") == "content_mismatch" or (selected.get("reading") or {}).get("institution_match") == "mismatched":
                    return None, None, _permission(reasons=["evidence_invalidated"])
                check = selected.get("recheck") or {}
                if check.get("status") in ("failed", "succeeded") and not _past(check.get("attempted_at"), now):
                    return None, None, _permission(reasons=["value_not_supported"])
                if check.get("status") == "failed" and _instant(check["attempted_at"]) >= _instant(manual["confirmed_at"]):
                    return value, manual.get("bound_date"), _permission(True, False, manual["confirmed_at"], "failed", ["recheck_failed"])
        return value, manual.get("bound_date"), _permission(True, True, manual["confirmed_at"])
    if conflict or automatic.get("status") != "official_confirmed" or automatic.get("value") is None:
        return None, None, _permission(reasons=reasons or ["publication_held"])
    supports, failures = _support(field, automatic, event, season, context, now)
    if failures:
        return None, None, _permission(reasons=failures)
    value = automatic["value"]
    if field == "event_date" and not _within(value, season):
        return None, None, _permission(reasons=["outside_window"])
    statuses = []
    for document in supports:
        check = document.get("recheck") or {}
        status = check.get("status", "not_attempted")
        if status in ("failed", "succeeded") and not _past(check.get("attempted_at"), now):
            return None, None, _permission(reasons=["value_not_supported"])
        statuses.append(status)
    current = any(status != "failed" for status in statuses)
    recheck = "succeeded" if "succeeded" in statuses else ("not_attempted" if current else "failed")
    return value, automatic.get("bound_date"), _permission(True, current, automatic["value_check"]["checked_at"], recheck, [] if current else ["recheck_failed"])


def calculate_publication(event, season, validated_context):
    """서버 근거에서 모든 조회의 유일한 게시값을 계산한다.

    Args:
        event: 같은 snapshot의 행사와 필드별 이력.
        season: 행사 검색 범위.
        validated_context: 서버가 조립한 원문·관측·허용 규칙·판정시각.
    Returns:
        일반 조회와 현재 판매 허용을 구별한 Publication.
    Raises:
        ValueError: 판정시각에 timezone이 없거나 잘못된 경우.
    """
    now = _instant(validated_context.get("now"))
    if now is None:
        raise ValueError("validated_context.now must be a timezone-aware timestamp")
    permissions, values, bound_dates = {}, {}, {}
    global_reasons = set(validated_context.get("event_blockers", [])) | (set(event.get("reason_codes", [])) & EVENT_BLOCKERS)
    if event.get("season_id") != season.get("id") or event.get("event_type") not in GRADUATION_EVENTS:
        global_reasons.add("event_match_required")
    for name in FIELDS:
        values[name], bound_dates[name], permissions[name] = _field(name, event.get("fields", {}).get(name, {}), event, season, validated_context, now)
    status = values["ceremony_status"]
    institution = event.get("institution", {})
    event_day = (values.get("event_date") or {}).get("date")
    closed_on = institution.get("operation_effective_date")
    closed_before_event = institution.get("operation_status") == "closed_confirmed" and bool(closed_on and event_day and closed_on <= event_day)
    excluded = status in ("cancelled", "not_held") or closed_before_event
    if global_reasons or excluded or status != "scheduled":
        for name in FIELDS:
            permissions[name] = _permission(reasons=global_reasons or ["cancelled_notice" if excluded else "publication_held"])
            values[name] = None
    day_claim = values["event_date"]
    if isinstance(day_claim, dict) and day_claim.get("precision") == "month":
        permissions["event_date"] = _permission(reasons=["month_only"])
        values["event_date"] = None
        day_claim = None
    day = day_claim.get("date") if isinstance(day_claim, dict) and day_claim.get("precision") == "day" else None
    if not day or bound_dates["start_time"] != day:
        permissions["start_time"] = _permission(reasons=["date_context_mismatch"] if values["start_time"] is not None else permissions["start_time"]["reason_codes"])
        values["start_time"] = None
    # 과거 날짜를 보존해도 현재 허용되지 않으면 연결 시각을 판매에 사용할 수 없다.
    permissions["start_time"]["current_allowed"] = bool(permissions["start_time"]["current_allowed"] and permissions["event_date"]["current_allowed"] and permissions["ceremony_status"]["current_allowed"])
    permissions["event_date"]["current_allowed"] = bool(permissions["event_date"]["current_allowed"] and permissions["ceremony_status"]["current_allowed"])
    time = values["start_time"] if day else None
    if values["venue"] is not None and bound_dates["venue"] is not None and bound_dates["venue"] != day:
        permissions["venue"] = _permission(reasons=["date_context_mismatch"])
        values["venue"] = None
    venue = values["venue"] if day else None
    if not day:
        for name in ("start_time", "venue"):
            permissions[name]["general_visible"] = False
            permissions[name]["current_allowed"] = False
    current_date = permissions["event_date"]["current_allowed"]
    current_time = permissions["start_time"]["current_allowed"]
    reasons = sorted(global_reasons | {reason for permission in permissions.values() for reason in permission["reason_codes"]})
    return {
        "policy_version": POLICY_VERSION,
        "state": "excluded" if excluded else ("date_and_time" if day and time else "date_only" if day else "hold"),
        "calendar_eligible": bool(day), "calendar_date": day, "calendar_time": time, "calendar_venue": venue,
        "withheld_fields": [name for name in FIELDS if not permissions[name]["general_visible"]],
        "reason_codes": reasons, "sales_ready": bool(current_date and current_time),
        "current_date_allowed": current_date, "current_time_allowed": current_time,
        "field_permissions": permissions,
    }


def validate_supplied_publication(event, season, validated_context):
    """제출된 파생값과 서버 재계산 결과의 일치를 검사한다.

    Args:
        event: 비교할 Publication을 포함한 행사.
        season: 시즌.
        validated_context: 서버 근거 snapshot.
    Returns:
        재계산한 Publication.
    Raises:
        ValueError: 게시값이 변조되거나 오래된 경우.
    """
    expected = calculate_publication(event, season, validated_context)
    if event.get("publication") != expected:
        raise ValueError("supplied Publication differs from the trusted calculation")
    return expected
