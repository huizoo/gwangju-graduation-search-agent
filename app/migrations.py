"""기존 근거·수기를 보존하는 작은 저장 구조 확장과 규칙 재평가."""

import json
from collections import defaultdict
from pathlib import Path

from .domain import ApiError, dumps, fingerprint, utcnow
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
CREATE TABLE IF NOT EXISTS count_observations(
 id TEXT PRIMARY KEY,institution_id TEXT NOT NULL REFERENCES institutions(id),
 season_id TEXT NOT NULL REFERENCES seasons(id),event_id TEXT REFERENCES events(id),
 document_id TEXT NOT NULL REFERENCES documents(id),claim_json TEXT NOT NULL CHECK(json_valid(claim_json)),
 proof_json TEXT NOT NULL CHECK(json_valid(proof_json)),fingerprint TEXT NOT NULL UNIQUE,created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS collection_routes(
 id TEXT PRIMARY KEY,institution_id TEXT NOT NULL REFERENCES institutions(id),
 url TEXT NOT NULL,purpose TEXT NOT NULL,reading_method TEXT NOT NULL,
 verified INTEGER NOT NULL DEFAULT 0,last_success_at TEXT,last_attempt_at TEXT,
 failure_reason TEXT,document_id TEXT REFERENCES documents(id),
 UNIQUE(institution_id,url,purpose)
);
CREATE TABLE IF NOT EXISTS route_attempts(
 id TEXT PRIMARY KEY,route_id TEXT REFERENCES collection_routes(id),
 institution_id TEXT NOT NULL REFERENCES institutions(id),attempt_json TEXT NOT NULL CHECK(json_valid(attempt_json)),
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS investigation_traces(
 id TEXT PRIMARY KEY,institution_id TEXT NOT NULL REFERENCES institutions(id),
 season_id TEXT NOT NULL REFERENCES seasons(id),purpose TEXT NOT NULL,target_event_id TEXT REFERENCES events(id),
 trace_json TEXT NOT NULL CHECK(json_valid(trace_json)),created_at TEXT NOT NULL
);
INSERT INTO app_meta(key,value) VALUES('experience_schema','2')
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
    if marker and marker[0] == "2":
        install_periods(store, existing=existing)
        return
    if marker and marker[0] != "1":
        raise ApiError("migration_required", "지원하지 않는 기록 확장 버전입니다. 기존 DB를 보존했습니다.", 409)
    if existing:
        store.backup(store.path.parent / "backups" / f"before-experience-2-{fingerprint(utcnow())[:12]}.sqlite3")
    with store.transaction(True) as conn:
        # executescript는 암묵적으로 commit하므로 각 문장을 같은 쓰기 거래 안에서 실행한다.
        for statement in EXTENSION_SQL.split(";"):
            if statement.strip():
                conn.execute(statement)
    install_periods(store, existing=existing)


def install_periods(store, *, existing: bool):
    """기존 UUID·외래키를 보존하고 기간과 행사 학년도 저장을 분리한다.

    Args:
        store: 열린 거래가 없는 저장소.
        existing: 변경 전 백업이 필요한 기존 파일인지 여부.
    Raises:
        ApiError: 지원하지 않는 확장 버전 또는 이관 무결성 오류.
    """
    import re
    from .identity import ceremony_term
    with store.transaction() as conn:
        marker = conn.execute("SELECT value FROM app_meta WHERE key='period_schema'").fetchone()
    if marker:
        if marker[0] != "1":
            raise ApiError("migration_required", "지원하지 않는 기간 저장 구조입니다.", 409)
        return
    if existing:
        store.backup(store.path.parent / "backups" / f"before-period-1-{fingerprint(utcnow())[:12]}.sqlite3")
    conn = store.connect()
    try:
        # SQLite는 NOT NULL만 제거하는 ALTER를 지원하지 않는다. 부모 UUID를
        # 그대로 복사해 교체하고 commit 전에 모든 자식 참조를 다시 검사한다.
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")
        columns = {r["name"]: dict(r) for r in conn.execute("PRAGMA table_info(seasons)")}
        if columns["academic_year"]["notnull"]:
            conn.execute("""CREATE TABLE seasons_period(
                id TEXT PRIMARY KEY,label TEXT NOT NULL,
                academic_year INTEGER CHECK(academic_year BETWEEN 2000 AND 2100),
                search_start TEXT NOT NULL,search_end TEXT NOT NULL CHECK(search_end>=search_start),
                export_key TEXT UNIQUE NOT NULL,timezone TEXT NOT NULL CHECK(timezone='Asia/Seoul'),
                region_profile TEXT NOT NULL CHECK(region_profile='gwangju_5_gu'),
                revision INTEGER NOT NULL DEFAULT 1)""")
            conn.execute("INSERT INTO seasons_period SELECT * FROM seasons")
            conn.execute("DROP TABLE seasons")
            conn.execute("ALTER TABLE seasons_period RENAME TO seasons")
        event_columns = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
        if "academic_year" not in event_columns:
            conn.execute("ALTER TABLE events ADD COLUMN academic_year INTEGER")
        if "ceremony_term" not in event_columns:
            conn.execute("ALTER TABLE events ADD COLUMN ceremony_term TEXT")
        conn.execute("""CREATE TABLE event_seasons(
            season_id TEXT NOT NULL REFERENCES seasons(id),event_id TEXT NOT NULL REFERENCES events(id),
            PRIMARY KEY(season_id,event_id))""")
        conn.execute("INSERT INTO event_seasons SELECT season_id,id FROM events")
        for row in conn.execute("SELECT e.*,i.type FROM events e JOIN institutions i ON i.id=e.institution_id").fetchall():
            years = {int(y) for y in re.findall(r"(20\d{2})\s*학년도", row["title"])}
            for observation in conn.execute(
                "SELECT candidate_json FROM observations WHERE event_id=? AND disposition='accepted'", (row["id"],)):
                candidate = json.loads(observation[0])
                if candidate.get("academic_year") is not None:
                    years.add(candidate["academic_year"])
            conn.execute("UPDATE events SET academic_year=?,ceremony_term=? WHERE id=?",
                         (next(iter(years)) if len(years) == 1 else None,
                          ceremony_term(dict(row)), row["id"]))
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            raise ApiError("migration_required", "기간 이관의 참조 검사를 통과하지 못해 변경을 취소했습니다.", 409)
        conn.execute("INSERT INTO app_meta VALUES('period_schema','1')")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.close()


def reconcile_rules(service):
    """저장 원문을 새 규칙으로 재평가하고 명백한 기존 중복을 연결한다.

    Args:
        service: 현재 허용 규칙이 설정된 서비스.
    """
    from .storage import _audit
    repair_null_homepages(service)
    rules = service._enabled_rules()
    if not rules:
        return
    marker = fingerprint({"rules": rules, "identity": 4})
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
                conn.execute("UPDATE count_observations SET event_id=? WHERE event_id=?", (current["id"], old["id"]))
                conn.execute("INSERT OR IGNORE INTO event_seasons SELECT season_id,? FROM event_seasons WHERE event_id=?",
                             (current["id"], old["id"]))
                conn.execute("INSERT INTO event_aliases VALUES(?,?,?)", (old["id"], current["id"], utcnow()))
                _audit(conn, "event", current["id"], "link_previous_event", before=old, after={"current_id": current["id"]},
                       actor="system", summary="같은 기관·과정·캠퍼스 행사의 이전 기록 연결")
        for row in conn.execute("SELECT id FROM events WHERE id NOT IN(SELECT old_id FROM event_aliases)").fetchall():
            service._reassess(conn, row[0])
            service._apply_source_blockers(conn, row[0])
        conn.execute("INSERT INTO app_meta VALUES('assessed_rules',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (marker,))
        if has_events:
            service.store.touch(conn)


def repair_null_homepages(service):
    """과거 null 문자열 변환 오류만 원본 이력과 함께 정정한다.

    Args:
        service: 기존 공식 명부·프로필을 가진 서비스.
    """
    from urllib.parse import urlsplit
    from .storage import _audit
    def placeholder(value):
        return isinstance(value, str) and urlsplit(value).hostname in {"none", "null", "nan"}
    with service.store.transaction() as conn:
        if conn.execute("SELECT 1 FROM app_meta WHERE key='null_homepage_repair'").fetchone():
            return
        affected = [dict(row) for row in conn.execute("SELECT id,homepage_url,official_channels_json FROM institutions")
                    if placeholder(row["homepage_url"])]
    if affected:
        service.store.backup(service.store.path.parent/"backups"/f"before-url-repair-{fingerprint(utcnow())[:12]}.sqlite3")
    with service.store.transaction(True) as conn:
        for row in affected:
            channels = [c for c in json.loads(row["official_channels_json"]) if not placeholder(c["url"])]
            conn.execute("UPDATE institutions SET homepage_url=NULL,official_channels_json=?,revision=revision+1,updated_at=? WHERE id=?",
                         (dumps(channels),utcnow(),row["id"]))
            _audit(conn,"institution",row["id"],"repair_null_homepage",before=row,
                   after={"homepage_url":None,"official_channels":channels},actor="system",
                   summary="NEIS null을 주소 문자열로 변환했던 오류 정정. 원본 명부와 사용자 프로필은 보존")
        conn.execute("INSERT INTO app_meta VALUES('null_homepage_repair','1')")
        if affected:
            service.store.touch(conn)
