"""보존한 실제 조사 근거를 격리 사본에서 재검증해 기간별 HTML을 만든다."""

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.domain import utcnow
from app.exporting import render_export
from app.results import result_rows
from app.storage import Service, Store
from verify_sources_v2 import database_signature


def main():
    """실제 원문 재사용과 새 네트워크 조사를 구별하여 검토 결과를 저장한다.

    Raises:
        AssertionError: 기존 확인값·사용자 원본 보존·기간별 결과 검사가 실패한 경우.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--previous-result", required=True, type=Path)
    parser.add_argument("--past-job", required=True, type=Path)
    parser.add_argument("--future-job", type=Path)
    args = parser.parse_args()
    out = ROOT / "runtime/period-v3/final-result" / utcnow().replace(":", "").replace("-", "").replace(".", "")
    path = out / "data/live/graduation.sqlite3"
    path.parent.mkdir(parents=True)
    original_db = database_signature(ROOT / "data/live/graduation.sqlite3")
    original_env = hashlib.sha256((ROOT / ".env").read_bytes()).hexdigest()
    source_db = (args.previous_result / "data/live/graduation.sqlite3").resolve()
    with closing(sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(path)) as target:
            source.backup(target)
    settings = load_settings(process_values={"APP_DATA_DIR": str(out / "data"), "AUTO_OPEN_BROWSER": "false"})
    service = Service(Store(path), settings)
    old_period = next(s for s in service.list_seasons()["items"] if
                      s["search_start"] == "2026-12-01" and s["search_end"] == "2027-03-31")
    new_period = service.create_season({"search_start": "2025-12-01", "search_end": "2026-03-15"})
    inputs = [(args.past_job, "첨단고등학교")]
    if args.future_job:
        inputs.append((args.future_job, "건국초등학교"))
    for run, name in inputs:
        report = json.loads((run / "result.json").read_text())
        assert report["passed"] and report["row"]["name"] == name
        actual_period = report["season"]
        source_period = service.create_season({
            "search_start": actual_period["search_start"], "search_end": actual_period["search_end"]})
        collected = json.loads((run / "research-result.json").read_text())
        school = next(s for s in service.list_institutions(q=name)["items"] if s["name"] == name)
        for document in collected["documents"]:
            document["institution_id"] = school["id"]
        # 정형 파서의 자료원 행사 힌트는 원문 검증 입력이다. 정본 행사 키는
        # save_research가 기관·학년도·행사 단위로 다시 계산한다.
        # 원래 성공한 API 작업은 별도 증빙으로 남긴다. 여기서는 새 조사로 가장하지 않는다.
        collected["agent_trace"] = [{"kind": "actual_job_replay", "source": str(run)}]
        collected["stop_reason"] = "verified_result_replay"
        # 실제 취득 기간을 더 넓은 표시 시즌의 완전 조회로 가장하지 않는다.
        # 같은 정본 행사의 연결을 통해 겹치는 표시 시즌에도 확인값이 보인다.
        collected["season_snapshot"] = source_period
        service.save_research(school["id"], source_period["id"], collected)
    artifacts = []
    for season in (old_period, new_period):
        snapshot = service.snapshot(season["id"])
        rows = result_rows(snapshot)
        assert len(rows) == 339
        target = ROOT / "output" / f"graduation-schedule-{season['search_start']}-{season['search_end']}.html"
        if season["id"] == old_period["id"]:
            checked = [next(row for row in rows if row["name"] == name)
                       for name in ("건국초등학교", "광주대학교")]
            assert checked[0]["date"] == "2027-01-06" and checked[0]["graduation_count"] == 188
            assert checked[1]["date"] == "2027-02-19"
        else:
            checked = [next(row for row in rows if row["name"] == "첨단고등학교")]
            assert checked[0]["date"] == "2026-01-21" and checked[0]["graduation_count"] == 355
        assert all(row["start_time"] is None for row in checked)
        html = render_export(snapshot)
        assert '<meta name="schedule-presentation" content="3.0">' in html
        target.write_text(html, encoding="utf-8")
        artifacts.append({"output": str(target), "rows": len(rows), "checked_rows": checked,
                          "sha256": hashlib.sha256(html.encode()).hexdigest()})
    report = {"passed": True, "previous_result": str(args.previous_result), "past_job": str(args.past_job),
              "future_job": str(args.future_job) if args.future_job else None,
              "artifacts": artifacts, "synthetic_values": False, "new_network_or_model_calls": 0,
              "user_database_written": False,
              "original_db_unchanged": database_signature(ROOT / "data/live/graduation.sqlite3") == original_db,
              "env_unchanged": hashlib.sha256((ROOT / ".env").read_bytes()).hexdigest() == original_env}
    assert report["original_db_unchanged"] and report["env_unchanged"]
    (out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"report": str(out / "result.json"), "passed": True,
                      "outputs": [entry["output"] for entry in artifacts]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
