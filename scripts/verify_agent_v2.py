"""실제 선택 모델과 공식 자료원을 사용자 DB 복사본에서 조사·게시까지 검사한다."""

import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from app.api import create_app
from app.config import load_settings
from app.providers import DocumentReader, HttpGateway, ModelProvider, utcnow
from app.research import ResearchRunner


class EvidenceGateway(HttpGateway):
    """비밀 요청 헤더 없이 선택 모델의 실제 응답과 호출 수만 보존한다.

    Args:
        settings: 기존 선택 설정.
        out: 격리한 내부 검증 결과 폴더.
        meter: 실제 작업에 연결할 예산 기록기. 독립 모델 진단은 None.
    """

    def __init__(self, settings, out, meter=None):
        super().__init__(settings, meter=meter)
        self.out, self.model_calls = out, 0

    async def request(self, method, url, **kwargs):
        """공개 자료 입력에 대한 모델 응답을 검증 증거로 남긴다.

        Args:
            method: HTTP 메서드.
            url: 공개 자료 또는 선택 모델의 요청 주소.
            **kwargs: Gateway에 전달할 요청 옵션. 비밀 헤더는 기록하지 않는다.
        Returns:
            실제 HTTP 응답.
        """
        is_model = url.endswith("/api/chat") or url == "https://api.openai.com/v1/responses"
        if is_model:
            self.model_calls += 1
        response = await super().request(method, url, **kwargs)
        if is_model:
            self.out.joinpath(f"model-response-{self.model_calls}.json").write_text(
                json.dumps(response.json(), ensure_ascii=False, indent=2) + "\n")
        return response


async def collect(settings, out, school, season, identifiers, smoke):
    """날짜 정답을 입력하지 않고 학교별 실제 Agent를 실행한다.

    Args:
        settings: 선택 백엔드와 공개 자료 접근 설정.
        out: 사용자 DB와 분리한 증빙 폴더.
        school: 조사할 실제 학교.
        season: 정답 날짜를 포함하지 않는 조사 기간.
        identifiers: 공식 자료원의 학교 식별자.
        smoke: 통제된 접근 실패 관찰만 모델에 전달할지 여부.
    Returns:
        실제 응답과 모델 호출 수. 통제된 관찰은 실제 발견 성과가 아니다.
    """
    gateway = EvidenceGateway(settings, out)
    try:
        if smoke:
            context = {"purpose": "date_count", "expected_fields": ["event_date", "graduation_count"],
                "missing_fields": ["event_date", "graduation_count"], "conflicts": [],
                "observations": [{"tool": "read_public_document", "url": school["homepage_url"],
                                  "status": "provider_error", "outcome": "access_failed"}],
                "prior_actions": [], "candidate_reviews": [], "available_links": [], "reusable_routes": [],
                "documents": [], "limits_remaining": {"decisions": 5, "search_calls": 4, "document_fetches": 8}}
            result = {"mode": "actual_model_controlled_observation", "decision":
                      await ModelProvider(settings, gateway=gateway).decide(school, season, context)}
        else:
            result = await ResearchRunner(settings, gateway=gateway).run(school, season, identifiers=identifiers)
        return result, gateway.model_calls
    finally:
        await gateway.close()


