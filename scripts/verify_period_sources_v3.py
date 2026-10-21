"""원본 DB를 읽기만 하며 과거 기간의 실제 학년도별 API 근거를 수집한다."""

import argparse
import asyncio
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.claims import validate_count
from app.config import load_settings
from app.domain import utcnow
from app.periods import fact_season
from app.providers import HttpGateway, NeisProvider
from app.providers_student import StudentProvider
from app.rules import extract_candidates, value_check
from verify_sources_v2 import AuditGateway, AuditMeter, database_signature


async def collect(settings, school, season, identifiers, output):
    """실제 API 응답을 학년도별로 저장하고 동일 행사·인원 관계를 검사한다.

    Args:
        settings: 기존 설정을 읽은 실행 설정. 비밀값은 출력하지 않는다.
        school: 원본 DB의 공식 학교 식별 정보.
        season: 과거 실제 행사일 범위.
        identifiers: 이미 확인한 NEIS 복합 식별자.
        output: 사용자 DB와 분리된 검사 폴더.
    Returns:
        실제 호출·원문·공통 검증 결과. UI 합성 자료는 사용하지 않는다.
    """
    meter = AuditMeter()
    gateway = AuditGateway(settings, meter)
    try:
        calendar = await NeisProvider(settings, meter, gateway).calendar(school, season, identifiers)
        candidates, checks, events = [], [], []
        for document in calendar["documents"]:
            for candidate in extract_candidates(document, school, season):
                event = {"id": str(uuid4()), "title": candidate["event_title"], **candidate,
                         "institution": school, "_identifiers": identifiers}
                observation = {"id": str(uuid4()), "candidate": candidate, "institution_id": school["id"],
                               "document_id": document["id"], "event_id": event["id"], "season_id": season["id"]}
                proof = value_check("event_date", candidate["event_date"], observation, document, event, season,
                                    institution=school)
                candidates.append({"document_id": document["id"], "candidate": candidate})
                checks.append({"kind": "date", "academic_year": candidate["academic_year"],
                               "date": candidate["event_date"], "proof": proof})
                if proof["status"] == "supported":
                    events.append(event)
        students = StudentProvider(settings, meter, gateway)
        student_results = []
        for year in sorted({event["academic_year"] for event in events if type(event["academic_year"]) is int}):
            result = await students.lookup(school, fact_season(season, year), identifiers)
            student_results.append({"academic_year": year, **result})
        documents = calendar["documents"] + [doc for result in student_results for doc in result["documents"]]
        counts = [claim for result in student_results for claim in result["counts"]]
        by_document = {doc["id"]: doc for doc in documents}
        linked = []
        for event in events:
            matches = []
            for claim in counts:
                proof = validate_count(claim, by_document[claim["document_id"]], school, season, event=event)
                checks.append({"kind": "count", "event_academic_year": event["academic_year"],
                               "source_academic_year": claim["academic_year"], "value": claim["value"], "proof": proof})
                if proof["allowed"]:
                    matches.append(claim["value"])
            linked.append({"date": event["event_date"], "academic_year": event["academic_year"],
                           "count": matches[0] if len(set(matches)) == 1 else None})
        payload = {"documents": documents, "candidates": candidates, "counts": counts, "purpose": "date_count",
                   "coverage": {"calendar": {k: v for k, v in calendar.items() if k != "documents"}},
                   "stop_reason": "actual_period_sources_collected"}
        (output / "research-result.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        return {"calendar": {k: v for k, v in calendar.items() if k != "documents"},
                "students": [{k: v for k, v in item.items() if k not in {"documents", "counts"}} for item in student_results],
                "requests": gateway.requests, "usage": dict(meter.usage), "checks": checks, "linked_events": linked,
                "source_success": calendar["coverage_complete"] and bool(linked) and all(item["count"] is not None for item in linked),
                "model_calls": 0, "model_validation": False, "app_publication_validation": False}
    finally:
        await gateway.close()


def main():
    """기간 경계 실제 호출 증거와 원본 보존 검사를 독립 폴더에 남긴다."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-01-01")
    parser.add_argument("--end", default="2026-03-15")
    parser.add_argument("--school", default="건국초등학교")
    args = parser.parse_args()
    from app.periods import validate_period
    validate_period(args.start, args.end)
    stamp = utcnow().replace(":", "").replace("-", "").replace(".", "")
    output = ROOT / "runtime/period-v3/sources" / stamp
    output.mkdir(parents=True)
    database = ROOT / "data/live/graduation.sqlite3"
    original = database_signature(database)
    env_hash = hashlib.sha256((ROOT / ".env").read_bytes()).hexdigest()
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        school = dict(conn.execute("SELECT * FROM institutions WHERE name=?", (args.school,)).fetchone())
        school["official_channels"] = json.loads(school.pop("official_channels_json"))
        identifiers = [dict(row) for row in conn.execute(
            "SELECT provider,external_key FROM institution_identifiers WHERE institution_id=?", (school["id"],))]
    season = {"id": str(uuid4()), "academic_year": None,
              "search_start": args.start, "search_end": args.end}
    settings = load_settings()
    report = {"started_at": utcnow(), "school": school["name"], "season": season, "passed": False}
    try:
        report.update(asyncio.run(collect(settings, school, season, identifiers, output)))
        report["passed"] = report["source_success"]
    except Exception as error:
        # HTTP 예외 문자열에는 키가 포함될 수 있으므로 유형과 안전한 코드만 남긴다.
        report["failure"] = {"type": type(error).__name__, "code": getattr(error, "code", None)}
    finally:
        report["original_db_unchanged"] = database_signature(database) == original
        report["env_unchanged"] = hashlib.sha256((ROOT / ".env").read_bytes()).hexdigest() == env_hash
        report["finished_at"] = utcnow()
        (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"report": str(output / "result.json"), "passed": report["passed"],
                          "linked_events": report.get("linked_events"), "failure": report.get("failure")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
