"""자연어 입력을 실제 저장·수기 보호·게시 경로에 연결해 검사한다."""

import json
from copy import deepcopy
from datetime import datetime, timezone

from app.natural import NaturalRecorder, parse_turn
from app.results import result_rows
from test_review_regressions import source_context, save_notice


def record(ctx, text, previous=None):
    """새 입력 또는 후속 답변을 같은 실제 서비스에 저장한다."""
    return NaturalRecorder(ctx[0]).record({
        "season_id": ctx[2]["id"], "text": text,
        "conversation_id": previous["id"] if previous else None,
        "expected_revision": previous["revision"] if previous else None,
    })


def test_natural_time_completes_existing_event_and_preserves_raw_input(source_context):
    """시간 보완은 기존 UUID를 유지하고 학교 주소와 행사장을 섞지 않는다."""
    service, school, season = source_context
    saved = save_notice(source_context, "2026학년도 검증초등학교 졸업식은 2027년 1월 7일 개최합니다.")
    text = "검증초 졸업식은 1월 7일 오전 10시, 학교 강당이래. 방금 학교에 전화해서 확인했어. 기록해줘."
    result = record(source_context, text)
    assert result["status"] == "saved", result
    assert result["event_id"] == saved["event_ids"][0]
    assert len(service.events(season["id"])["items"]) == 1
    event = service.event_detail(result["event_id"])["event"]
    assert event["publication"]["calendar_time"] == "10:00"
    assert event["fields"]["start_time"]["manual"]["basis"] == "phone_confirmed"
    row = result_rows(service.snapshot(season["id"]))[0]
    assert row["address"] != "학교 강당"
    with service.store.transaction() as conn:
        turns = json.loads(conn.execute("SELECT turns_json FROM natural_records WHERE id=?", (result["id"],)).fetchone()[0])
    assert turns[0]["text"] == text
    assert turns[0]["parsed"]["confirmation_expression"] == "방금"


def test_confirmation_time_is_asked_not_silently_substituted(source_context):
    """'전화로 확인'만으로 실제 확인시점을 입력시각으로 만들지 않는다."""
    service, _, season = source_context
    first = record(source_context, "검증초 졸업식은 1월 7일 오전 10시야. 학교에 전화해서 확인했어.")
    assert first["status"] == "question"
    assert any("언제" in q for q in first["questions"])
    assert not service.events(season["id"])["items"]
    second = record(source_context, "방금", first)
    assert second["status"] == "saved", second
    assert service.event_detail(second["event_id"])["event"]["publication"]["calendar_time"] == "10:00"


def test_partial_date_saves_while_ambiguous_time_is_asked(source_context):
    """오전·오후만 애매하면 확인한 날짜는 저장하고 시각만 질문한다."""
    service, _, season = source_context
    first = record(source_context, "검증초 졸업식은 1월 7일 10시래. 방금 전화해서 확인했어.")
    assert first["status"] == "question"
    assert any("오전" in q for q in first["questions"])
    event = service.event_detail(first["event_id"])["event"]
    assert event["publication"]["calendar_date"] == "2027-01-07"
    assert event["publication"]["calendar_time"] is None
    second = record(source_context, "오전 10시", first)
    assert second["status"] == "saved", second
    assert len(service.events(season["id"])["items"]) == 1


def test_guess_and_research_request_are_not_confirmations(source_context):
    """추측이나 조사 요청을 파싱 성공만으로 게시하지 않는다."""
    service, _, season = source_context
    assert record(source_context, "검증초는 아마 1월 7일 오전 10시 같아. 기록해줘.")["status"] == "noted"
    assert record(source_context, "검증초 졸업식 날짜를 조사해줘.")["status"] == "research_request"
    assert not service.events(season["id"])["items"]


def test_invalid_date_and_outside_season_are_not_saved(source_context):
    """달력에 없는 날짜와 다른 시즌을 보완 추측하지 않는다."""
    service, _, season = source_context
    for value in ("2월 30일", "2026년 1월 7일"):
        result = record(source_context, f"검증초 졸업식은 {value} 오전 10시야. 방금 전화해서 확인했어.")
        assert result["status"] == "question"
        assert result["event_id"] is None
    assert not service.events(season["id"], view="calendar")["items"]


def test_time_only_input_uses_existing_allowed_date(source_context):
    """현재 허용된 같은 행사 날짜에만 새로운 시각을 연결한다."""
    service, _, season = source_context
    saved = save_notice(source_context, "2026학년도 검증초등학교 졸업식은 2027년 1월 7일 개최합니다.")
    result = record(source_context, "검증초 졸업식은 오전 10시야. 방금 전화해서 확인했어.")
    assert result["status"] == "saved", result
    assert result["event_id"] == saved["event_ids"][0]
    assert service.event_detail(result["event_id"])["event"]["publication"]["calendar_time"] == "10:00"


