"""인원 근거·검증한 학교 경로·Agent 실행 기록을 로컬 이력에 보존한다."""

import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from app.validation.claims import validate_count
from app.core.domain import dumps, fingerprint, new_id, utcnow
from app.integrations.providers import official_channel


_PURPOSES = {"date_count": {"date_count", "event_date", "graduation_count"},
             "time": {"time", "start_time"}}


def _event_claim(event: dict) -> dict:
    """현재 허용된 날짜만 인원 검증에 전달한다.

    Args:
        event: 현재 게시 판정이 있는 행사.
    Returns:
        실제 행사 식별과 현재 날짜를 담은 검증 입력.
    """
    result = dict(event)
    publication = event.get("publication", {})
    result["event_date"] = event.get("fields", {}).get("event_date", {}).get("effective_value")
    if not publication.get("current_date_allowed"):
        result["event_date"] = None
    return result


def _count_event(claim: dict, events: list[dict]) -> dict | None:
    """대학 인원을 단 하나의 같은 날짜·행사에만 연결한다.

    Args:
        claim: 날짜와 선택적 행사 키가 있는 인원 후보.
        events: 같은 학교·시즌의 현재 행사.
    Returns:
        유일한 대표 행사. 날짜가 보류되거나 식별이 모호하면 None.
    """
    matches = []
    for event in events:
        if event.get("academic_year") != claim.get("academic_year"):
            continue
        item = _event_claim(event)
        day = item.get("event_date") or {}
        dates = day.get("dates", [day.get("date")]) if isinstance(day, dict) else [day]
        if claim.get("event_date") not in dates:
            continue
        hint = claim.get("event_key_hint")
        if hint and hint not in {event.get("event_key"), event.get("id")}:
            continue
        matches.append(item)
    return matches[0] if len(matches) == 1 else None


def _current_document(document: dict, institution: dict, *, structured: bool = False) -> bool:
    """실패 재확인·폐기된 공식 관계를 과거 인원에서 분리한다.

    Args:
        document: 보존된 문서와 최신 재확인 상태.
        institution: 현재 공식 관계를 가진 학교.
        structured: validate_count가 전용 학교알리미 관계를 검증한 원문인지 여부.
    Returns:
        현재 조회에 사용할 수 있는 원문인지 여부.
    """
    if document.get("recheck", {}).get("status") == "failed":
        return False
    if structured:
        return True  # 전용 인원 검증기가 실제 제공자·학교 코드·학년 관계를 다시 검사한다.
    from app.validation.publication import _authority
    return _authority(document, institution, datetime.now(timezone.utc))


def _url_key(url: str) -> str:
    """홈페이지와 경로를 비교할 때 무의미한 fragment와 끝 슬래시를 제거한다.

    Args:
        url: 검증 대상 URL.
    Returns:
        비교용 공개 URL.
    """
    value = urlsplit(url)
    return urlunsplit((value.scheme.lower(), value.netloc.lower(), value.path.rstrip("/"), value.query, ""))


def _reusable(attempt: dict, document: dict | None, institution: dict) -> bool:
    """성공 원문과 공식 링크를 갖춘 홈페이지·목록만 재사용 대상으로 승인한다.

    Args:
        attempt: 실행기가 제출한 읽기 결과.
        document: 실제 저장된 읽기 원문.
        institution: 공식 채널을 가진 학교.
    Returns:
        시즌 사실과 분리해 다시 방문할 진입 경로인지 여부.
    """
    url = attempt.get("url", "")
    if (not document or attempt.get("status") != "succeeded" or not attempt.get("reusable")
            or not official_channel(institution, url) or document.get("status") != "ok"
            or document.get("partial") or document.get("authority") != "official"
            or document.get("reading", {}).get("institution_match") != "matched"
            or document.get("reading", {}).get("content_match") != "expected"
            or _url_key(document.get("display_url", "")) != _url_key(url)
            or not _current_document(document, institution)):
        return False
    if institution.get("homepage_url") and _url_key(url) == _url_key(institution["homepage_url"]):
        return True
    title = document.get("title") or ""
    parsed = urlsplit(url)
    query = {key.lower(): value.lower() for key, value in parse_qsl(parsed.query)}
    individual = (any(query.get(key) in {"v", "view", "read"} for key in ("mode", "act", "action"))
                  or any(key in query for key in ("nttid", "articleid", "board_seq", "no", "idx"))
                  or bool(re.search(r"/(?:view|read|article)(?:/|\.|$)", parsed.path, re.I))
                  or bool(re.search(r"(?:20\d{2}\s*(?:학년도|년)|제\s*\d+\s*회).*(?:졸업|학위)", title)))
    links = {link for link in attempt.get("links", []) if isinstance(link, str)
             and official_channel(institution, link) and _url_key(link) != _url_key(url)}
    return not individual and len(links) >= 2 and bool(re.search(r"공지사항|학사일정|학사공지", title))


