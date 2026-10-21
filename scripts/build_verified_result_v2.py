"""실제 검증 사본의 근거를 합쳐 합성값 없는 검토용 HTML을 만든다."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.exporting import render_export
from app.results import result_rows
from app.storage import Service, Store


def main():
    """두 격리 실행의 원문·후보를 새 사본에서 공통 검증한 뒤 내보낸다.

    Raises:
        AssertionError: 검증했던 날짜·인원이 사본에서 현재 허용되지 않을 때.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--agent-run", type=Path, required=True)
    args = parser.parse_args()
    out = ROOT / "runtime/improvements-20261021/final-result" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = out / "data/live/graduation.sqlite3"
    path.parent.mkdir(parents=True)
    source_db = (args.source_run / "data/live/graduation.sqlite3").resolve()
    agent_db = (args.agent_run / "data/live/graduation.sqlite3").resolve()
    with sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(path) as target:
            source.backup(target)
    settings = load_settings(process_values={"APP_DATA_DIR": str(out / "data"), "AUTO_OPEN_BROWSER": "false"})
    service = Service(Store(path), settings)
    school = next(s for s in service.list_institutions(q="광주대학교")["items"] if s["name"] == "광주대학교")
    season = next(s for s in service.list_seasons()["items"] if s["academic_year"] == 2026)
    # 새 사실을 만들지 않고 실제 JobManager가 저장한 원문과 모델 관측을 다시 검증한다.
    with sqlite3.connect(agent_db.as_uri() + "?mode=ro", uri=True) as source:
        source.row_factory = sqlite3.Row
        identity = source.execute("SELECT id FROM institutions WHERE name=?", (school["name"],)).fetchone()[0]
        source_season = source.execute("SELECT id FROM seasons WHERE academic_year=2026").fetchone()[0]
        documents = list(service._documents(source, identity).values())
        candidates = [{"document_id": row["document_id"], "candidate": json.loads(row["candidate_json"])}
                      for row in source.execute("SELECT * FROM observations WHERE institution_id=? AND season_id=?",
                                                (identity, source_season))]
        traces = [json.loads(row[0]) for row in source.execute(
            "SELECT trace_json FROM investigation_traces WHERE institution_id=? AND season_id=?", (identity, source_season))]
    for document in documents:
        document["institution_id"] = school["id"]
    for item in candidates:
        # 실행 사본의 행사 키는 옮기지 않는다. 실제 근거로 현재 사본의 행사를 다시 연결한다.
        item["candidate"]["event_key_hint"] = None
    saved = service.save_research(school["id"], season["id"], {
        "documents": documents, "candidates": candidates, "counts": [],
        "purpose": "date_count", "agent_trace": [{"kind": "actual_job_replay", "source": str(args.agent_run),
                                                "original_traces": traces}],
        "stop_reason": "verified_result_replay"})
    snapshot = service.snapshot(season["id"])
    rows = result_rows(snapshot)
    elementary = next(row for row in rows if row["name"] == "건국초등학교")
    university = next(row for row in rows if row["name"] == "광주대학교")
    assert elementary["date"] == "2027-01-06" and elementary["graduation_count"] == 188
    assert university["date"] == "2027-02-19"
    assert elementary["start_time"] is None and university["start_time"] is None
    assert university["graduation_count"] is None
    assert len(rows) == 339
    html = render_export(snapshot)
    target = ROOT / "output/graduation-schedule-2026.html"
    target.write_text(html, encoding="utf-8")
    report = {"source_run": str(args.source_run), "agent_run": str(args.agent_run),
              "output": str(target), "rows": len(rows), "checked_rows": [elementary, university],
              "saved": saved, "synthetic_values": False, "new_network_or_model_calls": 0,
              "user_database_written": False, "passed": True}
    (out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"result": str(out / "result.json"), "rows": len(rows), "passed": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
