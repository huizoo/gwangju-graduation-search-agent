"""한 학교의 근거를 관찰하고 다음 공개 조사 행동을 선택하는 Agent."""

from __future__ import annotations

import hashlib
import asyncio
import re
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from app.core.domain import validate
from app.validation.identity import scope
from app.integrations.providers import (
    DocumentReader, HttpGateway, ModelProvider, NeisProvider, ProviderError,
    TavilyProvider, canonical, official_channel, reserve, settle, utcnow,
)
from app.integrations.providers_student import StudentProvider
from app.validation.rules import extract_candidates, value_check
from app.agent.tool_boundary import ResearchTools
from app.agent.tracing import CURRENT, TraceRecorder


def _url_key(url: str) -> str:
    """문서 조각 식별자만 다른 요청을 중복 읽기로 묶는다.

    Args:
        url: 공개 HTTP 주소.
    Returns:
        fragment를 제거한 비교용 URL.
    """
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))


def _query_key(query: str) -> str:
    """공백과 대소문자만 다른 검색을 같은 실제 요청으로 비교한다.

    Args:
        query: 모델이 제안하거나 이미 실행한 공개 검색어.
    Returns:
        중복 검사와 다음 선택지 안내에 함께 사용할 문자열.
    """
    return re.sub(r"\s+", " ", query.strip()).casefold()


def _repair_instruction(failure: dict, season: dict) -> str:
    """실패 단계에 맞는 수정 지시를 만들고 사실을 임의 보충하지 못하게 한다.

    Args:
        failure: 원문을 제외한 모델 응답의 내부 실패 진단.
        season: 실제 행사일의 고정 조사 기간.
    Returns:
        기존 예산 안에서 한 번만 사용할 짧은 수정 지시.
    """
    kind, path, rule = (failure.get(key, "") for key in ("kind", "path", "validator"))
    if kind == "json_syntax":
        if failure.get("output_truncated") is True:
            return "응답이 출력 한도에서 잘렸습니다. 이유와 중복 후보를 줄여 완결된 JSON을 제출하세요. 원문에서 확인한 사실만 보존하고 새 값을 만들지 마세요."
        return "JSON 문법을 바로잡아 완결된 객체 하나만 제출하세요. 설명문이나 코드 울타리를 붙이지 말고 사실을 추가하지 마세요."
    if kind == "schema" and ".dates." in path and rule in {"pattern", "format"}:
        return (f"dates는 원문의 실제 행사일을 YYYY-MM-DD로 적습니다. 조사 기간은 {season['search_start']}~{season['search_end']}입니다. "
                "원문의 모든 행사일이 기간 밖이면 그 후보를 제외하고 다른 공식 경로를 선택하세요. 기간에 맞추려고 원문 날짜를 바꾸지 마세요.")
    if kind == "evidence_mapping":
        return "제공된 evidence_block_id와 그 구간의 실제 인용만 사용하세요. 연결할 근거가 없는 후보는 제외하고 새 사실을 만들지 마세요."
    if rule == "required":
        return "진단 경로의 필수 항목을 응답 계약대로 포함하세요. 값이 불명확하면 계약에서 허용한 null 또는 빈 배열을 사용하고 사실을 만들어 채우지 마세요."
    return "진단 경로와 검사 규칙에 맞게 응답을 한 번 수정하세요. 근거 없는 후보는 제외하고 날짜·시간·인용을 임의로 보충하지 마세요."


def _failure(document: dict) -> str:
    """미발견과 취득 실패를 구분해 다음 행동의 관찰로 전달한다.

    Args:
        document: 공개 읽기 결과.
    Returns:
        문서의 성공 또는 구체적인 실패 종류.
    """
    status = document.get("status", "provider_error")
    codes = set(document.get("reason_codes", []))
    if status == "ok":
        return "read_success"
    if "unsupported_format" in codes or status == "unsupported":
        return "unsupported_format"
    if status in {"no_data", "not_found"} or "not_found" in codes:
        return "not_found"
    return "access_failed"