async def collect_job(app, out, school, season, learned_calendar=False):
    """실제 JobManager 예산·체크포인트·저장을 포함해 한 학교를 검사한다.

    Args:
        app: 원본 DB 복사본으로 생성한 실제 앱.
        out: 실제 응답·작업 기록을 남길 폴더.
        school: 검증할 실제 학교.
        season: 날짜 정답을 포함하지 않은 조사 시즌.
        learned_calendar: 앞선 실제 Agent 검색에서 발견한 공식 경로를 재확인할지 여부.
    Returns:
        실제 실행 결과와 선택 모델 호출 수. 종료 제한도 작업 기록에 남는다.
    """
    captured, gateways = [], []
    if learned_calendar:
        source = ROOT / "runtime/improvements-20261021/agent-live/calendar-followup/document.json"
        url = json.loads(source.read_text())["display_url"]
        gateway = EvidenceGateway(app.state.runtime.settings, out)
        try:
            reader = DocumentReader(gateway)
            document = await reader.read(school, url)
            prepared = {"documents": [document], "candidates": [], "counts": [], "purpose": "date_count",
                "stop_reason": "previous_agent_discovered_route_rechecked",
                "routes": [{"url": url, "purpose": "date_count", "reading_method": document["reading"]["method"],
                            "checked_at": document["retrieved_at"], "status": "succeeded" if document["status"] == "ok" else "failed",
                            "document_id": document["id"], "reusable": True, "links": reader.links.get(document["id"], [])}]}
            app.state.runtime.service.save_research(school["id"], season["id"], prepared)
            (out / "route-preparation.json").write_text(json.dumps(prepared, ensure_ascii=False, indent=2) + "\n")
        finally:
            await gateway.close()

    class CapturingRunner(ResearchRunner):
        """실제 조사·예산 처리는 그대로 두고 완료 결과만 검사에 보존한다."""

        def __init__(self, settings, meter=None):
            gateway = EvidenceGateway(settings, out, meter=meter)
            gateways.append(gateway)
            super().__init__(settings, meter=meter, gateway=gateway)

        async def run(self, institution, season, *, identifiers=None, purpose="date_count",
                      target_event=None, routes=None, existing_evidence=None):
            """프로덕션 실행기가 넘긴 목적·경로·시각 대상과 동일하게 조사한다."""
            try:
                result = await super().run(institution, season, identifiers=identifiers, purpose=purpose,
                    target_event=target_event, routes=routes, existing_evidence=existing_evidence)
                captured.append(result)
                return result
            finally:
                await self.gateway.close()

    manager = app.state.runtime.jobs
    manager.runner_factory = CapturingRunner
    job = manager.create({"kind": "schedule_scan", "season_id": season["id"], "mode": "recheck",
        "selection": {"scope": "ids", "institution_ids": [school["id"]]}, "limits": None}, str(uuid4()))
    await manager.run(job["id"])
    details = {"job": manager.get(job["id"]), "items": manager.items(job["id"])}
    with manager.store.transaction() as conn:
        details["checkpoint_histories"] = [
            {"item_id": row["id"], "history": json.loads(row["history_json"])}
            for row in conn.execute("SELECT id,history_json FROM job_items WHERE job_id=?", (job["id"],))]
    (out / "job.json").write_text(json.dumps(details, ensure_ascii=False, indent=2) + "\n")
    result = captured[0] if captured else {"candidates": [], "documents": [], "stop_reason": "job_stopped_before_return"}
    result["verification_job_id"] = job["id"]
    return result, sum(gateway.model_calls for gateway in gateways)


