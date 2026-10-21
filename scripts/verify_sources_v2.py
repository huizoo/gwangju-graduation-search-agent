"""격리한 SQLite 복사본에서 실제 목록·정형 조사·앱 저장 경로를 검증한다."""

from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import load_settings
from app.domain import utcnow
from app.providers import HttpGateway, canonical
from app.registry import sync_registry
from app.research import ResearchRunner
from app.scope import TARGET_TYPES


def _json(path: Path, value) -> None:
    """비밀을 포함하지 않는 검증 결과를 UTF-8 파일에 저장한다."""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _digest(data: bytes) -> str:
    """원문을 출력하지 않고 동일 바이트인지 비교할 해시를 얻는다."""
    return hashlib.sha256(data).hexdigest()


def database_signature(path: Path) -> dict:
    """읽기 전용 연결에서 모든 테이블의 건수·내용 해시를 계산한다.

    Args:
        path: 존재하는 사용자 DB 또는 격리 복사본.
    Returns:
        원문 값이 없는 테이블별 건수·해시와 무결성 결과.
    """
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        signatures = {}
        for table in tables:
            if not re.fullmatch(r"[a-zA-Z_][a-zA-Z_0-9]*", table):
                raise AssertionError("unexpected_table_name")
            rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
            encoded = []
            for row in rows:
                values = [value.hex() if isinstance(value, bytes) else value for value in row]
                encoded.append(canonical(values))
            signatures[table] = {"count": len(rows), "sha256": _digest("\n".join(sorted(encoded)).encode())}
        return {"tables": signatures, "quick_check": conn.execute("PRAGMA quick_check").fetchone()[0],
                "foreign_key_errors": len(conn.execute("PRAGMA foreign_key_check").fetchall())}


def preserved_rows(path: Path) -> dict:
    """기존 원문·수기 행을 UUID별로 비교하되 원문 내용은 보고하지 않는다.

    Args:
        path: 읽기 전용으로 열 SQLite 경로.
    Returns:
        원문·수기 UUID와 해당 내용의 해시.
    """
    columns = {
        "documents": ("id", "content_hash", "segments_json"),
        "manual_overrides": ("id", "field", "mode", "value_json", "basis", "confirmed_at", "recorded_at", "note"),
        "natural_records": ("id", "turns_json", "state_json", "created_at"),
    }
    result = {}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        for table, names in columns.items():
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'", (table,)).fetchone()
            if not exists:
                result[table] = {}
                continue
            rows = conn.execute("SELECT " + ",".join(names) + " FROM " + table).fetchall()
            result[table] = {row[0]: _digest(canonical(list(row[1:])).encode()) for row in rows}
    return result


class AuditMeter:
    """실제 자료원 실행량을 세되 외부 제공자를 대체하지 않는 관찰기."""

    def __init__(self):
        self.usage = Counter()
        self.checkpoints = []

    async def reserve(self, kind, amount=1):
        """자료원 호출 전에 실제 예약된 사용량을 합산한다."""
        self.usage[kind] += amount
        return str(uuid4())

    async def settle(self, _reservation_id, actual_amount=None):
        """이 검사는 모델을 호출하지 않으므로 추가 정산을 하지 않는다."""

    async def checkpoint(self, result, *, stage):
        """실제 조사 단계와 확보 문서 개수를 기록한다."""
        self.checkpoints.append({"stage": stage, "documents": len(result.get("documents", []))})


class AuditGateway(HttpGateway):
    """실제 HTTP 연결을 유지하며 인증 인자를 기록하지 않는 관찰기."""

    def __init__(self, settings, meter):
        super().__init__(settings, meter)
        self.requests = []

    async def request(self, method, url, **kwargs):
        """요청 주소와 비밀이 아닌 기간 인자만 검증 기록에 추가한다."""
        params = kwargs.get("params") or {}
        public_params = {key: params[key] for key in (
            "apiType", "pbanYr", "sidoCode", "sggCode", "schulKndCode",
            "ATPT_OFCDC_SC_CODE", "SD_SCHUL_CODE", "AY", "AA_FROM_YMD", "AA_TO_YMD",
        ) if key in params}
        response = await super().request(method, url, **kwargs)
        self.requests.append({"method": method, "url": url, "query": public_params, "status": response.status_code})
        return response


async def collect(settings, school, season, identifiers) -> tuple:
    """실제 MCP·NEIS·학교알리미를 날짜 시드와 저장 근거 없이 실행한다."""
    meter = AuditMeter()
    gateway = AuditGateway(settings, meter)
    runner = ResearchRunner(settings, meter=meter, gateway=gateway)
    try:
        result = await runner.run(school, season, identifiers=identifiers, purpose="date_count")
        return result, {
            "usage": dict(meter.usage), "http_requests": gateway.requests,
            "checkpoints": meter.checkpoints, "model_initialized": runner.model is not None,
        }
    finally:
        await gateway.close()