class ResearchRunner:
    """학교·목적에 고정된 도구와 근거 검사를 바탕으로 조사를 반복한다.

    Args:
        settings: 작업 시작 시 고정한 실행 설정.
        meter: 영속 예산 예약기.
        gateway: 검사 및 HTTP 연결 공유에 사용할 Gateway.
    """

    def __init__(self, settings, meter=None, gateway=None):
        self.settings, self.meter = settings, meter
        self.gateway = gateway or HttpGateway(settings, meter)
        self.owns_gateway = gateway is None
        self.reader = DocumentReader(self.gateway)
        self.calendar = NeisProvider(settings, meter, self.gateway)
        self.students = StudentProvider(settings, meter, self.gateway)
        self.search = TavilyProvider(settings, meter, self.gateway)
        self.model = None

    async def run(self, institution: dict, season: dict, *, identifiers=None,
                  purpose="date_count", target_event=None, routes=None, existing_evidence=None) -> dict:
        """학교별 수집을 실행하고 선택한 경우에만 요약 진단을 전송한다.

        Args:
            institution: 조사할 학교의 공개 식별 정보.
            season: 작업 시작 시 고정한 실제 행사일 범위.
            identifiers: 공식 자료원의 학교 식별 코드.
            purpose: 날짜·인원 또는 특정 행사의 시각 조사.
            target_event: 시각 조사의 대상 행사.
            routes: 이전에 확인한 공식 진입 경로.
            existing_evidence: 사적 메모를 제외한 기존 공개 근거.
        Returns:
            최종 게시 검사를 받기 전의 근거·후보·조사 이력.
        Raises:
            ValueError: 조사 목적 또는 대상 행사가 올바르지 않은 경우.
        """
        parent = CURRENT.get()
        recorder = parent.recorder if parent else TraceRecorder(self.settings)
        try:
            if parent:
                return await self._research(institution, season, identifiers=identifiers, purpose=purpose,
                                            target_event=target_event, routes=routes, existing_evidence=existing_evidence)
            with recorder.span("school", school_name=institution.get("name"), institution_type=institution.get("type"),
                               search_start=season.get("search_start"), search_end=season.get("search_end"), purpose=purpose) as trace:
                result = await self._research(institution, season, identifiers=identifiers, purpose=purpose,
                                              target_event=target_event, routes=routes, existing_evidence=existing_evidence)
                trace.update(status=result["status"], stop_reason=result["stop_reason"],
                             missing_fields=result.get("missing_fields", []))
                return result
        finally:
            if parent is None:
                await asyncio.to_thread(recorder.close)

    async def _research(self, institution: dict, season: dict, *, identifiers=None,
                        purpose="date_count", target_event=None, routes=None, existing_evidence=None) -> dict:
        """정형 근거·재사용 경로 이후 필요한 공개 조사 행동만 선택한다.

        Args:
            institution: 학교의 공식 식별자와 확인한 공식 채널.
            season: 실제 행사일의 고정 조사 기간. 학년도는 각 근거·행사에 속한다.
            identifiers: 공식 자료원에서 확인한 복합 학교 코드.
            purpose: 기본 날짜·인원 조사 또는 time 시각 조사.
            target_event: 시각 조사에 연결할 현재 행사와 허용 날짜.
            routes: 공식성과 활용 가능성을 확인한 학교별 진입 경로.
            existing_evidence: 사적 메모를 제외한 공개 근거와 현재 허용값.
        Returns:
            원문·후보·인원·경로 시도 및 행동/관찰/종료 기록. 게시 판단은 저장층이 한다.
        Raises:
            ValueError: 시각 조사에 대상 행사가 없거나 알 수 없는 목적일 때.
        """
        purpose = "time" if purpose == "start_time" else purpose
        if purpose not in {"date_count", "time"} or (purpose == "time" and not target_event):
            raise ValueError("조사 목적 또는 대상 행사가 올바르지 않습니다.")
        identifiers = identifiers or []
        existing_evidence = existing_evidence or {}
        tools = ResearchTools(institution, season, identifiers, self.calendar, self.search, self.reader, self.students)
        limits = self.settings.data["execution"]
        documents, candidates, counts, errors, observations = [], [], [], [], []
        agent_trace, route_results, queries, actions = [], [], [], []
        seen_urls, assessed, candidate_keys, count_keys = set(), set(), set(), set()
        coverage, reviews = {}, []
        known_urls = {item.get("display_url") or item.get("source_key") for item in institution.get("known_documents", [])}
        available_links = set()
        search_links = set()
        stop_reason = "not_started"
        decision_count = 0
        format_repair_used, recovery_used = False, False
        repeated_requests, decision_feedback = set(), None
        route_queue = []
        university = institution.get("type") in {"university", "college", "junior_college"}
        expected_fields = ["start_time"] if purpose == "time" else ["event_date", "graduation_count"]
        queried_student_years = set()

        def record(kind, **entry):
            """결정과 도구 관찰을 순서가 보존된 내부 실행 기록으로 남긴다."""
            agent_trace.append({"step": len(agent_trace) + 1, "kind": kind, "at": utcnow(), **entry})

        def add_error(provider, code, message):
            """같은 실패를 중복 기록하지 않고 공개된 오류 코드만 보관한다."""
            if not any(item["provider"] == provider and item["code"] == code for item in errors):
                errors.append({"provider": provider, "code": code, "message": message})

        def payload():
            """호출 경계마다 저장할 현재 근거와 전체 시도 이력을 조립한다."""
            return {"documents": documents, "candidates": candidates, "counts": counts,
                    "errors": errors, "coverage": coverage, "trace": tools.trace,
                    "agent_trace": agent_trace, "routes": route_results, "event_blockers": [],
                    "purpose": purpose, "target_event_id": target_event.get("id") if target_event else None,
                    "discovery_mode": "school_agent", "queries": queries,
                    "stop_reason": stop_reason}

        async def checkpoint(stage):
            """취소와 예산 경계를 유지하며 확보한 근거를 다음 호출 전에 보존한다."""
            if self.meter is not None and hasattr(self.meter, "checkpoint"):
                await self.meter.checkpoint(payload(), stage=stage)

        def add_candidates(items, method):
            """모델과 정형 파서 후보를 같은 저장 및 검증 경로에 놓는다."""
            document_map = {document["id"]: document for document in documents}
            for item in items:
                document = document_map.get(item.get("document_id"))
                if not document:
                    record("candidate_rejected", reason="unknown_document")
                    continue
                try:
                    candidate = validate("Candidate", item["candidate"])
                except Exception:
                    record("candidate_rejected", reason="candidate_contract")
                    continue
                key = hashlib.sha256(canonical([document["content_hash"], candidate]).encode()).hexdigest()
                if key not in candidate_keys:
                    candidates.append({"document_id": document["id"], "candidate": candidate,
                                       "fingerprint": key, "extraction_method": method})
                    candidate_keys.add(key)

        def add_counts(items):
            """인원 후보도 근거별로 보존하고 검증 없이 완료값으로 사용하지 않는다."""
            for item in items:
                key = hashlib.sha256(canonical(item).encode()).hexdigest()
                if key not in count_keys:
                    counts.append(item)
                    count_keys.add(key)

        def assess():
            """현재 근거를 공통 검증기로 평가해 부족함과 충돌을 계산한다."""
            nonlocal reviews
            document_map = {document["id"]: document for document in documents}
            for document in documents:
                if document["id"] in assessed:
                    continue
                assessed.add(document["id"])
                add_candidates([{"document_id": document["id"], "candidate": candidate}
                                for candidate in extract_candidates(document, institution, season)], "registered_mapping")
                for url in self.reader.links.get(document["id"], []):
                    if official_channel(institution, url):
                        available_links.add(url)
            reviews = []
            dates, times, populations, statuses = set(), set(), set(), set()
            date_groups = {}
            supported_events = []
            for item in candidates:
                candidate, document = item["candidate"], document_map[item["document_id"]]
                event = {"id": target_event["id"] if target_event else str(uuid4()),
                         "title": candidate["event_title"], "event_type": candidate["event_type"],
                         "audience": candidate.get("audience"), "campus_label": candidate.get("campus_label"),
                         "institution": institution, "_identifiers": identifiers,
                         "academic_year": candidate.get("academic_year"),
                         "ceremony_term": candidate.get("ceremony_term")}
                if target_event:
                    event.update({key: target_event[key] for key in ("title", "event_type", "audience", "campus_label", "academic_year", "ceremony_term") if key in target_event})
                observation = {"id": str(uuid4()), "candidate": candidate,
                               "document_id": document["id"], "institution_id": institution["id"],
                               "event_id": event["id"], "season_id": season["id"]}
                checks = {}
                for field in ("ceremony_status", "event_date", "start_time"):
                    value = candidate[field]
                    if value is None or (field == "event_date" and value.get("precision") == "unknown"):
                        continue
                    proof = value_check(field, value, observation, document, event, season, institution=institution)
                    checks[field] = {"status": proof["status"], "reason_codes": proof["reason_codes"]}
                    if proof["status"] != "supported":
                        continue
                    if field == "event_date" and value.get("precision") == "day":
                        dates.add(canonical(value))
                        date_groups.setdefault(scope(candidate, institution), set()).add(canonical(value))
                    elif field == "ceremony_status":
                        statuses.add(value)
                    elif field == "start_time":
                        bound = target_event.get("event_date", target_event.get("date")) if target_event else None
                        if not target_event or bound == candidate["event_date"] or bound == candidate["event_date"].get("date"):
                            times.add(value)
                reviews.append({"document_id": document["id"], "candidate": candidate, "checks": checks})
                if (checks.get("event_date", {}).get("status") == "supported"
                        and checks.get("ceremony_status", {}).get("status") == "supported"
                        and candidate["ceremony_status"] == "scheduled"):
                    supported_events.append(candidate)
            from app.validation.claims import validate_count
            population_groups, time_groups = {}, {}
            for review in reviews:
                if review.get("checks", {}).get("start_time", {}).get("status") == "supported":
                    item = review["candidate"]
                    time_groups.setdefault(scope(item, institution), set()).add(item["start_time"])
            expected_groups = ({scope(event, institution) for event in supported_events} if university else
                               {("year", event["academic_year"]) for event in supported_events})
            tools.allow_student_years(event.get("academic_year") for event in supported_events)
            for count in counts:
                document = document_map.get(count.get("document_id"))
                count_event = target_event
                if not count_event:
                    matching = {}
                    for event in supported_events:
                        event_days = event["event_date"].get("dates") or [event["event_date"].get("date")]
                        if (event.get("academic_year") == count["academic_year"]
                                and (not university or count.get("event_date") in event_days)
                                and (not count.get("event_key_hint") or count["event_key_hint"] == event.get("event_key_hint"))):
                            matching[scope(event, institution) if university else ("year", event["academic_year"])] = event
                    if len(matching) == 1:
                        count_event = next(iter(matching.values()))
                proof = validate_count(count, document, institution, season, event=count_event) if document else {"allowed": False, "reasons": ["unknown_document"]}
                reviews.append({"document_id": count.get("document_id"), "count": count, "checks": proof})
                if proof["allowed"] and count_event is not None:
                    key = scope(count_event, institution) if university and count_event else ("year", count["academic_year"])
                    population_groups.setdefault(key, set()).add(count["value"])
            # 서로 다른 학년도·본식의 인원은 별개 사실이며 같은 행사 안의 값 차이만 충돌이다.
            conflicts = []
            if any(len(values) > 1 for values in population_groups.values()):
                conflicts.append("graduation_count")
            if (purpose == "time" and len(times) > 1) or (purpose == "date_count" and any(len(values) > 1 for values in time_groups.values())):
                conflicts.append("start_time")
            if any(len(values) > 1 for values in date_groups.values()):
                conflicts.append("event_date")
            missing = []
            if purpose == "date_count":
                if not dates or "event_date" in conflicts:
                    missing.append("event_date")
                if not expected_groups or any(len(population_groups.get(key, set())) != 1 for key in expected_groups):
                    missing.append("graduation_count")
            elif len(times) != 1:
                missing.append("start_time")
            if statuses & {"cancelled", "not_held", "unclear"}:
                conflicts.append("ceremony_status")
            return missing, sorted(set(conflicts))

        async def collect_student_years():
            """새로 검증한 행사 학년도만 조회해 지연 행사와 다른 연도 인원을 구분한다."""
            if purpose != "date_count" or university:
                return
            results = coverage.setdefault("students", {}).setdefault("academic_year_coverage", [])
            for year in sorted(tools.student_years - queried_student_years):
                queried_student_years.add(year)
                result = await tools.call("get_graduating_students", {
                    "institution_id": institution["id"], "season_id": season["id"], "academic_year": year})
                documents.extend(result["documents"])
                add_counts(result["counts"])
                results.append({"academic_year": year,
                    **{key: value for key, value in result.items() if key not in {"documents", "counts"}}})
                observation = {"tool": "get_graduating_students", "academic_year": year,
                    "status": result["status"], "reason_codes": result["reason_codes"]}
                observations.append(observation)
                record("observation", **observation)
                await checkpoint("students_collected")

        async def read(url, route=None):
            """공식 출처·중복·문서 한도를 확인한 후 MCP로 원문을 읽는다."""
            key = _url_key(url)
            if key in seen_urls:
                return "duplicate_request"
            if len(seen_urls) >= limits["item_max_document_fetches"]:
                return "document_limit"
            if not official_channel(institution, url):
                return "unverified_source"
            seen_urls.add(key)
            document = await tools.call("read_public_document", {"institution_id": institution["id"], "url": url})
            if url in known_urls:
                success = document["status"] == "ok" and document["reading"]["content_match"] == "expected"
                document["recheck"] = {"status": "succeeded" if success else "failed",
                    "attempted_at": document["last_checked_at"], "reason_codes": [] if success else ["recheck_failed"]}
            documents.append(document)
            outcome = _failure(document)
            academic_years = sorted({int(year) for segment in document.get("segments", [])
                                     for year in re.findall(r"(20\d{2})\s*학년도", segment["text"])})
            from app.validation.claims import explicit_days
            source_days = {day for segment in document.get("segments", []) for day in explicit_days(segment["text"])}
            if (outcome == "read_success" and source_days
                    and not any(season["search_start"] <= day <= season["search_end"] for day in source_days)):
                outcome = "outside_window"
            observation = {"tool": "read_public_document", "url": url, "document_id": document["id"],
                           "status": document["status"], "outcome": outcome, "reason_codes": document["reason_codes"],
                           "academic_years": academic_years}
            observations.append(observation)
            record("observation", **observation)
            page_links = [link for link in self.reader.links.get(document["id"], []) if official_channel(institution, link)]
            is_homepage = institution.get("homepage_url") and _url_key(url) == _url_key(institution["homepage_url"])
            is_listing = len(page_links) >= 2 and bool(re.search(r"공지사항|학사일정|학사공지", document.get("title") or ""))
            # 개별 공지는 사실의 근거로 남기고 검증한 목록·홈페이지만 재사용 경로로 제안한다.
            reusable = bool(outcome == "read_success" and (is_homepage or is_listing))
            route_results.append({"url": url, "purpose": (route or {}).get("purpose", purpose),
                "reading_method": document.get("reading", {}).get("method"), "checked_at": utcnow(),
                "status": "succeeded" if outcome == "read_success" else "failed",
                "failure_reason": None if outcome == "read_success" else outcome,
                "document_id": document["id"], "route_id": (route or {}).get("id"),
                "reusable": reusable, "links": page_links})
            if document["status"] != "ok":
                for code in document["reason_codes"] or ["provider_error"]:
                    add_error("reader", code, f"공식 원문 읽기: {outcome}")
            # 새 원문만 먼저 저장하면 옛 버전이 보류되어 같은 작업의 행사일 검사에
            # 걸린다. 정형 후보의 재검증까지 묶어 원문과 필드 지원을 함께 교체한다.
            assess()
            await checkpoint("document_read")
            return outcome

        def action_options():
            """방문하지 않은 공식 경로와 남은 검색 예산만 선택지로 제공한다."""
            links = {}
            if len(seen_urls) < limits["item_max_document_fetches"]:
                for url in sorted(available_links):
                    key = _url_key(url)
                    if key not in seen_urls and official_channel(institution, url):
                        links.setdefault(key, url)
            # 검색에서 발견한 본문을 메뉴보다 먼저 검토하되 URL에서 행사 사실을 추론하지 않는다.
            eligible = sorted(links.values(), key=lambda url: (
                _url_key(url) not in search_links,
                not bool(re.search(r"calendar|schedule|haksa|notice|board|bbs", url, re.I)), url))
            return {"fetch_urls": eligible[:8],
                    "search_allowed": bool(self.settings.secrets.get("tavily_api_key"))
                        and len(queries) < limits["item_max_search_calls"],
                    "blocked_queries": [_query_key(query) for query in queries]}

        def make_context(missing, conflicts):
            """사적 수기 내용 없이 공개 근거와 앞선 관찰을 모델에 전달한다."""
            options = action_options()
            return {"purpose": purpose, "expected_fields": expected_fields,
                    "institution_type": institution.get("type"), "representative_ceremony_only": university,
                    "target_event": target_event, "documents": documents, "candidate_reviews": reviews,
                    "missing_fields": missing, "conflicts": conflicts, "observations": observations,
                    "prior_actions": actions, "available_links": options["fetch_urls"],
                    "action_options": options, "decision_feedback": decision_feedback,
                    "available_tools": (["search_web"] if options["search_allowed"] else [])
                        + (["fetch_document"] if options["fetch_urls"] else []),
                    "reusable_routes": routes or [],
                    "existing_evidence": existing_evidence,
                    "limits_remaining": {"decisions": max(0, limits["item_max_replans"] - decision_count),
                        "search_calls": max(0, limits["item_max_search_calls"] - len(queries)),
                        "document_fetches": max(0, limits["item_max_document_fetches"] - len(seen_urls))}}

        try:
            async with tools.connect():
                record("start", purpose=purpose, expected_fields=expected_fields)
                if purpose == "date_count" and institution["type"] in {"elementary", "middle", "high"}:
                    result = await tools.call("get_school_calendar", {
                        "institution_id": institution["id"], "season_id": season["id"],
                        "from_date": season["search_start"], "to_date": season["search_end"]})
                    documents.extend(result["documents"])
                    coverage["calendar"] = {key: value for key, value in result.items() if key != "documents"}
                    coverage["calendar"]["checked_at"] = utcnow()
                    observation = {"tool": "get_school_calendar", "status": result["status"], "reason_codes": result["reason_codes"]}
                    observations.append(observation)
                    record("observation", **observation)
                    if result["status"] not in {"ok", "no_data"}:
                        for code in result["reason_codes"]:
                            add_error("neis", code, "공식 학사일정 조회를 완료하지 못했습니다.")
                    await checkpoint("calendar_collected")
                    assess()
                    await collect_student_years()
                # 이전 사실 문서는 올해 근거로 복사하지 않고, 필요하면 같은 URL을 새로 읽는다.
                for route in routes or []:
                    route_purpose = route.get("purpose", "date_count")
                    verified = route.get("status") in {"verified", "active", "succeeded"} or route.get("verified") is True or bool(route.get("verified_at"))
                    if verified and route.get("url") and not (purpose == "date_count" and route_purpose in {"time", "start_time"}):
                        route_queue.append(route)
                for known in institution.get("known_documents", [])[:3]:
                    url = known.get("display_url") or known.get("source_key")
                    if url and url.startswith(("http://", "https://")) and not url.startswith("https://open.neis.go.kr/hub"):
                        route_queue.append({"url": url, "purpose": purpose})
                homepage = institution.get("homepage_url")
                if homepage:
                    route_queue.append({"url": homepage, "purpose": purpose})
                missing, conflicts = assess()
                if not missing and not conflicts:
                    stop_reason = "structured_evidence_sufficient"
                else:
                    for route in route_queue:
                        await read(route["url"], route)
                        assess()
                        await collect_student_years()
                        missing, conflicts = assess()
                        if not missing and not conflicts:
                            stop_reason = "verified_route_sufficient"
                            break
                    else:
                        stop_reason = "investigating"
                while stop_reason == "investigating":
                    assess()
                    await collect_student_years()
                    missing, conflicts = assess()
                    await checkpoint("evidence_assessed")
                    if not missing and not conflicts:
                        stop_reason = "evidence_sufficient"
                        break
                    if decision_count >= limits["item_max_replans"]:
                        stop_reason = "decision_limit"
                        break
                    reservation_id = await reserve(self.meter, "replans")
                    decision_count += 1
                    decision_charge = None
                    if self.model is None:
                        self.model = ModelProvider(self.settings, self.meter, self.gateway)
                    try:
                        with CURRENT.get().recorder.span("model", backend=self.settings.data["app"]["llm_backend"]) as trace:
                            try:
                                decision = await self.model.decide(institution, season, make_context(missing, conflicts))
                                validate("AgentDecision", decision)
                                trace.update(status="ok", action=decision.get("action", {}).get("kind"))
                            finally:
                                # 마지막 응답에서 실제 확인한 토큰만 보낸다. 예약량은 사용량이 아니다.
                                trace.update(**(getattr(self.model, "last_usage", None) or {}))
                    except ProviderError as error:
                        diagnostics = getattr(self.model, "last_diagnostics", None)
                        if diagnostics:
                            record("model_proposal", **diagnostics)
                        add_error(error.provider, error.code, error.message)
                        failure = getattr(error, "diagnostic", None) or getattr(self.model, "last_failure", None) or {}
                        record("decision_failed", reason=error.code, diagnostic=failure)
                        if (error.code == "parse_failed" and not format_repair_used
                                and decision_count < limits["item_max_replans"]):
                            # 전송 재시도와 별개다. 구체적 형식 수정도 기존 판단·호출 예산을 사용한다.
                            format_repair_used = True
                            decision_feedback = {"kind": "invalid_contract", "details": failure,
                                "instruction": _repair_instruction(failure, season)}
                            observations.append({"tool": "agent_decision", "status": "rejected",
                                "outcome": "invalid_contract", "reason_codes": ["parse_failed"],
                                **decision_feedback})
                            await checkpoint("decision_rejected")
                            continue
                        stop_reason = "model_unavailable"
                        break
                    except Exception as error:
                        # 사용자의 멈춤·예산 예외는 일반 계약 오류로 삼키지 않는다.
                        from app.agent.jobs import BudgetStop
                        if isinstance(error, BudgetStop):
                            # 요청 전 한도 거절은 모델 판단을 수행한 것으로 세지 않는다.
                            # 전송 여부가 불명확한 어댑터는 보수적으로 비용을 유지한다.
                            if getattr(self.model, "last_request_sent", None) is False:
                                decision_charge = 0
                            raise
                        add_error("model", "parse_failed", "Agent 행동이 허용 계약과 맞지 않습니다.")
                        stop_reason = "invalid_decision"
                        record("decision_failed", reason="invalid_decision")
                        break
                    finally:
                        await settle(self.meter, reservation_id, decision_charge)
                    action = decision["action"]
                    diagnostics = getattr(self.model, "last_diagnostics", None)
                    if diagnostics:
                        record("model_proposal", **diagnostics)
                    record("decision", action=action, reason=decision["reason"], missing_fields=missing, conflicts=conflicts)
                    add_candidates(decision.get("candidates", []), "model_candidate")
                    add_counts(decision.get("counts", []))
                    assess()
                    await collect_student_years()
                    missing, conflicts = assess()
                    await checkpoint("agent_decision")
                    if not missing and not conflicts:
                        # 마지막 판단의 정상 후보도 먼저 채택하고 불필요한 후속 도구를 실행하지 않는다.
                        stop_reason = "evidence_sufficient"
                        break
                    violation = None
                    if (university and "event_date" in missing and action["objective"] == "graduation_count"
                            and action["kind"] != "stop"):
                        violation = "representative_event_required"
                    elif purpose == "date_count" and (action["objective"] == "start_time" or
                            (action["kind"] == "search_web" and re.search(r"시간|시각|몇\s*시|시작\s*시|start\s*time|ceremony\s*time", action.get("query") or "", re.I))):
                        violation = "purpose_mismatch"
                    elif action["kind"] == "search_web" and _query_key(action["query"]) in {_query_key(query) for query in queries}:
                        violation = "duplicate_request"
                    elif action["kind"] == "search_web" and len(queries) >= limits["item_max_search_calls"]:
                        violation = "search_limit"
                    elif action["kind"] == "fetch_document" and _url_key(action["url"]) in seen_urls:
                        violation = "duplicate_request"
                    elif action["kind"] == "fetch_document" and not official_channel(institution, action["url"]):
                        violation = "unverified_source"
                    actions.append(action)
                    if violation:
                        observations.append({"action": action, "status": "rejected", "outcome": violation})
                        record("action_rejected", action=action, reason=violation)
                        if violation == "duplicate_request":
                            key = (action["kind"], _query_key(action["query"]) if action["kind"] == "search_web"
                                   else _url_key(action["url"]))
                            if key in repeated_requests:
                                options = action_options()
                                if options["fetch_urls"] and not recovery_used:
                                    # 한 번만 남은 공식 원문으로 복구한다. 같은 읽기·시간·호출 한도를 통과한다.
                                    recovery_used = True
                                    fallback = {"kind": "fetch_document", "objective": action["objective"],
                                                "url": options["fetch_urls"][0], "query": None}
                                    actions.append(fallback)
                                    record("recovery_action", action=fallback, reason="duplicate_request")
                                    await read(fallback["url"])
                                    repeated_requests.clear()
                                    decision_feedback = None
                                    continue
                                stop_reason = "repeated_action"
                                break
                            repeated_requests.add(key)
                            decision_feedback = {"kind": "duplicate_request", "details": {"action": action["kind"]},
                                "instruction": "방금 거절한 검색어나 URL은 이미 실행했습니다. 미방문 공식 URL 또는 다른 검색어를 선택하세요. 시간 조사라면 target_event.event_date의 실제 행사일을 검색에 참고하고 학년도를 행사 연도로 바꾸지 마세요."}
                        await checkpoint("action_rejected")
                        continue
                    repeated_requests.clear()
                    decision_feedback = None
                    if action["kind"] == "stop":
                        stop_reason = "agent_stop"
                        break
                    if action["kind"] == "fetch_document":
                        outcome = await read(action["url"])
                        if outcome in {"document_limit", "unverified_source"}:
                            record("action_rejected", action=action, reason=outcome)
                            stop_reason = outcome
                        continue
                    queries.append(action["query"])
                    domains = sorted({urlsplit(item["url"]).hostname for item in institution.get("official_channels", [])
                                      if urlsplit(item["url"]).hostname != "open.neis.go.kr"})
                    result = await tools.call("search_public_web", {"query": action["query"], "include_domains": domains[:10], "max_results": 5})
                    usable = [item for item in result["results"] if official_channel(institution, item["url"])]
                    available_links.update(item["url"] for item in usable)
                    search_links.update(_url_key(item["url"]) for item in usable)
                    observation = {"tool": "search_public_web", "query": action["query"], "status": result["status"],
                                   "reason_codes": result["reason_codes"], "results": usable}
                    observations.append(observation)
                    record("observation", **observation)
                    if result["status"] not in {"ok", "no_data"}:
                        for code in result["reason_codes"]:
                            add_error("tavily", code, "공개 검색을 완료하지 못했습니다.")
                    await checkpoint("search_observed")
                missing, conflicts = assess()
                record("finish", reason=stop_reason, missing_fields=missing, conflicts=conflicts)
                await checkpoint("agent_finished")
            result = payload()
            result.update(status="partial" if missing or conflicts or errors else "completed",
                          missing_fields=missing, conflicts=conflicts)
            return result
        finally:
            if self.owns_gateway:
                await self.gateway.close()
