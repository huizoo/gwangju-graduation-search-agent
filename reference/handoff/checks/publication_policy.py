"""Executable publication specification; this is not the application's persistence layer.

The context is assembled by trusted server code from one read snapshot. Fixture
rules deliberately stay disabled unless a caller opts into this reference suite.
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
FIXTURE_RULES = {"explicit_notice": "1.0.0"}
# NEIS/calendar IDs are designed contracts; their live mappings are not implemented here.


def _instant(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def _past(value, now):
    parsed = _instant(value)
    return parsed is not None and parsed <= now


def _within(value, season):
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


def _url_under(url, channel):
    try:
        source, allowed = urlsplit(url), urlsplit(channel)
        prefix = allowed.path.rstrip("/")
        return source.scheme == allowed.scheme == "https" and source.netloc == allowed.netloc and (not prefix or source.path == prefix or source.path.startswith(prefix + "/"))
    except (AttributeError, TypeError, ValueError):
        return False


def _authority(document, institution, now):
    authority = document.get("authority_evidence") or {}
    if document.get("authority") != "official" or not _past(authority.get("verified_at"), now):
        return False
    return any(
        channel.get("url") == authority.get("channel_url")
        and channel.get("verified_by_url") == authority.get("verified_by_url")
        and channel.get("verified_at") == authority.get("verified_at")
        and _url_under(document.get("display_url"), channel.get("url"))
        and _url_under((document.get("acquisition") or {}).get("final_url"), channel.get("url"))
        for channel in institution.get("official_channels", [])
    )


def _literal_support(candidate, field, document):
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
    """Small fixture normalizer. It is not an enabled production extraction rule."""
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
    proof = automatic.get("value_check") or {}
    rule = proof.get("rule_id")
    registered = context.get("registered_rules", {})
    if not context.get("use_fixture_rules") or FIXTURE_RULES.get(rule) != proof.get("rule_version") or registered.get(rule) != proof.get("rule_version"):
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
        if quote is None or not _literal_value(field, automatic.get("value"), quote, candidate):
            continue
        if field == "start_time" and (candidate.get("event_date") or {}).get("date") != automatic.get("bound_date"):
            continue
        supports.append(document)
    return supports, [] if supports else ["evidence_invalidated"]


def _permission(visible=False, allowed=False, confirmed=None, recheck="not_applicable", reasons=()):
    return {"general_visible": visible, "current_allowed": allowed, "confirmed_at": confirmed, "recheck_status": recheck, "reason_codes": sorted(set(reasons))}


def _field(field, snapshot, event, season, context, now):
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
            # Paper/offline human confirmation may have no stored web document.
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
    """Derive all views from trusted evidence, never from supplied output values."""
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
    # A retained historical date cannot authorize a currently usable start time.
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
    """Reject stale or altered output; callers serialize the recomputed result."""
    expected = calculate_publication(event, season, validated_context)
    if event.get("publication") != expected:
        raise ValueError("supplied Publication differs from the trusted calculation")
    return expected
