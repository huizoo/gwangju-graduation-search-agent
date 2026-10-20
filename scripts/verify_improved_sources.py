"""실제 NEIS·공식 GIST 원문을 새 규칙과 앱 API까지 다시 검증한다."""

import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from fastapi.testclient import TestClient
from app.api import create_app
from app.config import load_settings
from app.domain import utcnow
from app.providers import DocumentReader, HttpGateway, NeisProvider, TavilyProvider
from app.rules import extract_candidates
from app.tool_boundary import ResearchTools


async def collect(runtime, institution, season, *, url=None):
    """실제 읽기 MCP를 사용하며 모델·날짜·시각을 수집 입력에 주입하지 않는다.

    Args:
        runtime: 격리 DB와 기존 선택 설정.
        institution: 공식 관계가 있는 대상 기관.
        season: 날짜 미지정 전체 조사 기간.
        url: 과거 공지 검증에만 쓰는 저장된 공식 URL.
    Returns:
        저장 경로에 넣을 읽기 결과와 실제 도구 trace.
    """
    gateway = HttpGateway(runtime.settings)
    with runtime.store.transaction() as conn:
        identifiers = [dict(row) for row in conn.execute("SELECT provider,external_key FROM institution_identifiers WHERE institution_id=?",(institution["id"],))]
    tools = ResearchTools(institution,season,identifiers,NeisProvider(runtime.settings,gateway=gateway),
                          TavilyProvider(runtime.settings,gateway=gateway),DocumentReader(gateway))
    try:
        async with tools.connect():
            if url:
                documents = [await tools.call("read_public_document",{"institution_id":institution["id"],"url":url})]
                coverage = {}
            else:
                response = await tools.call("get_school_calendar",{"institution_id":institution["id"],"season_id":season["id"],
                    "from_date":season["search_start"],"to_date":season["search_end"]})
                documents = response["documents"]
                coverage = {"calendar":{**{k:v for k,v in response.items() if k!="documents"},"checked_at":utcnow()}}
        candidates = [{"document_id":doc["id"],"candidate":candidate} for doc in documents
                      for candidate in extract_candidates(doc,institution,season)]
        return {"documents":documents,"candidates":candidates,"coverage":coverage}, tools.trace
    finally:
        await gateway.close()


def main():
    """검증용 복사본에만 저장하고 실제 연결과 과거 seeded 검사를 구분해 기록한다."""
    out = ROOT/"runtime/improvements-20261020"/datetime.now().strftime("sources-%H%M%S")
    clone = out/"data/live/graduation.sqlite3"
    clone.parent.mkdir(parents=True)
    with sqlite3.connect((ROOT/"data/live/graduation.sqlite3").as_uri()+"?mode=ro",uri=True) as source:
        with sqlite3.connect(clone) as target:
            source.backup(target)
    settings = load_settings(process_values={"APP_DATA_DIR":str(out/"data"),"AUTO_OPEN_BROWSER":"false"})
    app = create_app(settings);runtime=app.state.runtime;service=runtime.service
    report = {"scope":"실제 읽기 MCP → 등록 규칙 1.2.0 → 격리 SQLite → 실제 앱 API → HTML",
              "started_at":utcnow(),"selected_backend":settings.data["app"]["llm_backend"],
              "model_calls":0,"unselected_backend_initialized":False,"checks":[]}
    seasons = service.list_seasons()["items"]
    checks = [("건국초등학교",2026,None),
              ("광주과학기술원",2025,"https://www.gist.ac.kr/kr/html/sub05/050209.html?mode=V&no=220361&GotoPage=1")]
    with TestClient(app,base_url="http://127.0.0.1") as client:
        for name,year,url in checks:
            school = next(i for i in service.list_institutions(q=name)["items"] if i["name"]==name)
            season = next(s for s in seasons if s["academic_year"]==year)
            collected, trace = asyncio.run(collect(runtime,school,season,url=url))
            saved = service.save_research(school["id"],season["id"],collected)
            result = client.get("/api/results",params={"season_id":season["id"]})
            assert result.status_code==200,result.text
            rows = [r for r in result.json()["rows"] if r["institution_id"]==school["id"]]
            docs = [{"id":d["id"],"status":d["status"],"source_kind":d["source_kind"],
                     "hash":d["content_hash"],"url":d["display_url"],"retrieved_at":d["retrieved_at"]}
                    for d in collected["documents"]]
            item = {"institution":name,"academic_year":year,"mode":"seeded_historical_url" if url else "whole_window_no_date_seed",
                    "trace":trace,"documents":docs,"candidate_count":len(collected["candidates"]),"rows":rows}
            report["checks"].append(item)
            expected = ("2027-01-06",None) if not url else ("2026-02-20","14:00")
            actual = [(r["date"],r["start_time"]) for r in rows]
            item["passed"] = expected in actual and bool(collected["candidates"]) and all(d["status"]=="ok" for d in docs)
            if not item["passed"]:
                continue
            # 결과 생성도 API의 현재 정책 경로를 통과한다. 비밀 토큰은 증빙에 넣지 않는다.
            token=client.get("/api/bootstrap").json()["token"]
            from uuid import uuid4
            export=client.post("/api/exports/html",headers={"X-App-Token":token,"Idempotency-Key":str(uuid4())},
                json={"season_id":season["id"],"include_notes":False,"publication_policy_version":"4.0"})
            assert export.status_code==202,export.text
            html=client.get(export.json()["url"]).content
            (out/f"verified-{year}.html").write_bytes(html)
            item["html_sha256"]=hashlib.sha256(html).hexdigest()
            print(json.dumps({"institution":name,"passed":item["passed"],"rows":actual},ensure_ascii=False),flush=True)
    report["finished_at"]=utcnow()
    report["passed"]=all(c["passed"] for c in report["checks"])
    (out/"result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    print(json.dumps({"out":str(out),"passed":report["passed"]},ensure_ascii=False))
    if not report["passed"]:
        raise SystemExit(1)


if __name__=="__main__":
    main()
