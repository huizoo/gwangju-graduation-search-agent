"""격리 앱의 전체 대상 한 학교를 실제 과거 조사·저장·HTML·재시작까지 검사한다."""

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from app.api import create_app
from app.exporting import schedule_filename
from app.config import load_settings
from app.domain import utcnow
from app.research import ResearchRunner
from app.rules import REGISTERED_RULES
from verify_sources_v2 import AuditGateway, database_signature


def main():
    """공식 기관만 복사한 새 DB에서 실제 요청 경로와 원본 보존을 검사한다."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--school", default="첨단고등학교")
    parser.add_argument("--start", default="2025-12-01")
    parser.add_argument("--end", default="2026-03-15")
    parser.add_argument("--expected-date", default="2026-01-21")
    parser.add_argument("--expected-count", type=int, default=355)
    parser.add_argument("--expected-year", type=int, default=2025)
    args = parser.parse_args()
    stamp = utcnow().replace(":", "").replace("-", "").replace(".", "")
    output = ROOT / "runtime/period-v3/job" / stamp
    output.mkdir(parents=True)
    source = ROOT / "data/live/graduation.sqlite3"
    original = database_signature(source)
    env_hash = hashlib.sha256((ROOT / ".env").read_bytes()).hexdigest()
    settings = load_settings(process_values={"APP_DATA_DIR": str(output / "data"), "AUTO_OPEN_BROWSER": "false"})
    requests, collected = [], []

    class CapturingRunner(ResearchRunner):
        """실제 실행·JobManager 계량을 유지하고 공개 검증 증거만 보존한다."""

        def __init__(self, fixed_settings, meter=None):
            gateway = AuditGateway(fixed_settings, meter)
            super().__init__(fixed_settings, meter, gateway)

        async def run(self, *args, **kwargs):
            """실제 저장 체크포인트와 같은 조사 결과를 반환한다.

            Args:
                *args: 실제 JobManager의 학교·시즌 문맥.
                **kwargs: 공식 식별자·목적·대상 행사 등 실행 인자.
            Returns:
                실제 Runner가 수집한 근거와 종료 상태.
            """
            try:
                result = await super().run(*args, **kwargs)
                collected.append(result)
                return result
            finally:
                requests.extend(self.gateway.requests)
                await self.gateway.close()

    app = create_app(settings, runner_factory=CapturingRunner)
    runtime, report = app.state.runtime, {"started_at": utcnow(), "passed": False,
                                         "scope": "실제 API→JobManager→ResearchRunner/MCP→SQLite→API→HTML→재시작",
                                         "browser_validation": False, "date_answer_seed": False,
                                         "registered_rules": REGISTERED_RULES}
    phase = "copy_official_institution_only"
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            institution = dict(conn.execute("SELECT * FROM institutions WHERE name=?", (args.school,)).fetchone())
            identifiers = [tuple(row) for row in conn.execute(
                "SELECT provider,external_key,institution_id FROM institution_identifiers WHERE institution_id=?", (institution["id"],))]
        with runtime.store.transaction(True) as conn:
            columns = ",".join(institution)
            placeholders = ",".join("?" for _ in institution)
            conn.execute(f"INSERT INTO institutions({columns}) VALUES({placeholders})", tuple(institution.values()))
            conn.executemany("INSERT INTO institution_identifiers VALUES(?,?,?)", identifiers)
        with TestClient(app, base_url="http://127.0.0.1") as client:
            token = client.get("/api/bootstrap").json()["token"]

            def post(path, payload):
                return client.post(path, json=payload, headers={"X-App-Token": token, "Idempotency-Key": str(uuid4())})

            phase = "period_create_api"
            response = post("/api/seasons", {"search_start": args.start, "search_end": args.end})
            assert response.status_code == 201, "period_create_api"
            season = response.json()
            assert season["academic_year"] is None
            phase = "actual_all_target_job"
            response = post("/api/jobs", {"kind": "schedule_scan", "purpose": "date_count",
                "season_id": season["id"], "mode": "recheck", "selection": {"scope": "all"}, "limits": None})
            assert response.status_code == 202, "job_create_api"
            job_id = response.json()["id"]
            deadline = time.monotonic() + settings.data["execution"]["item_timeout_seconds"] + 30
            while time.monotonic() < deadline:
                job = client.get(f"/api/jobs/{job_id}").json()
                if job["status"] not in {"queued", "running", "pausing", "cancelling"}:
                    break
                time.sleep(0.1)
            items = client.get(f"/api/jobs/{job_id}/items").json()
            report.update(job=job, items=items, requests=requests)
            assert job["status"] == "completed", "actual_job_completed"
            assert len(items["items"]) == 1 and items["items"][0]["institution_id"] == institution["id"]
            assert len(collected) == 1 and collected[0]["stop_reason"] == "structured_evidence_sufficient"
            assert job["usage"]["llm_calls"] == job["usage"]["search_calls"] == 0
            calendar_requests = [request for request in requests if request["url"].endswith("/SchoolSchedule")]
            assert len(calendar_requests) == 1
            assert "AY" not in calendar_requests[0]["query"]
            assert calendar_requests[0]["query"]["AA_FROM_YMD"] == args.start.replace("-", "")
            assert calendar_requests[0]["query"]["AA_TO_YMD"] == args.end.replace("-", "")
            count_requests = [request for request in requests if request["query"].get("apiType") == "09"]
            assert len(count_requests) == 1 and str(count_requests[0]["query"]["pbanYr"]) == str(args.expected_year)
            phase = "actual_result_api_html"
            result = client.get("/api/results", params={"season_id": season["id"]}).json()
            assert len(result["rows"]) == 1
            row = result["rows"][0]
            assert row["date"] == args.expected_date and row["graduation_count"] == args.expected_count
            assert row["start_time"] is None
            event = runtime.service.event_detail(row["event_ids"][0])["event"]
            assert event["academic_year"] == args.expected_year
            response = post("/api/exports/html", {"season_id": season["id"], "include_notes": False,
                                                  "publication_policy_version": "4.0"})
            assert response.status_code == 202 and response.json()["status"] == "current"
            html = client.get(response.json()["url"]).text
            html_path = output / schedule_filename(season)
            html_path.write_text(html)
            assert args.school in html and args.expected_date in html and str(args.expected_count) in html
            report.update(season=season, row=row, event_academic_year=event["academic_year"],
                          html=str(html_path), html_sha256=hashlib.sha256(html.encode()).hexdigest())
        phase = "restart_persistence"
        restarted = create_app(settings)
        with TestClient(restarted, base_url="http://127.0.0.1") as client:
            saved = client.get("/api/results", params={"season_id": season["id"]}).json()
            assert saved["rows"] == result["rows"]
            assert next(item for item in restarted.state.runtime.service.list_seasons()["items"] if item["id"] == season["id"]) == season
        report.update(passed=True, restart_preserved=True)
    except Exception as error:
        report["failure"] = {"phase": phase, "type": type(error).__name__, "code": getattr(error, "code", None)}
    finally:
        if collected:
            (output / "research-result.json").write_text(json.dumps(collected[0], ensure_ascii=False, indent=2) + "\n")
        report["original_db_unchanged"] = database_signature(source) == original
        report["env_unchanged"] = hashlib.sha256((ROOT / ".env").read_bytes()).hexdigest() == env_hash
        report["finished_at"] = utcnow()
        (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"report": str(output / "result.json"), "passed": report["passed"],
                          "row": report.get("row"), "failure": report.get("failure")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
