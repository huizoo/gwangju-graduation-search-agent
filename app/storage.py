"""SQLite 저장·읽기 snapshot·기관/시즌/행사 서비스를 제공한다."""
from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import contextmanager, closing
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from app.domain import ApiError, FIELDS, SCHEMA, dumps, fingerprint, new_id, normalize_instant, utcnow, validate
from app.publication import POLICY_VERSION, RULES, calculate_publication


class Store:
    """매 작업 독립 연결과 원자적 트랜잭션으로 로컬 이력을 보존한다."""

    def __init__(self, path: str | Path):
        """DB를 준비하고 지원하지 않는 이전 형식은 보존한 채 중단한다.

        Args:
            path: SQLite 파일 경로.
        Raises:
            ApiError: 기존 DB의 마이그레이션이 필요할 때.
        """
        self._active = ContextVar(f"store_{id(self)}", default=None)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables:
                conn.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
            else:
                version = conn.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone() if "app_meta" in tables else None
                if not version or version[0] != "4.0":
                    raise ApiError("migration_required", "기존 DB를 보존했습니다. 4.0 마이그레이션이 필요합니다.", 409)

    def connect(self) -> sqlite3.Connection:
        """필수 SQLite 보호 설정을 적용한 연결을 연다.

        Returns:
            Row 객체를 반환하는 DB 연결.
        """
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def transaction(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        """읽기 snapshot 또는 원자적 쓰기를 관리한다.

        Args:
            write: 쓰기 잠금을 먼저 잡아 revision 경합을 막을지 여부.
        Yields:
            한 트랜잭션에 속한 연결.
        """
        active = self._active.get()
        if active is not None:
            conn, outer_write = active
            if write and not outer_write:
                raise RuntimeError("읽기 snapshot 안에서 쓰기 승격은 허용하지 않습니다.")
            yield conn
            return
        conn = self.connect()
        token = self._active.set((conn, write))
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            self._active.reset(token)
            conn.close()

    def revision(self, conn: sqlite3.Connection | None = None) -> int:
        """콘텐츠 revision을 읽는다.

        Args:
            conn: 기존 snapshot 연결. 없으면 자체 읽기 연결을 사용한다.
        Returns:
            현재 콘텐츠 revision.
        """
        if conn is None:
            with self.transaction() as current:
                return self.revision(current)
        return int(conn.execute("SELECT value FROM app_meta WHERE key='content_revision'").fetchone()[0])

    def touch(self, conn: sqlite3.Connection, season_id: str | None = None) -> int:
        """변경과 같은 트랜잭션에서 revision 및 export의 stale 상태를 기록한다.

        Args:
            conn: 쓰기 트랜잭션.
            season_id: 영향을 받은 시즌. null은 모든 시즌이다.
        Returns:
            증가한 콘텐츠 revision.
        """
        conn.execute("UPDATE app_meta SET value=CAST(value AS INTEGER)+1 WHERE key='content_revision'")
        revision = self.revision(conn)
        seasons = conn.execute("SELECT id FROM seasons" + (" WHERE id=?" if season_id else ""), (season_id,) if season_id else ()).fetchall()
        for season in seasons:
            conn.execute("INSERT INTO exports(season_id,content_revision,status) VALUES(?,?,'pending') ON CONFLICT(season_id) DO UPDATE SET content_revision=excluded.content_revision,status='pending'", (season[0], revision))
        return revision

    def backup(self, path: str | Path) -> str:
        """WAL을 포함한 일관된 SQLite 백업을 만든다.

        Args:
            path: 새 백업 파일 경로.
        Returns:
            생성한 절대 경로.
        Raises:
            ApiError: 기존 파일을 덮어쓰려는 경우.
        """
        target = Path(path)
        if target.exists():
            raise ApiError("backup_exists", "기존 백업 파일을 덮어쓰지 않습니다.", 409)
        target.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as source, closing(sqlite3.connect(target)) as dest:
            source.backup(dest)
        return str(target.resolve())


def _one(conn, table, identity):
    """서버 고정 테이블에서 필수 행을 읽는다.

    Args:
        conn: DB 연결.
        table: 내부에서 고정한 테이블 이름.
        identity: UUID.
    Returns:
        행 사전.
    Raises:
        ApiError: 대상이 없을 때.
    """
    if table not in {"institutions", "events", "seasons", "documents", "observations", "import_previews"}:
        raise ValueError("unsupported table")
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (identity,)).fetchone()
    if row is None:
        raise ApiError("not_found", "대상 기록을 찾을 수 없습니다.", 404)
    return dict(row)


def _expect(row, expected):
    """오래된 화면의 덮어쓰기를 차단한다.

    Args:
        row: 현재 저장 행.
        expected: 클라이언트가 읽은 revision.
    Raises:
        ApiError: revision 불일치.
    """
    if row["revision"] != expected:
        raise ApiError("revision_conflict", "다른 변경이 있습니다. 새로 조회한 뒤 다시 저장해 주세요.", 409, {"current_revision": row["revision"]})


def _audit(conn, entity_type, entity_id, action, before=None, after=None, field=None, actor="user", summary=""):
    """기존 값을 지우지 않고 변경 이유와 전후 값을 저장한다.

    Args:
        conn: 쓰기 연결.
        entity_type: 기록 종류.
        entity_id: 대상 UUID.
        action: 변경 종류.
        before: 변경 전 값.
        after: 변경 후 값.
        field: 필드 이름.
        actor: user 또는 system.
        summary: 간결한 변경 설명.
    """
    conn.execute("INSERT INTO audit_log(id,entity_type,entity_id,field,action,actor,recorded_at,before_json,after_json,summary) VALUES(?,?,?,?,?,?,?,?,?,?)", (new_id(), entity_type, entity_id, field, action, actor, utcnow(), dumps(before) if before is not None else None, dumps(after) if after is not None else None, summary or action))


def empty_automatic() -> dict:
    """근거 없는 값을 명시적인 unchecked 상태로 구성한다.

    Returns:
        필드별 automatic 기본 객체.
    """
    return {"status": "unchecked", "value": None, "bound_date": None, "evidence_ids": [], "assessment_id": None, "fingerprint": None, "checked_at": None, "reason_codes": [], "conflict_detected_at": None, "value_check": {"status": "unchecked", "rule_id": None, "rule_version": None, "checks": {key: "unknown" for key in ("institution", "content", "season", "event_identity", "literal_presence", "normalized_value", "time_role")}, "evidence_ids": [], "document_ids": [], "reason_codes": [], "checked_at": None, "policy_version": POLICY_VERSION, "field": None, "document_hashes": {}}}