def save_supplement(service, conn, institution: dict, season: dict, result: dict, document_ids: dict) -> None:
    """기각 후보와 실패 시도를 포함한 인원·경로·실행 이력을 같은 거래에 저장한다.

    Args:
        service: 원문과 현재 행사 판정을 읽는 서비스.
        conn: save_research와 공유하는 쓰기 연결.
        institution: 조사한 학교.
        season: 조사 시즌.
        result: 실행기가 제출한 counts·routes·agent_trace 결과.
        document_ids: 이번 읽기의 임시 UUID와 실제 불변 문서 UUID 매핑.
    """
    documents = service._documents(conn, institution["id"])
    from app.validation.periods import fact_season
    events = [event for event in service._events(conn, season["id"])
              if event["institution"]["id"] == institution["id"]]
    event_ids = [event["id"] for event in events]
    rejected = []
    for original in result.get("counts", []):
        claim = dict(original)
        identity = document_ids.get(claim.get("document_id"))
        if not identity or identity not in documents:
            rejected.append({"claim": original, "proof": {"allowed": False, "reasons": ["count_evidence_missing"]}})
            continue
        claim["document_id"] = identity
        event = _count_event(claim, events) if institution.get("type") in {"university", "junior_college"} else None
        proof = validate_count(claim, documents[identity], institution,
                               fact_season(season, claim["academic_year"]), event=event)
        if proof["allowed"] and not _current_document(documents[identity], institution,
                structured=claim.get("source") in {"schoolinfo_api09", "schoolinfo_public"}):
            proof = {"allowed": False, "reasons": ["count_recheck_failed"]}
        event_id = event["id"] if event else None
        digest = fingerprint({"institution_id": institution["id"], "season_id": season["id"],
                              "event_id": event_id, "claim": claim, "proof": proof})
        conn.execute("INSERT OR IGNORE INTO count_observations VALUES(?,?,?,?,?,?,?,?,?)",
                     (new_id(), institution["id"], season["id"], event_id, identity, dumps(claim), dumps(proof), digest, utcnow()))
    for raw in result.get("routes", []):
        attempt = dict(raw)
        identity = document_ids.get(attempt.get("document_id"))
        document = documents.get(identity)
        attempt["document_id"] = identity
        approved = _reusable(attempt, document, institution)
        url, purpose = attempt.get("url", ""), attempt.get("purpose", result.get("purpose", "date_count"))
        saved = conn.execute("SELECT * FROM collection_routes WHERE institution_id=? AND url=? AND purpose=?",
                             (institution["id"], url, purpose)).fetchone()
        route_id = saved["id"] if saved else None
        checked_at = document.get("last_checked_at") or document["retrieved_at"] if document else utcnow()
        if approved:
            route_id = route_id or new_id()
            conn.execute("INSERT INTO collection_routes VALUES(?,?,?,?,?,1,?,?,NULL,?) ON CONFLICT(institution_id,url,purpose) "
                         "DO UPDATE SET reading_method=excluded.reading_method,verified=1,last_success_at=excluded.last_success_at,"
                         "last_attempt_at=excluded.last_attempt_at,failure_reason=NULL,document_id=excluded.document_id",
                         (route_id, institution["id"], url, purpose, document["reading"]["method"], checked_at, checked_at, identity))
        elif saved:
            failure = attempt.get("failure_reason") or "route_not_reusable"
            conn.execute("UPDATE collection_routes SET last_attempt_at=?,failure_reason=? WHERE id=?", (checked_at, failure, route_id))
        attempt["verified_reusable"] = approved
        conn.execute("INSERT INTO route_attempts VALUES(?,?,?,?,?)", (new_id(), route_id, institution["id"], dumps(attempt), utcnow()))
    trace = {"agent_trace": result.get("agent_trace") or [{"kind": "completion", "reason": result.get("stop_reason", "result_saved")}],
             "tool_trace": result.get("trace", []), "stop_reason": result.get("stop_reason"), "rejected_counts": rejected}
    target = result.get("target_event_id")
    if target not in event_ids:
        target = None
    conn.execute("INSERT INTO investigation_traces VALUES(?,?,?,?,?,?,?)", (new_id(), institution["id"], season["id"],
                 result.get("purpose", "date_count"), target, dumps(trace), utcnow()))


