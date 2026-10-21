"""실제 앱의 마지막 저장 상태와 읽기용 HTML을 검토 가능한 증빙으로 남긴다."""

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import httpx


ROOT = Path(__file__).resolve().parents[1]
JOB_ID = "30078065-3167-413a-8f62-da3c660d5108"


def main() -> None:
    """이미 조사한 실제 앱 결과를 읽고 두 시즌의 HTML과 DB 백업을 저장한다.

    새 조사나 외부 서비스 호출은 하지 않는다. 실행별 토큰은 메모리에서만 쓴다.
    """
    metadata = json.loads((ROOT / "data/live/instance.lock").read_text())
    output = ROOT / "output"
    output.mkdir(exist_ok=True)
    with httpx.Client(base_url=metadata["url"], timeout=60, trust_env=False) as client:
        instance = client.get("/api/instance").raise_for_status().json()
        assert instance["instance_id"] == metadata["instance_id"]
        bootstrap = client.get("/api/bootstrap").raise_for_status().json()
        headers = {"X-App-Token": bootstrap["token"]}

        def read(path, **params):
            return client.get(path, params=params).raise_for_status().json()

        def rows(path, **params):
            items, cursor = [], None
            while True:
                page = read(path, limit=500, **params, **({"cursor": cursor} if cursor else {}))
                items.extend(page["items"])
                cursor = page["next_cursor"]
                if not cursor:
                    return items

        def write(path, payload):
            response = client.post(path, headers={**headers, "Idempotency-Key": str(uuid4())}, json=payload)
            return response.raise_for_status().json()

        institutions = {item["id"]: item for item in rows("/api/institutions")}
        seasons = read("/api/seasons")
        if isinstance(seasons, dict):
            seasons = seasons["items"]
        current = next(item for item in seasons if item["export_key"] == "2026-winter")
        historical = next(item for item in seasons if item["export_key"] == "2025-winter-source-check")
        job = read(f"/api/jobs/{JOB_ID}")
        assert job["status"] == "completed"
        items = rows(f"/api/jobs/{JOB_ID}/items")
        all_events = rows("/api/events", season_id=current["id"], view="all")
        calendar = rows("/api/events", season_id=current["id"], view="calendar")
        sales = rows("/api/events", season_id=current["id"], view="sales")
        results = []
        for item in items:
            institution = institutions[item["institution_id"]]
            events = [event for event in all_events if event["institution"]["id"] == institution["id"]]
            results.append({
                "name": institution["name"], "type": institution["type"], "district": institution["district"],
                "status": item["status"], "outcome": item["outcome"], "reason_codes": item["reason_codes"],
                "usage": item["usage"],
                "confirmed_dates": [{
                    "event_id": event["id"], "audience": event["audience"], "campus_label": event["campus_label"],
                    "date": event["publication"]["calendar_date"], "time": event["publication"]["calendar_time"],
                    "current_date_allowed": event["publication"]["current_date_allowed"],
                    "current_time_allowed": event["publication"]["current_time_allowed"],
                    "field_permissions": event["publication"]["field_permissions"],
                } for event in events if event["publication"]["calendar_eligible"]],
            })
        exports = []
        for season in (current, historical):
            exported = write("/api/exports/html", {"season_id": season["id"], "include_notes": False,
                                                  "publication_policy_version": "4.0"})
            assert exported["status"] == "current", exported["status"]
            content = client.get(exported["url"]).raise_for_status().content
            target = output / exported["filename"]
            target.write_bytes(content)
            exports.append({
                "season": season, "snapshot": exported, "file": str(target.relative_to(ROOT)),
                "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
                "published_events": len(rows("/api/events", season_id=season["id"], view="calendar")),
                "sales_ready_events": len(rows("/api/events", season_id=season["id"], view="sales")),
            })
        backup = write("/api/backups", {})
        backup_bytes = client.get(backup["download_url"]).raise_for_status().content
        assert backup_bytes.startswith(b"SQLite format 3\0")
        report = {
            "checked_at": datetime.now(timezone.utc).isoformat(), "evidence_level": "actual_app_http_after_restart",
            "app_mode": bootstrap["status"]["app_mode"], "new_external_requests_performed": False,
            "current_season": current,
            "registry": {"registered_institutions": len(institutions),
                         "types": dict(Counter(item["type"] for item in institutions.values())),
                         "all_gwangju_complete": False},
            "job": job, "discovery_mode": "blind_window", "seeded_date": False, "seeded_notice_url": False,
            "representative_results": results,
            "current_counts": {"target_institutions": len(items),
                               "date_institutions": len({event["institution"]["id"] for event in calendar
                                                         if event["publication"]["current_date_allowed"]}),
                               "calendar_events": len(calendar), "time_events": sum(bool(event["publication"]["current_time_allowed"]) for event in calendar),
                               "sales_ready_events": len(sales)},
            "overview": read("/api/overview", season_id=current["id"]),
            "historical": {"seeded_notice_url": True, "seeded_date": False, "is_current_season_result": False,
                           "report": "runtime/live-app/historical-gist-publication.json"},
            "exports": exports,
            "backup": {"id": backup["id"], "created_at": backup["created_at"], "bytes": len(backup_bytes),
                       "sha256": hashlib.sha256(backup_bytes).hexdigest(), "sqlite_header_valid": True},
            "limits": [
                "done은 조사 시도 완료이며 날짜·시각 확인 완료를 뜻하지 않는다.",
                "과거 GIST 공지는 시작 시각 경로의 실제 양성 사례이며 현재 시즌의 무시드 성과에 합산하지 않는다.",
                "토큰에는 사용량 미상 예약이 포함된다. llm_calls는 구버전 선예약을 포함한 예산 계상이며 실제 성공 호출수가 아니다.",
            ],
        }
        path = ROOT / "docs/evidence/live-validation.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"evidence": str(path.relative_to(ROOT)), "counts": report["current_counts"],
                          "exports": [item["file"] for item in exports], "backup_valid": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
