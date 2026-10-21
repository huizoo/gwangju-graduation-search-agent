"""격리 DB·앱에서 고정 과거 URL의 게시 경로를 검증한다.

일반 일정찾기 성과가 아니다. 운영 서버·운영 DB에는 연결하지 않는다.
"""

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from uuid import uuid4

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.api import create_app
from app.config import Settings, load_settings
from app.domain import dumps, validate
from app.providers import DocumentReader, HttpGateway, NeisProvider, TavilyProvider
from app.rules import extract_candidates
from app.tool_boundary import ResearchTools

NOTICE = "https://www.gist.ac.kr/kr/html/sub05/050209.html?mode=V&no=220361&GotoPage=1"
VALIDATION_ROOT = ROOT / "runtime" / "development-validation"


def isolated_settings(directory: Path, source: Settings) -> Settings:
    """명시한 새 검증 폴더만 허용하고 운영 경로와 같은 연결은 거절한다.

    Args:
        directory: runtime/development-validation 아래의 새 전용 폴더.
        source: 메모리에만 보관할 API 설정.
    Returns:
        데이터·출력 경로를 격리한 설정.
    Raises:
        ValueError: 운영 경로, 허용 영역 밖 경로 또는 기존 검증 파일을 지정한 경우.
    """
    target, allowed = directory.resolve(), VALIDATION_ROOT.resolve()
    if target == allowed or not target.is_relative_to(allowed):
        raise ValueError("runtime/development-validation 아래의 새 검증 폴더가 필요합니다.")
    if (target.is_relative_to(source.data_dir.resolve())
            or source.data_dir.resolve().is_relative_to(target)):
        raise ValueError("운영 DB 경로를 검증에 사용할 수 없습니다.")
    if target.exists() and any(target.iterdir()):
        raise ValueError("기존 파일을 보존하려면 비어 있는 새 검증 폴더를 지정해 주세요.")
    data = deepcopy(source.data)
    data["app"].update(data_dir=str(target / "data"), auto_open_browser=False)
    return Settings(data, dict(source.secrets), source.root)


async def read(institution, season, settings):
    """전용 앱의 기관·기간으로 실제 원문을 읽고 후보를 추출한다.

    Args:
        institution: 별도 파일로 전달한 공식 기관 기록.
        season: 격리 DB에서 만든 기간.
        settings: 격리 경로를 검증한 설정.
    Returns:
        원문·후보·도구 호출 기록. 날짜·시간은 원문에서 추출한다.
    """
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
            for candidate in candidates], "trace": tools.trace, "discovery_mode": "seeded_url"}
    finally:
        await gateway.close()


def main():
    """명시한 검증 폴더의 전용 ASGI 앱만 사용해 결과를 보관한다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validation-dir", type=Path, required=True,
                        help="runtime/development-validation 아래의 새 폴더")
    parser.add_argument("--institution-record", type=Path, required=True,
                        help="공식 채널 근거를 포함한 Institution JSON 사본")
    arguments = parser.parse_args()
    settings = isolated_settings(arguments.validation_dir, load_settings())
    institution = validate("Institution", json.loads(arguments.institution_record.read_text(encoding="utf-8")))
    if institution["name"] != "광주과학기술원" or not institution["official_channels"]:
        raise ValueError("공식 채널 관계를 포함한 광주과학기술원 기록이 필요합니다.")
    app = create_app(settings)
    service = app.state.runtime.service
    with service.store.transaction(True) as conn:
        service._insert_institution(conn, institution, identity=institution["id"])
        conn.execute("UPDATE institutions SET official_channels_json=?,source_checked_at=? WHERE id=?",
                     (dumps(institution["official_channels"]), institution["source_checked_at"], institution["id"]))
    # 네트워크의 localhost 서버를 선택하지 않고 위에서 만든 전용 앱에 직접 연결한다.
    with TestClient(app, base_url="http://127.0.0.1") as client:
        token = client.get("/api/bootstrap").raise_for_status().json()["token"]
        headers = {"X-App-Token": token, "Idempotency-Key": str(uuid4())}
        season = client.post("/api/seasons", headers=headers, json={
            "search_start": "2025-12-01", "search_end": "2026-03-31"}).raise_for_status().json()
        collected = asyncio.run(read(institution, season, settings))
        stored = service.save_research(institution["id"], season["id"], collected)
        details = [client.get(f"/api/events/{identity}").raise_for_status().json() for identity in stored["event_ids"]]
        published = [row for row in details if row["event"]["publication"]["calendar_eligible"]]
        assert len(published) == 1
        publication = published[0]["event"]["publication"]
        assert publication["calendar_date"] == "2026-02-20" and publication["calendar_time"] == "14:00"
        exported = client.post("/api/exports/html", headers=headers, json={
            "season_id": season["id"], "include_notes": False, "publication_policy_version": "4.0",
        }).raise_for_status().json()
        html = client.get(exported["url"]).raise_for_status().text
        assert "2026-02-20" in html and "14:00" in html
        evidence = {
            "checked_at": datetime.now(timezone.utc).isoformat(), "evidence_level": "isolated_app_mcp_storage_api_html",
            "discovery_mode": "seeded_url", "seeded_date": False, "seeded_notice_url": True,
            "current_season_result": False, "institution": institution["name"], "season": season,
            "source_origin": NOTICE, "collected": collected, "details": details, "export": exported,
            "assertions": {"day": "2026-02-20", "actual_start": "14:00", "same_api_html_values": True}}
        output = arguments.validation_dir.resolve() / "historical-gist-publication.json"
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": "passed", "historical_only": True, "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
