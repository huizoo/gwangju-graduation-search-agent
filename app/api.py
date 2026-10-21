"""계약 기반 로컬 API와 같은 snapshot의 일정표 저장을 제공한다."""

import asyncio
import base64
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jsonschema import Draft202012Validator, FormatChecker

from . import __version__
from .config import ROOT, load_settings
from .domain import ApiError, SCHEMA, model_for, validate
from .jobs import JobManager, dump, now
from .storage import Service, Store

CONTRACT = json.loads((Path(__file__).parent / "openapi.contract.json").read_text())


def _definition(reference: dict) -> str | None:
    """OpenAPI 참조에서 계약 이름을 추출한다."""
    return reference.get("$ref", "").rsplit("/", 1)[-1] or None


def _query(request: Request, operation: dict) -> dict:
    """선언된 query/path/header 타입과 범위를 검사한다.

    Args:
        request: 현재 HTTP 요청.
        operation: 인계 OpenAPI operation.
    Returns:
        파싱된 query 값.
    Raises:
        ApiError: 필수 값 누락·잘못된 형식·추가 query.
    """
    result = {}
    query_names = {p["name"] for p in operation.get("parameters", []) if p["in"] == "query"}
    if set(request.query_params) - query_names:
        raise ApiError("validation_error", "지원하지 않는 조회 조건입니다.")
    for parameter in operation.get("parameters", []):
        source = {"query": request.query_params, "path": request.path_params, "header": request.headers}[parameter["in"]]
        name, schema = parameter["name"], deepcopy(parameter["schema"])
        value = source.get(name)
        if value is None:
            if parameter.get("required"):
                raise ApiError("validation_error", f"{name} 항목이 필요합니다.", details={"fields": [name]})
            if "default" in schema:
                result[name] = schema["default"]
            continue
        if schema.get("type") == "integer":
            try:
                value = int(value)
            except ValueError:
                raise ApiError("validation_error", f"{name}은 정수여야 합니다.") from None
        if schema.get("type") == "boolean":
            if value not in {"true", "false"}:
                raise ApiError("validation_error", f"{name}은 true/false여야 합니다.")
            value = value == "true"
        if "$ref" in schema:
            schema["$ref"] = "#/$defs/" + _definition(schema)
        schema["$defs"] = SCHEMA["$defs"]
        if not Draft202012Validator(schema, format_checker=FormatChecker()).is_valid(value):
            raise ApiError("validation_error", f"{name} 형식을 확인해 주세요.", details={"fields": [name]})
        if parameter["in"] == "query":
            result[name] = value
    return result