class Service:
    """API·HTML·집계가 같은 게시 판정을 사용하도록 조립한다."""

    def __init__(self, store: Store, settings: Any = None):
        """저장소와 비밀 없는 정책 설정을 연결한다.

        Args:
            store: 로컬 SQLite 저장소.
            settings: 설정 객체 또는 None.
        """
        self.store, self.settings = store, settings
        self._configure_rules()

    def _configure_rules(self):
        """구현이 존재하는 규칙만 서버 레지스트리에 연결한다."""
        try:
            from app.rules import REGISTERED_RULES, verify
        except ImportError:
            return
        for key, version in REGISTERED_RULES.items():
            RULES.register(key, version, verify)

    def _enabled_rules(self):
        """명시적으로 허용한 규칙 ID와 버전만 반환한다.

        Returns:
            허용된 규칙 버전 사전.
        """
        data = getattr(self.settings, "data", self.settings or {})
        def search(value):
            if isinstance(value, dict):
                if "auto_rule_ids" in value:
                    return value["auto_rule_ids"]
                for child in value.values():
                    found = search(child)
                    if found is not None:
                        return found
            return None
        enabled = search(data) or []
        # 실제 설정은 ID@버전으로 고정한다. 새 코드 버전을 같은 ID만으로
        # 자동 승인하지 않으며 dict 주입은 명시적인 로컬 시험 경계다.
        allow_unversioned = self.settings is None or isinstance(self.settings, dict)
        return {key: version for key, version in RULES.versions().items() if f"{key}@{version}" in enabled or (allow_unversioned and key in enabled)}

    def _season(self, conn, identity):
        """시즌 행을 계약 형식으로 읽는다.

        Args:
            conn: snapshot 연결.
            identity: 시즌 UUID.
        Returns:
            Season.
        """
        return validate("Season", _one(conn, "seasons", identity))

    def list_seasons(self):
        """저장 시즌을 반환한다.

        Returns:
            SeasonPage.
        """
        with self.store.transaction() as conn:
            items = [dict(row) for row in conn.execute("SELECT * FROM seasons ORDER BY search_start DESC,id")]
            return validate("SeasonPage", {"items": items, "total": len(items), "next_cursor": None})

    def create_season(self, payload):
        """새 시즌을 저장한다.

        Args:
            payload: SeasonCreate.
        Returns:
            Season.
        """
        data = validate("SeasonCreate", payload)
        if data["search_end"] < data["search_start"]:
            raise ApiError("invalid_date_range", "종료일은 시작일보다 빠를 수 없습니다.")
        with self.store.transaction(True) as conn:
            if conn.execute("SELECT 1 FROM seasons WHERE export_key=?", (data["export_key"],)).fetchone():
                raise ApiError("export_key_exists", "일정표 저장 이름이 이미 사용 중입니다.", 409)
            identity = new_id()
            conn.execute("INSERT INTO seasons(id,label,academic_year,search_start,search_end,export_key,timezone,region_profile) VALUES(?,?,?,?,?,?,?,?)", (identity, *(data[key] for key in ("label", "academic_year", "search_start", "search_end", "export_key", "timezone", "region_profile"))))
            self.store.touch(conn, identity)
            _audit(conn, "season", identity, "create", after=data)
            return self._season(conn, identity)

    def patch_season(self, identity, payload):
        """기존 시즌 범위를 변경하고 게시 결과를 다음 조회에서 재계산한다.

        Args:
            identity: 시즌 UUID.
            payload: SeasonPatch.
        Returns:
            변경한 계약 객체.
        """
        data = validate("SeasonPatch", payload)
        if data["search_end"] < data["search_start"]:
            raise ApiError("invalid_date_range", "종료일은 시작일보다 빠를 수 없습니다.")
        with self.store.transaction(True) as conn:
            old = _one(conn, "seasons", identity)
            _expect(old, data["expected_revision"])
            conn.execute("UPDATE seasons SET label=?,search_start=?,search_end=?,revision=revision+1 WHERE id=?", (data["label"], data["search_start"], data["search_end"], identity))
            revision = self.store.touch(conn, identity)
            _audit(conn, "season", identity, "update", old, data)
            return self._season(conn, identity)

    def _institution(self, conn, identity):
        """공식 원본과 보호된 사용자 수정값을 합성한다.

        Args:
            conn: snapshot 연결.
            identity: 기관 UUID.
        Returns:
            Institution.
        """
        row = _one(conn, "institutions", identity)
        names = ("name", "address", "phone", "homepage_url", "district")
        original = {name: row[name] for name in names}
        overrides = json.loads(row.pop("profile_overrides_json"))
        row["official_channels"] = json.loads(row.pop("official_channels_json"))
        row["profile_original"], row["profile_overrides"] = original, overrides
        for name, value in overrides.items():
            row[name] = value["value"]
        row.pop("created_at")
        row.pop("updated_at")
        return validate("Institution", row)

    def get_institution(self, identity):
        """기관을 조회한다.

        Args:
            identity: 기관 UUID.
        Returns:
            Institution.
        """
        with self.store.transaction() as conn:
            return self._institution(conn, identity)

    def list_institutions(self, **filters):
        """등록 기관 목록을 필터링한다.

        Args:
            filters: type, district, q 필터.
        Returns:
            InstitutionPage.
        """
        with self.store.transaction() as conn:
            items = [self._institution(conn, row[0]) for row in conn.execute("SELECT id FROM institutions ORDER BY name,id")]
            for name in ("type", "district"):
                if filters.get(name):
                    items = [item for item in items if item[name] == filters[name]]
            if filters.get("q"):
                items = [item for item in items if filters["q"].casefold() in (item["name"] + " " + item["address"]).casefold()]
            return {"items": items, "total": len(items), "next_cursor": None}

    def _insert_institution(self, conn, data, identity=None):
        """입력받은 기관을 미검증 운영 상태로 저장한다.

        Args:
            conn: 쓰기 연결.
            data: 기관 값.
            identity: 내부에서 미리 발급한 UUID.
        Returns:
            저장한 UUID.
        """
        identity = identity or new_id()
        now = utcnow()
        conn.execute("INSERT INTO institutions(id,name,type,district,address,campus_label,phone,homepage_url,operation_status,operation_effective_date,operation_evidence_url,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (identity, *(data.get(key) for key in ("name", "type", "district", "address", "campus_label", "phone", "homepage_url")), data.get("operation_status", "unknown"), data.get("operation_effective_date"), data.get("operation_evidence_url"), now, now))
        return identity

    def create_institution(self, payload):
        """직접 등록 기관을 저장하며 공식성은 자동 승인하지 않는다.

        Args:
            payload: InstitutionCreate.
        Returns:
            Institution.
        """
        data = validate("InstitutionCreate", payload)
        with self.store.transaction(True) as conn:
            identity = self._insert_institution(conn, data)
            self.store.touch(conn)
            _audit(conn, "institution", identity, "create", after=data)
            return self._institution(conn, identity)

    def patch_institution(self, identity, payload):
        """기관 수기 정보의 보호·해제를 기록한다.

        Args:
            identity: 기관 UUID.
            payload: InstitutionPatch.
        Returns:
            변경한 계약 객체.
        """
        data = validate("InstitutionPatch", payload)
        if set(data["set"]) & set(data["release_fields"]):
            raise ApiError("ambiguous_update", "같은 항목을 수정하면서 해제할 수 없습니다.")
        with self.store.transaction(True) as conn:
            old = _one(conn, "institutions", identity)
            _expect(old, data["expected_revision"])
            overrides = json.loads(old["profile_overrides_json"])
            for name in data["release_fields"]:
                overrides.pop(name, None)
            for name, value in data["set"].items():
                overrides[name] = {"value": value, "recorded_at": utcnow(), "note": data["note"]}
            conn.execute("UPDATE institutions SET profile_overrides_json=?,revision=revision+1,updated_at=? WHERE id=?", (dumps(overrides), utcnow(), identity))
            _audit(conn, "institution", identity, "profile_override", old, data)
            revision = self.store.touch(conn)
            return self._institution(conn, identity)

    def _insert_event(self, conn, data, key=None):
        """서버 발급 불변 키로 새 행사를 저장한다.

        Args:
            conn: 쓰기 연결.
            data: 내부 행사 정보.
            key: 서버가 원문 단위에서 계산한 식별 키.
        Returns:
            행사 UUID.
        """
        identity = new_id()
        now = utcnow()
        conn.execute("INSERT INTO events(id,institution_id,season_id,event_key,title,event_type,audience,campus_label,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (identity, data["institution_id"], data["season_id"], key or new_id(), data["title"], data["event_type"], data.get("audience"), data.get("campus_label"), now, now))
        return identity

    def create_event(self, payload):
        """수기 행사와 최초 확인 기록을 한 번에 저장한다.

        Args:
            payload: EventCreate.
        Returns:
            EventProjection.
        """
        from app.manual import apply_operations
        data = validate("EventCreate", payload)
        with self.store.transaction(True) as conn:
            _one(conn, "institutions", data["institution_id"])
            self._season(conn, data["season_id"])
            existing = conn.execute("SELECT id FROM events WHERE institution_id=? AND season_id=? AND event_type=? AND audience IS ? AND campus_label IS ?", (data["institution_id"], data["season_id"], data["event_type"], data["audience"], data["campus_label"])).fetchall()
            if existing and not data["allow_separate_event"]:
                raise ApiError("event_match_required", "비슷한 행사가 있습니다. 기존 행사를 선택하거나 별도 행사임을 확인해 주세요.", 409, {"candidate_ids": [row[0] for row in existing]})
            identity = self._insert_event(conn, data)
            event, context = self._project(conn, identity)
            apply_operations(conn, event, data["operations"], context)
            self.store.touch(conn, data["season_id"])
            _audit(conn, "event", identity, "create", after=data)
            return self._project(conn, identity)[0]

    def patch_event(self, identity, payload):
        """행사 구분 정보만 바꾸고 불변 키를 유지한다.

        Args:
            identity: 행사 UUID.
            payload: EventMetadataPatch.
        Returns:
            변경한 계약 객체.
        """
        data = validate("EventMetadataPatch", payload)
        with self.store.transaction(True) as conn:
            old = _one(conn, "events", identity)
            _expect(old, data["expected_revision"])
            conn.execute("UPDATE events SET title=?,event_type=?,audience=?,campus_label=?,revision=revision+1,updated_at=? WHERE id=?", (data["title"], data["event_type"], data["audience"], data["campus_label"], utcnow(), identity))
            revision = self.store.touch(conn, old["season_id"])
            _audit(conn, "event", identity, "metadata", old, data)
            return self._project(conn, identity)[0]

    def apply_overrides(self, identity, payload):
        """수기 수정과 revision 검사를 원자적으로 처리한다.

        Args:
            identity: 행사 UUID.
            payload: OverridePatch.
        Returns:
            변경한 계약 객체.
        """
        from app.manual import apply_operations
        data = validate("OverridePatch", payload)
        with self.store.transaction(True) as conn:
            row = _one(conn, "events", identity)
            _expect(row, data["expected_revision"])
            event, context = self._project(conn, identity)
            apply_operations(conn, event, data["operations"], context)
            conn.execute("UPDATE events SET revision=revision+1,updated_at=? WHERE id=?", (utcnow(), identity))
            revision = self.store.touch(conn, row["season_id"])
            return self._project(conn, identity)[0]

    def _documents(self, conn, institution_id):
        """불변 원문과 최신 실제 재확인 상태를 읽는다.

        Args:
            conn: snapshot 연결.
            institution_id: 기관 UUID.
        Returns:
            문서 ID별 DocumentRead.
        """
        result = {}
        names = set(SCHEMA["$defs"]["DocumentRead"]["properties"])
        for saved in conn.execute("SELECT * FROM documents WHERE institution_id=? ORDER BY retrieved_at,id", (institution_id,)):
            row = dict(saved)
            for key in list(row):
                if key.endswith("_json"):
                    row[key[:-5]] = json.loads(row.pop(key)) if row[key] is not None else None
            row["partial"] = bool(row["partial"])
            result[row["id"]] = validate("DocumentRead", {key: value for key, value in row.items() if key in names})
        return result

    def _observations(self, conn, institution_id, season_id):
        """기각 관측도 포함한 원본 관측을 읽는다.

        Args:
            conn: snapshot 연결.
            institution_id: 기관 UUID.
            season_id: 시즌 UUID.
        Returns:
            ID별 Observation.
        """
        result = {}
        for saved in conn.execute("SELECT * FROM observations WHERE institution_id=? AND season_id=?", (institution_id, season_id)):
            row = dict(saved)
            row["candidate"] = json.loads(row.pop("candidate_json"))
            row["reason_codes"] = json.loads(row.pop("reason_codes_json"))
            result[row["id"]] = validate("Observation", row)
        return result

    def _project(self, conn, identity, now=None):
        """단일 snapshot에서 행사·근거를 조립하고 게시값을 계산한다.

        Args:
            conn: 읽기 또는 쓰기 snapshot.
            identity: 행사 UUID.
            now: snapshot 전체에서 동일하게 쓰는 판정시각.
        Returns:
            EventProjection과 서버 내부 ValidatedContext.
        """
        row = _one(conn, "events", identity)
        season = self._season(conn, row["season_id"])
        institution = self._institution(conn, row.pop("institution_id"))
        documents = self._documents(conn, institution["id"])
        observations = self._observations(conn, institution["id"], season["id"])
        fields = {}
        for name in FIELDS:
            latest = conn.execute("SELECT automatic_json FROM assessments WHERE event_id=? AND field=? ORDER BY seq DESC LIMIT 1", (identity, name)).fetchone()
            automatic = json.loads(latest[0]) if latest else empty_automatic()
            saved = conn.execute("SELECT * FROM manual_overrides WHERE event_id=? AND field=? AND active=1", (identity, name)).fetchone()
            manual = None
            if saved:
                manual = dict(saved)
                manual["value"] = json.loads(manual.pop("value_json"))
                for key in ("event_id", "field", "active"):
                    manual.pop(key)
            difference = bool(manual and manual["mode"] == "set" and automatic["status"] in ("official_confirmed", "conflict") and (automatic["status"] == "conflict" or manual["value"] != automatic["value"] or (name in ("start_time", "venue") and manual["bound_date"] != automatic["bound_date"])))
            reasons = set(automatic["reason_codes"])
            if difference:
                reasons.add("manual_difference")
            review = automatic["status"] == "conflict" or difference or "semantic_review_required" in reasons
            fields[name] = {"effective_value": None, "effective_basis": "none", "usable": False, "automatic": automatic, "manual": manual, "has_web_difference": difference, "review_required": review, "reason_codes": sorted(reasons)}
        # 미연결 관측의 식별 문제는 같은 기관의 모든 행사를 억지로 합치지 않는다.
        blockers = sorted({reason for observation in observations.values() if observation["event_id"] == identity and observation["disposition"] != "rejected" for reason in observation["reason_codes"] if reason in ("event_unclear", "event_match_required", "season_unclear", "cancelled_notice")})
        for key in ("created_at", "updated_at"):
            row.pop(key)
        row.update(institution=institution, fields=fields, review_required=any(item["review_required"] for item in fields.values()) or bool(blockers), reason_codes=blockers)
        context = {"documents": documents, "observations": observations, "registered_rules": self._enabled_rules(), "now": now or utcnow(), "event_blockers": blockers, "use_fixture_rules": False}
        row["_identifiers"] = {item["provider"]: item["external_key"] for item in conn.execute("SELECT provider,external_key FROM institution_identifiers WHERE institution_id=?", (institution["id"],))}
        publication = calculate_publication(row, season, context)
        row.pop("_identifiers")
        row["publication"] = publication
        for name in FIELDS:
            field = fields[name]
            permission = publication["field_permissions"][name]
            if permission["general_visible"]:
                value = field["manual"]["value"] if field["manual"] else field["automatic"]["value"]
                field["effective_value"] = value
                field["effective_basis"] = field["manual"]["basis"] if field["manual"] else "official"
                field["usable"] = value is not None
        return validate("EventProjection", row), context

    def event_detail(self, identity):
        """행사와 모든 근거·수정 이력을 같은 snapshot으로 반환한다.

        Args:
            identity: 행사 UUID.
        Returns:
            EventDetail.
        """
        with self.store.transaction() as conn:
            event, context = self._project(conn, identity)
            observations = [item for item in context["observations"].values() if item["event_id"] == identity]
            docs = set(item["document_id"] for item in observations) | {field["manual"]["source_document_id"] for field in event["fields"].values() if field["manual"] and field["manual"]["source_document_id"]}
            support_sources = {context["documents"][key]["source_key"] for key in docs if key in context["documents"]}
            docs.update(key for key, document in context["documents"].items() if document["source_key"] in support_sources)
            history = [dict(row) for row in conn.execute("SELECT id,field,action,actor,recorded_at,summary FROM audit_log WHERE entity_type='event' AND entity_id=? ORDER BY seq DESC", (identity,))]
            return validate("EventDetail", {"event": event, "observations": observations, "documents": [doc for key, doc in context["documents"].items() if key in docs], "history": history, "content_revision": self.store.revision(conn)})

    def _events(self, conn, season_id, now=None):
        """시즌 행사를 같은 시각으로 계산한다.

        Args:
            conn: snapshot 연결.
            season_id: 시즌 UUID.
            now: 판정시각.
        Returns:
            행사 projection 목록.
        """
        self._season(conn, season_id)
        moment = now or utcnow()
        return [self._project(conn, row[0], moment)[0] for row in conn.execute("SELECT id FROM events WHERE season_id=? ORDER BY created_at,id", (season_id,))]

    def events(self, season_id, view="all", **filters):
        """같은 게시 판정에서 일반·판매·관리 조회를 선택한다.

        Args:
            season_id: 시즌 UUID.
            view: calendar, sales, all.
            filters: institution_id, type, district, q 필터.
        Returns:
            EventProjectionPage.
        """
        if view not in ("calendar", "sales", "all"):
            raise ApiError("validation_error", "지원하지 않는 조회 방식입니다.")
        with self.store.transaction() as conn:
            items = self._events(conn, season_id)
            if view != "all":
                key = "sales_ready" if view == "sales" else "calendar_eligible"
                items = [item for item in items if item["publication"][key]]
            for name in ("type", "district"):
                if filters.get(name):
                    items = [item for item in items if item["institution"][name] == filters[name]]
            if filters.get("institution_id"):
                items = [item for item in items if item["institution"]["id"] == filters["institution_id"]]
            if filters.get("q"):
                items = [item for item in items if filters["q"].casefold() in (item["title"] + item["institution"]["name"]).casefold()]
            items.sort(key=lambda item: (item["publication"]["calendar_date"] or "9999", item["publication"]["calendar_time"] or "99:99", item["institution"]["name"], item["id"]))
            return validate("EventProjectionPage", {"items": items, "total": len(items), "next_cursor": None, "publication_policy_version": POLICY_VERSION, "content_revision": self.store.revision(conn)})

    def _registry_snapshots(self, conn):
        """등록 원본의 범위·완전성 이력을 읽는다.

        Args:
            conn: snapshot 연결.
        Returns:
            RegistrySnapshot 목록.
        """
        result = []
        for saved in conn.execute("SELECT * FROM registry_snapshots ORDER BY retrieved_at DESC,id"):
            row = dict(saved)
            row["source"] = json.loads(row.pop("source_json"))
            row.pop("source_file_ref")
            result.append(validate("RegistrySnapshot", row))
        return result

    def registry_snapshots(self):
        """등록 원본 이력을 반환한다.

        Returns:
            RegistrySnapshotPage.
        """
        with self.store.transaction() as conn:
            items = self._registry_snapshots(conn)
            return {"items": items, "total": len(items), "next_cursor": None}

    def _eligible(self, conn, season):
        """시즌 시작 전에 폐쇄가 증명된 기관만 분모에서 제외한다.

        Args:
            conn: snapshot 연결.
            season: 시즌.
        Returns:
            조사 대상 Institution 목록.
        """
        items = [self._institution(conn, row[0]) for row in conn.execute("SELECT id FROM institutions ORDER BY name,id")]
        return [item for item in items if not (item["operation_status"] == "closed_confirmed" and item["operation_effective_date"] and item["operation_effective_date"] <= season["search_start"])]

    def _review(self, conn, season, events):
        """기관별 조사 누락과 미확인 질문을 만든다.

        Args:
            conn: snapshot 연결.
            season: 시즌.
            events: 이미 계산한 행사 목록.
        Returns:
            ReviewItem 목록.
        """
        result = []
        for institution in self._eligible(conn, season):
            items = [event for event in events if event["institution"]["id"] == institution["id"]]
            unresolved = [event for event in items if not event["publication"]["sales_ready"] or event["review_required"]]
            orphan = conn.execute("SELECT reason_codes_json FROM observations WHERE institution_id=? AND season_id=? AND event_id IS NULL AND disposition!='rejected'", (institution["id"], season["id"])).fetchall()
            if items and not unresolved and not orphan:
                continue
            reasons = {reason for event in unresolved for reason in event["publication"]["reason_codes"]}
            for row in orphan:
                reasons.update(json.loads(row[0]))
            if not items:
                reasons.add("not_found")
            if not any(event["publication"]["current_date_allowed"] for event in items):
                reasons.add("date_missing")
            if not any(event["publication"]["current_time_allowed"] for event in items):
                reasons.add("time_missing")
            latest = conn.execute("SELECT ji.status,ji.last_attempt_at,ji.reason_codes_json,ji.history_json FROM job_items ji JOIN jobs j ON j.id=ji.job_id WHERE ji.institution_id=? AND j.season_id=? ORDER BY j.created_at DESC LIMIT 1", (institution["id"], season["id"])).fetchone()
            if latest:
                reasons.update(json.loads(latest["reason_codes_json"]))
                history = json.loads(latest["history_json"])
                codes = {error.get("code") for entry in history for error in entry.get("errors", [])}
                reasons.update(codes & set(SCHEMA["$defs"]["ReasonCode"]["enum"]))
            questions = []
            if "date_missing" in reasons:
                questions.append("졸업식의 정확한 날짜가 언제인가요?")
            if "time_missing" in reasons:
                questions.append("입장·하교 시간이 아닌 졸업식 실제 시작 시각이 언제인가요?")
            if orphan:
                questions.append("발견한 근거가 어느 행사·대상·캠퍼스에 해당하나요?")
            if "manual_difference" in reasons or "conflict" in reasons:
                questions.append("서로 다른 일정 중 어떤 값이 현재 유효한가요?")
            result.append({"institution": institution, "season_id": season["id"], "event_ids": [event["id"] for event in items], "reason_codes": sorted(reasons), "unresolved_questions": questions or ["보류 사유와 근거를 확인해 주세요."], "latest_item_status": latest["status"] if latest else None, "last_attempt_at": latest["last_attempt_at"] if latest else max((event["last_attempt_at"] for event in items if event["last_attempt_at"]), default=None)})
        return result

    def review(self, season_id):
        """한 기관당 하나의 확인 목록을 반환한다.

        Args:
            season_id: 시즌 UUID.
        Returns:
            ReviewItemPage.
        """
        with self.store.transaction() as conn:
            season = self._season(conn, season_id)
            items = self._review(conn, season, self._events(conn, season_id))
            return validate("ReviewItemPage", {"items": items, "total": len(items), "next_cursor": None})

    def _overview(self, conn, season, events):
        """이미 계산한 Publication으로 고유 기관 확인율을 계산한다.

        Args:
            conn: snapshot 연결.
            season: 시즌.
            events: 같은 snapshot의 행사 목록.
        Returns:
            Overview.
        """
        eligible = {item["id"] for item in self._eligible(conn, season)}
        def count(predicate):
            return len({event["institution"]["id"] for event in events if event["institution"]["id"] in eligible and predicate(event)})
        date_count = count(lambda event: event["publication"]["current_date_allowed"])
        time_count = count(lambda event: event["publication"]["current_time_allowed"])
        snapshots = self._registry_snapshots(conn)
        # 서로 다른 기준일의 전체성 주장을 무조건 합치지 않고 유형×구 범위를 증명한다.
        covered = set()
        for snapshot in snapshots:
            source = snapshot["source"]
            if source["coverage_complete"] and snapshot["rejected_count"] == 0:
                covered.update((kind, district) for kind in source["scope_types"] for district in source["scope_districts"])
        needed = {(kind, district) for kind in SCHEMA["$defs"]["InstitutionType"]["enum"] for district in SCHEMA["$defs"]["District"]["enum"]}
        attempted = {row[0] for row in conn.execute("SELECT DISTINCT ji.institution_id FROM job_items ji JOIN jobs j ON j.id=ji.job_id WHERE j.season_id=? AND ji.last_attempt_at IS NOT NULL", (season["id"],))}
        attempted |= {event["institution"]["id"] for event in events if event["last_attempt_at"]}
        output = {"season_id": season["id"], "content_revision": self.store.revision(conn), "registry_complete": needed <= covered, "eligible_institutions": len(eligible), "attempted_institutions": len(attempted & eligible), "date_confirmed_institutions": date_count, "time_confirmed_institutions": time_count, "review_institutions": len(self._review(conn, season, events)), "events_total": len(events), "events_with_usable_date": sum(event["publication"]["current_date_allowed"] for event in events), "events_with_usable_time": sum(event["publication"]["current_time_allowed"] for event in events), "date_confirmation_ratio": date_count / len(eligible) if eligible else None, "time_confirmation_ratio": time_count / len(eligible) if eligible else None, "registry_snapshots": snapshots, "month_only_institutions": count(lambda event: any((field["manual"] or {}).get("value", {}).get("precision") == "month" if isinstance((field["manual"] or {}).get("value"), dict) else isinstance(field["automatic"].get("value"), dict) and field["automatic"]["value"].get("precision") == "month" for key, field in event["fields"].items() if key == "event_date")), "publication_held_institutions": count(lambda event: event["publication"]["state"] == "hold"), "publication_policy_version": POLICY_VERSION}
        for label, field_name in (("date", "event_date"), ("time", "start_time")):
            permission_name = "current_date_allowed" if label == "date" else "current_time_allowed"
            output[f"official_{label}_institutions"] = count(lambda event: event["publication"][permission_name] and event["fields"][field_name]["manual"] is None)
            output[f"manual_{label}_institutions"] = count(lambda event: event["publication"][permission_name] and event["fields"][field_name]["manual"] is not None)
        return validate("Overview", output)

    def overview(self, season_id):
        """확인율과 목록 범위를 조회한다.

        Args:
            season_id: 시즌 UUID.
        Returns:
            Overview.
        """
        with self.store.transaction() as conn:
            season = self._season(conn, season_id)
            return self._overview(conn, season, self._events(conn, season_id))

    def snapshot(self, season_id):
        """HTML·집계·행사가 공유할 하나의 읽기 snapshot을 만든다.

        Args:
            season_id: 시즌 UUID.
        Returns:
            season, events, documents, overview, revision, policy, generated_at.
        """
        with self.store.transaction() as conn:
            season = self._season(conn, season_id)
            moment = utcnow()
            events = self._events(conn, season_id, moment)
            documents = {}
            for event in events:
                all_docs = self._documents(conn, event["institution"]["id"])
                refs = {row[0] for row in conn.execute("SELECT document_id FROM observations WHERE event_id=?", (event["id"],))}
                refs.update(field["manual"]["source_document_id"] for field in event["fields"].values() if field["manual"] and field["manual"]["source_document_id"])
                support_sources = {all_docs[key]["source_key"] for key in refs if key in all_docs}
                refs.update(key for key, document in all_docs.items() if document["source_key"] in support_sources)
                documents[event["id"]] = [item for key, item in all_docs.items() if key in refs]
            latest = conn.execute("SELECT id FROM jobs WHERE kind='schedule_scan' AND season_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (season_id,)).fetchone()
            target_ids = {row[0] for row in conn.execute("SELECT institution_id FROM job_items WHERE job_id=? AND institution_id IS NOT NULL", (latest[0],))} if latest else {event["institution"]["id"] for event in events}
            # 직접 기록도 그 기관을 결과에 넣어 달라는 요청이다. 전체 명부를 임의로 출력하지 않는다.
            target_ids.update(row[0] for row in conn.execute("SELECT DISTINCT e.institution_id FROM events e JOIN manual_overrides m ON m.event_id=e.id WHERE e.season_id=? AND m.active=1", (season_id,)))
            return {"season": season, "events": events, "targets": [self._institution(conn, key) for key in sorted(target_ids)],
                    "request_job_id": latest[0] if latest else None, "documents": documents, "overview": self._overview(conn, season, events), "content_revision": self.store.revision(conn), "publication_policy_version": POLICY_VERSION, "generated_at": moment}

    def registry_preview(self, payload):
        """공식 목록의 행별 오류·중복을 먼저 저장해 검토 가능하게 한다.

        Args:
            payload: RegistryImport. 잘못된 행은 preview에 거절 사유로 남는다.
        Returns:
            ImportPreview.
        """
        if not isinstance(payload, dict) or set(payload) != {"schema_version", "source", "rows"} or payload.get("schema_version") != "4.0" or not isinstance(payload.get("rows"), list):
            raise ApiError("validation_error", "4.0 기관 목록 JSON이 필요합니다.")
        source = validate("RegistrySource", payload["source"])
        import re
        from urllib.parse import urlsplit
        from app.registry import district_for
        if not re.fullmatch(r"[a-f0-9]{64}", source["source_hash"]):
            raise ApiError("source_hash_required", "원본 파일의 SHA-256 해시가 필요합니다.")
        settings = getattr(self.settings, "data", self.settings or {})
        mode = settings.get("app", {}).get("mode", settings.get("app_mode", "live"))
        hostname = urlsplit(source["official_url"]).hostname or ""
        if mode != "demo" and (source["provider"].lower().startswith(("demo", "fixture", "fake")) or hostname.endswith((".example", ".invalid", ".test")) or hostname == "localhost"):
            raise ApiError("source_unverified", "가상·시험 자료를 live 기관 목록으로 적재할 수 없습니다.")
        if source["coverage_complete"] and (not source["as_of"] or not source["completeness_note"].strip() or source["expected_total"] != len(payload["rows"])):
            raise ApiError("coverage_incomplete", "전체 목록 주장에는 기준일·완전성 근거·일치하는 원본 행수가 필요합니다.")
        rejected, duplicates, seen = [], [], set()
        with self.store.transaction(True) as conn:
            for index, raw in enumerate(payload["rows"]):
                try:
                    row = validate("RegistryRow", raw)
                    if district_for(row["address"]) != row["district"]:
                        raise ApiError("region_mismatch", "광주광역시 주소와 구가 일치하지 않습니다.")
                    if row["type"] in ("university", "junior_college") and not row["campus_label"]:
                        raise ApiError("campus_identity_required", "대학 목록에는 캠퍼스 구분이 필요합니다.")
                    if row["type"] not in source["scope_types"] or row["district"] not in source["scope_districts"]:
                        raise ApiError("scope_mismatch", "원본에서 선언한 유형·구 범위를 벗어났습니다.")
                    if row["external_key"] in seen:
                        raise ApiError("duplicate_key", "같은 외부 식별자가 중복되었습니다.")
                    if row["operation_status"] == "closed_confirmed" and not row["operation_evidence_url"]:
                        raise ApiError("closure_evidence_missing", "운영 종료 확인에는 근거 주소가 필요합니다.")
                    seen.add(row["external_key"])
                    known = conn.execute("SELECT institution_id FROM institution_identifiers WHERE provider=? AND external_key=?", (source["provider"], row["external_key"])).fetchone()
                    if not known:
                        matched = conn.execute("SELECT id FROM institutions WHERE name=? AND type=? AND district=?", (row["name"], row["type"], row["district"])).fetchall()
                        if matched:
                            duplicates.append({"row_index": index, "institution_ids": [item[0] for item in matched]})
                except ApiError as error:
                    rejected.append({"row_index": index, "reason": error.message})
            now = datetime.now(timezone.utc)
            identity = new_id()
            preview = {"id": identity, "preview_hash": fingerprint(payload), "content_revision": self.store.revision(conn), "received_count": len(payload["rows"]), "accepted_count": len(payload["rows"]) - len(rejected), "rejected_rows": rejected, "duplicate_candidates": duplicates, "expires_at": (now + timedelta(hours=1)).isoformat()}
            conn.execute("INSERT INTO import_previews(id,preview_hash,content_revision,payload_json,preview_json,status,created_at,expires_at) VALUES(?,?,?,?,?,'preview',?,?)", (identity, preview["preview_hash"], preview["content_revision"], dumps(payload), dumps(preview), now.isoformat(), preview["expires_at"]))
            return validate("ImportPreview", preview)

    def registry_commit(self, identity, payload, idempotency_key=None):
        """검토한 목록만 원자적으로 적재하고 수기 기관 정보를 보호한다.

        Args:
            identity: preview UUID.
            payload: ImportCommit.
            idempotency_key: 재시도 식별자.
        Returns:
            RegistrySnapshot.
        """
        data = validate("ImportCommit", payload)
        with self.store.transaction(True) as conn:
            saved = _one(conn, "import_previews", identity)
            if saved["status"] == "committed":
                if idempotency_key and saved["idempotency_key"] == idempotency_key:
                    return json.loads(saved["result_json"])
                raise ApiError("already_committed", "이미 적재한 미리보기입니다.", 409)
            if saved["expires_at"] < utcnow():
                raise ApiError("preview_expired", "미리보기가 만료되었습니다. 다시 검토해 주세요.", 409)
            if data["expected_preview_hash"] != saved["preview_hash"] or data["expected_content_revision"] != self.store.revision(conn) or saved["content_revision"] != self.store.revision(conn):
                raise ApiError("preview_changed", "미리보기 이후 데이터가 바뀌었습니다. 다시 검토해 주세요.", 409)
            original, preview = json.loads(saved["payload_json"]), json.loads(saved["preview_json"])
            rejected = {item["row_index"] for item in preview["rejected_rows"]}
            mappings = {item["row_index"]: item for item in data["mappings"]}
            if len(mappings) != len(data["mappings"]) or any(index >= len(original["rows"]) for index in mappings):
                raise ApiError("invalid_mapping", "행 연결 정보가 중복되거나 범위를 벗어났습니다.")
            if rejected and not data["allow_partial"]:
                raise ApiError("partial_import_required", "거절된 행이 있습니다. 부분 적재를 명시해야 합니다.", 409)
            for duplicate in preview["duplicate_candidates"]:
                if duplicate["row_index"] not in mappings:
                    raise ApiError("mapping_required", "중복 후보의 연결 또는 별도 생성을 선택해 주세요.", 409)
            snapshot_id = new_id()
            source = copy.deepcopy(original["source"])
            selected = [(index, row) for index, row in enumerate(original["rows"]) if index not in rejected and mappings.get(index, {}).get("decision") != "skip"]
            skipped = len(original["rows"]) - len(selected)
            if skipped or source["expected_total"] is None or source["expected_total"] != len(selected):
                source["coverage_complete"] = False
                source["completeness_note"] += " / 전체 행수·거절·제외 조건상 등록 목록의 완전성은 확인되지 않았습니다."
            result = {"id": snapshot_id, "source": source, "retrieved_at": utcnow(), "loaded_count": len(selected), "linked_count": len(selected), "rejected_count": skipped}
            conn.execute("INSERT INTO registry_snapshots(id,source_json,retrieved_at,loaded_count,linked_count,rejected_count) VALUES(?,?,?,?,?,?)", (snapshot_id, dumps(source), result["retrieved_at"], len(selected), 0, skipped))
            for index, row in selected:
                mapping = mappings.get(index)
                known = conn.execute("SELECT institution_id FROM institution_identifiers WHERE provider=? AND external_key=?", (source["provider"], row["external_key"])).fetchone()
                institution_id = known[0] if known else None
                if mapping:
                    if mapping["decision"] == "update":
                        if not mapping["institution_id"]:
                            raise ApiError("mapping_required", "연결할 기관을 선택해 주세요.")
                        if institution_id and institution_id != mapping["institution_id"]:
                            raise ApiError("identifier_conflict", "외부 식별자가 다른 기관과 이미 연결되어 있습니다.", 409)
                        institution_id = mapping["institution_id"]
                        _one(conn, "institutions", institution_id)
                    elif mapping["decision"] == "create" and institution_id:
                        raise ApiError("identifier_conflict", "기존 외부 식별자를 새 기관에 중복 연결할 수 없습니다.", 409)
                if institution_id:
                    old = _one(conn, "institutions", institution_id)
                    conn.execute("UPDATE institutions SET name=?,type=?,district=?,address=?,campus_label=?,phone=?,homepage_url=?,operation_status=?,operation_effective_date=?,operation_evidence_url=?,source_checked_at=?,revision=revision+1,updated_at=? WHERE id=?", (*(row[key] for key in ("name", "type", "district", "address", "campus_label", "phone", "homepage_url", "operation_status", "operation_effective_date", "operation_evidence_url")), result["retrieved_at"], utcnow(), institution_id))
                    _audit(conn, "institution", institution_id, "registry_update", old, row, actor="system")
                else:
                    institution_id = self._insert_institution(conn, row)
                    conn.execute("UPDATE institutions SET source_checked_at=? WHERE id=?", (result["retrieved_at"], institution_id))
                conn.execute("INSERT OR IGNORE INTO institution_identifiers(provider,external_key,institution_id) VALUES(?,?,?)", (source["provider"], row["external_key"], institution_id))
                conn.execute("INSERT INTO registry_memberships(snapshot_id,institution_id,external_key,raw_row_json) VALUES(?,?,?,?)", (snapshot_id, institution_id, row["external_key"], dumps(row)))
            conn.execute("UPDATE registry_snapshots SET linked_count=? WHERE id=?", (result["linked_count"], snapshot_id))
            from .registry_assets import matching_authorities
            for relation in matching_authorities(original):
                member = conn.execute(
                    "SELECT institution_id FROM registry_memberships WHERE snapshot_id=? AND external_key=?",
                    (snapshot_id, relation["external_key"]),
                ).fetchone()
                if member is None:
                    continue
                institution = _one(conn, "institutions", member[0])
                channels = json.loads(institution["official_channels_json"])
                channel = relation["channel"]
                if not any(existing == channel for existing in channels):
                    channels.append(channel)
                    conn.execute("UPDATE institutions SET official_channels_json=? WHERE id=?", (dumps(channels), member[0]))
                    _audit(conn, "institution", member[0], "official_file_channel", None, relation, actor="system")
            conn.execute("UPDATE import_previews SET status='committed',idempotency_key=?,result_json=? WHERE id=?", (idempotency_key, dumps(result), identity))
            self.store.touch(conn)
            return validate("RegistrySnapshot", result)

    def _save_document(self, conn, institution_id, raw):
        """원문 버전을 보존하고 같은 버전의 재확인 정보만 갱신한다.

        Args:
            conn: 쓰기 연결.
            institution_id: 조사 대상 기관.
            raw: 내부 수집기가 반환한 DocumentRead.
        Returns:
            저장 문서 UUID.
        """
        document = validate("DocumentRead", raw)
        if document["institution_id"] != institution_id:
            raise ApiError("evidence_link_invalid", "조사 대상과 문서 기관이 일치하지 않습니다.")
        for key in ("retrieved_at", "last_checked_at"):
            document[key] = normalize_instant(document[key])
        if document["recheck"]["attempted_at"]:
            document["recheck"]["attempted_at"] = normalize_instant(document["recheck"]["attempted_at"])
        if document["authority_evidence"]:
            document["authority_evidence"]["verified_at"] = normalize_instant(document["authority_evidence"]["verified_at"])
        existing = conn.execute("SELECT id,authority FROM documents WHERE institution_id=? AND source_key=? AND content_hash IS ?", (institution_id, document["source_key"], document["content_hash"])).fetchone()
        if existing:
            identity = existing[0]
            if existing["authority"] == "official" and document["acquisition"]["transport_status"] == "ok":
                from app.publication import _authority
                if not _authority(document, self._institution(conn, institution_id), datetime.now(timezone.utc)):
                    document["recheck"] = {"status": "failed", "attempted_at": document["retrieved_at"], "reason_codes": ["source_unverified"]}
                    document["last_checked_at"] = document["retrieved_at"]
            if document["recheck"]["status"] == "not_attempted" and document["status"] == "ok" and document["acquisition"]["transport_status"] == "ok" and document["reading"]["content_match"] == "expected" and document["reading"]["scope"] in ("full_text", "rendered_dom"):
                document["recheck"] = {"status": "succeeded", "attempted_at": document["retrieved_at"], "reason_codes": []}
                document["last_checked_at"] = document["retrieved_at"]
            # 반복 오류 버전도 직전 성공 재확인을 무효화해야 한다. 오류 기록만
            # 갱신하면 실패→성공→실패 순서에서 과거 성공이 현재로 잘못 남는다.
            if document["recheck"]["status"] == "failed":
                conn.execute("UPDATE documents SET recheck_json=?,last_checked_at=? WHERE institution_id=? AND source_key=? AND status='ok'", (dumps(document["recheck"]), document["last_checked_at"], institution_id, document["source_key"]))
            elif document["content_hash"] and document["acquisition"]["transport_status"] == "ok":
                changed = {"status": "failed", "attempted_at": document["retrieved_at"], "reason_codes": ["semantic_review_required"]}
                conn.execute("UPDATE documents SET recheck_json=?,last_checked_at=? WHERE institution_id=? AND source_key=? AND id!=? AND status='ok' AND content_hash IS NOT ?", (dumps(changed), document["retrieved_at"], institution_id, document["source_key"], identity, document["content_hash"]))
            if document["recheck"]["status"] != "not_attempted":
                conn.execute("UPDATE documents SET recheck_json=?,last_checked_at=? WHERE id=?", (dumps(document["recheck"]), document["last_checked_at"], identity))
            return identity
        identity = new_id()
        document["id"] = identity
        json_keys = {"segments", "reason_codes", "acquisition", "reading", "image_size", "authority_evidence", "recheck"}
        columns, values = [], []
        for key, value in document.items():
            columns.append(key + "_json" if key in json_keys else key)
            values.append(dumps(value) if key in json_keys else int(value) if key == "partial" else value)
        conn.execute(f"INSERT INTO documents({','.join(columns)}) VALUES({','.join('?' for _ in values)})", values)
        if document["content_hash"] and document["acquisition"]["transport_status"] == "ok":
            # 같은 URL의 새 본문은 옛 버전의 현재 지원을 증명하지 않는다. 새 후보가
            # 동일 값을 재검증하면 독립 지원 경로로 현재 권한을 다시 얻는다.
            changed = {"status": "failed", "attempted_at": document["retrieved_at"], "reason_codes": ["semantic_review_required"]}
            conn.execute("UPDATE documents SET recheck_json=?,last_checked_at=? WHERE institution_id=? AND source_key=? AND id!=? AND status='ok' AND content_hash IS NOT ?", (dumps(changed), document["retrieved_at"], institution_id, document["source_key"], identity, document["content_hash"]))
        if document["recheck"]["status"] == "failed":
            # 실패 응답은 새 오류 기록으로 보존하고, 같은 지원 경로의 기존 버전에 표시한다.
            conn.execute("UPDATE documents SET recheck_json=?,last_checked_at=? WHERE institution_id=? AND source_key=? AND id!=? AND status='ok'", (dumps(document["recheck"]), document["last_checked_at"], institution_id, document["source_key"], identity))
        return identity

    def _reassess(self, conn, event_id, refreshed_document_ids=None):
        """등록 검증기를 실제 근거에 실행하고 필드별 평가 이력을 추가한다.

        Args:
            conn: 쓰기 연결.
            event_id: 행사 UUID.
            refreshed_document_ids: 이번 조사에서 본문을 실제 성공적으로 읽은 문서 ID.
        """
        from app.publication import _support
        from app.rules import supersession_edges, value_check
        event, context = self._project(conn, event_id)
        season = self._season(conn, event["season_id"])
        event["_identifiers"] = {item["provider"]: item["external_key"] for item in conn.execute("SELECT provider,external_key FROM institution_identifiers WHERE institution_id=?", (event["institution"]["id"],))}
        observations = [item for item in context["observations"].values() if item["event_id"] == event_id and item["disposition"] != "rejected"]
        accepted_ids = set()
        now = utcnow()
        for name in FIELDS:
            supported, references, actual_checks = [], [], []
            for observation in observations:
                candidate = observation["candidate"]
                value = candidate[name]
                if value is None or (name == "event_date" and value["precision"] == "unknown") or (name == "ceremony_status" and value == "unclear"):
                    continue
                references.append((observation, value))
                document = context["documents"][observation["document_id"]]
                for rule_id, rule_version in context["registered_rules"].items():
                    proof = validate("ValueCheck", value_check(name, value, observation, document, event, season, institution=event["institution"]))
                    actual_checks.append(proof)
                    if proof["rule_id"] != rule_id or proof["rule_version"] != rule_version or proof["status"] != "supported":
                        continue
                    automatic = empty_automatic()
                    automatic.update(status="official_confirmed", value=value, bound_date=candidate["event_date"].get("date") if name in ("start_time", "venue") else None, evidence_ids=[observation["id"]], checked_at=now, value_check=proof)
                    # accepted는 클라이언트 입력이 아니며 지원 검사를 위한 내부 가정이다.
                    probe_context = copy.deepcopy(context)
                    probe_context["observations"][observation["id"]]["disposition"] = "accepted"
                    supports, failures = _support(name, automatic, event, season, probe_context, datetime.now(timezone.utc))
                    if supports and not failures:
                        supported.append((observation, automatic))
                        break
                    proof["status"] = "rejected"
                    proof["reason_codes"] = failures or ["evidence_invalidated"]
                    proof["checks"]["content"] = "fail"
            # 최신 문서라는 이유만으로 교체하지 않는다. 원문·이전값·새값의
            # 대체 관계를 서버 규칙이 검증한 필드에서만 이전 근거를 제외한다.
            edges = supersession_edges(name, supported, context["documents"], event, season)
            superseded = {edge["old_id"] for edge in edges}
            if edges:
                recorded = {dumps(json.loads(row[0])) for row in conn.execute(
                    "SELECT after_json FROM audit_log WHERE entity_type='event' AND entity_id=? "
                    "AND action='supersede_observation' AND field=?",
                    (event_id, name),
                )}
                for edge in edges:
                    serialized = dumps(edge)
                    if serialized not in recorded:
                        _audit(conn, "event", event_id, "supersede_observation", after=edge,
                               field=name, actor="system", summary="공식 변경 공지의 필드별 대체 관계 확인")
                        recorded.add(serialized)
                supported = [(observation, value) for observation, value in supported
                             if observation["id"] not in superseded]
            automatic = empty_automatic()
            automatic.update(assessment_id=new_id(), checked_at=now)
            if supported:
                # 검증된 행사 취소는 과거 개최 안내와 평균내거나 날짜가 없다고 버리지 않는다.
                # 다시 개최한다는 해제는 별도 직접 재확인 경로에서 이력을 남긴다.
                negative = [pair for pair in supported if name == "ceremony_status"
                            and pair[1]["value"] in {"cancelled", "not_held"}]
                if negative:
                    supported = negative
                grouped = {}
                for observation, value in supported:
                    grouped.setdefault(fingerprint({"value": value["value"], "bound_date": value["bound_date"]}), []).append((observation, value))
                if len(grouped) > 1:
                    automatic.update(status="conflict", reason_codes=["conflict"], evidence_ids=sorted({item[0]["id"] for item in supported}), fingerprint=fingerprint(sorted(grouped)))
                else:
                    automatic.update(copy.deepcopy(supported[0][1]))
                    automatic["assessment_id"] = new_id()
                    automatic["fingerprint"] = fingerprint({"value": automatic["value"], "bound_date": automatic["bound_date"]})
                    same_rule = [(observation, item) for observation, item in supported if item["value_check"]["rule_id"] == automatic["value_check"]["rule_id"]]
                    automatic["evidence_ids"] = sorted({observation["id"] for observation, _ in same_rule})
                    automatic["value_check"]["evidence_ids"] = automatic["evidence_ids"]
                    automatic["value_check"]["document_ids"] = sorted({observation["document_id"] for observation, _ in same_rule})
                    automatic["value_check"]["document_hashes"] = {document_id: context["documents"][document_id]["content_hash"] for document_id in automatic["value_check"]["document_ids"]}
            elif references:
                if actual_checks:
                    automatic["value_check"] = actual_checks[-1]
                automatic.update(status="reference_only", value=references[-1][1], bound_date=references[-1][0]["candidate"]["event_date"].get("date") if name in ("start_time", "venue") else None, evidence_ids=[item[0]["id"] for item in references], reason_codes=["semantic_review_required"] if context["registered_rules"] else ["rule_not_enabled"], fingerprint=fingerprint([{name: value} for _, value in references]))
            else:
                automatic.update(status="missing", reason_codes=["date_missing" if name == "event_date" else "time_missing" if name == "start_time" else "not_found"])
            if automatic["status"] in {"official_confirmed", "conflict"}:
                # 관측 상태는 전역이므로 미대체 필드가 여전히 사용하는 근거는
                # accepted로 남기고, 어느 필드에도 쓰이지 않는 관측만 reference로 둔다.
                accepted_ids.update(automatic["evidence_ids"])
            old = event["fields"][name]["automatic"]
            if automatic["status"] == old["status"] == "official_confirmed" and automatic["fingerprint"] == old["fingerprint"] and not (set(automatic["value_check"]["document_ids"]) & set(refreshed_document_ids or [])):
                # 재확인 실패·기각 처리만으로 과거 값의 실제 확인시각을 새로 만들지 않는다.
                automatic["checked_at"] = old["checked_at"]
                automatic["value_check"]["checked_at"] = old["value_check"]["checked_at"]
            manual = event["fields"][name]["manual"]
            difference = bool(manual and manual["mode"] == "set" and automatic["status"] == "official_confirmed" and (manual["value"] != automatic["value"] or (name in ("start_time", "venue") and manual["bound_date"] != automatic["bound_date"])))
            if automatic["status"] == "conflict" or difference:
                automatic["conflict_detected_at"] = old["conflict_detected_at"] if old["fingerprint"] == automatic["fingerprint"] and old["conflict_detected_at"] else now
            validate("Projection_" + name, {"effective_value": None, "effective_basis": "none", "usable": False, "automatic": automatic, "manual": manual, "has_web_difference": difference, "review_required": automatic["status"] == "conflict" or difference, "reason_codes": automatic["reason_codes"]})
            conn.execute("INSERT INTO assessments(id,event_id,field,status,automatic_json,created_at) VALUES(?,?,?,?,?,?)", (automatic["assessment_id"], event_id, name, automatic["status"], dumps(automatic), now))
        for observation in observations:
            disposition = "accepted" if observation["id"] in accepted_ids else "reference"
            reasons = [] if disposition == "accepted" else ["semantic_review_required"] if context["registered_rules"] else ["rule_not_enabled"]
            conn.execute("UPDATE observations SET disposition=?,reason_codes_json=? WHERE id=?", (disposition, dumps(reasons), observation["id"]))

    def _apply_calendar_recheck(self, conn, institution_id, season_id, result):
        """실제 NEIS 조회 실패·행 누락을 그 시즌의 지원 근거에만 반영한다.

        Args:
            conn: 결과 저장과 공유하는 쓰기 연결.
            institution_id: 조회한 기관 UUID.
            season_id: 전체 기간을 조회한 시즌 UUID.
            result: 문서와 calendar 조회 상태를 담은 내부 조사 결과.
        """
        coverage = result.get("coverage", {}).get("calendar", {})
        status = coverage.get("status")
        # 키 미설정·미시도·보조 검색 실패는 필요한 원문 재확인 실패가 아니다.
        if status not in {"ok", "no_data", "provider_error"}:
            return
        checked_at = normalize_instant(coverage.get("checked_at") or utcnow())
        complete = status in {"ok", "no_data"} and coverage.get("coverage_complete") is True
        returned = {document["source_key"] for document in result.get("documents", [])
                    if document.get("source_kind") == "official_api" and document.get("status") == "ok"
                    and document.get("reading", {}).get("content_match") == "expected"}
        rows = conn.execute(
            "SELECT DISTINCT d.id,d.source_key,d.recheck_json FROM documents d "
            "WHERE d.institution_id=? AND d.source_kind='official_api' AND d.status='ok' "
            "AND (d.source_key LIKE 'neis:SchoolSchedule:%' OR d.source_key LIKE 'https://open.neis.go.kr/hub/SchoolSchedule?%') "
            "AND (EXISTS(SELECT 1 FROM observations o WHERE o.document_id=d.id AND o.season_id=?) "
            "OR EXISTS(SELECT 1 FROM manual_overrides m JOIN events e ON e.id=m.event_id "
            "WHERE e.season_id=? AND m.active=1 AND (m.source_document_id=d.id "
            "OR (m.source_document_id IS NULL AND m.source_url=d.display_url))))",
            (institution_id, season_id, season_id),
        ).fetchall()
        valid_codes = set(SCHEMA["$defs"]["ReasonCode"]["enum"])
        for row in rows:
            if complete and row["source_key"] in returned:
                continue
            codes = ["recheck_failed", "not_found"] if complete else ["recheck_failed", *coverage.get("reason_codes", [])]
            recheck = {"status": "failed", "attempted_at": checked_at, "reason_codes": sorted(set(codes) & valid_codes)}
            previous = json.loads(row["recheck_json"])
            if previous == recheck:
                continue
            conn.execute("UPDATE documents SET recheck_json=?,last_checked_at=? WHERE id=?",
                         (dumps(recheck), checked_at, row["id"]))
            _audit(conn, "document", row["id"], "calendar_recheck", previous, recheck,
                   actor="system", summary="NEIS 재조회 실패 또는 전체 조회에서 기존 행사 행 미발견")

    def save_research(self, institution_id, season_id, result, conn=None):
        """내부 조사 결과를 검증·저장하고 자동값을 서버에서 평가한다.

        Args:
            institution_id: 실제 큐의 기관 UUID.
            season_id: 실제 큐의 시즌 UUID.
            result: documents, candidates를 포함한 내부 수집 결과.
            conn: 작업 취소 검사와 저장을 묶는 기존 쓰기 연결.
        Returns:
            저장한 문서·관측·행사 UUID 목록.
        """
        if conn is None:
            with self.store.transaction(True) as transaction:
                return self.save_research(institution_id, season_id, result, conn=transaction)
        institution = self._institution(conn, institution_id)
        season = self._season(conn, season_id)
        document_ids = {}
        for document in result.get("documents", []):
            document_ids[document["id"]] = self._save_document(conn, institution_id, document)
        events, observations = set(), []
        for extracted in result.get("candidates", []):
            original = validate("Candidate", extracted["candidate"])
            candidate = copy.deepcopy(original)
            document_id = document_ids.get(extracted["document_id"])
            if document_id is None:
                raise ApiError("evidence_link_invalid", "관측 원문이 조사 결과에 없습니다.")
            event_id = None
            reasons = ["event_match_required"]
            if candidate["event_key_hint"] and candidate["academic_year"] == season["academic_year"] and candidate["season_alignment"] == "explicit" and candidate["event_type"] != "unknown":
                event_key = fingerprint({"institution_id": institution_id, "season_id": season_id, "hint": candidate["event_key_hint"], "event_type": candidate["event_type"], "audience": candidate["audience"], "campus_label": candidate["campus_label"]})
                existing = conn.execute("SELECT id FROM events WHERE institution_id=? AND season_id=? AND event_key=?", (institution_id, season_id, event_key)).fetchone()
                if not existing:
                    from .identity import same_event
                    matches = [dict(row) for row in conn.execute(
                        "SELECT * FROM events WHERE institution_id=? AND season_id=?", (institution_id, season_id))
                        if same_event(candidate, dict(row), institution)]
                    if len(matches) == 1:
                        existing = (matches[0]["id"],)
                        event_key = matches[0]["event_key"]
                    elif len(matches) > 1:
                        # 이미 여러 행사에 연결될 수 있으면 새 중복 행사도 만들지 않는다.
                        candidate["event_key_hint"] = None
                if existing:
                    event_id = existing[0]
                elif candidate["event_key_hint"]:
                    event_id = self._insert_event(conn, {"institution_id": institution_id, "season_id": season_id, "title": candidate["event_title"], "event_type": candidate["event_type"], "audience": candidate["audience"], "campus_label": candidate["campus_label"]}, event_key)
                if event_id:
                    candidate["event_key_hint"] = event_key
                    events.add(event_id)
                    reasons = ["rule_not_enabled"]
            identity = new_id()
            digest = fingerprint({"institution_id": institution_id, "season_id": season_id, "event_id": event_id, "document_id": document_id, "candidate": candidate})
            existing_observation = conn.execute("SELECT id FROM observations WHERE fingerprint=?", (digest,)).fetchone()
            if existing_observation:
                observations.append(existing_observation[0])
                continue
            conn.execute("INSERT INTO observations(id,institution_id,season_id,event_id,document_id,candidate_json,fingerprint,disposition,reason_codes_json,created_at) VALUES(?,?,?,?,?,?,?,'reference',?,?)", (identity, institution_id, season_id, event_id, document_id, dumps(candidate), digest, dumps(reasons), utcnow()))
            observations.append(identity)
            _audit(conn, "event" if event_id else "observation", event_id or identity, "research_candidate", after={"original_candidate": original, "observation_id": identity}, actor="system", summary="원문 후보 보존 및 서버 행사 연결")
        self._apply_calendar_recheck(conn, institution_id, season_id, result)
        # 실패 재확인도 기존 행사에 반영되므로 신규 후보가 없어도 revision을 갱신한다.
        events.update(row[0] for row in conn.execute("SELECT id FROM events WHERE institution_id=? AND season_id=?", (institution_id, season_id)))
        success = any(document.get("status") == "ok" and document.get("reading", {}).get("content_match") == "expected" for document in result.get("documents", []))
        for blocker in result.get("event_blockers", []):
            source_key = blocker.get("source_key")
            stored_id = document_ids.get(blocker.get("document_id"))
            if not source_key or not stored_id or "cancelled_notice" not in blocker.get("reason_codes", []):
                continue
            affected = conn.execute("SELECT DISTINCT o.event_id FROM observations o JOIN documents d ON d.id=o.document_id WHERE o.institution_id=? AND o.season_id=? AND d.source_key=? AND o.event_id IS NOT NULL", (institution_id, season_id, source_key)).fetchall()
            for affected_event in affected:
                events.add(affected_event[0])
                document = _one(conn, "documents", stored_id)
                entry = {"source_key": source_key, "document_id": stored_id, "content_hash": document["content_hash"], "reason_codes": ["cancelled_notice"], "recorded_at": utcnow()}
                latest = conn.execute("SELECT after_json FROM audit_log WHERE entity_type='event' AND entity_id=? AND action='source_blocker' ORDER BY seq DESC LIMIT 1", (affected_event[0],)).fetchone()
                previous = json.loads(latest[0]) if latest else {}
                if previous.get("content_hash") != entry["content_hash"] or previous.get("source_key") != source_key:
                    _audit(conn, "event", affected_event[0], "source_blocker", after=entry, field="ceremony_status", actor="system", summary="같은 공식 지원 경로에서 취소·변경 주의 발견")
        for event_id in events:
            refreshed = {document_ids[document["id"]] for document in result.get("documents", []) if document.get("status") == "ok" and document.get("reading", {}).get("content_match") == "expected"}
            self._reassess(conn, event_id, refreshed)
            self._apply_source_blockers(conn, event_id)
            conn.execute("UPDATE events SET revision=revision+1,last_attempt_at=?,last_successful_source_check_at=CASE WHEN ? THEN ? ELSE last_successful_source_check_at END,change_check_status=?,updated_at=? WHERE id=?", (utcnow(), success, utcnow(), "completed" if success else "limited", utcnow(), event_id))
        revision = self.store.touch(conn, season_id)
        return {"document_ids": list(document_ids.values()), "observation_ids": observations, "event_ids": sorted(events), "content_revision": revision}


    def _apply_source_blockers(self, conn, event_id):
        """취소 주의를 안정적인 상태 충돌로 보존하고 수기 재확인을 허용한다.

        Args:
            conn: 쓰기 연결.
            event_id: 영향받은 행사 UUID.
        """
        blockers = {}
        for row in conn.execute("SELECT after_json FROM audit_log WHERE entity_type='event' AND entity_id=? AND action='source_blocker' ORDER BY seq", (event_id,)):
            blocker = json.loads(row[0])
            blockers[blocker["source_key"]] = blocker
        if not blockers:
            return
        event, context = self._project(conn, event_id)
        automatic = event["fields"]["ceremony_status"]["automatic"]
        unresolved = []
        for source_key, blocker in blockers.items():
            newly_supported = any(
                observation["id"] in automatic["evidence_ids"] and automatic["status"] == "official_confirmed"
                and automatic["value"] == "scheduled"
                and context["documents"][observation["document_id"]]["source_key"] == source_key
                and normalize_instant(context["documents"][observation["document_id"]]["retrieved_at"]) > normalize_instant(blocker["recorded_at"])
                for observation in context["observations"].values() if observation["event_id"] == event_id
            )
            if not newly_supported:
                unresolved.append(blocker)
        if not unresolved:
            return
        digest = fingerprint(sorted((item["source_key"], item["content_hash"]) for item in unresolved))
        previous = conn.execute("SELECT automatic_json FROM assessments WHERE event_id=? AND field='ceremony_status' AND status='conflict' ORDER BY seq DESC LIMIT 1", (event_id,)).fetchone()
        previous = json.loads(previous[0]) if previous else {}
        now = utcnow()
        assessment = empty_automatic()
        assessment.update(status="conflict", assessment_id=new_id(), fingerprint=digest, evidence_ids=automatic["evidence_ids"], checked_at=now, reason_codes=["conflict", "cancelled_notice"], conflict_detected_at=previous.get("conflict_detected_at") if previous.get("fingerprint") == digest else min(item["recorded_at"] for item in unresolved))
        conn.execute("INSERT INTO assessments(id,event_id,field,status,automatic_json,created_at) VALUES(?,?,'ceremony_status','conflict',?,?)", (assessment["assessment_id"], event_id, dumps(assessment), now))

    def save_registry_sync(self, result, conn=None):
        """내부 공식 목록 수집 결과를 동일 트랜잭션에서 적재한다.

        Args:
            result: RegistryImport 또는 registry 키에 이를 담은 수집 결과.
            conn: 작업 취소 검사와 공유할 쓰기 연결.
        Returns:
            RegistrySnapshot.
        """
        if conn is None:
            with self.store.transaction(True) as transaction:
                return self.save_registry_sync(result, conn=transaction)
        payload = result.get("registry", result)
        preview = self.registry_preview(payload)
        if preview["duplicate_candidates"]:
            raise ApiError("mapping_required", "공식 목록에 수기 등록 기관과 중복 후보가 있습니다. 파일 가져오기로 연결을 선택해 주세요.", 409)
        snapshot = self.registry_commit(preview["id"], {"expected_preview_hash": preview["preview_hash"], "expected_content_revision": preview["content_revision"], "allow_partial": False, "mappings": []}, idempotency_key=new_id())
        if payload["source"]["provider"] == "neis":
            evidence = {"url": "https://open.neis.go.kr/hub", "verified_by_url": "https://open.neis.go.kr/hub/schoolInfo?ATPT_OFCDC_SC_CODE=F10&Type=json", "verified_at": snapshot["retrieved_at"]}
            for row in conn.execute("SELECT institution_id,external_key FROM registry_memberships WHERE snapshot_id=?", (snapshot["id"],)):
                existing = json.loads(_one(conn, "institutions", row[0])["official_channels_json"])
                channels = existing if any(channel["url"] == evidence["url"] and channel["verified_by_url"] == evidence["verified_by_url"] for channel in existing) else existing + [evidence]
                homepage = _one(conn, "institutions", row[0])["homepage_url"]
                if homepage and homepage.startswith(("http://", "https://")) and not any(channel["url"] == homepage for channel in channels):
                    from urllib.parse import urlencode
                    office, code = row["external_key"].split(":", 1)
                    relation_url = "https://open.neis.go.kr/hub/schoolInfo?" + urlencode({"ATPT_OFCDC_SC_CODE": office, "SD_SCHUL_CODE": code, "Type": "json"})
                    channels.append({"url": homepage, "verified_by_url": relation_url, "verified_at": evidence["verified_at"]})
                conn.execute("UPDATE institutions SET official_channels_json=? WHERE id=?", (dumps(channels), row[0]))
        return snapshot

    def reject_observation(self, identity, note):
        """잘못된 근거를 보존하면서 의존 게시값을 즉시 재계산한다.

        Args:
            identity: 관측 UUID.
            note: 잘못된 기관·인용 등의 기각 이유.
        Returns:
            변경된 콘텐츠 revision.
        """
        if not str(note).strip():
            raise ApiError("reason_required", "기각 이유가 필요합니다.")
        with self.store.transaction(True) as conn:
            old = _one(conn, "observations", identity)
            conn.execute("UPDATE observations SET disposition='rejected',reason_codes_json=? WHERE id=?", (dumps(["evidence_invalidated"]), identity))
            if old["event_id"]:
                self._reassess(conn, old["event_id"])
                self._apply_source_blockers(conn, old["event_id"])
                conn.execute("UPDATE events SET revision=revision+1,updated_at=? WHERE id=?", (utcnow(), old["event_id"]))
            _audit(conn, "event" if old["event_id"] else "observation", old["event_id"] or identity, "reject_observation", old, {"note": note}, actor="user", summary=note)
            return self.store.touch(conn, old["season_id"])
