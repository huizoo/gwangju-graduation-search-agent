"""사람이 확인한 값의 보호와 충돌 재확인 이력을 저장한다."""
from __future__ import annotations

from datetime import datetime
from copy import deepcopy

from app.core.domain import ApiError, dumps, new_id, normalize_instant, utcnow, validate


def apply_operations(conn, event: dict, operations: list[dict], context: dict) -> None:
    """수기 작업을 기존 쓰기 트랜잭션 안에서 적용한다.

    Args:
        conn: 호출자가 시작한 SQLite 쓰기 연결.
        event: 현재 revision의 EventProjection.
        operations: set, clear, release, acknowledge 작업.
        context: 같은 snapshot의 서버 근거.
    Raises:
        ApiError: 중복 필드·미래 확인·잘못된 충돌 해결·근거 연결.
    """
    from app.records.storage import _audit
    names = [operation["field"] for operation in operations]
    if len(names) != len(set(names)):
        raise ApiError("duplicate_field", "한 요청에서 같은 항목을 두 번 변경할 수 없습니다.")
    now = utcnow()
    for operation in operations:
        data = validate("ManualOperation", operation)
        name, action = data["field"], data["action"]
        current = event["fields"][name]
        old = current["manual"]
        automatic = current["automatic"]
        if action == "acknowledge":
            if old:
                conn.execute("UPDATE manual_overrides SET acknowledged_fingerprint=? WHERE id=?", (automatic["fingerprint"], old["id"]))
            _audit(conn, "event", event["id"], action, old, {"fingerprint": automatic["fingerprint"]}, name, summary="알림 읽음: 게시 보류는 그대로 유지")
            continue
        if action == "release":
            conn.execute("UPDATE manual_overrides SET active=0 WHERE event_id=? AND field=? AND active=1", (event["id"], name))
            _audit(conn, "event", event["id"], action, old, None, name, summary=data["note"] or "사용자 지정 해제")
            continue
        confirmed = normalize_instant(data.get("confirmed_at"))
        if confirmed and datetime.fromisoformat(confirmed) > datetime.fromisoformat(now):
            raise ApiError("future_confirmation", "실제 확인 시각은 현재보다 미래일 수 없습니다.")
        resolves = data.get("resolves_fingerprint")
        if resolves is not None:
            detected = automatic.get("conflict_detected_at")
            if not (current["review_required"] or current["has_web_difference"]):
                raise ApiError("conflict_changed", "현재 해결할 충돌이 없습니다.", 409)
            if resolves != automatic.get("fingerprint"):
                raise ApiError("conflict_changed", "새로운 충돌이 있습니다. 근거를 다시 확인해 주세요.", 409)
            if not confirmed or not detected or datetime.fromisoformat(confirmed) < datetime.fromisoformat(normalize_instant(detected)):
                raise ApiError("confirmation_too_early", "충돌을 인지한 이후에 실제로 다시 확인한 시각이 필요합니다.")
            if data.get("basis") not in ("phone_confirmed", "document_confirmed") or not data.get("note", "").strip():
                raise ApiError("resolution_evidence_required", "충돌 해결에는 확인 방법·실제 시각·근거 메모가 필요합니다.")
        source_id = data.get("source_document_id")
        if source_id:
            document = context["documents"].get(source_id)
            if not document or document["institution_id"] != event["institution"]["id"] or (data.get("source_url") and data["source_url"] != document["display_url"]):
                raise ApiError("evidence_link_invalid", "확인 문서와 기관 또는 주소가 일치하지 않습니다.")
            if confirmed and datetime.fromisoformat(normalize_instant(document["retrieved_at"])) > datetime.fromisoformat(confirmed):
                raise ApiError("evidence_link_invalid", "확인한 시각보다 나중에 취득한 문서 버전을 연결할 수 없습니다.")
        if action == "set" and name == "venue" and not data["value"].strip():
            raise ApiError("validation_error", "장소는 공백만 저장할 수 없습니다. 비우기를 사용해 주세요.")
        manual = {"id": new_id(), "mode": action, "value": data.get("value") if action == "set" else None, "basis": data.get("basis", "user_recorded"), "bound_date": data.get("bound_date"), "confirmed_at": confirmed, "recorded_at": now, "note": data["note"], "acknowledged_fingerprint": None, "resolved_fingerprint": resolves, "source_url": data.get("source_url"), "source_document_id": source_id}
        if action == "set" and automatic["status"] == "official_confirmed" and (manual["value"] != automatic["value"] or (name in ("start_time", "venue") and manual["bound_date"] != automatic["bound_date"])) and not automatic.get("conflict_detected_at"):
            # 최초 수기 차이도 안정적인 감지시각이 있어야 다음 재확인을 검증할 수 있다.
            detected = deepcopy(automatic)
            detected.update(assessment_id=new_id(), conflict_detected_at=now)
            conn.execute("INSERT INTO assessments(id,event_id,field,status,automatic_json,created_at) VALUES(?,?,?,?,?,?)", (detected["assessment_id"], event["id"], name, detected["status"], dumps(detected), now))
        # 비활성화 뒤 새 행을 추가하므로 자동 조사와 수기 원본 모두 사라지지 않는다.
        conn.execute("UPDATE manual_overrides SET active=0 WHERE event_id=? AND field=? AND active=1", (event["id"], name))
        conn.execute("INSERT INTO manual_overrides(id,event_id,field,mode,value_json,basis,bound_date,confirmed_at,recorded_at,note,acknowledged_fingerprint,resolved_fingerprint,source_url,source_document_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (manual["id"], event["id"], name, manual["mode"], dumps(manual["value"]), manual["basis"], manual["bound_date"], confirmed, now, manual["note"], None, resolves, manual["source_url"], source_id))
        _audit(conn, "event", event["id"], action, old, manual, name, summary=data["note"] or "직접 입력")
