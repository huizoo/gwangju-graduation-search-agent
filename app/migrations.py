"""기존 근거·수기를 보존하는 작은 저장 구조 확장과 규칙 재평가."""

import json
from collections import defaultdict
from pathlib import Path

from .domain import dumps, fingerprint, utcnow
from .identity import scope

EXTENSION_SQL = """
CREATE TABLE IF NOT EXISTS natural_records(
 id TEXT PRIMARY KEY,season_id TEXT NOT NULL REFERENCES seasons(id),
 revision INTEGER NOT NULL DEFAULT 1,turns_json TEXT NOT NULL CHECK(json_valid(turns_json)),
 state_json TEXT NOT NULL CHECK(json_valid(state_json)),created_at TEXT NOT NULL,updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_aliases(
 old_id TEXT PRIMARY KEY REFERENCES events(id),current_id TEXT NOT NULL REFERENCES events(id),
 created_at TEXT NOT NULL, CHECK(old_id<>current_id)
);
INSERT INTO app_meta(key,value) VALUES('experience_schema','1')
 ON CONFLICT(key) DO UPDATE SET value=excluded.value;
"""


def install_extensions(store, *, existing: bool):
    """기존 파일은 백업한 뒤 추가 테이블만 원자적으로 설치한다.

    Args:
        store: 열린 트랜잭션이 없는 저장소.
        existing: 사용자의 기존 DB인지 여부.
    """
    with store.transaction() as conn:
        marker = conn.execute("SELECT value FROM app_meta WHERE key='experience_schema'").fetchone()
    if marker and marker[0] == "1":
        return
    if existing:
        store.backup(store.path.parent / "backups" / f"before-experience-1-{fingerprint(utcnow())[:12]}.sqlite3")
    with store.transaction(True) as conn:
        # executescript는 암묵적으로 commit하므로 각 문장을 같은 쓰기 거래 안에서 실행한다.
        for statement in EXTENSION_SQL.split(";"):
            if statement.strip():
                conn.execute(statement)


def reconcile_rules(service):
    """저장 원문을 새 규칙으로 재평가하고 명백한 기존 중복을 연결한다.

    Args:
        service: 현재 허용 규칙이 설정된 서비스.
    """
    from .storage import _audit
    rules = service._enabled_rules()
    if not rules:
        return
    marker = fingerprint({"rules": rules, "identity": 1})
    with service.store.transaction() as conn:
        previous = conn.execute("SELECT value FROM app_meta WHERE key='assessed_rules'").fetchone()
        has_events = conn.execute("SELECT 1 FROM events LIMIT 1").fetchone()
    if previous and previous[0] == marker:
        return
    if has_events:
        service.store.backup(service.store.path.parent / "backups" / f"before-rules-{fingerprint(utcnow())[:12]}.sqlite3")
    with service.store.transaction(True) as conn:
        groups = defaultdict(list)
        for row in conn.execute("SELECT * FROM events WHERE id NOT IN(SELECT old_id FROM event_aliases)"):
            item = dict(row)
            inst = service._institution(conn, item["institution_id"])
            groups[(item["institution_id"], item["season_id"], scope(item, inst))].append(item)
        for group in groups.values():
            if len(group) < 2:
                continue
            fields = [row[0] for item in group for row in conn.execute(
                "SELECT field FROM manual_overrides WHERE event_id=? AND active=1", (item["id"],))]
            # 보호된 수기끼리 충돌하면 어느 쪽도 자동으로 덮어쓰지 않는다.
            if len(fields) != len(set(fields)):
                continue
            group.sort(key=lambda e: (e["audience"] in (None, "", "졸업 대상 학생"), e["created_at"], e["id"]))
            current, *old_events = group
            for old in old_events:
                for row in conn.execute("SELECT * FROM observations WHERE event_id=?", (old["id"],)).fetchall():
                    candidate = json.loads(row["candidate_json"])
                    candidate["event_key_hint"] = current["event_key"]
                    # 관측 UUID와 원문은 보존하고 연결 변경의 전후를 감사 이력에 남긴다.
                    digest = fingerprint({"migrated_observation": row["id"], "event_id": current["id"], "candidate": candidate})
                    conn.execute("UPDATE observations SET event_id=?,candidate_json=?,fingerprint=? WHERE id=?",
                                 (current["id"], dumps(candidate), digest, row["id"]))
                conn.execute("UPDATE manual_overrides SET event_id=? WHERE event_id=?", (current["id"], old["id"]))
                conn.execute("INSERT INTO event_aliases VALUES(?,?,?)", (old["id"], current["id"], utcnow()))
                _audit(conn, "event", current["id"], "link_previous_event", before=old, after={"current_id": current["id"]},
                       actor="system", summary="같은 기관·과정·캠퍼스 행사의 이전 기록 연결")
        for row in conn.execute("SELECT id FROM events WHERE id NOT IN(SELECT old_id FROM event_aliases)").fetchall():
            service._reassess(conn, row[0])
            service._apply_source_blockers(conn, row[0])
        conn.execute("INSERT INTO app_meta VALUES('assessed_rules',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (marker,))
        if has_events:
            service.store.touch(conn)
