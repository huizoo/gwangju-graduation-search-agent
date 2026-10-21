"""작업 이력의 기술 원인을 사실 확정과 분리해 짧은 사용자 안내로 투영한다."""

import json


_SOURCE_FAILURES = {
    "access_failed", "tls_error", "read_failed", "recheck_failed",
    "unsupported_format", "document_partial", "empty_article", "ocr_required",
}
_SERVICE_FAILURES = {
    "provider_error", "configuration_required", "external_transfer_blocked",
    "parse_failed", "search_request_error", "model_unavailable", "invalid_decision",
    "invalid_contract",
}
_INCOMPLETE = {
    "item_budget", "item_timeout", "segment_budget", "job_budget", "interrupted",
    "document_limit", "search_limit", "decision_limit", "coverage_incomplete",
}


def _read_json(value, fallback):
    """이전 이력의 빈 필드를 허용하되 구조 오류는 안전한 기본값으로 제한한다."""
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


def _nodes(value):
    """원문이나 오류 메시지를 해석하지 않고 구조화된 이력 노드만 순회한다."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nodes(child)


def _item_feedback(item):
    """마지막 실행 결과의 원인과 항목 상태를 함께 분류한다."""
    history = _read_json(item["history_json"], [])
    latest = history[-1] if isinstance(history, list) and history else {}
    error = _read_json(item["error_json"], {})
    raw_reasons = _read_json(item["reason_codes_json"], [])
    codes = set(raw_reasons) if isinstance(raw_reasons, list) else {"parse_failed"}
    source_failed = False
    # 재개 전 실패 이력을 다시 합치지 않는다. 마지막 체크포인트는 그 실행의 누적 근거다.
    for node in _nodes([latest, error]):
        codes.update(code for code in node.get("reason_codes", []) if isinstance(code, str))
        codes.update(node[key] for key in ("code", "status", "outcome", "stop_reason", "reason")
                     if isinstance(node.get(key), str))
        source_failed |= (
            node.get("provider") == "reader" and node.get("code") in _SERVICE_FAILURES
            or node.get("tool") == "read_public_document"
            and node.get("status") in _SOURCE_FAILURES | {"parse_failed"}
        )
    if item["outcome"] == "verified" and item["status"] == "done":
        return None
    if source_failed or codes & _SOURCE_FAILURES:
        return "source_unavailable"
    if codes & _SERVICE_FAILURES or item["status"] == "failed" or item["outcome"] == "error":
        return "service_error"
    if codes & _INCOMPLETE or item["status"] != "done" or item["outcome"] in {"limit_reached", "blocked", "needs_review"}:
        return "incomplete"
    return "not_found" if item["outcome"] == "no_data" else "incomplete"


def _purpose(job):
    """이전 작업의 목적 누락은 기본 조사로 읽고 충돌한 목적은 승인하지 않는다."""
    request = _read_json(job["request_json"], {})
    snapshot = _read_json(job["scope_snapshot_json"], {})
    if not isinstance(request, dict) or not isinstance(snapshot, dict):
        return None, {}
    purpose = snapshot.get("purpose", request.get("purpose", "date_count"))
    if request.get("purpose", purpose) != purpose:
        return None, request
    return purpose, request


def time_feedback(conn, job_id):
    """종료된 시간 찾기의 미확인 원인을 읽기 전용으로 반환한다.

    Args:
        conn: 현재 SQLite snapshot 연결. sqlite3.Row를 반환한다.
        job_id: 해당 시즌·행사에 연결된 시간 찾기 작업 ID.
    Returns:
        not_found, source_unavailable, service_error, incomplete 또는 None.
        현재 허용 시간과 진행·일시정지·취소 표시는 호출자가 우선 적용한다.
    """
    job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job or job["kind"] != "schedule_scan" or _purpose(job)[0] != "time":
        return None
    if job["status"] not in {"completed", "failed"}:
        return None
    items = conn.execute("SELECT * FROM job_items WHERE job_id=?", (job_id,)).fetchall()
    if not items:
        return "service_error" if job["status"] == "failed" else "incomplete"
    feedback = {_item_feedback(item) for item in items}
    for value in ("source_unavailable", "service_error", "incomplete", "not_found"):
        if value in feedback:
            return value
    return None


def season_research_summary(conn, season_id, eligible_ids):
    """현재 명부 전체를 대상으로 한 최신 기본 조사 상태를 요약한다.

    Args:
        conn: 현재 SQLite snapshot 연결. 데이터를 변경하지 않는다.
        season_id: 사용자가 고른 날짜 범위 시즌 ID.
        eligible_ids: 현재 기본 조사 대상 전체 기관 ID iterable.
    Returns:
        state와 짧은 한국어 message. 기존 수기·이월 자료의 유무는 추정하지 않는다.
    """
    eligible = set(eligible_ids)
    if not eligible:
        return {"state": "not_started", "message": "학교 목록을 먼저 불러와 주세요."}
    selected_seen = False
    full_job = None
    for job in conn.execute(
            "SELECT * FROM jobs WHERE kind='schedule_scan' AND season_id=? "
            "ORDER BY created_at DESC,rowid DESC", (season_id,)):
        purpose, request = _purpose(job)
        if purpose != "date_count":
            continue
        if request.get("selection", {}).get("scope") == "all":
            full_job = job
            break
        selected_seen = True
    if full_job is None:
        return ({"state": "partial", "message": "일부 학교의 조사 기록만 있어요. 전체 학교를 조사해 주세요."}
                if selected_seen else
                {"state": "not_started", "message": "이 기간의 전체 학교 조사를 아직 시작하지 않았어요."})
    items = conn.execute("SELECT * FROM job_items WHERE job_id=?", (full_job["id"],)).fetchall()
    targets = {item["institution_id"] for item in items}
    if targets != eligible or len(items) != len(eligible) or full_job["target_count"] != len(eligible):
        return {"state": "partial", "message": "조사 대상이 현재 학교 목록과 달라요. 전체 학교를 다시 조사해 주세요."}
    status = full_job["status"]
    if status in {"queued", "running", "pausing", "cancelling"}:
        messages = {"queued": "전체 학교 조사를 준비하고 있어요.", "running": "전체 학교를 조사하고 있어요.",
                    "pausing": "전체 조사를 잠시 멈추고 있어요.", "cancelling": "전체 조사를 취소하고 있어요."}
        return {"state": "running", "message": messages[status]}
    if status == "paused":
        if full_job["paused_reason"] in {"job_budget", "configuration_required"}:
            return {"state": "paused", "message": "전체 조사가 멈췄어요. '이 시즌 다시 조사'로 계속 확인해 주세요."}
        return {"state": "paused", "message": "전체 조사를 잠시 멈췄어요. 이어서 조사할 수 있어요."}
    if status == "cancelled":
        return {"state": "cancelled", "message": "전체 조사를 취소했어요. 저장한 정보는 그대로 남아 있어요."}
    if status == "failed" or all(item["status"] == "failed" for item in items):
        return {"state": "failed", "message": "전체 조사를 마치지 못했어요. 다시 조사해 주세요."}
    if any(item["status"] != "done" or _item_feedback(item) in {"source_unavailable", "service_error"}
           or set(_read_json(item["reason_codes_json"], [])) & _INCOMPLETE for item in items):
        return {"state": "partial", "message": "일부 학교의 조사를 마치지 못했어요. 빈칸은 아직 미확인이에요."}
    return {"state": "completed", "message": "전체 학교 조사를 마쳤어요. 모든 일정이 확인된 것은 아니에요."}