async def verify_adaptation(out: Path) -> dict:
    """통제된 실패 관찰 네 종류에 대한 실제 선택 모델의 다음 행동을 기록한다.

    Args:
        out: 시나리오 입력과 실제 응답을 저장할 새 폴더.
    Returns:
        실제 자료 조사와 구분한 시나리오별 적응 행동 결과.
    """
    base = ROOT / "runtime/improvements-20261021/agent-live/calendar-followup"
    metadata = json.loads((base / "metadata.json").read_text())
    school, season = metadata["institution"], metadata["season"]
    calendar = json.loads((base / "document.json").read_text())["display_url"]
    homepage = school["homepage_url"]
    cases = [
        ("path_failure", "access_failed", "공식 홈페이지를 읽지 못했으며 다른 공식 학사일정 링크는 아직 읽지 않았다.", []),
        ("past_year", "wrong_year", "읽은 안내는 2025학년도이며 2026학년도 행사 근거가 아니다.", []),
        ("other_event", "different_event", "읽은 안내는 학위복 대여와 기념촬영이며 대표 학위수여식 본식 날짜가 아니다.", []),
        ("conflict", "conflict", "같은 대표 행사 날짜에 서로 다른 제안이 있어 변경·취소 공지 재확인이 필요하다.", ["event_date"]),
    ]
    items = []
    for name, outcome, message, conflicts in cases:
        target = out / name
        target.mkdir(parents=True)
        prior_url = calendar if name == "conflict" else homepage
        prior = [{"kind": "fetch_document", "objective": "event_date", "url": prior_url, "query": None}]
        if name == "conflict":
            prior.append({"kind": "search_web", "objective": "event_date", "query": "광주대학교 2026학년도 학위수여식 일정", "url": None})
        context = {"purpose": "date_count", "expected_fields": ["event_date", "graduation_count"],
            "missing_fields": ["event_date", "graduation_count"], "conflicts": conflicts,
            "documents": [], "candidate_reviews": [], "prior_actions": prior,
            "observations": [{"tool": "read_public_document", "url": prior_url,
                              "status": "rejected", "outcome": outcome, "reason": message}],
            "available_links": [] if name == "conflict" else [calendar], "reusable_routes": [],
            "available_tools": ["search_web", "fetch_document"],
            "limits_remaining": {"decisions": 4, "search_calls": 3, "document_fetches": 5}}
        (target / "controlled-context.json").write_text(json.dumps(context, ensure_ascii=False, indent=2) + "\n")
        gateway = EvidenceGateway(load_settings(), target)
        item = {"scenario": name, "evidence_type": "controlled_observation_actual_selected_model", "published": False}
        try:
            decision = await ModelProvider(gateway.settings, gateway=gateway).decide(school, season, context)
            action = decision["action"]
            changed = action["kind"] != "stop" and action not in prior
            conflict_specific = name != "conflict" or any(
                word in (action.get("query") or "") + decision["reason"] for word in ("변경", "취소", "최신", "정정"))
            item.update(decision=decision, passed=changed and conflict_specific and not decision["candidates"] and not decision["counts"])
        except Exception as error:
            item.update(passed=False, error_type=type(error).__name__, error_code=getattr(error, "code", "verification_error"))
        finally:
            await gateway.close()
        items.append(item)
        (target / "result.json").write_text(json.dumps(item, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(item, ensure_ascii=False), flush=True)
    report = {"mode": "controlled_observation_actual_selected_model", "items": items,
              "passed": all(item["passed"] for item in items), "real_world_accuracy_verified": False}
    (out / "adaptation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def publish_actual_decision(decision_folder: Path, out: Path) -> dict:
    """실제 모델 응답을 빈 격리 DB의 공통 저장·게시 경로로 검사한다.

    Args:
        decision_folder: 실제 decision.json과 diagnostics.json이 있는 폴더.
        out: 이번 검사만 기록하는 새 폴더.
    Returns:
        API 행·관측·게시 근거를 포함하는 검증 결과. 모델 재생과 새 호출을 구분한다.
    """
    from app.official_registry import VerifiedOfficialRegistry
    source = decision_folder.parent
    document = json.loads((source / "document.json").read_text())
    metadata = json.loads((source / "metadata.json").read_text())
    decision = json.loads((decision_folder / "decision.json").read_text())
    diagnostic = json.loads((decision_folder / "diagnostics.json").read_text())
    captured = json.loads((ROOT / "runtime/sources-20261021/university-provider-live.json").read_text())
    settings = load_settings(process_values={"APP_DATA_DIR": str(out / "data"), "AUTO_OPEN_BROWSER": "false"})
    app = create_app(settings)
    service = app.state.runtime.service
    verified = VerifiedOfficialRegistry(captured["registry"], tuple(captured["authorities"]), captured["provenance"])
    service.save_registry_sync({"registry": captured["registry"], "_verified_registry": verified})
    school = next(item for item in service.list_institutions(q=metadata["institution"]["name"])["items"]
                  if item["name"] == metadata["institution"]["name"])
    season = next(item for item in service.list_seasons()["items"]
                  if item["academic_year"] == metadata["season"]["academic_year"])
    # 원문·인용·공식 관계는 보존하고 새 격리 명부의 기관 UUID만 연결한다.
    document["institution_id"] = school["id"]
    result = {"documents": [document], "candidates": [
        {**item, "extraction_method": "model_candidate"} for item in decision["candidates"]],
        "counts": decision["counts"], "purpose": "date_count",
        "agent_trace": [{"kind": "model_proposal", **diagnostic},
                        {"kind": "decision", "action": decision["action"], "reason": decision["reason"]}],
        "stop_reason": "actual_model_decision_publication_check"}
    saved = service.save_research(school["id"], season["id"], result)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        rows = [row for row in client.get("/api/results", params={"season_id": season["id"]}).json()["rows"]
                if row["institution_id"] == school["id"]]
        token = client.get("/api/bootstrap").json()["token"]
        export = client.post("/api/exports/html", headers={"X-App-Token": token, "Idempotency-Key": str(uuid4())},
            json={"season_id": season["id"], "include_notes": False, "publication_policy_version": "4.0"})
        assert export.status_code == 202
        (out / "result.html").write_bytes(client.get(export.json()["url"]).content)
    snapshot = service.snapshot(season["id"])
    events = [event for event in snapshot["events"] if event["institution"]["id"] == school["id"]]
    report = {"mode": "actual_model_response_publication_replay", "response_folder": str(decision_folder),
        "model_called_during_publication": False, "date_seeded": False, "fixture_document": False,
        "original_registry_provenance": captured["provenance"], "saved": saved, "rows": rows, "events": events,
        "passed": any(row.get("date") for row in rows), "user_database_written": False}
    (out / "publication.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main():
    """원본은 SQLite 읽기 전용으로 복제하고 실제 조사 결과만 복사본에 저장한다."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--school", default="광주대학교")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--job", action="store_true")
    parser.add_argument("--learned-calendar", action="store_true")
    parser.add_argument("--adaptation", action="store_true")
    parser.add_argument("--publish-decision", type=Path)
    args = parser.parse_args()
    out = ROOT / "runtime/improvements-20261021/agent-live" / datetime.now().strftime("run-%H%M%S-%f")
    if args.adaptation:
        out.mkdir(parents=True)
        report = asyncio.run(verify_adaptation(out))
        print(json.dumps({"out": str(out), "passed": report["passed"]}, ensure_ascii=False))
        return
    if args.publish_decision:
        out.mkdir(parents=True)
        report = publish_actual_decision(args.publish_decision, out)
        print(json.dumps({"out": str(out), "passed": report["passed"], "rows": report["rows"]}, ensure_ascii=False))
        return
    clone = out / "data/live/graduation.sqlite3"
    clone.parent.mkdir(parents=True)
    with sqlite3.connect((ROOT / "data/live/graduation.sqlite3").as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(clone) as target:
            source.backup(target)
    settings = load_settings(process_values={"APP_DATA_DIR": str(out / "data"), "AUTO_OPEN_BROWSER": "false"})
    app = create_app(settings)
    service = app.state.runtime.service
    school = next(item for item in service.list_institutions(q=args.school)["items"] if item["name"] == args.school)
    season = next(item for item in service.list_seasons()["items"] if item["academic_year"] == 2026)
    with service.store.transaction() as conn:
        identifiers = [dict(row) for row in conn.execute("SELECT provider,external_key FROM institution_identifiers WHERE institution_id=?", (school["id"],))]
    from app.results import result_rows
    baseline_rows = [row for row in result_rows(service.snapshot(season["id"])) if row["institution_id"] == school["id"]]
    report = {"started_at": utcnow(), "institution": school["name"], "academic_year": 2026,
              "backend": settings.data["app"]["llm_backend"],
              "model": settings.data["models"]["ollama_text_model"] if settings.data["app"]["llm_backend"] == "ollama" else settings.data["models"]["openai_fast_model"],
              "date_seeded": False, "source_url_seeded": False, "user_database_written": False,
              "baseline_rows": baseline_rows,
              "mode": "controlled_observation_model_smoke" if args.smoke else "whole_window_actual_agent"}
    if args.learned_calendar:
        report.update(source_url_seeded=True, source_url_origin="prior_actual_agent_search_run-113438-483298",
                      mode="production_job_reuses_rechecked_discovered_route")
    try:
        result, calls = asyncio.run(collect_job(app, out, school, season, args.learned_calendar) if args.job else
                                  collect(settings, out, school, season, identifiers, args.smoke))
        (out / "research.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        report.update(model_calls=calls, stop_reason=result.get("stop_reason"),
                      agent_trace=result.get("agent_trace"), missing_fields=result.get("missing_fields"))
        if args.smoke:
            report.update(passed=True, decision=result["decision"])
        else:
            saved = {"job_id": result["verification_job_id"]} if args.job else service.save_research(school["id"], season["id"], result)
            report["saved"] = saved
            with TestClient(app, base_url="http://127.0.0.1") as client:
                response = client.get("/api/results", params={"season_id": season["id"]})
                assert response.status_code == 200
                rows = [row for row in response.json()["rows"] if row["institution_id"] == school["id"]]
                report["rows"] = rows
                token = client.get("/api/bootstrap").json()["token"]
                export = client.post("/api/exports/html", headers={"X-App-Token": token, "Idempotency-Key": str(uuid4())},
                    json={"season_id": season["id"], "include_notes": False, "publication_policy_version": "4.0"})
                assert export.status_code == 202
                (out / "result.html").write_bytes(client.get(export.json()["url"]).content)
            report["model_candidate_count"] = sum(item.get("extraction_method") == "model_candidate" for item in result["candidates"])
            report["date_found"] = any(row.get("date") for row in report["rows"])
            report["passed"] = report["date_found"] and (calls == 0 if args.school == "건국초등학교" else report["model_candidate_count"] > 0)
            if args.job:
                detail = json.loads((out / "job.json").read_text())
                old_dates = {(row.get("event_label"), row.get("date")) for row in baseline_rows if row.get("date")}
                report.update(field_publication_check=any(
                    row.get("date") and (row.get("event_label"), row["date"]) not in old_dates for row in report["rows"]),
                    budget_enforced=any(item["outcome"] == "limit_reached" and "item_budget" in item["reason_codes"]
                                        for item in detail["items"]),
                    completed_date_and_count=any(row.get("date") and row.get("graduation_count") is not None for row in report["rows"]))
    except Exception as error:
        report.update(passed=False, error_type=type(error).__name__, error_code=getattr(error, "code", "verification_error"))
    report["finished_at"] = utcnow()
    (out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"out": str(out), "passed": report["passed"], "model_calls": report.get("model_calls"),
                      "stop_reason": report.get("stop_reason"), "rows": report.get("rows"),
                      "error_type": report.get("error_type"), "error_code": report.get("error_code")}, ensure_ascii=False), flush=True)
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
