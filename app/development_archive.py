"""확인된 개발 검증 자료만 복구 가능한 보관본으로 분리한다."""

import json
import os
import sqlite3
from uuid import uuid4

from .domain import ApiError, dumps, fingerprint, utcnow

ARCHIVE_KEY = "development_archive_gist_20261020_v1"
SEASON_ID = "ce4a7893-04e5-46de-b5df-22d781ded241"
EVENT_ID = "ad0298ce-5398-419f-bdba-1f6e7e2eb5f6"
INSTITUTION_ID = "a447a350-c421-4806-b449-8c249e47abf1"
NOTICE = "https://www.gist.ac.kr/kr/html/sub05/050209.html?mode=V&no=220361&GotoPage=1"
OBSERVATIONS = {
    "cb4649e1-cb65-4f53-96e8-ad26900d6a66": "9b5b6844-be74-4c63-b69b-02ed5dde04ef",
    "6fdc4a73-d221-4511-af95-3a784e01d3ac": "c2664199-0378-43fd-a23f-95d481ca96f3",
}


def _rows(conn, sql, args=()):
    """보관할 원본 열과 값을 변환 없이 읽는다."""
    return [dict(row) for row in conn.execute(sql, args)]


def _source_rows(conn):
    """고정 UUID와 원문 관계가 모두 일치하는 보관 대상을 수집한다.

    Args:
        conn: 기존 저장소의 읽기 연결.
    Returns:
        원본 행 묶음. 알려진 검증 기록이 없으면 None.
    Raises:
        ApiError: 일부 ID나 원문 관계가 달라 안전하게 분리할 수 없을 때.
    """
    if conn.execute("SELECT 1 FROM app_meta WHERE key=?", (ARCHIVE_KEY,)).fetchone():
        return None
    found = _rows(conn, "SELECT * FROM observations WHERE id IN (?,?)", tuple(OBSERVATIONS))
    season = _rows(conn, "SELECT * FROM seasons WHERE id=?", (SEASON_ID,))
    event = _rows(conn, "SELECT * FROM events WHERE id=?", (EVENT_ID,))
    if not found and not season and not event:
        return None
    documents = _rows(conn, "SELECT * FROM documents WHERE id IN (?,?)", tuple(OBSERVATIONS.values()))
    valid = (len(found) == 2 and len(season) == 1 and len(event) == 1 and len(documents) == 2
             and season[0]["export_key"] == "2025-winter-source-check"
             and (season[0]["search_start"], season[0]["search_end"]) == ("2025-12-01", "2026-03-31")
             and event[0]["institution_id"] == INSTITUTION_ID and event[0]["season_id"] == SEASON_ID)
    for row in found:
        candidate = json.loads(row["candidate_json"])
        valid = valid and (row["event_id"] == EVENT_ID and row["season_id"] == SEASON_ID
                           and row["institution_id"] == INSTITUTION_ID
                           and row["document_id"] == OBSERVATIONS[row["id"]]
                           and candidate.get("academic_year") == 2025
                           and candidate.get("event_date", {}).get("date") == "2026-02-20"
                           and candidate.get("start_time") == "14:00"
                           and row["created_at"].startswith("2026-10-20T10:"))
    valid = valid and all(row["institution_id"] == INSTITUTION_ID and row["source_key"] == NOTICE
                          and row["display_url"] == NOTICE for row in documents)
    if not valid:
        raise ApiError("migration_required", "개발 검증 기록의 식별 정보가 달라 분리를 중단했습니다. 기존 데이터는 보존했습니다.", 409)
    doc_ids = tuple(OBSERVATIONS.values())
    return {
        "seasons": season, "events": event, "observations": found, "documents": documents,
        "institutions": _rows(conn, "SELECT * FROM institutions WHERE id=?", (INSTITUTION_ID,)),
        "assessments": _rows(conn, "SELECT * FROM assessments WHERE event_id=?", (EVENT_ID,)),
        "manual_overrides": _rows(conn, "SELECT * FROM manual_overrides WHERE event_id=?", (EVENT_ID,)),
        "event_seasons": _rows(conn, "SELECT * FROM event_seasons WHERE event_id=?", (EVENT_ID,)),
        "count_observations": _rows(conn, "SELECT * FROM count_observations WHERE document_id IN (?,?)", doc_ids),
        "collection_routes": _rows(conn, "SELECT * FROM collection_routes WHERE document_id IN (?,?)", doc_ids),
        "route_attempts": _rows(conn, "SELECT * FROM route_attempts WHERE route_id IN "
                               "(SELECT id FROM collection_routes WHERE document_id IN (?,?))", doc_ids),
        "exports": _rows(conn, "SELECT * FROM exports WHERE season_id=?", (SEASON_ID,)),
        "audit_log": _rows(conn, "SELECT * FROM audit_log WHERE entity_id IN (?,?,?,?,?,?) ORDER BY seq",
                           (SEASON_ID, EVENT_ID, *OBSERVATIONS, *doc_ids)),
        "request_keys": _rows(conn, "SELECT * FROM request_keys WHERE json_extract(response_json,'$.id') IN (?,?) "
                             "OR json_extract(response_json,'$.season_id')=?", (SEASON_ID, EVENT_ID, SEASON_ID)),
    }


