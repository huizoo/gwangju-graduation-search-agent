"""독립 리뷰의 최소 재현: 실제 서비스와 분리한 임시 DB만 사용한다."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.web.api import create_app
from app.core.config import load_settings
from app.agent.jobs import BudgetStop
from app.launcher import InstanceLock, bind_server
from test_jobs_api import settings_for, institution_payload, job_payload, seed_job


@pytest.fixture
def reviewed_runtime(tmp_path):
    """네트워크 없는 독립 리뷰 앱을 준비한다."""
    return create_app(settings_for(tmp_path)).state.runtime


def test_all_institution_queue_includes_rows_after_api_page_limit(reviewed_runtime):
    """전체 조사 큐는 화면의 최대 500행 페이지에 잘리지 않는다."""
    job = seed_job(reviewed_runtime, 501)
    queue = reviewed_runtime.jobs.items(job["id"])
    assert job["target_count"] == len(queue) == 501
    assert len({row["institution_id"] for row in queue}) == 501


def test_export_database_failure_keeps_last_good_bytes(reviewed_runtime):
    """파일 교체 이후 DB 오류에서도 기존 정상 파일을 지켜야 한다."""
    runtime = reviewed_runtime
    season = runtime.service.list_seasons()["items"][0]
    payload = {"season_id": season["id"], "include_notes": False, "publication_policy_version": "4.0"}
    runtime.export(payload)
    with runtime.store.transaction() as conn:
        row = dict(conn.execute("SELECT * FROM exports WHERE season_id=?", (season["id"],)).fetchone())
    path = Path(row["file_ref"])
    original = path.read_bytes()
    runtime.service.create_institution(institution_payload())
    with runtime.store.transaction(True) as conn:
        conn.execute("CREATE TRIGGER refuse_export_update BEFORE UPDATE ON exports BEGIN SELECT RAISE(ABORT,'review injected database error'); END")
    try:
        runtime.export(payload)
    except sqlite3.DatabaseError:
        pass
    assert path.read_bytes() == original, "DB 업데이트 실패 전에 정상 HTML을 덮어썼습니다."


@pytest.mark.asyncio
async def test_final_save_failure_preserves_usage_and_closes_uncertain_reservations(reviewed_runtime):
    """한 기관 저장 오류를 격리하면서 소비한 예산을 누락하지 않는다."""
    runtime = reviewed_runtime
    class Runner:
        def __init__(self, settings, meter=None):
            self.meter = meter

        async def run(self, institution, season, identifiers):
            await self.meter.reserve("http_requests")
            if institution["name"] == "검사0":
                return {"documents": [{"id": "malformed"}], "candidates": []}
            return {"documents": [], "candidates": []}
    runtime.jobs.runner_factory = Runner
    job = seed_job(runtime, 2)
    await runtime.jobs.run(job["id"])
    result = runtime.jobs.get(job["id"])
    assert result["counts"]["failed"] == 1 and result["counts"]["done"] == 1
    assert result["usage"]["http_requests"] == 2
    assert result["usage"]["active_seconds"] >= 2, "저장 오류 항목의 실제 실행 비용이 rollback되었습니다."
    with runtime.store.transaction() as conn:
        pending = conn.execute("SELECT COUNT(*) FROM call_reservations WHERE job_id=? AND status='reserved'", (job["id"],)).fetchone()[0]
    assert pending == 0, "작업 종료 뒤 응답 불명 예약이 reserved로 남았습니다."


@pytest.mark.asyncio
async def test_known_token_usage_refunds_only_unused_reservation(reviewed_runtime):
    """실제 토큰은 차액 정산하고 미상 응답 비용은 보수적으로 남긴다."""
    runtime = reviewed_runtime
    # 기본값과 무관하게 환불 누락 시 두 번째 14,000 예약이 막히는 경계를 고정한다.
    runtime.settings.data["execution"]["item_max_tokens"] = 24000
    class Runner:
        def __init__(self, settings, meter=None):
            self.meter = meter

        async def run(self, institution, season, identifiers):
            known = await self.meter.reserve("tokens", 14000)
            await self.meter.settle(known, 3000)
            # 첫 예약을 실제 비용으로 정산하지 않으면 기관 24000 한도에 잘못 걸린다.
            uncertain = await self.meter.reserve("tokens", 14000)
            await self.meter.settle(uncertain)
            return {"documents": [], "candidates": []}

    runtime.jobs.runner_factory = Runner
    job = seed_job(runtime, 1)
    await runtime.jobs.run(job["id"])
    result = runtime.jobs.get(job["id"])
    assert result["counts"]["done"] == 1
    assert result["usage"]["tokens"] == 17000
    assert result["usage"]["reserved_tokens"] == 14000
    with runtime.store.transaction() as conn:
        uncertain = conn.execute(
            "SELECT actual_json FROM call_reservations WHERE job_id=? AND status='charged_unknown'",
            (job["id"],),
        ).fetchall()
        assert len(uncertain) == 1 and uncertain[0][0] is None


@pytest.mark.asyncio
async def test_unconfirmed_institution_keeps_specific_source_failure(reviewed_runtime):
    """행사가 없어도 원문 접근·모델 오류 원인을 확인 목록에서 잃지 않는다."""
    runtime = reviewed_runtime
    class Runner:
        def __init__(self, settings, meter=None):
            pass

        async def run(self, institution, season, identifiers):
            return {"documents": [], "candidates": [], "errors": [
                {"provider": "reader", "code": "tls_error", "message": "TLS 실패"},
                {"provider": "model", "code": "parse_failed", "message": "인용 오류"},
            ]}
    runtime.jobs.runner_factory = Runner
    job = seed_job(runtime, 1)
    await runtime.jobs.run(job["id"])
    assert {"tls_error", "parse_failed"} <= set(runtime.jobs.items(job["id"])[0]["reason_codes"])
    review = runtime.service.review(job["season_id"])["items"][0]
    assert {"tls_error", "parse_failed", "date_missing"} <= set(review["reason_codes"])


@pytest.mark.asyncio
async def test_running_call_respects_remaining_total_active_budget(reviewed_runtime):
    """한 번의 긴 호출도 남은 작업 전체 실행시간으로 제한해야 한다."""
    runtime = reviewed_runtime
    runtime.settings.data["execution"]["job_max_active_seconds"] = 1
    class Runner:
        def __init__(self, settings, meter=None):
            self.meter = meter

        async def run(self, institution, season, identifiers):
            await self.meter.reserve("http_requests")
            await asyncio.sleep(1.2)
            return {"documents": [], "candidates": []}
    runtime.jobs.runner_factory = Runner
    job = seed_job(runtime, 1)
    await runtime.jobs.run(job["id"])
    result = runtime.jobs.get(job["id"])
    assert result["status"] == "paused" and result["paused_reason"] == "job_budget", "전체 예산을 초과한 기관을 completed로 저장했습니다."


def test_production_rejects_testserver_host(tmp_path):
    """테스트용 호스트 예외가 실제 앱의 DNS rebinding 경계가 되면 안 된다."""
    settings = load_settings(process_values={"APP_MODE": "live", "LLM_BACKEND": "ollama", "APP_DATA_DIR": str(tmp_path), "AUTO_OPEN_BROWSER": "false"}, dotenv_path=tmp_path / "absent.env")
    with TestClient(create_app(settings), base_url="http://testserver") as client:
        assert client.get("/api/bootstrap").status_code == 403


@pytest.mark.asyncio
async def test_checkpoint_after_cancel_never_saves_late_result(reviewed_runtime):
    """취소 의도 기록 후 실행 중인 checkpoint가 새 원문을 저장하지 않는다."""
    runtime = reviewed_runtime
    entered, release = asyncio.Event(), asyncio.Event()
    class Runner:
        def __init__(self, settings, meter=None):
            self.meter = meter

        async def run(self, institution, season, identifiers):
            entered.set()
            await release.wait()
            await self.meter.checkpoint({"documents": [{"id": "must never reach storage"}], "candidates": []})
            return {"documents": [], "candidates": []}
    runtime.jobs.runner_factory = Runner
    job = seed_job(runtime, 1)
    runtime.jobs.start(job["id"])
    task = runtime.jobs.tasks[job["id"]]
    await asyncio.wait_for(entered.wait(), 2)
    runtime.jobs.cancel(job["id"])
    release.set()
    await task
    assert runtime.jobs.get(job["id"])["status"] == "cancelled"
    with runtime.store.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_provider_exception_text_and_double_shutdown_safe(reviewed_runtime):
    """인증 문자열 예외를 기록하지 않고 종료를 반복해도 취소가 부활하지 않는다."""
    runtime = reviewed_runtime
    secret = "review-only-secret-should-never-persist"
    class Runner:
        def __init__(self, settings, meter=None):
            pass

        async def run(self, institution, season, identifiers):
            raise RuntimeError("https://api.example.org/?KEY=" + secret)
    runtime.jobs.runner_factory = Runner
    job = seed_job(runtime, 1)
    await runtime.jobs.run(job["id"])
    await runtime.jobs.shutdown()
    await runtime.jobs.shutdown()
    assert secret not in json.dumps(runtime.jobs.items(job["id"]))
    with runtime.store.transaction() as conn:
        all_sql = "\n".join(conn.iterdump())
    assert secret not in all_sql
    assert runtime.jobs.get(job["id"])["status"] == "completed"


def test_duplicate_instance_lock_uses_only_owned_resources(tmp_path):
    """중복 실행은 자체 lock으로 막고 남의 프로세스를 종료하지 않는다."""
    first, duplicate = InstanceLock(tmp_path / "instance.lock"), InstanceLock(tmp_path / "instance.lock")
    try:
        assert first.acquire()
        first.write({"instance_id": "review-fixture", "url": "http://127.0.0.1:1"})
        assert not duplicate.acquire()
        assert duplicate.read()["instance_id"] == "review-fixture"
    finally:
        duplicate.close()
        first.close()


def test_occupied_port_fallback_requires_local_socket_permission():
    """실제 bind 권한이 있는 환경에서 점유 포트 우회를 검사한다."""
    try:
        occupied = bind_server("127.0.0.1", 0)
    except PermissionError:
        pytest.skip("현재 sandbox는 localhost socket bind를 금지합니다. 실제 런처 포트 검증은 별도로 필요합니다.")
    other = bind_server("127.0.0.1", occupied.getsockname()[1])
    try:
        assert other.getsockname()[1] != occupied.getsockname()[1]
    finally:
        other.close()
        occupied.close()


def test_job_filter_uses_same_effective_profile_as_visible_list(reviewed_runtime):
    """수기로 수정해 화면에 보이는 이름을 같은 조사 필터가 사용해야 한다."""
    runtime = reviewed_runtime
    institution = runtime.service.create_institution(institution_payload("원본 기관명"))
    runtime.service.patch_institution(institution["id"], {"expected_revision": institution["revision"], "set": {"name": "보호된 기관명", "district": "남구"}, "release_fields": [], "note": "사용자 수정"})
    season = runtime.service.list_seasons()["items"][0]
    payload = job_payload(season["id"])
    payload["selection"] = {"scope": "filter", "types": [], "districts": ["남구"], "query": "보호된"}
    job = runtime.jobs.create(payload, str(uuid4()))
    assert job["target_count"] == 1, "화면의 보호된 프로필과 조사 큐의 필터 기준이 다릅니다."


def test_missing_mode_keeps_institution_with_another_unresolved_event(reviewed_runtime):
    """한 기관의 다른 행사 시간이 미확인이라면 누락 조사 대상에 남긴다."""
    from test_jobs_api import manual_event
    runtime = reviewed_runtime
    institution = runtime.service.create_institution(institution_payload())
    season = runtime.service.list_seasons()["items"][0]
    first = manual_event(institution["id"], season["id"])
    first["audience"] = "오전 대상"
    first["operations"].append({"field": "start_time", "action": "set", "value": "10:00", "basis": "phone_confirmed", "confirmed_at": "2026-10-19T03:00:00Z", "note": "", "bound_date": "2027-01-07", "resolves_fingerprint": None, "source_url": None})
    ready = runtime.service.create_event(first)
    assert ready["publication"]["sales_ready"]
    second = manual_event(institution["id"], season["id"])
    second.update(title="별도 대상 졸업식", audience="오후 대상", allow_separate_event=True)
    runtime.service.create_event(second)
    assert runtime.service.review(season["id"])["total"] == 1
    payload = job_payload(season["id"])
    payload["mode"] = "missing_only"
    assert runtime.jobs.create(payload, str(uuid4()))["target_count"] == 1


@pytest.mark.asyncio
async def test_active_reservation_survives_crash_without_double_charge(reviewed_runtime):
    """강제 종료의 실행 상한을 보존하고 재시작 때 두 번 가산하지 않는다."""
    from app.agent.jobs import Meter, dump, empty_usage, now
    from app.core.domain import ApiError
    runtime = reviewed_runtime
    runtime.settings.data["execution"]["job_max_active_seconds"] = 5
    job = seed_job(runtime, 1)
    item = runtime.jobs.items(job["id"])[0]
    segment_id = str(uuid4())
    with runtime.store.transaction(True) as conn:
        conn.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
        conn.execute("INSERT INTO job_segments VALUES(?,?,?,?,?,?,?)", (segment_id, job["id"], 1, now(), None, now(), dump(empty_usage())))
    claimed = await runtime.jobs._claim_active_time(job["id"], item["id"], segment_id)
    assert claimed[-1]
    meter = Meter(runtime.jobs, job["id"], item["id"], segment_id)
    await meter.reserve("http_requests")
    before = runtime.jobs.get(job["id"])["usage"]
    assert before["active_seconds"] == 5 and before["http_requests"] == 1
    runtime.jobs.recover()
    runtime.jobs.recover()
    after = runtime.jobs.get(job["id"])
    assert after["usage"] == before
    with pytest.raises(ApiError) as error:
        runtime.jobs.resume(job["id"], {"expected_revision": after["revision"], "increase_limits": None})
    assert error.value.code == "job_budget"
    with runtime.store.transaction() as conn:
        states = [row[0] for row in conn.execute("SELECT status FROM call_reservations WHERE job_id=?", (job["id"],))]
    assert states and set(states) == {"charged_unknown"}


@pytest.mark.asyncio
async def test_shared_active_reservation_waits_for_actual_settlement(reviewed_runtime):
    """다른 기관의 임시 시간 예약을 실제 예산 소진으로 오인하지 않는다."""
    runtime = reviewed_runtime
    runtime.settings.data["execution"].update(job_max_active_seconds=2, concurrency=2)
    seen = []
    class Runner:
        def __init__(self, settings, meter=None):
            self.meter = meter

        async def run(self, institution, season, identifiers):
            seen.append(institution["id"])
            await self.meter.reserve("http_requests")
            await asyncio.sleep(0.01)
            return {"documents": [], "candidates": []}
    runtime.jobs.runner_factory = Runner
    job = seed_job(runtime, 2)
    await asyncio.wait_for(runtime.jobs.run(job["id"]), 3)
    current = runtime.jobs.get(job["id"])
    assert current["status"] == "completed" and current["counts"]["done"] == 2
    assert len(set(seen)) == 2
    assert current["usage"]["active_seconds"] == 2


@pytest.mark.asyncio
async def test_unknown_active_cost_is_not_charged_again_on_resume(reviewed_runtime):
    """시간 한도를 늘린 재개에서도 이전 예약 비용이 누적 상태로 유지된다."""
    from app.agent.jobs import dump, empty_usage, now
    runtime = reviewed_runtime
    runtime.settings.data["execution"]["job_max_active_seconds"] = 2
    job = seed_job(runtime, 1)
    item = runtime.jobs.items(job["id"])[0]
    segment_id = str(uuid4())
    with runtime.store.transaction(True) as conn:
        conn.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
        conn.execute("INSERT INTO job_segments VALUES(?,?,?,?,?,?,?)", (segment_id, job["id"], 1, now(), None, now(), dump(empty_usage())))
    await runtime.jobs._claim_active_time(job["id"], item["id"], segment_id)
    runtime.jobs.recover()
    old = runtime.jobs.get(job["id"])
    runtime.jobs.resume(job["id"], {"expected_revision": old["revision"], "increase_limits": {"job_max_active_seconds": 5}})
    class Runner:
        def __init__(self, settings, meter=None):
            pass
        async def run(self, institution, season, identifiers):
            return {"documents": [], "candidates": []}
    runtime.jobs.runner_factory = Runner
    await runtime.jobs.run(job["id"])
    current = runtime.jobs.get(job["id"])
    assert current["status"] == "completed"
    assert current["usage"]["active_seconds"] == 3
    assert current["segment_usage"]["active_seconds"] == 1
    assert runtime.jobs.items(job["id"])[0]["usage"]["active_seconds"] == 3


@pytest.mark.asyncio
async def test_known_documents_are_current_season_support_sources_only(reviewed_runtime):
    """다른 시즌·미연결 문서를 섞지 않고 지원 URL의 최신 버전만 재확인한다."""
    import hashlib
    from app.core.domain import new_id, utcnow
    from test_publication import live_document, result_for
    runtime = reviewed_runtime
    institution = runtime.service.create_institution(institution_payload("시험학교"))
    current = runtime.service.list_seasons()["items"][0]
    old = runtime.service.create_season({"label": "지난 시즌", "academic_year": 2025, "search_start": "2025-12-01", "search_end": "2026-03-31", "export_key": "old2025", "timezone": "Asia/Seoul", "region_profile": "gwangju_5_gu"})
    document = live_document(runtime.service, institution, url="https://school.gen.go.kr/notice/current")
    institution = runtime.service.get_institution(institution["id"])
    runtime.service.save_research(institution["id"], current["id"], result_for(document, institution, current))
    unrelated = live_document(runtime.service, institution, url="https://school.gen.go.kr/notice/unrelated")
    runtime.service.save_research(institution["id"], current["id"], {"documents": [unrelated], "candidates": []})
    old_document = live_document(runtime.service, institution, url="https://school.gen.go.kr/notice/old")
    old_document["segments"][0]["text"] = "시험학교 2025학년도 졸업식은 2026년 1월 7일 오전 10시 시작합니다."
    old_document["content_hash"] = hashlib.sha256(old_document["segments"][0]["text"].encode()).hexdigest()
    runtime.service.save_research(institution["id"], old["id"], result_for(old_document, institution, old))
    latest = deepcopy(document)
    latest.update(id=new_id(), retrieved_at=utcnow())
    latest["segments"][0]["text"] += "\n다시 확인한 본문"
    latest["content_hash"] = hashlib.sha256(latest["segments"][0]["text"].encode()).hexdigest()
    runtime.service.save_research(institution["id"], current["id"], result_for(latest, institution, current))
    captured = []
    class Runner:
        def __init__(self, settings, meter=None):
            pass
        async def run(self, institution, season, identifiers):
            captured.extend(institution["known_documents"])
            return {"documents": [], "candidates": []}
    runtime.jobs.runner_factory = Runner
    job = runtime.jobs.create(job_payload(current["id"]), str(uuid4()))
    await runtime.jobs.run(job["id"])
    assert len(captured) == 1
    assert captured[0]["display_url"] == "https://school.gen.go.kr/notice/current"