class Application:
    """도메인 서비스·작업 실행기·파일 저장을 하나의 앱 수명에 연결한다."""

    def __init__(self, settings, runner_factory=None):
        """지속 경로와 서비스를 준비하되 외부 제공자를 초기화하지 않는다."""
        self.settings = settings
        self.data_dir = settings.data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.data_dir / "graduation.sqlite3")
        self.service = Service(self.store, settings)
        self.jobs = JobManager(self.service, settings, runner_factory)
        self.token = secrets.token_urlsafe(32)
        self.instance_id = secrets.token_hex(16)
        self.write_lock = asyncio.Lock()
        self.shutdown_callback = None
        if not self.service.list_seasons()["items"]:
            self.service.create_season({**settings.data["season"], "timezone": "Asia/Seoul"})

    def page(self, value: dict | list, request: Request, query: dict) -> dict:
        """동일 필터·정렬·내용 snapshot에만 유효한 서명 cursor를 만든다."""
        result = {"items": value, "total": len(value), "next_cursor": None} if isinstance(value, list) else deepcopy(value)
        rows = result["items"]
        identity = {
            "path": request.url.path,
            "query": {k: v for k, v in query.items() if k not in {"cursor", "limit"}},
            "rows_hash": hashlib.sha256(dump([
                (row.get("id"), row.get("revision"), row.get("institution", {}).get("id"))
                for row in rows
            ]).encode()).hexdigest(),
            "content_revision": result.get("content_revision", self.store.revision()),
        }
        digest = hashlib.sha256(dump(identity).encode()).hexdigest()
        offset = 0
        if query.get("cursor"):
            try:
                raw = base64.urlsafe_b64decode(query["cursor"].encode()).decode()
                position, old_digest, signature = raw.split(":")
                expected = hmac.new(self.token.encode(), f"{position}:{old_digest}".encode(), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(signature, expected) or old_digest != digest:
                    raise ValueError
                offset = int(position)
                if offset < 0:
                    raise ValueError
            except (ValueError, UnicodeError):
                raise ApiError("cursor_expired", "목록이 바뀌었습니다. 처음부터 다시 조회해 주세요.", 409) from None
        limit = query.get("limit", 100)
        result.update(items=rows[offset:offset + limit], total=len(rows), next_cursor=None)
        if offset + limit < len(rows):
            raw = f"{offset + limit}:{digest}"
            signature = hmac.new(self.token.encode(), raw.encode(), hashlib.sha256).hexdigest()
            result["next_cursor"] = base64.urlsafe_b64encode(f"{raw}:{signature}".encode()).decode()
        return result

    def status(self) -> dict:
        """설정 여부와 실제 연결 확인 여부를 혼동하지 않고 제공한다."""
        summary = self.settings.connection_summary()
        capabilities = []
        entries = [
            ("저장 조회·직접 입력", [], True),
            ("NEIS", summary["neis_missing"], False),
            ("공개 검색", summary["search_missing"], False),
            (summary["llm"]["backend"], summary["llm"]["missing_settings"], False),
        ]
        for name, missing, local in entries:
            capabilities.append({
                "name": name, "configured": not missing, "available": local,
                "last_checked_at": None,
                "message": f"설정 필요: {', '.join(missing)}" if missing else ("사용 가능" if local else "호출 시 연결 확인"),
            })
        active = next((j["id"] for j in self.jobs.list() if j["status"] in {"queued", "running", "pausing", "cancelling"}), None)
        return {"app_mode": self.settings.data["app"]["mode"], "app_version": __version__,
                "schema_version": "4.0", "capabilities": capabilities,
                "content_revision": self.store.revision(), "active_job_id": active}

    def export_result(self, season_id: str) -> dict:
        """검증된 같은 정책 파일만 현재 일정 URL로 제공한다."""
        from .exporting import current_export
        with self.store.transaction() as conn:
            season = conn.execute("SELECT * FROM seasons WHERE id=?", (season_id,)).fetchone()
            if season is None:
                raise ApiError("not_found", "시즌을 찾을 수 없습니다.", 404)
            row = conn.execute("SELECT * FROM exports WHERE season_id=?", (season_id,)).fetchone()
            valid = row and row["publication_policy_version"] == "4.0" and row["file_ref"] and current_export(row["file_ref"])
            return {
                "season_id": season_id, "status": row["status"] if row else "pending",
                "content_revision": self.store.revision(conn), "exported_revision": row["exported_revision"] if valid else None,
                "exported_at": row["exported_at"] if valid else None,
                "url": f"/exports/{season['export_key']}/schedule.html" if valid else None,
                "error": json.loads(row["error_json"]) if row and row["error_json"] else None,
                "publication_policy_version": "4.0",
                "withheld_event_count": sum(not e["publication"]["calendar_eligible"] for e in self.service._events(conn, season_id)),
            }

    def export(self, payload: dict) -> dict:
        """한 snapshot을 새 불변 파일로 렌더링한 뒤 현재 파일 포인터를 갱신한다."""
        from .exporting import render_export
        snapshot = self.service.snapshot(payload["season_id"])
        folder = self.data_dir / "exports"
        folder.mkdir(exist_ok=True)
        # DB와 파일을 한 트랜잭션으로 묶을 수 없으므로 기존 파일 대신 새 불변 버전을 쓴다.
        period_name = f"graduation-schedule-{snapshot['season']['search_start']}-{snapshot['season']['search_end']}"
        path = folder / f"{period_name}-r{snapshot['content_revision']}-{uuid4().hex[:8]}.html"
        temp = folder / f".{uuid4()}.tmp"
        revision = snapshot["content_revision"]
        try:
            temp.write_text(render_export(snapshot, include_notes=payload["include_notes"]), encoding="utf-8")
            with self.store.transaction(write=True) as conn:
                os.replace(temp, path)
                status = "current" if self.store.revision(conn) == revision else "pending"
                conn.execute(
                    "INSERT INTO exports(season_id,content_revision,exported_revision,exported_at,status,include_notes,publication_policy_version,file_ref,error_json) VALUES(?,?,?,?,?,?,?,?,NULL) "
                    "ON CONFLICT(season_id) DO UPDATE SET content_revision=excluded.content_revision,exported_revision=excluded.exported_revision,exported_at=excluded.exported_at,status=excluded.status,include_notes=excluded.include_notes,publication_policy_version=excluded.publication_policy_version,file_ref=excluded.file_ref,error_json=NULL",
                    (payload["season_id"], self.store.revision(conn), revision, snapshot["generated_at"], status, int(payload["include_notes"]), "4.0", str(path)),
                )
        except OSError:
            temp.unlink(missing_ok=True)
            with self.store.transaction(write=True) as conn:
                error = ApiError("export_failed", "일정표 파일을 저장하지 못했습니다. 마지막 정상 파일은 보존합니다.", 503).as_dict()
                conn.execute("UPDATE exports SET status='failed',error_json=? WHERE season_id=?", (dump(error), payload["season_id"]))
        return self.export_result(payload["season_id"])

    async def dispatch(self, path: str, method: str, request: Request, query: dict, body: dict | None):
        """계약 경로를 도메인 서비스의 단일 구현에 연결한다."""
        identity = request.path_params.get("id")
        service = self.service
        filters = {k: v for k, v in query.items() if k in {"type", "district"}}
        filters["q"] = query.get("query", "")
        if path == "/health":
            return {"ok": True}
        if path == "/api/status":
            return self.status()
        if path == "/api/seasons":
            return self.page(service.list_seasons(), request, query) if method == "get" else service.create_season(body)
        if path == "/api/seasons/{id}":
            return service.patch_season(identity, body)
        if path == "/api/institutions":
            return self.page(service.list_institutions(**filters), request, query) if method == "get" else service.create_institution(body)
        if path == "/api/institutions/{id}":
            return service.get_institution(identity) if method == "get" else service.patch_institution(identity, body)
        if path == "/api/overview":
            return service.overview(query["season_id"])
        if path == "/api/results":
            from .results import result_rows, PRESENTATION_VERSION
            snapshot = service.snapshot(query["season_id"])
            return {"presentation_version": PRESENTATION_VERSION, "rows": result_rows(snapshot),
                    "season_id": query["season_id"], "request_job_id": snapshot["request_job_id"],
                    "active_job_id": snapshot["active_job_id"]}
        if path == "/api/records/natural":
            from .natural import NaturalRecorder
            return NaturalRecorder(service).record(body)
        if path == "/api/events":
            if method == "post":
                return service.create_event(body)
            result = service.events(query["season_id"], view=query.get("view", "calendar"), **filters)
            rows = result["items"]
            if query.get("review_only"):
                rows = [r for r in rows if r["review_required"]]
            for name, compare in (("from_date", lambda a, b: a >= b), ("to_date", lambda a, b: a <= b)):
                if query.get(name):
                    rows = [r for r in rows if r["publication"]["calendar_date"] and compare(r["publication"]["calendar_date"], query[name])]
            result["items"] = rows
            return self.page(result, request, query)
        if path == "/api/events/{id}":
            return service.event_detail(identity) if method == "get" else service.patch_event(identity, body)
        if path == "/api/events/{id}/overrides":
            return service.apply_overrides(identity, body)
        if path == "/api/review-queue":
            result = service.review(query["season_id"])
            for name in ("type", "district"):
                if filters.get(name):
                    result["items"] = [r for r in result["items"] if r["institution"][name] == filters[name]]
            if filters["q"]:
                result["items"] = [r for r in result["items"] if filters["q"].casefold() in r["institution"]["name"].casefold()]
            return self.page(result, request, query)
        if path == "/api/registry/snapshots":
            return self.page(service.registry_snapshots(), request, query)
        if path == "/api/registry/imports/preview":
            return service.registry_preview(body)
        if path == "/api/registry/imports/{id}/commit":
            return service.registry_commit(identity, body, request.headers["Idempotency-Key"])
        if path == "/api/jobs":
            if method == "get":
                return self.page(self.jobs.list(), request, query)
            result = self.jobs.create(body, request.headers["Idempotency-Key"])
            self.jobs.start(result["id"])
            return result
        if path == "/api/jobs/{id}":
            return self.jobs.get(identity)
        if path == "/api/jobs/{id}/items":
            return self.page(self.jobs.items(identity), request, query)
        if path == "/api/jobs/{id}/pause":
            return self.jobs.pause(identity)
        if path == "/api/jobs/{id}/cancel":
            return self.jobs.cancel(identity)
        if path == "/api/jobs/{id}/resume":
            result = self.jobs.resume(identity, body)
            self.jobs.start(identity)
            return result
        if path == "/api/exports/html":
            return self.export(body)
        if path == "/api/exports/{id}":
            return self.export_result(identity)
        if path == "/api/backups":
            identity = str(uuid4())
            self.store.backup(self.data_dir / "backups" / f"{identity}.sqlite3")
            return {"id": identity, "created_at": now(), "download_url": f"/api/backups/{identity}/download"}
        if path == "/api/backups/{id}/download":
            target = self.data_dir / "backups" / f"{identity}.sqlite3"
            if not target.is_file():
                raise ApiError("not_found", "백업을 찾을 수 없습니다.", 404)
            return FileResponse(target, filename="졸업식-기록-백업.sqlite3", media_type="application/vnd.sqlite3")
        if path == "/exports/{export_key}/schedule.html":
            from .exporting import current_export
            with self.store.transaction() as conn:
                row = conn.execute("SELECT e.file_ref,e.publication_policy_version,s.search_start,s.search_end FROM exports e JOIN seasons s ON s.id=e.season_id WHERE s.export_key=?", (request.path_params["export_key"],)).fetchone()
            if not row or row["publication_policy_version"] != "4.0" or not row["file_ref"] or not current_export(row["file_ref"]):
                raise ApiError("not_found", "현재 정책으로 저장한 일정표가 없습니다.", 404)
            return FileResponse(row["file_ref"], media_type="text/html; charset=utf-8",
                                filename=f"graduation-schedule-{row['search_start']}-{row['search_end']}.html",
                                content_disposition_type="inline")
        if path == "/api/app/shutdown":
            await self.jobs.shutdown()
            if self.shutdown_callback:
                asyncio.get_running_loop().call_later(0.3, self.shutdown_callback)
            return {"accepted": True}
        raise ApiError("not_found", "지원하지 않는 경로입니다.", 404)


def create_app(settings=None, runner_factory=None) -> FastAPI:
    """검사 가능한 의존성을 주입해 FastAPI 앱을 구성한다.

    Args:
        settings: 실행 설정. 생략하면 기존 파일을 읽는다.
        runner_factory: 네트워크 없이 작업 실패·복구를 시험할 조사 실행기.
    Returns:
        시작 시 저장 작업을 복구하는 FastAPI 애플리케이션.
    """
    state = Application(settings or load_settings(), runner_factory)

    @asynccontextmanager
    async def lifespan(app):
        """서버 수명과 작업 checkpoint를 연결한다."""
        state.jobs.recover()
        yield
        await state.jobs.shutdown()

    app = FastAPI(title="광주 졸업식 일정 도우미", version=__version__, lifespan=lifespan)
    app.state.runtime = state

    @app.exception_handler(ApiError)
    async def domain_error(request, error):
        """내부 예외와 외부 API 오류의 응답 형식을 통일한다."""
        return JSONResponse(error.as_dict(), status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        """Pydantic의 입력 원문을 응답에 포함하지 않는다."""
        return JSONResponse(ApiError("validation_error", "입력 형식과 필수 항목을 확인해 주세요.").as_dict(), status_code=422)

    @app.exception_handler(ResponseValidationError)
    async def response_validation_error(request, error):
        """내부 출력 계약 위반을 성공 응답이나 민감한 traceback으로 바꾸지 않는다."""
        return JSONResponse(ApiError("response_contract_error", "저장 결과의 응답 형식 검사에 실패했습니다. 기록을 확인해 주세요.", 500).as_dict(), status_code=500)

    @app.middleware("http")
    async def local_boundary(request, call_next):
        """DNS rebinding·교차 출처 변경 요청을 로컬 토큰과 함께 차단한다."""
        host = request.url.hostname
        if host not in {"127.0.0.1", "::1", "localhost"}:
            return JSONResponse(ApiError("invalid_host", "로컬 주소로 열어 주세요.", 403).as_dict(), status_code=403)
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            return JSONResponse(ApiError("invalid_origin", "다른 사이트에서 접근할 수 없습니다.", 403).as_dict(), status_code=403)
        if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
            if not hmac.compare_digest(request.headers.get("X-App-Token", ""), state.token):
                return JSONResponse(ApiError("invalid_token", "화면을 새로 열고 다시 시도해 주세요.", 403).as_dict(), status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/bootstrap", include_in_schema=False)
    async def bootstrap():
        """같은 출처 화면에 실행별 토큰과 비밀 없는 초기값을 제공한다."""
        with state.store.transaction() as conn:
            row = conn.execute("SELECT value FROM app_meta WHERE key='selected_season_id'").fetchone()
        seasons = state.service.list_seasons()
        return {"token": state.token, "seasons": seasons["items"],
                "selected_season_id": row[0] if row else seasons["items"][0]["id"],
                "settings": state.settings.connection_summary(), "status": state.status(),
                "poll_interval_ms": state.settings.data["app"]["poll_interval_ms"]}

    @app.post("/api/selected-season", include_in_schema=False)
    async def selected_season(request: Request):
        """사용자가 선택한 시즌을 재시작 후에도 유지한다."""
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or set(payload) != {"season_id"} or not isinstance(payload["season_id"], str):
                raise ValueError
            UUID(payload["season_id"])
        except (ValueError, TypeError):
            raise ApiError("validation_error", "시즌 식별자가 필요합니다.")
        with state.store.transaction(write=True) as conn:
            state.service._season(conn, payload["season_id"])
            conn.execute("INSERT INTO app_meta VALUES('selected_season_id',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (payload["season_id"],))
        return {"accepted": True}

    @app.get("/api/instance", include_in_schema=False)
    async def instance():
        """중복 실행 확인에 쓰는 비밀 아닌 실행 식별자를 제공한다."""
        return {"application": "gwangju-graduation-search-agent", "instance_id": state.instance_id}

    @app.get("/api/registry/official-files", include_in_schema=False)
    async def official_files():
        """원문 검토를 거친 고정 명부의 범위와 기준일을 제공한다."""
        from .official_registry import IMPORT_FILES
        from .registry_assets import official_bundle
        items = []
        for filename in IMPORT_FILES:
            bundle = official_bundle(filename)
            items.append({"filename": filename, "source": bundle.registry["source"],
                          "row_count": len(bundle.registry["rows"]), "provenance": bundle.provenance})
        return {"items": items}

    @app.get("/api/registry/official-files/{filename}", include_in_schema=False)
    async def official_file(filename: str):
        """동일한 일반 미리보기·중복 검토 경로에 넣을 검증된 명부를 반환한다."""
        from .registry_assets import official_bundle
        return official_bundle(filename).registry

    def endpoint_for(path, method, operation):
        """요청·응답 계약을 실제 endpoint에 결합한다."""
        body_name = _definition(operation.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {}))

        async def execute(request, body=None):
            query = _query(request, operation)
            payload = body.root if body is not None else None
            if method == "get":
                return await state.dispatch(path, method, request, query, payload)
            async with state.write_lock:
                key = request.headers.get("Idempotency-Key")
                key_required = any(p["name"] == "Idempotency-Key" for p in operation.get("parameters", []))
                if not key_required:
                    return await state.dispatch(path, method, request, query, payload)
                request_hash = hashlib.sha256(dump({"path": request.url.path, "body": payload}).encode()).hexdigest()
                if path == "/api/backups":
                    # SQLite backup은 열린 쓰기 트랜잭션 밖에서 실행해야 교착되지 않는다.
                    with state.store.transaction() as conn:
                        old = conn.execute("SELECT * FROM request_keys WHERE key=?", (key,)).fetchone()
                    if old:
                        if old["payload_hash"] != request_hash:
                            raise ApiError("idempotency_conflict", "같은 요청 키에 다른 내용이 있습니다.", 409)
                        return json.loads(old["response_json"])
                    result = await state.dispatch(path, method, request, query, payload)
                    with state.store.transaction(write=True) as conn:
                        conn.execute("INSERT INTO request_keys VALUES(?,?,?,?,?,?)", (key, request.url.path, request_hash, _success(operation), dump(result), now()))
                    return result
                with state.store.transaction(write=True) as conn:
                    old = conn.execute("SELECT * FROM request_keys WHERE key=?", (key,)).fetchone()
                    if old:
                        if old["payload_hash"] != request_hash:
                            raise ApiError("idempotency_conflict", "같은 요청 키에 다른 내용이 있습니다.", 409)
                        return json.loads(old["response_json"])
                    result = await state.dispatch(path, method, request, query, payload)
                    conn.execute("INSERT INTO request_keys VALUES(?,?,?,?,?,?)", (key, request.url.path, request_hash, _success(operation), dump(result), now()))
                    return result

        if body_name:
            async def endpoint(request: Request, body):
                return await execute(request, body)
            endpoint.__annotations__["body"] = model_for(body_name)
        else:
            async def endpoint(request: Request):
                return await execute(request)
        endpoint.__name__ = operation.get("operationId") or f"{method}_{path}"
        return endpoint

    for path, methods in CONTRACT["paths"].items():
        for method, operation in methods.items():
            response_schema = operation["responses"][str(_success(operation))].get("content", {}).get("application/json", {}).get("schema", {})
            response_name = _definition(response_schema)
            app.add_api_route(
                path, endpoint_for(path, method, operation), methods=[method.upper()],
                status_code=_success(operation), response_model=model_for(response_name) if response_name else None,
                summary=operation.get("summary", ""), operation_id=operation.get("operationId"),
                openapi_extra={
                    "parameters": _inline_schema(operation.get("parameters", [])),
                    "security": operation.get("security", []),
                    "responses": {"default": {"description": "구조화된 오류", "content": {"application/json": {"schema": _inline_schema({"$ref": "#/$defs/Error"})}}}},
                },
            )
    original_openapi = app.openapi

    def runtime_openapi():
        """실행 토큰의 보안 정의를 실제 요청·응답 모델과 함께 노출한다."""
        schema = original_openapi()
        schema.setdefault("components", {})["securitySchemes"] = CONTRACT["components"]["securitySchemes"]
        return schema

    app.openapi = runtime_openapi
    static = ROOT / "app/static"
    if static.exists():
        app.mount("/static", StaticFiles(directory=static), name="assets")
        app.mount("/", StaticFiles(directory=static, html=True), name="ui")
    return app


def _success(operation: dict) -> int:
    """계약에 선언된 성공 응답 코드를 반환한다."""
    return next(int(code) for code in operation["responses"] if code.startswith("2"))


def _inline_schema(value):
    """외부 파일 참조를 독립적인 런타임 OpenAPI 스키마로 풀어낸다."""
    if isinstance(value, list):
        return [_inline_schema(item) for item in value]
    if isinstance(value, dict):
        if "$ref" in value:
            return _inline_schema(SCHEMA["$defs"][_definition(value)])
        return {key: _inline_schema(item) for key, item in value.items()}
    return value