def require(condition, label):
    """실패 기준을 낮추지 않고 안전한 검사 이름으로 종료한다."""
    if not condition:
        raise AssertionError(label)


def main() -> None:
    """원본을 보존한 채 목록부터 앱 API·HTML·재실행까지 실제로 연결한다."""
    stamp = utcnow().replace(":", "").replace("-", "").replace(".", "")
    output = ROOT / "runtime/improvements-20261021/sources-integrated" / stamp
    output.mkdir(parents=True, exist_ok=False)
    source = ROOT / "data/live/graduation.sqlite3"
    clone = output / "data/live/graduation.sqlite3"
    clone.parent.mkdir(parents=True)
    before = database_signature(source)
    originals = preserved_rows(source)
    env_hash = _digest((ROOT / ".env").read_bytes())
    report = {
        "started_at": utcnow(), "scope": "실제 공식 목록·MCP·정형 조사 → 격리 SQLite → 앱 API·HTML → 재실행",
        "browser_validation": False, "http_test_method": "FastAPI TestClient",
        "original_db_tables_before": before, "checks": [], "passed": False,
        "test_date_seed_provided": False, "historical_success_in_current_results": False,
    }
    phase = "clone_readonly"
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as origin:
            with closing(sqlite3.connect(clone)) as destination:
                origin.backup(destination)
        settings = load_settings(process_values={"APP_DATA_DIR": str(output / "data"), "AUTO_OPEN_BROWSER": "false"})
        report["selected_backend"] = settings.data["app"]["llm_backend"]
        app = create_app(settings)
        runtime, service = app.state.runtime, app.state.runtime.service
        season = next(item for item in service.list_seasons()["items"]
                      if item["academic_year"] == settings.data["season"]["academic_year"])
        phase = "live_registry_sync"
        sync_details = []
        for provider in ("neis", "higher_education"):
            fetched = asyncio.run(sync_registry(settings, provider))
            saved = service.save_registry_sync(fetched)
            payload = fetched.get("registry", fetched)
            evidence = {"provider": provider, "source": payload["source"], "saved_snapshot_id": saved["id"],
                        "raw_loaded_rows": len(payload["rows"]),
                        "target_types": dict(Counter(row["type"] for row in payload["rows"] if row["type"] in TARGET_TYPES))}
            if provider == "higher_education":
                evidence["provenance"] = fetched["_verified_registry"].provenance
            sync_details.append(evidence)
        report["registry_sync"] = sync_details
        schools = service.list_institutions()["items"]
        targets = [school for school in schools if school["type"] in TARGET_TYPES]
        type_counts = dict(Counter(school["type"] for school in targets))
        require(len(targets) == 339, "official_target_count_339")
        require(type_counts == {"elementary": 158, "middle": 92, "high": 71, "university": 11, "junior_college": 7},
                "official_five_type_counts")
        school = next(item for item in targets if item["name"] == "건국초등학교")
        with runtime.store.transaction() as conn:
            identifiers = [dict(row) for row in conn.execute(
                "SELECT provider,external_key FROM institution_identifiers WHERE institution_id=?", (school["id"],))]
        phase = "live_blind_structured_agent"
        result, execution = asyncio.run(collect(settings, school, season, identifiers))
        _json(output / "research-result.json", result)
        _json(output / "research-execution.json", execution)
        require(result["stop_reason"] == "structured_evidence_sufficient", "structured_completion")
        require(execution["usage"].get("llm_calls", 0) == 0 and not execution["model_initialized"], "unnecessary_model_calls_zero")
        require(result["queries"] == [] and execution["usage"].get("search_calls", 0) == 0, "time_and_other_search_queries_zero")
        calendar = [item for item in execution["http_requests"] if item["url"].endswith("/SchoolSchedule")]
        require(calendar and all(item["query"]["AA_FROM_YMD"] == season["search_start"].replace("-", "")
                                and item["query"]["AA_TO_YMD"] == season["search_end"].replace("-", "") for item in calendar),
                "no_seed_whole_season_calendar")
        phase = "save_research_and_api"
        saved = service.save_research(school["id"], season["id"], result)
        with TestClient(app, base_url="http://127.0.0.1") as client:
            response = client.get("/api/results", params={"season_id": season["id"]})
            require(response.status_code == 200, "results_api_success")
            api_result = response.json()
            rows = api_result["rows"]
            target_ids = {school["id"] for school in targets}
            require({row["institution_id"] for row in rows} == target_ids, "results_include_all_targets")
            row = next(row for row in rows if row["institution_id"] == school["id"])
            require(row["date"] == "2027-01-06" and row["graduation_count"] == 188, "current_actual_date_and_count_published")
            require(row["start_time"] is None, "no_invented_time")
            by_id = {item["id"]: item for item in targets}
            require(all(item["address"] == by_id[item["institution_id"]]["address"] for item in rows),
                    "confirmed_school_addresses_only")
            require(all(item["url"] in (None, by_id[item["institution_id"]]["homepage_url"]) for item in rows),
                    "school_links_homepage_only")
            require(all(not item["url"] or not re.search(r"open\.neis|/openApi|[.]json(?:\?|$)|[.]xml(?:\?|$)", item["url"], re.I)
                        for item in rows), "no_machine_links")
            token = client.get("/api/bootstrap").json()["token"]
            export = client.post("/api/exports/html", headers={
                "X-App-Token": token, "Idempotency-Key": str(uuid4()),
            }, json={"season_id": season["id"], "include_notes": False, "publication_policy_version": "4.0"})
            require(export.status_code == 202 and export.json()["status"] == "current", "html_export_api_success")
            html = client.get(export.json()["url"]).content
            (output / "graduation-schedule-2026.html").write_bytes(html)
            soup = BeautifulSoup(html, "html.parser")
            require([cell.get_text(" ", strip=True) for cell in soup.select("thead th")] ==
                    ["날짜", "학교명", "시간", "졸업인원수", "장소"], "five_column_html_order")
            backup = client.post("/api/backups", headers={"X-App-Token": token, "Idempotency-Key": str(uuid4())})
            require(backup.status_code == 201, "backup_api_success")
            backup_bytes = client.get(backup.json()["download_url"]).content
            manual_backup = output / "verified-api-backup.sqlite3"
            manual_backup.write_bytes(backup_bytes)
            report["checks"].append({
                "name": "live_blind_structured_agent_to_api_html", "passed": True,
                "institution": school["name"], "academic_year": season["academic_year"],
                "season_window": [season["search_start"], season["search_end"]],
                "row": row, "candidate_count": len(result["candidates"]),
                "stop_reason": result["stop_reason"], "tool_trace": result["trace"],
                "agent_trace": result["agent_trace"], "usage": execution["usage"],
                "saved_event_ids": saved["event_ids"], "model_calls": 0, "search_queries": [],
                "html_sha256": _digest(html),
            })
            report["results_summary"] = {
                "institutions": len(target_ids), "rows": len(rows), "types": type_counts,
                "official_homepage_links": sum(bool(row["url"]) for row in rows),
                "confirmed_addresses": sum(bool(row["address"]) for row in rows),
                "confirmed_dates": sum(bool(row["date"]) for row in rows),
                "confirmed_counts": sum(row["graduation_count"] is not None for row in rows),
                "confirmed_times": sum(bool(row["start_time"]) for row in rows),
            }
        phase = "restart_persistence"
        restarted = create_app(settings)
        with TestClient(restarted, base_url="http://127.0.0.1") as client:
            persisted = client.get("/api/results", params={"season_id": season["id"]})
            require(persisted.status_code == 200, "restart_results_success")
            require(persisted.json()["rows"] == rows, "restart_preserves_results")
        preservation = preserved_rows(clone)
        details = {}
        for table, expected in originals.items():
            retained = all(preservation[table].get(identity) == digest for identity, digest in expected.items())
            details[table] = {"original_count": len(expected), "preserved_original_count": sum(
                preservation[table].get(identity) == digest for identity, digest in expected.items()), "passed": retained}
            require(retained, table + "_original_rows_preserved")
        report["preservation"] = details
        backups = sorted((clone.parent / "backups").glob("*.sqlite3")) + [manual_backup]
        report["backup_integrity"] = []
        for backup in backups:
            signature = database_signature(backup)
            require(signature["quick_check"] == "ok" and signature["foreign_key_errors"] == 0, "backup_integrity")
            report["backup_integrity"].append({"name": backup.name, "quick_check": signature["quick_check"],
                                               "foreign_key_errors": signature["foreign_key_errors"]})
        clone_signature = database_signature(clone)
        require(clone_signature["quick_check"] == "ok" and clone_signature["foreign_key_errors"] == 0, "clone_integrity")
        report["clone_final_tables"] = clone_signature
        report["checks"].append({"name": "restart_and_backup_persistence", "passed": True,
                                 "backup_count": len(backups), "same_result_rows": True})
        report["passed"] = True
    except Exception as error:
        report["failure"] = {"phase": phase, "type": type(error).__name__,
                             "check": str(error) if isinstance(error, AssertionError) else getattr(error, "code", None)}
    finally:
        after = database_signature(source)
        report["original_db_unchanged"] = before == after
        report["environment_file_unchanged"] = env_hash == _digest((ROOT / ".env").read_bytes())
        report["finished_at"] = utcnow()
        report["passed"] = bool(report["passed"] and report["original_db_unchanged"] and report["environment_file_unchanged"])
        _json(output / "result.json", report)
        print(json.dumps({"output": str(output), "passed": report["passed"], "failure": report.get("failure")}, ensure_ascii=False))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