def _write_archive(path, payload):
    """원본 묶음을 덮어쓰기 없이 저장하고 디스크의 내용을 다시 검사한다.

    Args:
        path: 새 보관 JSON 경로.
        payload: 복구 정보와 원본 행.
    Raises:
        OSError: 저장·동기화·재읽기에 실패할 때. 운영 DB는 아직 변경하지 않는다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, indent=2))
        stream.flush()
        os.fsync(stream.fileno())
    if json.loads(path.read_text(encoding="utf-8")) != payload:
        raise OSError("개발 검증 보관본의 재읽기 검사가 실패했습니다.")


def _has_reference(conn, table, column, identity):
    """모듈 내부에서 정한 외래키에 일반 기록이 남아 있는지 검사한다."""
    return conn.execute(f"SELECT 1 FROM {table} WHERE {column}=? LIMIT 1", (identity,)).fetchone() is not None


def separate_development_artifacts(store):
    """검증 기록을 백업·선별 보관한 뒤 운영 근거와 분리한다.

    Args:
        store: 이미 기간 구조를 설치한 저장소. 실제 수집은 실행하지 않는다.
    Returns:
        이번 분리 영수증 또는 분리할 기록이 없을 때 None.
    Raises:
        ApiError: 식별 정보 변경, 백업 또는 외래키 검사 실패.
        OSError: 원본 보관 파일을 완전히 저장하지 못한 경우.
    """
    with store.transaction() as conn:
        original = _source_rows(conn)
    if original is None:
        return None
    suffix = uuid4().hex
    backup = store.backup(store.path.parent / "backups" / f"before-development-gist-{suffix}.sqlite3")
    with sqlite3.connect(f"file:{backup}?mode=ro", uri=True) as saved:
        saved.row_factory = sqlite3.Row
        if (saved.execute("PRAGMA quick_check").fetchone()[0] != "ok"
                or saved.execute("PRAGMA foreign_key_check").fetchone()
                or fingerprint(_source_rows(saved)) != fingerprint(original)):
            raise ApiError("migration_required", "개발 검증 분리 전 백업 검사를 통과하지 못했습니다.", 409)
    archive_path = store.path.parent / "archives" / f"gist-seeded-20261020-{suffix}.json"
    payload = {"version": 1, "reason": "개발 중 고정 URL로 실행한 과거 공지 검증",
               "created_at": utcnow(), "backup": backup, "source_rows_hash": fingerprint(original), "tables": original}
    _write_archive(archive_path, payload)
    with store.transaction(True) as conn:
        if fingerprint(_source_rows(conn)) != fingerprint(original):
            raise ApiError("migration_required", "보관 중 기록이 변경되어 운영 자료 분리를 취소했습니다.", 409)
        doc_ids = tuple(OBSERVATIONS.values())
        conn.execute("DELETE FROM observations WHERE id IN (?,?)", tuple(OBSERVATIONS))
        # 같은 본문을 일반 조사에서 다시 읽으면 문서 UUID를 재사용한다.
        # 독립 관측이나 사용자 확인에 연결된 문서의 파생 기록은 분리하지 않는다.
        shared_documents = {identity for identity in doc_ids if
            _has_reference(conn, "observations", "document_id", identity)
            or _has_reference(conn, "manual_overrides", "source_document_id", identity)
            or conn.execute("SELECT 1 FROM manual_overrides WHERE source_url=?",
                            (NOTICE,)).fetchone()}
        for identity in set(doc_ids) - shared_documents:
            conn.execute("DELETE FROM count_observations WHERE document_id=?", (identity,))
            conn.execute("DELETE FROM route_attempts WHERE route_id IN "
                         "(SELECT id FROM collection_routes WHERE document_id=?)", (identity,))
            conn.execute("DELETE FROM collection_routes WHERE document_id=?", (identity,))
        retained_event = any(_has_reference(conn, table, column, EVENT_ID) for table, column in (
            ("observations", "event_id"), ("manual_overrides", "event_id"), ("count_observations", "event_id"),
            ("event_aliases", "old_id"), ("event_aliases", "current_id"), ("investigation_traces", "target_event_id")))
        retained_event = retained_event or conn.execute(
            "SELECT 1 FROM natural_records WHERE json_extract(state_json,'$.event_id')=? UNION ALL "
            "SELECT 1 FROM jobs WHERE json_extract(scope_snapshot_json,'$.target_event_id')=?",
            (EVENT_ID, EVENT_ID)).fetchone() is not None
        if not retained_event:
            conn.execute("DELETE FROM assessments WHERE event_id=?", (EVENT_ID,))
            conn.execute("DELETE FROM event_seasons WHERE event_id=?", (EVENT_ID,))
            conn.execute("DELETE FROM events WHERE id=?", (EVENT_ID,))
        else:
            # 일반 관측·수기가 섞이면 UUID를 유지하고 새 정책으로 다시 평가한다.
            conn.execute("INSERT OR REPLACE INTO app_meta VALUES('development_archive_reassess',?)", (dumps([EVENT_ID]),))
        removed_documents = []
        for identity in doc_ids:
            if identity not in shared_documents and not any(_has_reference(conn, table, column, identity) for table, column in (
                    ("observations", "document_id"), ("manual_overrides", "source_document_id"),
                    ("count_observations", "document_id"), ("collection_routes", "document_id"))):
                conn.execute("DELETE FROM documents WHERE id=?", (identity,))
                removed_documents.append(identity)
        retained_season = any(_has_reference(conn, table, "season_id", SEASON_ID) for table in (
            "events", "observations", "event_seasons", "jobs", "natural_records", "count_observations", "investigation_traces"))
        if not retained_season:
            conn.execute("DELETE FROM exports WHERE season_id=?", (SEASON_ID,))
            conn.execute("DELETE FROM seasons WHERE id=?", (SEASON_ID,))
        removed_ids = ([] if retained_event else [EVENT_ID]) + ([] if retained_season else [SEASON_ID])
        for identity in removed_ids:
            conn.execute("DELETE FROM request_keys WHERE json_extract(response_json,'$.id')=? "
                         "OR json_extract(response_json,'$.season_id')=?", (identity, identity))
        receipt = {"archive": str(archive_path.resolve()), "backup": backup, "rows_hash": fingerprint(original),
                   "removed_observation_ids": list(OBSERVATIONS), "removed_document_ids": removed_documents,
                   "removed_event_ids": [] if retained_event else [EVENT_ID],
                   "removed_season_ids": [] if retained_season else [SEASON_ID], "completed_at": utcnow()}
        conn.execute("INSERT INTO app_meta VALUES(?,?)", (ARCHIVE_KEY, dumps(receipt)))
        from .storage import _audit
        _audit(conn, "development_archive", ARCHIVE_KEY, "separate_development_evidence",
               after=receipt, actor="system", summary="고정 URL 개발 검증 자료를 원본 보관 후 운영 데이터와 분리")
        store.touch(conn)
        if conn.execute("PRAGMA foreign_key_check").fetchone() or conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ApiError("migration_required", "개발 검증 분리의 무결성 검사에 실패해 운영 변경을 취소했습니다.", 409)
    return receipt


def reassess_retained_events(service):
    """분리 대상에 섞인 일반 근거와 수기만 다시 평가한다.

    Args:
        service: 실제 운영 규칙을 설정한 서비스.
    """
    with service.store.transaction(True) as conn:
        row = conn.execute("SELECT value FROM app_meta WHERE key='development_archive_reassess'").fetchone()
        if not row:
            return
        for identity in json.loads(row[0]):
            service._reassess(conn, identity)
            service.link_event_periods(conn, identity)
        conn.execute("DELETE FROM app_meta WHERE key='development_archive_reassess'")
