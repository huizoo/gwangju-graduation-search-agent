"""고정 조사 큐와 호출 예산을 SQLite에 보존하는 단일 실행기."""

import asyncio
import contextvars
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import time
from uuid import uuid4

from .domain import ApiError, SCHEMA, validate

TERMINAL = {"completed", "cancelled", "failed"}
USAGE_KEYS = (
    "active_seconds", "http_requests", "search_calls", "document_fetches",
    "llm_calls", "tokens", "reserved_tokens", "replans", "browser_reads",
)


def now() -> str:
    """저장용 UTC 시점을 반환한다.

    Returns:
        시간대가 포함된 ISO 8601 문자열.
    """
    return datetime.now(timezone.utc).isoformat()


def dump(value) -> str:
    """요청 해시와 JSON 저장에 사용하는 결정적인 문자열을 반환한다.

    Args:
        value: JSON으로 직렬화할 값.
    Returns:
        키를 정렬하고 공백을 제거한 JSON 문자열.
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def empty_usage() -> dict:
    """호출마다 새 사용량 객체를 반환한다.

    Returns:
        모든 계량 항목이 0인 독립 사전.
    """
    return dict.fromkeys(USAGE_KEYS, 0)


class BudgetStop(Exception):
    """게시를 강제하지 않고 작업 또는 기관을 중단할 예산 사유."""

    def __init__(self, reason: str):
        """중단 사유를 보관한다.

        Args:
            reason: 예산 소진 또는 사용자 중단 사유 코드.
        """
        self.reason = reason
        super().__init__(reason)


class Meter:
    """한 기관의 호출을 실행 전에 영속 예약한다.

    Args:
        manager: 작업 저장 및 상태 전이를 담당하는 실행기.
        job_id: 부모 작업 식별자.
        item_id: 기관 큐 항목.
        segment_id: 현재 실행 구간.
    """

    def __init__(self, manager, job_id: str, item_id: str, segment_id: str):
        """하나의 기관·구간에 속한 사용량 계량기를 준비한다.

        Args:
            manager: 저장소와 실행 상태를 소유한 작업 관리자.
            job_id: 부모 작업 UUID.
            item_id: 기관 큐 항목 UUID.
            segment_id: 현재 실행 구간 UUID.
        """
        self.manager, self.job_id = manager, job_id
        self.item_id, self.segment_id = item_id, segment_id
        self.started = time.monotonic()

    async def check_active(self) -> float:
        """재시도 대기 중에도 취소·일시정지와 남은 시간 한도를 검사한다.

        Returns:
            기관·구간·전체 작업에 남은 시간의 최솟값(초).
        Raises:
            BudgetStop: 사용자 중단 또는 시간 예산이 소진된 경우.
        """
        with self.manager.store.transaction() as conn:
            job = self.manager._row(conn, self.job_id)
            if job["status"] != "running":
                raise BudgetStop("user_cancel" if job["status"] in {"cancelling", "cancelled"} else "user_pause")
            item = conn.execute("SELECT * FROM job_items WHERE id=?", (self.item_id,)).fetchone()
            segment = conn.execute("SELECT * FROM job_segments WHERE id=?", (self.segment_id,)).fetchone()
            limits = json.loads(job["limits_json"])
            own_active = sum(json.loads(row[0]).get("active_seconds", 0) for row in conn.execute(
                "SELECT reserved_json FROM call_reservations WHERE job_item_id=? AND status='reserved'", (self.item_id,)))
            elapsed = time.monotonic() - self.started
            remaining, reason = min((
                (limits["item_timeout_seconds"] - json.loads(item["usage_json"])["active_seconds"] + own_active - elapsed, "item_timeout"),
                (limits["job_max_active_seconds"] - json.loads(job["usage_json"])["active_seconds"] + own_active - elapsed, "job_budget"),
                (limits["segment_timeout_seconds"] - (datetime.now(timezone.utc) - datetime.fromisoformat(segment["started_at"])).total_seconds(), "segment_budget"),
            ), key=lambda pair: pair[0])
            if remaining <= 0:
                raise BudgetStop(reason)
            return remaining

    async def reserve(self, kind: str, amount: int = 1) -> str:
        """한도를 대조하고 호출 전에 보수적인 사용량을 기록한다.

        Args:
            kind: Usage의 계량 항목 이름.
            amount: 예약할 사용량.
        Returns:
            응답 후 settle에 전달할 예약 UUID.
        Raises:
            BudgetStop: 취소·멈춤·기관/구간/전체 한도에 도달한 경우.
            ValueError: 계량 항목이나 예약량이 유효하지 않은 경우.
        """
        if kind not in USAGE_KEYS or type(amount) is not int or amount < 0:
            raise ValueError("invalid reservation")
        with self.manager.store.transaction(write=True) as conn:
            job = self.manager._row(conn, self.job_id)
            item = conn.execute("SELECT * FROM job_items WHERE id=?", (self.item_id,)).fetchone()
            segment = conn.execute("SELECT * FROM job_segments WHERE id=?", (self.segment_id,)).fetchone()
            if job["status"] != "running":
                raise BudgetStop("user_cancel" if job["status"] in {"cancelling", "cancelled"} else "user_pause")
            limits = json.loads(job["limits_json"])
            if time.monotonic() - self.started >= limits["item_timeout_seconds"]:
                raise BudgetStop("item_timeout")
            segment_elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(segment["started_at"])).total_seconds()
            if segment_elapsed >= limits["segment_timeout_seconds"]:
                raise BudgetStop("segment_budget")
            job_usage, item_usage, segment_usage = (
                json.loads(row["usage_json"]) for row in (job, item, segment)
            )
            own_active = sum(json.loads(reserved[0]).get("active_seconds", 0) for reserved in conn.execute(
                "SELECT reserved_json FROM call_reservations WHERE job_item_id=? AND status='reserved'", (self.item_id,)))
            elapsed = time.monotonic() - self.started
            if item_usage["active_seconds"] - own_active + elapsed >= limits["item_timeout_seconds"]:
                raise BudgetStop("item_timeout")
            for level, usage in (("item", item_usage), ("job", job_usage), ("segment", segment_usage)):
                limit_key = f"{level}_max_{kind}"
                if limit_key in limits and usage[kind] + amount > limits[limit_key]:
                    raise BudgetStop(f"{level}_budget")
            if job_usage["active_seconds"] - own_active + elapsed >= limits["job_max_active_seconds"]:
                raise BudgetStop("job_budget")
            for table, row_id, usage in (
                ("jobs", self.job_id, job_usage), ("job_items", self.item_id, item_usage),
                ("job_segments", self.segment_id, segment_usage),
            ):
                usage[kind] += amount
                if kind == "tokens":
                    usage["reserved_tokens"] += amount
                conn.execute(f"UPDATE {table} SET usage_json=? WHERE id=?", (dump(usage), row_id))
            reservation_id = str(uuid4())
            deadline = datetime.now(timezone.utc) + timedelta(seconds=limits["item_timeout_seconds"])
            conn.execute(
                "INSERT INTO call_reservations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (reservation_id, self.job_id, self.item_id, self.segment_id,
                 "llm" if kind in {"tokens", "llm_calls"} else "http", "reserved",
                 dump({kind: amount}), None, now(), deadline.isoformat(), None),
            )
            return reservation_id

    async def settle(self, reservation_id: str, actual_amount: int | None = None) -> None:
        """성공/알려진 실패를 기록하되 불명확한 비용을 환불하지 않는다.

        Args:
            reservation_id: reserve가 반환한 UUID.
            actual_amount: 확인된 실제 사용량; 초과 사용도 누적한다.
        Raises:
            ValueError: 실제 사용량이 음수이거나 정수가 아닌 경우.
        """
        with self.manager.store.transaction(write=True) as conn:
            row = conn.execute("SELECT * FROM call_reservations WHERE id=?", (reservation_id,)).fetchone()
            if row is None or row["status"] != "reserved" or row["job_item_id"] != self.item_id:
                return
            reserved = json.loads(row["reserved_json"])
            kind, amount = next(iter(reserved.items()))
            if actual_amount is not None and (type(actual_amount) is not int or actual_amount < 0):
                raise ValueError("invalid actual usage")
            if kind == "tokens" and actual_amount is None:
                # 사용량 없는 응답을 실제 토큰 계측값으로 기록하지 않는다.
                conn.execute(
                    "UPDATE call_reservations SET status='charged_unknown',finished_at=? WHERE id=?",
                    (now(), reservation_id),
                )
                return
            # 응답에서 확인한 실제 비용만 환불한다. 응답 유실·사용량 미상은 예약분을 유지한다.
            actual = amount if actual_amount is None else actual_amount
            for table, row_id in (("jobs", self.job_id), ("job_items", self.item_id), ("job_segments", self.segment_id)):
                usage = json.loads(conn.execute(f"SELECT usage_json FROM {table} WHERE id=?", (row_id,)).fetchone()[0])
                usage[kind] += actual - amount
                if kind == "tokens":
                    usage["reserved_tokens"] = max(0, usage["reserved_tokens"] - amount)
                conn.execute(f"UPDATE {table} SET usage_json=? WHERE id=?", (dump(usage), row_id))
            conn.execute(
                "UPDATE call_reservations SET status='settled',actual_json=?,finished_at=? WHERE id=?",
                (dump({kind: actual}), now(), reservation_id),
            )

    async def checkpoint(self, result: dict, stage: str = "verify") -> None:
        """모은 근거를 호출 사이에 저장하되 취소된 작업의 늦은 결과는 버린다.

        Args:
            result: 현재까지의 공개 문서와 서버 검증 전 후보.
            stage: JobItem의 현재 단계.
        Raises:
            BudgetStop: 취소되었거나 이미 종료된 작업인 경우.
            ApiError: 저장할 근거·후보가 도메인 계약에 맞지 않는 경우.
        """
        with self.manager.store.transaction(write=True) as conn:
            job = self.manager._row(conn, self.job_id)
            if job["status"] in TERMINAL | {"cancelling"}:
                raise BudgetStop("user_cancel")
            item = conn.execute("SELECT * FROM job_items WHERE id=?", (self.item_id,)).fetchone()
            if job["kind"] == "schedule_scan":
                snapshot = json.loads(job["scope_snapshot_json"])
                self.manager._assert_scope(conn, snapshot, self.job_id)
                self.manager.service.save_research(item["institution_id"], job["season_id"],
                    {**result, "season_snapshot": snapshot["season"]}, conn=conn)
            history = json.loads(item["history_json"] or "[]") + [{"at": now(), "stage": stage, "trace": result.get("trace", []),
                        "document_versions": [{"source_key": document["source_key"], "content_hash": document["content_hash"]}
                                              for document in result.get("documents", [])],
                        "agent_trace": result.get("agent_trace", []), "stop_reason": result.get("stop_reason"),
                        "coverage": result.get("coverage", {}), "errors": result.get("errors", []),
                        "discovery_mode": result.get("discovery_mode", "blind_window")}]
            conn.execute("UPDATE job_items SET stage=?,history_json=? WHERE id=?", (stage, dump(history), self.item_id))


class JobManager:
    """한 번 확정한 전체 기관 큐를 제한된 동시성으로 처리한다."""

    def __init__(self, service, settings, runner_factory=None):
        """저장 서비스와 지연 생성할 조사 실행기를 연결한다.

        Args:
            service: 기관·행사와 근거를 저장하는 도메인 서비스.
            settings: 현재 실행 설정과 메모리 전용 비밀.
            runner_factory: 검사에 주입할 조사 실행기 생성자. 생략하면 실제 실행기를 쓴다.
        """
        self.service, self.store, self.settings = service, service.store, settings
        self.runner_factory = runner_factory
        self.tasks = {}
        self.stopping = False
        self.active_changed = asyncio.Event()
        from .tracing import TraceRecorder
        self.tracing = TraceRecorder(settings)

    def _row(self, conn, job_id):
        """작업을 읽고 없는 식별자를 구분한다."""
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ApiError("not_found", "조사 작업을 찾을 수 없습니다.", 404)
        return row

    def _job(self, conn, row):
        """DB 작업·구간·항목을 Job 계약으로 투영한다."""
        counts = dict.fromkeys(("pending", "running", "done", "failed", "cancelled"), 0)
        for item in conn.execute("SELECT status,count(*) n FROM job_items WHERE job_id=? GROUP BY status", (row["id"],)):
            counts[item["status"]] = item["n"]
        segment = conn.execute("SELECT * FROM job_segments WHERE job_id=? ORDER BY segment_number DESC LIMIT 1", (row["id"],)).fetchone()
        result = {k: row[k] for k in ("id", "kind", "season_id", "status", "paused_reason", "target_count", "created_at", "updated_at", "revision")}
        result.update(
            scope_snapshot=json.loads(row["scope_snapshot_json"]), counts=counts,
            usage=json.loads(row["usage_json"]), limits=json.loads(row["limits_json"]),
            segment_number=segment["segment_number"] if segment else 0,
            segment_usage=json.loads(segment["usage_json"]) if segment else empty_usage(),
        )
        validate("Job", result)
        return result

    def get(self, job_id: str) -> dict:
        """작업 상태를 현재 저장값으로 반환한다.

        Args:
            job_id: 조회할 작업 UUID.
        Returns:
            누적 사용량과 최신 구간을 포함한 Job.
        Raises:
            ApiError: 작업이 존재하지 않는 경우.
        """
        with self.store.transaction() as conn:
            return self._job(conn, self._row(conn, job_id))

    def list(self) -> list:
        """가장 최근 작업부터 반환한다.

        Returns:
            같은 읽기 snapshot의 Job 목록.
        """
        with self.store.transaction() as conn:
            return [self._job(conn, r) for r in conn.execute("SELECT * FROM jobs ORDER BY created_at DESC,id")]

    def items(self, job_id: str) -> list:
        """고정 큐의 기관별 진행 상태를 반환한다.

        Args:
            job_id: 조회할 작업 UUID.
        Returns:
            최초 큐 순서의 JobItem 목록.
        Raises:
            ApiError: 작업이 존재하지 않는 경우.
        """
        with self.store.transaction() as conn:
            self._row(conn, job_id)
            result = []
            for row in conn.execute("SELECT * FROM job_items WHERE job_id=? ORDER BY rowid", (job_id,)):
                item = {k: row[k] for k in ("id", "job_id", "target_ref", "institution_id", "status", "stage", "outcome", "attempt", "last_attempt_at", "last_successful_source_check_at")}
                item.update(reason_codes=json.loads(row["reason_codes_json"]), usage=json.loads(row["usage_json"]), error=json.loads(row["error_json"]) if row["error_json"] else None)
                # 옛 작업도 이미 저장한 상세 원인을 읽어 화면에서 접근 실패와 미발견을 구분한다.
                history = json.loads(row["history_json"])
                codes = {error.get("code") for entry in history for error in entry.get("errors", [])}
                item["reason_codes"] = sorted(set(item["reason_codes"]) | (codes & set(SCHEMA["$defs"]["ReasonCode"]["enum"])))
                validate("JobItem", item)
                result.append(item)
            return result

    def create(self, payload: dict, key: str) -> dict:
        """기관을 페이지 제한 없이 고정하고 같은 요청의 재전송을 재사용한다.

        Args:
            payload: JobCreate 요청.
            key: 멱등성 키.
        Returns:
            저장한 Job.
        Raises:
            ApiError: 다른 의도의 키 재사용, 동시 활성 작업 또는 조사 대상이 없는 경우.
        """
        validate("JobCreate", payload)
        request_hash = hashlib.sha256(dump(payload).encode()).hexdigest()
        with self.store.transaction(write=True) as conn:
            prior = conn.execute("SELECT * FROM jobs WHERE idempotency_key=?", (key,)).fetchone()
            if prior:
                if prior["request_hash"] != request_hash:
                    raise ApiError("idempotency_conflict", "같은 요청 키에 다른 내용이 있습니다.", 409)
                return self._job(conn, prior)
            active = conn.execute("SELECT id FROM jobs WHERE status IN ('queued','running','pausing','cancelling')").fetchone()
            if active:
                raise ApiError("job_active", "진행 중인 조사를 먼저 멈추거나 취소해 주세요.", 409, {"existing_job_id": active["id"]})
            season = None
            if payload["kind"] == "schedule_scan":
                row = conn.execute("SELECT * FROM seasons WHERE id=?", (payload["season_id"],)).fetchone()
                if row is None:
                    raise ApiError("not_found", "시즌을 찾을 수 없습니다.", 404)
                season = dict(row)
                from .periods import validate_period
                validate_period(season["search_start"], season["search_end"])
                from .scope import eligible_school
                institutions = [self.service._institution(conn, row[0]) for row in conn.execute("SELECT id FROM institutions ORDER BY name,id")]
                if not institutions:
                    raise ApiError("roster_required", "학교 목록이 없어요. 학교 목록을 먼저 불러와 주세요.")
                institutions = [r for r in institutions if eligible_school(r, season)]
                if not institutions:
                    raise ApiError("no_eligible_schools", "이 기간에 조사할 학교가 목록에 없어요. 학교 목록을 다시 불러와 주세요.")
                selection = payload["selection"]
                purpose = payload.get("purpose", "date_count")
                if payload.get("purpose") == "date_count" and selection["scope"] != "all":
                    raise ApiError("scope_invalid", "일정찾기는 전체 학교를 조사합니다. 구와 검색 조건은 결과에 적용해 주세요.")
                if selection["scope"] == "ids":
                    ids = set(selection["institution_ids"])
                    institutions = [r for r in institutions if r["id"] in ids]
                    if len(institutions) != len(ids):
                        raise ApiError("not_found", "선택 기관 일부를 찾을 수 없습니다.", 404)
                elif selection["scope"] == "filter":
                    institutions = [r for r in institutions if (
                        (not selection["types"] or r["type"] in selection["types"])
                        and (not selection["districts"] or r["district"] in selection["districts"])
                        and selection["query"].casefold() in r["name"].casefold()
                    )]
                if purpose == "time":
                    target = conn.execute("SELECT * FROM events WHERE id=?", (payload["target_event_id"],)).fetchone()
                    if (not target or not conn.execute("SELECT 1 FROM event_seasons WHERE season_id=? AND event_id=?", (season["id"], target["id"])).fetchone()
                            or len(institutions) != 1 or target["institution_id"] != institutions[0]["id"]):
                        raise ApiError("event_match_required", "시간을 찾을 학교와 해당 시즌의 행사를 확인해 주세요.")
                    if conn.execute("SELECT 1 FROM event_aliases WHERE old_id=?", (target["id"],)).fetchone():
                        raise ApiError("event_match_required", "연결된 현재 행사에서 시간 찾기를 눌러 주세요.")
                    if not self.service._project(conn, target["id"], season_id=season["id"])[0]["publication"]["current_date_allowed"]:
                        raise ApiError("event_match_required", "일정찾기로 졸업식 날짜를 먼저 확인해 주세요.")
                # 종료일 미상 기관은 남기고, 시즌 시작 전 공식 종료만 큐에서 제외한다.
                institutions = [r for r in institutions if not (
                    r["operation_status"] == "closed_confirmed" and r["operation_effective_date"]
                    and r["operation_effective_date"] <= season["search_start"]
                )]
                if payload["mode"] != "recheck" and "purpose" not in payload:
                    events = self.service._events(conn, season["id"])
                    if payload["mode"] == "initial":
                        attempted = {e["institution"]["id"] for e in events}
                        attempted.update(r[0] for r in conn.execute(
                            "SELECT DISTINCT ji.institution_id FROM job_items ji JOIN jobs j ON j.id=ji.job_id WHERE j.season_id=? AND ji.last_attempt_at IS NOT NULL", (season["id"],)))
                        institutions = [r for r in institutions if r["id"] not in attempted]
                    else:
                        unresolved = {item["institution"]["id"] for item in self.service._review(conn, season, events)}
                        institutions = [r for r in institutions if r["id"] in unresolved]
                targets = [(r["id"], r["id"]) for r in institutions]
                # 빈 큐를 완료로 저장하면 조사하지 않은 시즌을 조사 완료로 오인하게 된다.
                if not targets:
                    raise ApiError("no_eligible_schools", "선택한 조건에 맞는 조사 대상 학교가 없어요. 조사 조건을 확인해 주세요.")
            else:
                targets = [(provider, None) for provider in payload["providers"]]
            job_id = str(uuid4())
            limits = payload["limits"] or deepcopy(self.settings.data["execution"])
            from .scope import RESEARCH_VERSION
            snapshot = {"season": season, "source_config_hash": self.settings.source_config_hash,
                        "purpose": payload.get("purpose", "date_count"),
                        "target_event_id": payload.get("target_event_id"), "research_version": RESEARCH_VERSION}
            snapshot["target_event_date"] = (self.service._project(conn, payload["target_event_id"], season_id=season["id"])[0]["fields"]["event_date"]["effective_value"]
                                              if payload.get("purpose") == "time" else None)
            conn.execute(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, payload["kind"], season["id"] if season else None, "queued", None,
                 dump(payload), dump(snapshot), dump(limits), dump(empty_usage()), len(targets),
                 key, request_hash, 1, now(), now()),
            )
            for target, institution_id in targets:
                conn.execute(
                    "INSERT INTO job_items(id,job_id,target_ref,institution_id,status,stage,usage_json) VALUES(?,?,?,?,?,?,?)",
                    (str(uuid4()), job_id, target, institution_id, "pending", "idle", dump(empty_usage())),
                )
            return self._job(conn, self._row(conn, job_id))

    def _assert_scope(self, conn, snapshot, job_id=None):
        """실행·재개·저장 직전에 고정 기간과 대상 행사일을 대조한다.

        Args:
            conn: 작업 상태와 공유하는 거래.
            snapshot: 작업 시작 시 고정한 범위.
            job_id: 원문 재검토를 계속하는 기존 작업 UUID.
        Raises:
            ApiError: 기간·행사일이 바뀌거나 새 원문 재검토 이외의 보류 사유가 있는 경우.
        """
        frozen = snapshot.get("season")
        if not frozen:
            return
        current = self.service._season(conn, frozen["id"])
        if any(current.get(key) != frozen.get(key) for key in ("id", "search_start", "search_end", "revision")):
            raise ApiError("season_changed", "조사 기간이 작업 시작 당시와 다릅니다. 새 조사를 시작해 주세요.", 409)
        if snapshot.get("purpose") == "time":
            event = self.service._project(conn, snapshot["target_event_id"], season_id=frozen["id"])[0]
            if (event["fields"]["event_date"]["effective_value"] != snapshot.get("target_event_date")
                    or (not event["publication"]["current_date_allowed"]
                        and not self._pending_source_review(conn, event, job_id))):
                raise ApiError("event_date_changed", "졸업식 날짜가 바뀌었습니다. 현재 행사에서 시간을 다시 찾아 주세요.", 409)

    def _pending_source_review(self, conn, event, job_id):
        """정상 취득한 새 본문이 모델 재검토를 기다리는 동안 같은 작업만 이어간다.

        Args:
            conn: 원문·행사와 공유하는 읽기 연결.
            event: 고정 행사일과 값이 같은 현재 투영.
            job_id: 새 원문을 직접 저장한 작업 UUID.
        Returns:
            날짜 근거의 보류 이유가 새 공식 본문의 미검토뿐인지 여부.
            반환값은 게시 권한을 바꾸지 않으며 접근 실패·취소·충돌은 허용하지 않는다.
        """
        if not job_id:
            return False
        own_versions = set()
        for row in conn.execute("SELECT history_json FROM job_items WHERE job_id=? AND institution_id=?",
                                (job_id, event["institution"]["id"])):
            for entry in json.loads(row[0]):
                own_versions.update((version["source_key"], version["content_hash"])
                                    for version in entry.get("document_versions", []))
        field = event["fields"]["event_date"]
        permission = event["publication"]["field_permissions"]["event_date"]
        if (field["effective_basis"] != "official"
                or set(permission["reason_codes"]) != {"recheck_failed"}):
            return False
        documents = self.service._documents(conn, event["institution"]["id"])
        supports = field["automatic"].get("value_check", {}).get("document_ids", [])
        if not supports:
            return False
        from .publication import _authority
        for identity in supports:
            old = documents.get(identity)
            if not old or set(old.get("recheck", {}).get("reason_codes", [])) != {"semantic_review_required"}:
                return False
            versions = [document for document in documents.values() if document["source_key"] == old["source_key"]]
            latest = max(versions, key=lambda document: (document["retrieved_at"], document["id"]))
            if ((latest["source_key"], latest["content_hash"]) not in own_versions
                    or latest["content_hash"] == old["content_hash"] or latest["status"] != "ok"
                    or latest.get("recheck", {}).get("status") != "succeeded"
                    or latest["reading"]["content_match"] != "expected"
                    or latest["reading"]["scope"] not in {"full_text", "rendered_dom"}
                    or not _authority(latest, event["institution"], datetime.now(timezone.utc))):
                return False
        return True

    def recover(self) -> None:
        """재시작 시 취소 의도를 우선하고 불명확한 호출 비용을 유지한다."""
        from .scope import RESEARCH_VERSION
        with self.store.transaction(write=True) as conn:
            conn.execute("UPDATE call_reservations SET status='charged_unknown',finished_at=? WHERE status='reserved'", (now(),))
            for row in conn.execute("SELECT * FROM jobs WHERE status IN ('running','pausing','cancelling','queued')").fetchall():
                if row["status"] == "cancelling":
                    conn.execute("UPDATE job_items SET status='cancelled' WHERE job_id=? AND status IN ('pending','running')", (row["id"],))
                    status, reason = "cancelled", None
                else:
                    conn.execute("UPDATE job_items SET status='pending',stage='idle' WHERE job_id=? AND status='running'", (row["id"],))
                    remaining = conn.execute("SELECT count(*) FROM job_items WHERE job_id=? AND status='pending'", (row["id"],)).fetchone()[0]
                    status, reason = ("paused", "interrupted") if remaining else ("completed", None)
                conn.execute("UPDATE jobs SET status=?,paused_reason=?,revision=revision+1,updated_at=? WHERE id=?", (status, reason, now(), row["id"]))
                conn.execute("UPDATE job_segments SET ended_at=? WHERE job_id=? AND ended_at IS NULL", (now(), row["id"]))
            # 이전 조사 계약에는 자료 학년도·고정 행사일 조건이 없어 그대로
            # 재개할 수 없다. 화면에서도 불가능한 재개 대신 새 조사를 안내한다.
            for row in conn.execute("SELECT id,scope_snapshot_json FROM jobs WHERE status='paused' AND paused_reason IS NOT 'configuration_required'").fetchall():
                if json.loads(row["scope_snapshot_json"]).get("research_version") != RESEARCH_VERSION:
                    conn.execute("UPDATE jobs SET paused_reason='configuration_required',revision=revision+1,updated_at=? WHERE id=?",
                                 (now(), row["id"]))

    def pause(self, job_id: str) -> dict:
        """다음 호출 전 멈추도록 의도를 먼저 영속화한다.

        Args:
            job_id: 잠시 멈출 작업 UUID.
        Returns:
            멈춤 의도가 반영된 현재 Job.
        Raises:
            ApiError: 작업이 존재하지 않는 경우.
        """
        with self.store.transaction(write=True) as conn:
            row = self._row(conn, job_id)
            if row["status"] in {"queued", "running"}:
                conn.execute("UPDATE jobs SET status='pausing',paused_reason='user_pause',revision=revision+1,updated_at=? WHERE id=?", (now(), job_id))
        self.active_changed.set()
        if job_id not in self.tasks:
            self._finish(job_id)
        return self.get(job_id)

    def cancel(self, job_id: str) -> dict:
        """늦은 응답과 재시작에도 유지할 취소 의도를 기록한다.

        Args:
            job_id: 취소할 작업 UUID.
        Returns:
            취소 의도가 반영된 현재 Job. 이미 종료된 작업은 유지한다.
        Raises:
            ApiError: 작업이 존재하지 않는 경우.
        """
        with self.store.transaction(write=True) as conn:
            row = self._row(conn, job_id)
            if row["status"] not in TERMINAL:
                conn.execute("UPDATE jobs SET status='cancelling',paused_reason='user_cancel',revision=revision+1,updated_at=? WHERE id=?", (now(), job_id))
                conn.execute("UPDATE job_items SET status='cancelled' WHERE job_id=? AND status='pending'", (job_id,))
        self.active_changed.set()
        if job_id not in self.tasks:
            self._finish(job_id)
        return self.get(job_id)

    def resume(self, job_id: str, payload: dict) -> dict:
        """설정·revision을 검사한 뒤 재개를 준비하고 누적 비용은 유지한다.

        Args:
            job_id: 이어갈 작업 UUID.
            payload: 예상 revision과 선택적 한도 상향을 담은 JobResume.
        Returns:
            새 구간 실행을 기다리는 queued 상태의 Job.
        Raises:
            ApiError: 요청·revision·설정이 다르거나 상태·누적 한도가 재개를 막는 경우.
        """
        validate("JobResume", payload)
        with self.store.transaction(write=True) as conn:
            row = self._row(conn, job_id)
            if row["revision"] != payload["expected_revision"]:
                raise ApiError("revision_conflict", "작업 상태가 변경되었습니다.", 409, {"current_revision": row["revision"]})
            if row["status"] != "paused":
                raise ApiError("job_not_resumable", "잠시 멈춘 작업만 이어갈 수 있습니다.", 409)
            active = conn.execute(
                "SELECT id FROM jobs WHERE id<>? AND status IN ('queued','running','pausing','cancelling')",
                (job_id,),
            ).fetchone()
            if active:
                raise ApiError("job_active", "진행 중인 조사를 먼저 멈추거나 취소해 주세요.", 409,
                               {"existing_job_id": active["id"]})
            if json.loads(row["scope_snapshot_json"])["source_config_hash"] != self.settings.source_config_hash:
                raise ApiError("settings_changed", "설정이 바뀌었습니다. 이전 설정을 복원하거나 새 조사를 시작해 주세요.", 409, {"reason_codes": ["configuration_required"]})
            from .scope import RESEARCH_VERSION
            if json.loads(row["scope_snapshot_json"]).get("research_version") != RESEARCH_VERSION:
                raise ApiError("settings_changed", "조사 방식이 변경되었습니다. 기록을 유지하고 새 일정찾기를 시작해 주세요.", 409)
            self._assert_scope(conn, json.loads(row["scope_snapshot_json"]), job_id)
            limits = json.loads(row["limits_json"])
            for name, value in (payload["increase_limits"] or {}).items():
                if value < limits[name]:
                    raise ApiError("limit_decreased", "누적 한도는 늘릴 수만 있습니다.", 422)
                limits[name] = value
            usage = json.loads(row["usage_json"])
            if any(
                usage[k] >= value for name, value in limits.items()
                if name.startswith("job_max_") and (k := name.removeprefix("job_max_")) in usage
            ):
                raise ApiError("job_budget", "전체 누적 한도를 늘린 뒤 이어갈 수 있습니다.", 409, {"reason_codes": ["job_budget"]})
            conn.execute("UPDATE jobs SET status='queued',paused_reason=NULL,limits_json=?,revision=revision+1,updated_at=? WHERE id=?", (dump(limits), now(), job_id))
        return self.get(job_id)

    def start(self, job_id: str) -> None:
        """동일 작업의 실행 코루틴을 하나만 시작한다.

        Args:
            job_id: 실행 대기 중인 작업 UUID.
        Raises:
            RuntimeError: 실행 중인 asyncio 이벤트 루프가 없는 경우.
        """
        if job_id not in self.tasks and not self.stopping:
            # 요청의 중첩 DB 연결을 새 작업에 상속하면 commit 뒤 닫힌 연결을 재사용하게 된다.
            task = asyncio.create_task(self.run(job_id), context=contextvars.Context())
            self.tasks[job_id] = task
            task.add_done_callback(lambda _: self.tasks.pop(job_id, None))

    def _finish(self, job_id: str) -> None:
        """취소 우선순위와 남은 항목에 따라 부모 상태를 정리한다."""
        with self.store.transaction(write=True) as conn:
            row = self._row(conn, job_id)
            if row["status"] in TERMINAL:
                return
            if row["status"] == "cancelling":
                conn.execute("UPDATE job_items SET status='cancelled' WHERE job_id=? AND status IN ('pending','running')", (job_id,))
                status, reason = "cancelled", None
            else:
                remaining = conn.execute("SELECT count(*) FROM job_items WHERE job_id=? AND status IN ('pending','running')", (job_id,)).fetchone()[0]
                status, reason = ("paused", row["paused_reason"] or "interrupted") if remaining else ("completed", None)
            conn.execute("UPDATE jobs SET status=?,paused_reason=?,revision=revision+1,updated_at=? WHERE id=?", (status, reason, now(), job_id))
            conn.execute("UPDATE job_segments SET ended_at=? WHERE job_id=? AND ended_at IS NULL", (now(), job_id))

    async def run(self, job_id: str) -> None:
        """기존 작업 실행을 선택적 진단 구간으로 감싼다.

        Args:
            job_id: 실행할 작업 UUID.
        """
        job = self.get(job_id)
        snapshot = job["scope_snapshot"]
        season = snapshot.get("season") or {}
        with self.tracing.span("job", purpose=snapshot.get("purpose", "registry_sync"),
                               search_start=season.get("search_start"), search_end=season.get("search_end")) as trace:
            try:
                await self._execute_job(job_id)
            finally:
                current = self.get(job_id)
                trace.update(status=current["status"], target_count=current["target_count"],
                             completed_count=current["counts"]["done"], failed_count=current["counts"]["failed"],
                             http_requests=current["usage"]["http_requests"], llm_calls=current["usage"]["llm_calls"])

    async def _execute_job(self, job_id: str) -> None:
        """고정 큐를 처리하고 중단되더라도 상태를 영속 정리한다.

        Args:
            job_id: 실행할 작업 UUID. queued 상태가 아니면 실행하지 않는다.
        Raises:
            ApiError: 작업이 존재하지 않는 경우.
        """
        with self.store.transaction(write=True) as conn:
            row = self._row(conn, job_id)
            if row["status"] != "queued":
                return
            if json.loads(row["scope_snapshot_json"])["source_config_hash"] != self.settings.source_config_hash:
                conn.execute("UPDATE jobs SET status='paused',paused_reason='configuration_required' WHERE id=?", (job_id,))
                return
            from .scope import RESEARCH_VERSION
            if json.loads(row["scope_snapshot_json"]).get("research_version") != RESEARCH_VERSION:
                conn.execute("UPDATE jobs SET status='paused',paused_reason='configuration_required' WHERE id=?", (job_id,))
                return
            try:
                self._assert_scope(conn, json.loads(row["scope_snapshot_json"]), job_id)
            except ApiError:
                conn.execute("UPDATE jobs SET status='paused',paused_reason='configuration_required' WHERE id=?", (job_id,))
                return
            number = conn.execute("SELECT coalesce(max(segment_number),0)+1 FROM job_segments WHERE job_id=?", (job_id,)).fetchone()[0]
            segment_id = str(uuid4())
            conn.execute("INSERT INTO job_segments VALUES(?,?,?,?,?,?,?)", (segment_id, job_id, number, now(), None, now(), dump(empty_usage())))
            conn.execute("UPDATE jobs SET status='running',revision=revision+1,updated_at=? WHERE id=?", (now(), job_id))
            limits = json.loads(row["limits_json"])
        semaphore = asyncio.Semaphore(limits["concurrency"])

        async def bounded(item):
            """동시에 실행하는 기관 수만 제한하고 전체 큐는 보존한다."""
            async with semaphore:
                try:
                    await self._run_item(job_id, item["id"], segment_id)
                except Exception as exc:
                    # 한 기관의 저장/계약 오류도 숨기지 않고 격리해 다른 기관 큐를 계속한다.
                    with self.store.transaction(write=True) as conn:
                        row = self._row(conn, job_id)
                        cancelled = row["status"] in TERMINAL | {"cancelling"}
                        error = {"code": type(exc).__name__, "message": "기관 결과 저장 검증에 실패했습니다.", "retryable": False, "details": {"reason_codes": ["parse_failed"]}}
                        conn.execute("UPDATE job_items SET status=?,outcome='error',error_json=?,reason_codes_json=? WHERE id=?", ("cancelled" if cancelled else "failed", dump(error), dump(["parse_failed"]), item["id"]))
                        conn.execute("UPDATE call_reservations SET status='charged_unknown',finished_at=? WHERE job_item_id=? AND status='reserved'", (now(), item["id"]))
                    self.active_changed.set()

        try:
            pending = [item for item in self.items(job_id) if item["status"] == "pending"]
            await asyncio.gather(*(bounded(item) for item in pending))
        finally:
            self._finish(job_id)

    async def _claim_active_time(self, job_id, item_id, segment_id):
        """동시 실행까지 포함한 남은 시간을 선예약한다.

        Args:
            job_id: 부모 작업 UUID.
            item_id: 대기 기관 UUID.
            segment_id: 현재 실행 구간 UUID.
        Returns:
            실행 입력과 제한시간·예약 ID. 중단 의도가 있으면 None.
        """
        while True:
            self.active_changed.clear()
            waiting = False
            with self.store.transaction(write=True) as conn:
                job = dict(self._row(conn, job_id))
                if job["status"] != "running":
                    return None
                item = dict(conn.execute("SELECT * FROM job_items WHERE id=?", (item_id,)).fetchone())
                if item["status"] != "pending":
                    return None
                snapshot, limits = json.loads(job["scope_snapshot_json"]), json.loads(job["limits_json"])
                job_usage, item_usage = json.loads(job["usage_json"]), json.loads(item["usage_json"])
                segment = conn.execute("SELECT * FROM job_segments WHERE id=?", (segment_id,)).fetchone()
                segment_remaining = limits["segment_timeout_seconds"] - (datetime.now(timezone.utc) - datetime.fromisoformat(segment["started_at"])).total_seconds()
                job_remaining = limits["job_max_active_seconds"] - job_usage["active_seconds"]
                item_remaining = limits["item_timeout_seconds"] - item_usage["active_seconds"]
                reservations = conn.execute("SELECT reserved_json FROM call_reservations WHERE job_id=? AND status='reserved'", (job_id,)).fetchall()
                active_elsewhere = any(json.loads(row[0]).get("active_seconds", 0) for row in reservations)
                if job_remaining <= 0 and active_elsewhere and segment_remaining > 0 and item_remaining > 0:
                    # 다른 기관이 잠시 예약한 예산은 실제 소진과 구분한다.
                    waiting = True
                else:
                    remaining, reason = min(((job_remaining, "job_budget"), (segment_remaining, "segment_budget"), (item_remaining, "item_timeout")), key=lambda pair: pair[0])
                    conn.execute("UPDATE job_items SET status='running',stage=?,attempt=attempt+1,last_attempt_at=? WHERE id=?", ("registry" if job["kind"] == "registry_sync" else "calendar", now(), item_id))
                    reservation_id = None
                    if remaining > 0:
                        amount = math.ceil(remaining)
                        reservation_id = str(uuid4())
                        for table, row_id, usage in (("jobs", job_id, job_usage), ("job_items", item_id, item_usage), ("job_segments", segment_id, json.loads(segment["usage_json"]))):
                            usage["active_seconds"] += amount
                            conn.execute(f"UPDATE {table} SET usage_json=? WHERE id=?", (dump(usage), row_id))
                        deadline = (datetime.now(timezone.utc) + timedelta(seconds=remaining)).isoformat()
                        conn.execute("INSERT INTO call_reservations VALUES(?,?,?,?,?,?,?,?,?,?,?)", (reservation_id, job_id, item_id, segment_id, "http", "reserved", dump({"active_seconds": amount}), None, now(), deadline, None))
                    return job, item, snapshot, limits, remaining, reason, reservation_id
            if waiting:
                # 잔여 구간시간에도 깨어나야 다른 기관이 정산되지 않아 무한 대기하지 않는다.
                try:
                    await asyncio.wait_for(self.active_changed.wait(), max(0.01, segment_remaining))
                except asyncio.TimeoutError:
                    pass

    def _settle_active_time(self, conn, reservation_id, elapsed):
        """초 단위 실제 실행 합으로 시간 예약의 차액을 정산한다.

        Args:
            conn: 결과 저장과 분리된 쓰기 연결.
            reservation_id: 실행 시작 전의 시간 예약 UUID.
            elapsed: 종료 시 측정한 실행 초의 올림값.
        """
        if reservation_id is None:
            return
        row = conn.execute("SELECT * FROM call_reservations WHERE id=?", (reservation_id,)).fetchone()
        if row is None or row["status"] != "reserved":
            return
        amount = json.loads(row["reserved_json"])["active_seconds"]
        for table, row_id in (("jobs", row["job_id"]), ("job_items", row["job_item_id"]), ("job_segments", row["segment_id"])):
            usage = json.loads(conn.execute(f"SELECT usage_json FROM {table} WHERE id=?", (row_id,)).fetchone()[0])
            usage["active_seconds"] += elapsed - amount
            conn.execute(f"UPDATE {table} SET usage_json=? WHERE id=?", (dump(usage), row_id))
        conn.execute("UPDATE call_reservations SET status='settled',actual_json=?,finished_at=? WHERE id=?", (dump({"active_seconds": elapsed}), now(), reservation_id))

    async def _run_item(self, job_id: str, item_id: str, segment_id: str) -> None:
        """학교 구간은 후보 생성뿐 아니라 최종 저장 결과까지 포함한다."""
        with self.store.transaction() as conn:
            item = conn.execute("SELECT * FROM job_items WHERE id=?", (item_id,)).fetchone()
            institution = self.service._institution(conn, item["institution_id"]) if item["institution_id"] else {}
        with self.tracing.span("school", school_name=institution.get("name"),
                               institution_type=institution.get("type")) as trace:
            try:
                await self._execute_item(job_id, item_id, segment_id)
            finally:
                with self.store.transaction() as conn:
                    item = conn.execute("SELECT * FROM job_items WHERE id=?", (item_id,)).fetchone()
                    trace.update(status=item["outcome"] or item["status"],
                                 reason_codes=json.loads(item["reason_codes_json"] or "[]"))

    async def _execute_item(self, job_id: str, item_id: str, segment_id: str) -> None:
        """기관 결과를 저장하기 직전에 최신 취소 상태를 다시 확인한다."""
        claimed = await self._claim_active_time(job_id, item_id, segment_id)
        if claimed is None:
            return
        job, item, snapshot, limits, remaining, deadline_reason, active_reservation = claimed
        meter = Meter(self, job_id, item_id, segment_id)
        started = time.monotonic()
        result, error, reason, outcome = None, None, None, "needs_review"
        try:
            if remaining <= 0:
                raise BudgetStop(deadline_reason)
            from .config import Settings
            job_config = deepcopy(self.settings.data)
            job_config["execution"] = limits
            job_settings = Settings(job_config, self.settings.secrets, self.settings.root)
            if job["kind"] == "registry_sync":
                from .registry import sync_registry
                result = await asyncio.wait_for(sync_registry(job_settings, item["target_ref"], meter=meter), max(0, remaining - (time.monotonic() - started)))
            else:
                from .research import ResearchRunner
                runner = (self.runner_factory or ResearchRunner)(job_settings, meter=meter)
                with self.store.transaction() as conn:
                    institution = self.service._institution(conn, item["institution_id"])
                    documents = self.service._documents(conn, item["institution_id"])
                    # 원문 취득 시즌과 행사 표시 시즌은 다르다. 정본 행사에 연결된
                    # 공개 문서만 재사용하고 수기 메모·대화 원문은 전달하지 않는다.
                    linked = {row[0] for row in conn.execute(
                        "SELECT o.document_id FROM observations o WHERE o.institution_id=? AND o.disposition!='rejected' "
                        "AND (o.season_id=? OR EXISTS(SELECT 1 FROM event_seasons es "
                        "WHERE es.event_id=o.event_id AND es.season_id=?)) "
                        "UNION SELECT m.source_document_id FROM manual_overrides m JOIN events e ON e.id=m.event_id "
                        "JOIN event_seasons es ON es.event_id=e.id "
                        "WHERE e.institution_id=? AND es.season_id=? AND m.active=1 AND m.source_document_id IS NOT NULL",
                        (item["institution_id"], snapshot["season"]["id"], snapshot["season"]["id"],
                         item["institution_id"], snapshot["season"]["id"]))}
                    linked.update(row[0] for row in conn.execute(
                        "SELECT DISTINCT d.id FROM manual_overrides m JOIN events e ON e.id=m.event_id "
                        "JOIN event_seasons es ON es.event_id=e.id "
                        "JOIN documents d ON d.institution_id=e.institution_id AND d.display_url=m.source_url "
                        "WHERE e.institution_id=? AND es.season_id=? AND m.active=1",
                        (item["institution_id"], snapshot["season"]["id"])))
                    source_keys = {documents[key]["source_key"] for key in linked if key in documents}
                    latest_sources = {}
                    for document in documents.values():
                        if document["source_key"] in source_keys and document["source_kind"] != "official_api":
                            current = latest_sources.get(document["source_key"])
                            if current is None or document["retrieved_at"] > current["retrieved_at"]:
                                latest_sources[document["source_key"]] = document
                    institution["known_documents"] = [
                        {"display_url": document["display_url"], "url": document["display_url"], "source_key": document["source_key"], "status": document["status"]}
                        for document in latest_sources.values()
                    ]
                    identifiers = [dict(r) for r in conn.execute("SELECT provider,external_key FROM institution_identifiers WHERE institution_id=?", (item["institution_id"],))]
                    from .research_records import reusable_routes
                    routes = reusable_routes(conn, institution["id"], snapshot.get("purpose", "date_count"))
                    target_event = None
                    if snapshot.get("target_event_id"):
                        projection = self.service._project(conn, snapshot["target_event_id"], season_id=snapshot["season"]["id"])[0]
                        target_event = {k: projection.get(k) for k in ("id", "title", "event_type", "audience", "campus_label", "academic_year", "ceremony_term")}
                        target_event["event_date"] = deepcopy(snapshot["target_event_date"])
                    evidence = {"documents": list(latest_sources.values()), "verified_values": {}}
                import inspect
                arguments = {"identifiers": identifiers}
                # 오래된 격리 실행기도 검증에 사용할 수 있으나 실제 실행기는 확장 계약을 받는다.
                if "purpose" in inspect.signature(runner.run).parameters:
                    arguments.update(purpose=snapshot.get("purpose", "date_count"), target_event=target_event,
                                     routes=routes, existing_evidence=evidence)
                result = await asyncio.wait_for(runner.run(institution, snapshot["season"], **arguments), max(0, remaining - (time.monotonic() - started)))
                result.update(purpose=snapshot.get("purpose", "date_count"), target_event_id=snapshot.get("target_event_id"))
        except BudgetStop as exc:
            reason, outcome = exc.reason, "limit_reached"
        except asyncio.TimeoutError:
            reason, outcome = deadline_reason, "limit_reached"
        except asyncio.CancelledError:
            reason, outcome = "interrupted", "blocked"
        except Exception as exc:
            # 공급자 예외 문자열에는 인증 URL이 포함될 수 있으므로 타입과 고정 안내만 저장한다.
            from .providers import ProviderError
            reason, outcome = (exc.code if isinstance(exc, ProviderError) and exc.code in {"configuration_required", "external_transfer_blocked"} else "provider_error"), "error"
            error = {"code": type(exc).__name__, "message": "자료원 설정 또는 호출 결과를 확인해 주세요.", "retryable": True, "details": {"reason_codes": [reason]}}
        measured = time.monotonic() - started
        if result is not None and measured > remaining:
            # 취소를 삼킨 adapter의 늦은 완료도 기한 내 정상 완료로 인정하지 않는다.
            result, reason, outcome = None, deadline_reason, "limit_reached"
        elapsed = max(1, math.ceil(measured)) if active_reservation else 0
        # 시간 정산은 결과 저장 오류와 독립적으로 commit한다. crash는 선차감한
        # 상한을 유지하며, 정상 종료에서만 실제 측정한 비용으로 차액을 조정한다.
        with self.store.transaction(write=True) as conn:
            self._settle_active_time(conn, active_reservation, elapsed)
            conn.execute("UPDATE call_reservations SET status='charged_unknown',finished_at=? WHERE job_item_id=? AND status='reserved'", (now(), item_id))
        self.active_changed.set()
        with self.store.transaction(write=True) as conn:
            current = self._row(conn, job_id)
            cancelled = current["status"] in TERMINAL | {"cancelling"}
            if reason in {"segment_budget", "job_budget"} and not cancelled:
                conn.execute("UPDATE jobs SET status='pausing',paused_reason=? WHERE id=?", (reason, job_id))
            # 저장은 같은 트랜잭션의 취소 검사 뒤 수행해야 취소 후 늦은 응답을 반영하지 않는다.
            if result is not None and not cancelled and job["kind"] == "schedule_scan":
                try:
                    self._assert_scope(conn, snapshot, job_id)
                except ApiError:
                    result, reason, outcome = None, "configuration_required", "blocked"
                    error = {"code": "scope_changed", "message": "작업 시작 당시의 기간이나 행사일이 바뀌어 결과를 반영하지 않았습니다.",
                             "retryable": False, "details": {"reason_codes": ["configuration_required"]}}
            if result is not None and not cancelled:
                if job["kind"] == "registry_sync":
                    self.service.save_registry_sync(result, conn=conn)
                    outcome = "verified"
                else:
                    self._assert_scope(conn, snapshot, job_id)
                    saved = self.service.save_research(item["institution_id"], snapshot["season"]["id"],
                        {**result, "season_snapshot": snapshot["season"]}, conn=conn)
                    if result.get("errors"):
                        reason = "provider_error"
                    from .research_records import current_counts
                    projections = [self.service._project(conn, event_id, season_id=snapshot["season"]["id"])[0] for event_id in saved["event_ids"]]
                    counts = current_counts(self.service, conn, snapshot["season"], projections)
                    if snapshot.get("purpose") == "time":
                        ready = any(e["id"] == snapshot.get("target_event_id") and e["publication"]["current_time_allowed"] for e in projections)
                    else:
                        relevant = [e for e in projections if e["publication"]["current_date_allowed"]]
                        ready = bool(relevant) and all(e["id"] in counts for e in relevant)
                    outcome = "verified" if ready else ("no_data" if not saved["observation_ids"] and not reason else "needs_review")
            pending = reason in {"segment_budget", "job_budget", "user_pause", "interrupted"}
            status = "cancelled" if cancelled else ("pending" if pending else ("failed" if error else "done"))
            conn.execute(
                "UPDATE job_items SET status=?,stage=?,outcome=?,reason_codes_json=?,error_json=?,last_successful_source_check_at=COALESCE(?,last_successful_source_check_at) WHERE id=?",
                (status, "idle" if pending else "save", outcome, dump([reason] if reason else []), dump(error) if error else None,
                 now() if result and any(document.get("status") == "ok" and document.get("reading", {}).get("content_match") == "expected" for document in result.get("documents", [])) else None, item_id),
            )
            conn.execute("UPDATE call_reservations SET status='charged_unknown',finished_at=? WHERE job_item_id=? AND status='reserved'", (now(), item_id))
            if result:
                history = json.loads(conn.execute("SELECT history_json FROM job_items WHERE id=?", (item_id,)).fetchone()[0] or "[]")
                conn.execute("UPDATE job_items SET history_json=? WHERE id=?", (dump(history + [{
                    "at": now(), "trace": result.get("trace", []), "coverage": result.get("coverage", {}),
                    "agent_trace": result.get("agent_trace", []), "stop_reason": result.get("stop_reason"),
                    "errors": result.get("errors", []), "discovery_mode": result.get("discovery_mode"),
                }]), item_id))
            conn.execute("UPDATE jobs SET updated_at=?,revision=revision+1 WHERE id=?", (now(), job_id))

    async def shutdown(self) -> None:
        """실행 중 작업을 checkpoint 상태로 정리하고 코루틴을 회수한다."""
        self.stopping = True
        tasks = list(self.tasks.items())
        for job_id, task in tasks:
            self.pause(job_id)
            task.cancel()
        if tasks:
            await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)
        self.recover()
