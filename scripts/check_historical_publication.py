"""실제 과거 공지를 앱 MCP→규칙→SQLite→API→HTML 끝까지 검증한다.

공식 목록에서 연결된 GIST 기관과 사람이 선정한 과거 URL을 사용한다.
날짜·시각은 수집기에 주입하지 않으며 원문에서 추출한 후 기대값과 대조한다.
현재 시즌의 blind 발견 성과에는 포함하지 않는다.
"""

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_settings
from app.providers import DocumentReader, HttpGateway, NeisProvider, TavilyProvider
from app.rules import extract_candidates
from app.storage import Service, Store
from app.tool_boundary import ResearchTools

NOTICE = "https://www.gist.ac.kr/kr/html/sub05/050209.html?mode=V&no=220361&GotoPage=1"
EXPORT_KEY = "2025-winter-source-check"


async def read(institution, season):
    """실제 앱 MCP에서 원문을 읽고 등록 규칙의 미승인 후보를 반환한다."""
    settings = load_settings()
    gateway = HttpGateway(settings)
    tools = ResearchTools(institution, season, [], NeisProvider(settings, gateway=gateway),
                          TavilyProvider(settings, gateway=gateway), DocumentReader(gateway))
    try:
        async with tools.connect():
            document = await tools.call("read_public_document", {"institution_id": institution["id"], "url": NOTICE})
        candidates = extract_candidates(document, institution, season)
        assert document["status"] == "ok" and candidates
        return {"documents": [document], "candidates": [
            {"document_id": document["id"], "candidate": candidate, "origin": "registered_rule"}
            for candidate in candidates
        ], "trace": tools.trace, "discovery_mode": "seeded_url"}
    finally:
        await gateway.close()


def main():
    """별도 과거 시즌의 실제 게시값을 현재 시즌과 혼동하지 않게 기록한다."""
    settings = load_settings()
    with httpx.Client(base_url="http://127.0.0.1:8600", trust_env=False, timeout=60) as client:
        token = client.get("/api/bootstrap").raise_for_status().json()["token"]
        headers = {"X-App-Token": token, "Idempotency-Key": str(uuid4())}
        seasons = client.get("/api/seasons").raise_for_status().json()["items"]
        season = next((row for row in seasons if row["export_key"] == EXPORT_KEY), None)
        if season is None:
            season = client.post("/api/seasons", headers=headers, json={
                "label": "2025학년도 과거 공지 검증 (현재 판매 시즌 아님)", "academic_year": 2025,
                "search_start": "2025-12-01", "search_end": "2026-03-31", "export_key": EXPORT_KEY,
                "timezone": "Asia/Seoul", "region_profile": "gwangju_5_gu",
            }).raise_for_status().json()
        rows = client.get("/api/institutions", params={"query": "광주과학기술원"}).raise_for_status().json()["items"]
        assert len(rows) == 1 and rows[0]["official_channels"]
        institution = rows[0]
        collected = asyncio.run(read(institution, season))
        service = Service(Store(settings.data_dir / "graduation.sqlite3"), settings)
        stored = service.save_research(institution["id"], season["id"], collected)
        details = [client.get(f"/api/events/{identity}").raise_for_status().json() for identity in stored["event_ids"]]
        published = [row for row in details if row["event"]["publication"]["calendar_eligible"]]
        assert len(published) == 1
        publication = published[0]["event"]["publication"]
        assert publication["calendar_date"] == "2026-02-20" and publication["calendar_time"] == "14:00"
        assert publication["sales_ready"] and publication["calendar_venue"] == "광주과학기술원 오룡관 대강당"
        exported = client.post("/api/exports/html", headers=headers, json={
            "season_id": season["id"], "include_notes": False, "publication_policy_version": "4.0",
        }).raise_for_status().json()
        html = client.get(exported["url"]).raise_for_status().text
        assert "2026-02-20" in html and "14:00" in html and "현재 판매 시즌 아님" in html
        evidence = {
            "checked_at": datetime.now(timezone.utc).isoformat(), "evidence_level": "actual_app_mcp_storage_api_html",
            "discovery_mode": "seeded_url", "seeded_date": False, "seeded_notice_url": True,
            "current_season_result": False, "institution": institution["name"], "season": season,
            "source_origin": NOTICE, "collected": collected, "details": details, "export": exported,
            "assertions": {"day": "2026-02-20", "actual_start": "14:00", "same_api_html_values": True},
        }
        output = ROOT / "runtime/live-app/historical-gist-publication.json"
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": "passed", "historical_only": True, "date": publication["calendar_date"],
                          "time": publication["calendar_time"], "output": str(output.relative_to(ROOT))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
