"""실제 인증 목록·날짜 미지정 일정·공개 검색·로컬 모델의 제한된 연동을 기록한다.

키는 출력하지 않으며 결과는 runtime/provider-live에만 남긴다.
"""

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings
from app.domain import validate
from app.providers import (
    DocumentReader, HttpGateway, ModelProvider, NeisProvider, ProviderError, TavilyProvider,
    canonical, empty_document, utcnow, public_addresses,
)
from app.registry import validate_registry_import, neis_registry, NEIS_TYPES, district_for
from app.rules import extract_candidates, value_check

OUTPUT = Path(__file__).resolve().parents[1] / "runtime/provider-live"


class Meter:
    """검증 스크립트에서 호출 수만 기록하는 예산 관측기."""

    def __init__(self):
        self.usage = {}
        self.reservations = {}

    async def reserve(self, kind, amount=1):
        self.usage[kind] = self.usage.get(kind, 0) + amount
        identity = str(len(self.reservations))
        self.reservations[identity] = (kind, amount)
        return identity

    async def settle(self, reservation, actual_amount=None):
        if actual_amount is not None:
            kind, reserved = self.reservations[reservation]
            self.usage[kind] += actual_amount - reserved


def save(name, value):
    """비밀이 없는 산출물을 UTF-8 JSON으로 저장한다."""
    OUTPUT.mkdir(parents=True, exist_ok=True)
    if (OUTPUT / name).exists():
        history = OUTPUT / "history"
        history.mkdir(exist_ok=True)
        previous = OUTPUT / name
        (history / f"{previous.stem}-{utcnow().replace(':','').replace('.','')}{previous.suffix}").write_bytes(previous.read_bytes())
    (OUTPUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


async def main(mode="registry"):
    """정해진 검증 모드 한 가지를 실행한다."""
    settings, meter = load_settings(), Meter()
    class RecordingGateway(HttpGateway):
        async def request(self, method, url, **kwargs):
            response = await super().request(method, url, **kwargs)
            if mode == "model" and "/api/chat" in url:
                attempts.append(response.json())
                save("model-raw-attempts.json", attempts)
            return response
    attempts = []
    gateway = RecordingGateway(settings, meter)
    result = {"checked_at": utcnow(), "mode": mode, "usage": meter.usage, "status": "failed"}
    try:
        if mode in {"registry", "registry_pages"}:
            page_size = 100 if mode == "registry_pages" else 1000
            raw_rows = await NeisProvider(settings, meter, gateway).rows("schoolInfo", {"ATPT_OFCDC_SC_CODE": "F10"}, page_size=page_size)
            save("neis-raw-rows.json" if mode == "registry" else "neis-paged-raw-rows.json", raw_rows)
            result["mapping_exceptions"] = [
                {"name": row.get("SCHUL_NM"), "kind": row.get("SCHUL_KND_SC_NM"), "address": row.get("ORG_RDNMA")}
                for row in raw_rows if row.get("SCHUL_KND_SC_NM") not in NEIS_TYPES or district_for(str(row.get("ORG_RDNMA", ""))) is None]
            payload = neis_registry(raw_rows)
            preview = validate_registry_import(payload)
            validate("RegistryImport", payload)
            if mode == "registry":
                save("neis-registry.json", payload)
            result.update(status="ok", row_count=len(payload["rows"]), coverage_complete=payload["source"]["coverage_complete"],
                          source_hash=payload["source"]["source_hash"], rejected_rows=preview["rejected_rows"],
                          scope_types=payload["source"]["scope_types"], scope_districts=payload["source"]["scope_districts"],
                          page_size=page_size)
        else:
            registry = json.loads((OUTPUT / "neis-registry.json").read_text())
            row = next(item for item in registry["rows"] if item["name"] == "첨단고등학교")
            now = utcnow()
            institution = {**row, "id": str(uuid4()), "official_channels": [
                {"url": "https://open.neis.go.kr/hub", "verified_by_url": registry["source"]["official_url"], "verified_at": now}]}
            if row["homepage_url"]:
                institution["official_channels"].append({"url": row["homepage_url"], "verified_by_url": registry["source"]["official_url"], "verified_at": now})
            season = {**settings.data["season"], "id": str(uuid4())}
            identifiers = [{"provider": "neis", "external_key": row["external_key"]}]
            if mode == "calendar":
                calendar = await NeisProvider(settings, meter, gateway).calendar(institution, season, identifiers)
                validate("CalendarResult", calendar)
                save("blind-calendar.json", {"institution": institution, "season": season, "result": calendar})
                checks = []
                for document in calendar["documents"]:
                    for candidate in extract_candidates(document, institution, season):
                        event = {"id": str(uuid4()), "institution": institution, "event_type": candidate["event_type"],
                                 "audience": candidate["audience"], "campus_label": candidate["campus_label"], "_identifiers": identifiers}
                        observation = {"id": str(uuid4()), "institution_id": institution["id"], "season_id": season["id"],
                                       "event_id": event["id"], "document_id": document["id"], "candidate": candidate}
                        proof = value_check("event_date", candidate["event_date"], observation, document, event, season)
                        validate("ValueCheck", proof)
                        negative = dict(candidate["event_date"], date="2000-01-01")
                        rejected = value_check("event_date", negative, observation, document, event, season)
                        checks.append({"date": candidate["event_date"], "positive": proof, "negative_wrong_date": rejected})
                result.update(status=calendar["status"], discovery_mode=calendar["discovery_mode"], auth_mode=calendar["auth_mode"],
                              coverage_complete=calendar["coverage_complete"], reason_codes=calendar["reason_codes"], checks=checks,
                              date_seeded=False, target_institution=row["name"])
            elif mode == "search_reader":
                query = f'"{row["name"]}" "{season["academic_year"]}학년도" 졸업식'
                search = await TavilyProvider(settings, meter, gateway).search(query)
                validate("SearchResult", search)
                save("tavily-search.json", {"query": query, "result": search})
                documents = []
                reader = DocumentReader(gateway)
                for item in search["results"][:3]:
                    document = await reader.read(institution, item["url"])
                    validate("DocumentRead", document)
                    documents.append(document)
                save("public-documents.json", documents)
                result.update(status=search["status"], query=query, result_count=len(search["results"]),
                              documents=[{"url": document["display_url"], "status": document["status"],
                                  "content_hash": document["content_hash"], "source_kind": document["source_kind"],
                                  "authority": document["authority"], "reading": document["reading"], "reasons": document["reason_codes"]}
                                  for document in documents])
            elif mode == "model":
                result["synthetic_validation_only"] = True
                result["backend"] = settings.data["app"]["llm_backend"]
                results = []
                for text in [
                    f'{season["academic_year"]}학년도 {institution["name"]} 졸업식은 2027년 1월 7일 오전 11시에 시작합니다. 입장은 오전 10시 30분부터입니다.',
                    f'{season["academic_year"]}학년도 {institution["name"]} 졸업식은 2027년 1월에 예정되어 있습니다. 오전 9시는 학교 운영 시작 시간이며 졸업식 날짜와 시각은 미정입니다.',
                ]:
                    document = empty_document(institution, institution["homepage_url"])
                    document.update(status="ok", source_kind="html", content_hash=hashlib.sha256(text.encode()).hexdigest(),
                                    segments=[{"id": "s1", "locator_kind": "html_text", "location": "synthetic test", "unit_id": "test", "text": text}])
                    try:
                        candidates = await ModelProvider(settings, meter, gateway).candidates(institution, season, document)
                        results.append({"input": text, "status": "ok", "candidates": candidates})
                    except ProviderError as error:
                        results.append({"input": text, "status": "failed", "error": error.as_dict()})
                result.update(status="ok" if all(item["status"] == "ok" for item in results) else "partial", results=results)
            elif mode == "reader_debug":
                import httpx
                url = "https://open.neis.go.kr/portal/guide/apiGuidePage.do"
                addresses = await public_addresses(url)
                diagnostics = []
                for pinned in (False, True):
                    target = str(httpx.URL(url).copy_with(host=addresses[0])) if pinned else url
                    kwargs = {"headers": {"Host": "open.neis.go.kr"}, "extensions": {"sni_hostname": "open.neis.go.kr"}} if pinned else {}
                    try:
                        response = await gateway.client.get(target, **kwargs)
                        diagnostics.append({"pinned": pinned, "status": response.status_code})
                    except Exception as error:
                        diagnostics.append({"pinned": pinned, "error_type": type(error).__name__, "error": str(error)[:250]})
                result.update(status="diagnostic", diagnostics=diagnostics)
            elif mode == "school_reader_debug":
                import httpx
                from urllib.parse import urlsplit
                diagnostics = []
                for scheme in ("http", "https"):
                    url = institution["homepage_url"].replace("http://", scheme + "://")
                    try:
                        addresses = await public_addresses(url)
                        for pinned in (False, True):
                            target = str(httpx.URL(url).copy_with(host=addresses[0])) if pinned else url
                            kwargs = {"headers": {"Host": urlsplit(url).hostname},
                                      "extensions": {"sni_hostname": urlsplit(url).hostname}} if pinned else {}
                            try:
                                response = await gateway.client.get(target, **kwargs)
                                diagnostics.append({"url": url, "pinned": pinned, "status": response.status_code,
                                                    "location": response.headers.get("location")})
                            except Exception as error:
                                diagnostics.append({"url": url, "pinned": pinned, "error_type": type(error).__name__, "error": str(error)[:250]})
                    except ProviderError as error:
                        diagnostics.append({"url": url, "error": error.as_dict()})
                result.update(status="diagnostic", diagnostics=diagnostics)
                document = await DocumentReader(gateway).read(institution, institution["homepage_url"])
                result["reader_result"] = {"status": document["status"], "acquisition": document["acquisition"],
                                           "reading": document["reading"], "reasons": document["reason_codes"]}
                if "empty_article" in document["reason_codes"]:
                    response = await gateway.request("GET", institution["homepage_url"], public=True)
                    (OUTPUT / "school-homepage-response.html").write_bytes(response.content)
            elif mode == "gist_discovery":
                from bs4 import BeautifulSoup
                response = await gateway.request("GET", "https://www.gist.ac.kr/kr/html/sub05/050209.html?category=04", public=True)
                soup = BeautifulSoup(response.text, "html.parser")
                found = [{"text": node.get_text(" ", strip=True), "href": node.get("href")} for node in soup.select("a[href]")
                         if "2025학년도 학위수여식 개최 안내" in node.get_text(" ", strip=True)]
                result.update(status="ok", found=found)
            elif mode == "seeded_documents":
                records = []
                targets = [
                    ("광주과학기술원", "https://www.gist.ac.kr/kr/html/sub05/050209.html?mode=V&no=220361&GotoPage=1",
                     "https://www.gist.ac.kr", "https://www.gist.ac.kr/kr/html/sub05/050209.html"),
                    ("조선대학교", "https://www4.chosun.ac.kr/bbs/grad/109/162028/download.do",
                     "https://www4.chosun.ac.kr", "https://www4.chosun.ac.kr/bbs/grad/109/229433/artclView.do"),
                ]
                for name, url, channel, provenance in targets:
                    target = {"id": str(uuid4()), "name": name, "type": "university", "campus_label": "광주",
                              "official_channels": [{"url": channel, "verified_by_url": provenance, "verified_at": utcnow()}]}
                    reader = DocumentReader(gateway)
                    document = await reader.read(target, url)
                    validate("DocumentRead", document)
                    records.append({"institution": target, "document": document})
                save("seeded-documents.json", records)
                result.update(status="ok" if all(r["document"]["status"] == "ok" for r in records) else "partial",
                    seeded_url=True, historical_gist=True, documents=[{"name": r["institution"]["name"],
                        "status": r["document"]["status"], "source_kind": r["document"]["source_kind"],
                        "reading": r["document"]["reading"], "hash": r["document"]["content_hash"]} for r in records])
            else:
                raise ValueError("지원하지 않는 검사 모드")
    except ProviderError as error:
        result["error"] = error.as_dict()
    finally:
        await gateway.close()
    save(f"{mode}-check.json", result)
    summary = {key: value for key, value in result.items() if key not in {"mapping_exceptions", "checks", "results", "documents"}}
    for key in ("mapping_exceptions", "checks", "results", "documents"):
        if key in result:
            summary[key + "_count"] = len(result[key])
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "registry"))
