"""기관별 LangGraph로 필요한 공개 근거만 추가 조사한다."""

from __future__ import annotations

import hashlib
import re
from typing import TypedDict
from urllib.parse import urlsplit

from langgraph.graph import END, START, StateGraph

from .providers import (
    DocumentReader, HttpGateway, ModelProvider, NeisProvider, ProviderError,
    TavilyProvider, canonical, official_channel, reserve, settle, utcnow,
)
from .rules import extract_candidates
from .tool_boundary import ResearchTools


class ResearchState(TypedDict):
    """한 기관 조사 중인 문서·후보·시도·오류를 보관하는 그래프 상태."""
    documents: list[dict]
    candidates: list[dict]
    errors: list[dict]
    coverage: dict
    seen_urls: list[str]
    assessed_ids: list[str]
    queries: list[str]
    plans: int
    actions: list[dict]
    node_trace: list[dict]
    event_blockers: list[dict]


class ResearchRunner:
    """하나의 기관·시즌에 묶인 읽기 도구와 조건부 탐색을 실행한다.

    Args:
        settings: 작업 시작 시 고정한 실행 설정.
        meter: 영속 예산 예약기.
        gateway: 테스트 및 연결 공유용 HTTP Gateway.
    """

    def __init__(self, settings, meter=None, gateway=None):
        self.settings, self.meter = settings, meter
        self.gateway = gateway or HttpGateway(settings, meter)
        self.owns_gateway = gateway is None
        self.reader = DocumentReader(self.gateway)
        self.calendar = NeisProvider(settings, meter, self.gateway)
        self.search = TavilyProvider(settings, meter, self.gateway)
        self.model = None

    async def run(self, institution: dict, season: dict, *, identifiers=None) -> dict:
        """정답 날짜·URL 없이 기관 식별자와 전체 기간으로 조사한다.

        Args:
            institution: 이름과 검증된 공식 채널이 있는 기관.
            season: 저장된 학년도와 전체 조사 기간.
            identifiers: 공식 자료원에서 확보한 기관 복합 식별자.
        Returns:
            문서·미승인 후보·오류·실제 MCP/그래프 호출 이력. 저장과 게시 권한은 없다.
        """
        identifiers = identifiers or []
        tools = ResearchTools(institution, season, identifiers, self.calendar, self.search, self.reader)
        limits = self.settings.data["execution"]

        async def checkpoint(state, stage):
            """완료한 근거를 다음 외부 호출 전에 실행기 트랜잭션으로 보존한다."""
            if self.meter is not None and hasattr(self.meter, "checkpoint"):
                await self.meter.checkpoint({"documents": state["documents"], "candidates": state["candidates"],
                    "errors": state["errors"], "coverage": state["coverage"],
                    "trace": tools.trace + state["node_trace"], "event_blockers": state["event_blockers"]}, stage=stage)

        def mark(state, node):
            """검사 결과와 구분할 실제 그래프 노드 통과 기록을 추가한다."""
            state["node_trace"].append({"node": node, "at": utcnow()})
            return state

        def add_error(state, provider, codes, status):
            """같은 제한을 무한 반복해서 기록하지 않는다."""
            for code in codes or [status]:
                error = {"provider": provider, "code": code, "message": f"{provider}: {status}"}
                if not any(item["provider"] == provider and item["code"] == code for item in state["errors"]):
                    state["errors"].append(error)

        async def read(state, url):
            """중복 URL과 기관별 문서 한도를 확인한 뒤 MCP로 읽는다."""
            if url in state["seen_urls"] or len(state["seen_urls"]) >= limits["item_max_document_fetches"]:
                return
            state["seen_urls"].append(url)
            document = await tools.call("read_public_document", {"institution_id": institution["id"], "url": url})
            known = any(url in {item.get("display_url"), item.get("source_key")} for item in institution.get("known_documents", []))
            if known:
                success = document["status"] == "ok" and document["reading"]["content_match"] == "expected"
                document["recheck"] = {"status": "succeeded" if success else "failed",
                    "attempted_at": document["last_checked_at"], "reason_codes": [] if success else ["recheck_failed"]}
            if document["authority"] == "official" and any(
                re.search(r"(?:졸업식|학위수여식)[^\n]{0,70}(?:취소|연기|미개최)", segment["text"])
                for segment in document["segments"]
            ):
                state["event_blockers"].append({"source_key": document["source_key"], "document_id": document["id"],
                                               "reason_codes": ["cancelled_notice"]})
            state["documents"].append(document)
            if document["status"] != "ok":
                add_error(state, "reader", document["reason_codes"], document["status"])
            await checkpoint(state, "document_read")

        async def initial_collect(state):
            """정형 일정과 확인된 기관 채널을 먼저 읽는다."""
            mark(state, "initial_collect")
            if institution["type"] in {"elementary", "middle", "high", "special_school", "other_school"}:
                result = await tools.call("get_school_calendar", {
                    "institution_id": institution["id"], "season_id": season["id"],
                    "from_date": season["search_start"], "to_date": season["search_end"],
                })
                state["documents"].extend(result["documents"])
                state["coverage"]["calendar"] = {key: value for key, value in result.items() if key != "documents"}
                state["coverage"]["calendar"]["checked_at"] = utcnow()
                if result["status"] not in {"ok", "no_data"}:
                    add_error(state, "neis", result["reason_codes"], result["status"])
                await checkpoint(state, "calendar_collected")
            for known in institution.get("known_documents", [])[:3]:
                url = known.get("display_url") or known.get("source_key")
                if url and not url.startswith("https://open.neis.go.kr/hub"):
                    await read(state, url)
            homepage = institution.get("homepage_url")
            if homepage and official_channel(institution, homepage):
                await read(state, homepage)
            return state

        async def assess(state):
            """정형 매핑을 우선 적용하고 지원하지 않는 문장에만 후보 모델을 쓴다."""
            mark(state, "assess")
            fingerprints = {item["fingerprint"] for item in state["candidates"]}
            for document in state["documents"]:
                if document["id"] in state["assessed_ids"]:
                    continue
                state["assessed_ids"].append(document["id"])
                candidates = extract_candidates(document, institution, season)
                method = "registered_mapping"
                if (not candidates and document["status"] == "ok" and document["authority"] == "official"
                        and document["source_kind"] in {"html", "pdf"}
                        and not any(item["document_id"] == document["id"] for item in state["event_blockers"])
                        and any(re.search(r"졸업|학위수여", segment["text"]) for segment in document["segments"])):
                    if self.model is None:
                        self.model = ModelProvider(self.settings, self.meter, self.gateway)
                    try:
                        candidates = await self.model.candidates(institution, season, document)
                        method = "model_candidate"
                    except ProviderError as error:
                        add_error(state, error.provider, [error.code], error.message)
                for candidate in candidates:
                    fingerprint = hashlib.sha256(canonical([document["content_hash"], candidate]).encode()).hexdigest()
                    if fingerprint not in fingerprints:
                        state["candidates"].append({"document_id": document["id"], "candidate": candidate,
                            "fingerprint": fingerprint, "extraction_method": method})
                        fingerprints.add(fingerprint)
                await checkpoint(state, "document_assessed")
            return state

        def decide(state):
            """부족한 값·남은 새 경로·한도에 따라 다음 탐색 또는 종료를 선택한다."""
            eligible = [item["candidate"] for item in state["candidates"] if item["extraction_method"] == "registered_mapping"]
            dates = {candidate["event_date"].get("date") for candidate in eligible if candidate["event_date"]["precision"] == "day"}
            times = [candidate for candidate in eligible if candidate["start_time"] and candidate["time_role"] == "ceremony_start"]
            if len(dates) == 1 and times:
                return "finish"
            if state["plans"] >= limits["item_max_replans"] or len(state["seen_urls"]) >= limits["item_max_document_fetches"]:
                return "finish"
            return "plan"

        async def plan(state):
            """새 검색어와 발견한 공식 공지 링크를 최대 세 개만 선택한다."""
            reservation_id = await reserve(self.meter, "replans")
            await settle(self.meter, reservation_id)
            mark(state, "plan")
            state["plans"] += 1
            actions = []
            suffix = "졸업식 시작 시간" if state["plans"] == 1 else "졸업식 안내 변경"
            query = f'"{institution["name"]}" "{season["academic_year"]}학년도" {suffix}'
            if query not in state["queries"] and len(state["queries"]) < limits["item_max_search_calls"]:
                actions.append({"kind": "search", "query": query})
            for document in state["documents"]:
                for url in self.reader.links.get(document["id"], []):
                    if url not in state["seen_urls"] and official_channel(institution, url) and any(
                        term in url.lower() for term in ("graduation", "notice", "bbs", "board", "haksa", "calendar", ".pdf")
                    ):
                        if not any(item.get("url") == url for item in actions):
                            actions.append({"kind": "read", "url": url})
                        if len(actions) >= 3:
                            break
                if len(actions) >= 3:
                    break
            state["actions"] = actions[:3]
            return state

        async def collect(state):
            """계획된 공개 도구를 실행하되 검색 요약을 근거로 승격하지 않는다."""
            mark(state, "collect")
            for action in state["actions"]:
                if action["kind"] == "read":
                    await read(state, action["url"])
                    continue
                state["queries"].append(action["query"])
                domains = sorted({urlsplit(item["url"]).hostname for item in institution.get("official_channels", [])
                                  if urlsplit(item["url"]).hostname != "open.neis.go.kr"})
                result = await tools.call("search_public_web", {"query": action["query"], "include_domains": domains[:10], "max_results": 5})
                if result["status"] not in {"ok", "no_data"}:
                    add_error(state, "tavily", result["reason_codes"], result["status"])
                    continue
                if result["status"] == "no_data":
                    add_error(state, "tavily", ["not_found"], "no_data")
                for item in result["results"]:
                    if len(state["seen_urls"]) >= limits["item_max_document_fetches"]:
                        break
                    await read(state, item["url"])
            await checkpoint(state, "collection_finished")
            return state

        def finish(state):
            """남은 부족함을 명시한 상태로 저장 계층에 넘긴다."""
            mark(state, "finish")
            return state

        graph = StateGraph(ResearchState)
        graph.add_node("initial_collect", initial_collect)
        graph.add_node("assess", assess)
        graph.add_node("plan", plan)
        graph.add_node("collect", collect)
        graph.add_node("finish", finish)
        graph.add_edge(START, "initial_collect")
        graph.add_edge("initial_collect", "assess")
        graph.add_conditional_edges("assess", decide, {"plan": "plan", "finish": "finish"})
        graph.add_edge("plan", "collect")
        graph.add_edge("collect", "assess")
        graph.add_edge("finish", END)
        initial: ResearchState = {"documents": [], "candidates": [], "errors": [], "coverage": {},
            "seen_urls": [], "assessed_ids": [], "queries": [], "plans": 0, "actions": [], "node_trace": [], "event_blockers": []}
        try:
            async with tools.connect():
                state = await graph.compile().ainvoke(initial, {"recursion_limit": 30})
            return {"documents": state["documents"], "candidates": state["candidates"], "errors": state["errors"],
                    "coverage": state["coverage"], "trace": tools.trace + state["node_trace"],
                    "event_blockers": state["event_blockers"],
                    "discovery_mode": "blind_window", "queries": state["queries"],
                    "status": "partial" if state["errors"] else "completed"}
        finally:
            if self.owns_gateway:
                await self.gateway.close()