def test_explicit_confirmation_timestamp_is_not_event_date(source_context):
    """실제 확인한 날과 졸업식 날을 구분해서 정규화한다."""
    _, _, season = source_context
    parsed = parse_turn("검증초 졸업식은 1월 7일 오전 10시야. 2026년 10월 19일 오후 3시에 전화해서 확인했어.",
                        season, "2026-10-20T03:00:00+00:00")
    assert parsed["date"] == "2027-01-07" and parsed["start_time"] == "10:00"
    assert parsed["confirmed_at"] == "2026-10-19T15:00:00+09:00"


def test_conflict_holds_only_time_and_reconfirmation_survives_same_source_recheck(source_context):
    """시각 충돌은 날짜를 유지하고 실제 재확인 뒤 같은 자료 재조회에도 해결을 보존한다."""
    service, _, season = source_context
    source = "2026학년도 검증초등학교 졸업식은 2027년 1월 7일 오전 11시에 시작합니다."
    save_notice(source_context, source)
    first = record(source_context, "검증초 졸업식은 오전 10시야. 방금 전화해서 확인했어.")
    assert first["status"] == "question", first
    event = service.events(season["id"])["items"][0]
    assert event["publication"]["calendar_date"] == "2027-01-07"
    assert not event["publication"]["current_time_allowed"]
    save_notice(source_context, source)
    assert not service.events(season["id"])["items"][0]["publication"]["current_time_allowed"]
    second = record(source_context, "방금 전화로 다시 확인했어. 본식은 오전 10시가 맞아.", first)
    assert second["status"] == "saved", second
    assert service.events(season["id"])["items"][0]["publication"]["calendar_time"] == "10:00"
    save_notice(source_context, source)
    assert service.events(season["id"])["items"][0]["publication"]["calendar_time"] == "10:00"


def test_same_natural_statement_does_not_create_duplicate_event(source_context):
    """같은 내용을 다시 적어도 동일 행사 UUID를 사용한다."""
    service, _, season = source_context
    text = "검증초 졸업식은 1월 7일 오전 10시야. 방금 전화해서 확인했어."
    a, b = record(source_context, text), record(source_context, text)
    assert a["event_id"] == b["event_id"]
    assert len(service.events(season["id"])["items"]) == 1


def test_phone_date_without_clock_requires_actual_time(source_context):
    """어제 확인했다는 말에 임의의 자정이나 입력시각을 붙이지 않는다."""
    result = record(source_context, "검증초 졸업식은 1월 7일 오전 10시야. 어제 학교에 전화해서 확인했어.")
    assert result["status"] == "question" and any("언제" in q for q in result["questions"])
    assert result["event_id"] is None


def test_preparation_meeting_and_unregistered_attached_kindergarten_are_not_school_ceremony(source_context):
    """직접 입력에서도 다른 행사·기관의 주장을 본교 본식으로 바꾸지 않는다."""
    service, _, season = source_context
    for subject in ("검증초 졸업식 준비 설명회", "검증초등학교 병설유치원 졸업식"):
        result = record(source_context, f"{subject}는 1월 7일 오전 10시야. 방금 전화해서 확인했어.")
        assert result["status"] == "question"
        assert result["event_id"] is None
    assert not service.events(season["id"])["items"]


def test_natural_api_idempotency_and_restart_preserve_conversation(tmp_path):
    """재전송과 재시작 이후에도 원문 대화와 한 행사 연결을 유지한다."""
    from fastapi.testclient import TestClient
    from uuid import uuid4
    from app.api import create_app
    from test_jobs_api import settings_for, institution_payload
    settings = settings_for(tmp_path)
    app = create_app(settings)
    app.state.runtime.service.create_institution(institution_payload("검증초등학교"))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        bootstrap = client.get("/api/bootstrap").json()
        headers = {"X-App-Token": bootstrap["token"], "Idempotency-Key": str(uuid4())}
        body = {"season_id": bootstrap["seasons"][0]["id"], "text": "검증초 졸업식은 1월 7일 오전 10시야. 전화해서 확인했어.",
                "conversation_id": None, "expected_revision": None}
        a = client.post("/api/records/natural", headers=headers, json=body)
        assert a.status_code == 200, a.text
        assert client.post("/api/records/natural", headers=headers, json=body).json() == a.json()
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        token = client.get("/api/bootstrap").json()["token"]
        body.update(text="방금", conversation_id=a.json()["id"], expected_revision=a.json()["revision"])
        b = client.post("/api/records/natural", headers={"X-App-Token":token, "Idempotency-Key":str(uuid4())}, json=body)
        assert b.status_code == 200 and b.json()["status"] == "saved", b.text
