"""시즌 없는 첫 실행과 기간 선택·재시작을 격리 저장소에서 검사한다."""

from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.domain import validate
from app.web.api import create_app
from test_jobs_api import institution_payload, job_payload, manual_event, seed_season, settings_for


def test_empty_start_and_restart_never_create_season_or_html(tmp_path):
    """최초 실행·조회·종료·재시작은 기본 기간이나 빈 일정표를 생성하지 않는다."""
    settings = settings_for(tmp_path)
    for _ in range(2):
        app = create_app(settings)
        runtime = app.state.runtime
        before = runtime.store.revision()
        with TestClient(app, base_url="http://127.0.0.1") as client:
            boot = client.get("/api/bootstrap").json()
            assert boot["seasons"] == [] and boot["selected_season_id"] is None
            assert boot["status"]["eligible_institution_count"] == 0
            assert boot["status"]["html_save"] is None
            status = client.get("/api/status")
            assert status.status_code == 200
            validate("Status", status.json())
            assert status.json()["eligible_institution_count"] == 0
            assert client.get("/api/seasons").json()["items"] == []
            assert client.get("/health").status_code == 200
        with runtime.store.transaction() as conn:
            for table in ("seasons", "events", "jobs", "exports", "audit_log"):
                assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert runtime.store.revision() == before
        assert not list(runtime.data_dir.glob("exports/**/*.html"))


def test_roster_readiness_is_available_without_a_season_and_survives_restart(tmp_path):
    """기관 목록만 있을 때 조사 대상 수를 제공하고 기간을 대신 만들지 않는다."""
    settings = settings_for(tmp_path)
    app = create_app(settings)
    runtime = app.state.runtime
    kinds = ("elementary", "middle", "high", "university", "junior_college", "kindergarten")
    schools = [runtime.service.create_institution({
        **institution_payload(f"명부학교{index}"), "type": kind,
    }) for index, kind in enumerate(kinds)]
    for app in (app, create_app(settings)):
        with TestClient(app, base_url="http://127.0.0.1") as client:
            boot = client.get("/api/bootstrap").json()
            assert boot["seasons"] == [] and boot["selected_season_id"] is None
            validate("Status", boot["status"])
            assert boot["status"]["eligible_institution_count"] == 5
            assert client.get("/api/status").json()["eligible_institution_count"] == 5
            assert {item["id"] for item in client.get("/api/institutions").json()["items"]} == {s["id"] for s in schools}
            assert app.state.runtime.service.list_seasons()["items"] == []


def test_only_valid_explicit_period_creates_a_season_and_same_period_is_reused(tmp_path):
    """기간 선택 API만 시즌을 만들며 잘못된 요청과 반복 요청은 시즌을 늘리지 않는다."""
    app = create_app(settings_for(tmp_path))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        boot = client.get("/api/bootstrap").json()
        headers = {"X-App-Token": boot["token"], "Idempotency-Key": str(uuid4())}
        invalid = client.post("/api/seasons", headers=headers, json={
            "search_start": "2026-12-01", "search_end": "2026-11-30",
        })
        assert invalid.status_code == 422
        assert client.get("/api/bootstrap").json()["selected_season_id"] is None
        request = {"search_start": "2025-12-01", "search_end": "2026-03-26"}
        headers["Idempotency-Key"] = str(uuid4())
        response = client.post("/api/seasons", headers=headers, json=request)
        assert response.status_code == 201, response.text
        season = response.json()
        headers["Idempotency-Key"] = str(uuid4())
        repeated = client.post("/api/seasons", headers=headers, json=request)
        assert repeated.status_code == 201 and repeated.json()["id"] == season["id"]
        after = client.get("/api/bootstrap").json()
        assert after["seasons"] == [season] and after["selected_season_id"] == season["id"]


def test_restart_preserves_existing_periods_selection_and_manual_record(tmp_path):
    """설정 기본 기간이 달라도 기존 시즌·선택·수기 행사를 복제하거나 바꾸지 않는다."""
    settings = settings_for(tmp_path)
    app = create_app(settings)
    runtime = app.state.runtime
    current = seed_season(runtime)
    previous = runtime.service.create_season({"search_start": "2025-12-01", "search_end": "2026-03-26"})
    school = runtime.service.create_institution(institution_payload())
    event = runtime.service.create_event(manual_event(school["id"], current["id"]))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        token = client.get("/api/bootstrap").json()["token"]
        response = client.post("/api/selected-season", headers={"X-App-Token": token},
                               json={"season_id": previous["id"]})
        assert response.status_code == 200
    with runtime.store.transaction() as conn:
        original = {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("seasons", "events", "event_seasons", "manual_overrides", "audit_log")
        }
    settings.data["season"].update(search_start="2024-12-01", search_end="2025-03-31")
    restarted = create_app(settings)
    with TestClient(restarted, base_url="http://127.0.0.1") as client:
        boot = client.get("/api/bootstrap").json()
        assert {s["id"] for s in boot["seasons"]} == {current["id"], previous["id"]}
        assert boot["selected_season_id"] == previous["id"]
        saved = client.get(f"/api/events/{event['id']}").json()["event"]
        assert saved["fields"]["event_date"]["manual"] == event["fields"]["event_date"]["manual"]
    with restarted.state.runtime.store.transaction() as conn:
        for table, rows in original.items():
            assert [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")] == rows


def test_status_school_count_matches_all_selection_with_and_without_period(tmp_path):
    """준비 대상 수가 유치원을 제외하고 선택 기간의 폐교 조건도 실제 큐와 맞춘다."""
    app = create_app(settings_for(tmp_path))
    runtime = app.state.runtime
    school = runtime.service.create_institution(institution_payload("운영중학교"))
    closed = runtime.service.create_institution(institution_payload("폐교학교"))
    runtime.service.create_institution({**institution_payload("대상외유치원"), "type": "kindergarten"})
    with runtime.store.transaction(write=True) as conn:
        conn.execute("UPDATE institutions SET operation_status='closed_confirmed',operation_effective_date=? WHERE id=?",
                     ("2026-01-01", closed["id"]))
    assert runtime.status()["eligible_institution_count"] == 2
    assert runtime.service.list_seasons()["items"] == []
    season = seed_season(runtime)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/status").json()["eligible_institution_count"] == 2
        status = client.get("/api/status", params={"season_id": season["id"]})
        assert status.status_code == 200 and status.json()["eligible_institution_count"] == 1
        job = runtime.jobs.create(job_payload(season["id"]), str(uuid4()))
        assert job["target_count"] == status.json()["eligible_institution_count"]
        assert [item["institution_id"] for item in runtime.jobs.items(job["id"])] == [school["id"]]