def current_counts(service, conn, season: dict, events: list[dict]) -> dict:
    """최신 원문을 재검사해 같은 값의 독립 근거는 유지하고 값 충돌은 보류한다.

    Args:
        service: 공식 기관·불변 원문 조회 서비스.
        conn: 일정표와 공유하는 읽기 snapshot.
        season: 현재 조사 시즌.
        events: 현재 게시 판정을 가진 행사 목록.
    Returns:
        해당 행사의 학년도와 일치하는 근거만 모은 행사 ID별 현재 허용 인원.
    """
    from app.validation.periods import fact_season
    values, cache = {}, {}
    by_institution = {}
    for event in events:
        if event.get("academic_year") is not None:
            by_institution.setdefault(event["institution"]["id"], []).append(event)
    if not by_institution:
        return {}
    placeholders = ",".join("?" for _ in by_institution)
    for row in conn.execute(
            f"SELECT * FROM count_observations WHERE institution_id IN ({placeholders}) ORDER BY created_at,id",
            tuple(by_institution)):
        if not json.loads(row["proof_json"]).get("allowed"):
            continue
        institution_id = row["institution_id"]
        if institution_id not in cache:
            cache[institution_id] = (service._institution(conn, institution_id), service._documents(conn, institution_id))
        institution, documents = cache[institution_id]
        document = documents.get(row["document_id"])
        claim = json.loads(row["claim_json"])
        if not document or not _current_document(document, institution,
                structured=claim.get("source") in {"schoolinfo_api09", "schoolinfo_public"}):
            continue
        university = institution.get("type") in {"university", "junior_college"}
        source_season = service._season(conn, row["season_id"])
        for projected in by_institution[institution_id]:
            if projected["academic_year"] != claim["academic_year"]:
                continue
            if university and projected["id"] != row["event_id"]:
                continue
            event = _event_claim(projected)
            # 취득 시즌과 현재 표시 시즌이 달라도 원문 학년도·행사 관계는 그대로 검사한다.
            if validate_count(claim, document, institution,
                              fact_season(source_season, projected["academic_year"]),
                              event=event if university else None)["allowed"]:
                values.setdefault(projected["id"], set()).add(claim["value"])
    return {key: next(iter(counts)) for key, counts in values.items() if len(counts) == 1}


def reusable_routes(conn, institution_id: str, purpose: str) -> list[dict]:
    """마지막 실패 원인을 유지한 검증 경로를 조사 목적별로 조회한다.

    Args:
        conn: 읽기 연결.
        institution_id: 조사 학교 UUID.
        purpose: date_count 또는 time.
    Returns:
        현재 공식 관계가 유지되는 경로. 과거 성공일은 올해 사실의 근거가 아니다.
    """
    row = conn.execute("SELECT * FROM institutions WHERE id=?", (institution_id,)).fetchone()
    if not row:
        return []
    institution = dict(row)
    institution["official_channels"] = json.loads(institution.pop("official_channels_json"))
    allowed = _PURPOSES.get(purpose, {purpose})
    return [{**dict(item), "verified": bool(item["verified"])} for item in conn.execute(
        "SELECT * FROM collection_routes WHERE institution_id=? AND verified=1 ORDER BY last_success_at DESC,id", (institution_id,))
        if item["purpose"] in allowed and official_channel(institution, item["url"])]
