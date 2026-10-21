"""자료 미발견·접속 실패·작업 미완료와 종료 안내의 의미를 검사한다."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


def _message(function, argument, *extra):
    """앱이 실제로 사용하는 독립 문구 함수를 Node에서 실행한다.

    Args:
        function: 검사할 공개 문구 함수명.
        argument: 작업 결과 또는 종료 응답 값.
        *extra: 문구 판단에 필요한 추가 입력.
    Returns:
        사용자에게 표시하는 문자열.
    """
    node = shutil.which("node")
    assert node, "사용자 문구 스크립트 검사에 Node.js가 필요합니다."
    source = Path("app/static/app-status.js").read_text()
    result = subprocess.run([node, "-e", "global.window={};" + source +
        "console.log(JSON.stringify(window." + function + "(" +
        ",".join(json.dumps(value) for value in (argument, *extra)) + ")));"],
        text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize("feedback,expected", [
    ("not_found", "이번 조사에서 시간을 찾지 못했어요."),
    ("source_unavailable", "일부 자료를 읽지 못했어요."),
    ("service_error", "조사 중 오류가 났어요."),
    ("incomplete", "찾기를 마치지 못했어요."),
    (None, "찾기를 마치지 못했어요."),
])
def test_completed_time_result_explains_evidence_limit(feedback, expected):
    """완료 상태만으로 미발견을 단정하지 않고 원문·서비스 오류를 구별한다.

    Args:
        feedback: 서버가 근거 이력에서 요약한 결과.
        expected: 사실 범위를 넘지 않는 사용자 문구.
    """
    value = _message("scheduleTimeFeedback", {"status": "completed", "feedback": feedback})
    assert value == expected
    assert "미공개" not in value and "공개되지" not in value


def test_finished_job_waits_for_result_projection_before_claiming_not_found():
    """상태 목록 응답만 먼저 도착하면 상세 결과 조회를 기다린다."""
    assert _message("scheduleTimeFeedback", {"status": "completed"}) == "결과를 확인하는 중…"


@pytest.mark.parametrize("feedback,expected", [
    ("not_found", "이번 조사에서 시간을 찾지 못했어요."),
    ("source_unavailable", "일부 자료를 읽지 못했어요."),
    ("service_error", "조사 중 오류가 났어요."),
])
def test_time_summary_uses_same_job_result(feedback, expected):
    """완료 안내도 시간 칸과 같은 실제 조사 결과를 설명한다."""
    job = {"id": "job-1", "status": "completed", "scope_snapshot": {"target_event_id": "event-1"}}
    rows = [{"event_ids": ["event-1"], "time_search": {"job_id": "job-1", "feedback": feedback}}]
    assert _message("scheduleTimeJobMessage", job, rows) == expected
    rows[0]["time_search"]["job_id"] = "older-job"
    assert _message("scheduleTimeJobMessage", job, rows) == "결과를 확인하는 중…"


def test_time_summary_only_announces_confirmed_time_for_matching_job():
    """다른 행의 확인 시간으로 완료를 꾸미지 않는다."""
    job = {"id": "job-1", "status": "completed", "scope_snapshot": {"target_event_id": "event-1"}}
    rows = [{"event_ids": ["other-event"], "start_time": "10:30",
             "time_search": {"job_id": "job-1"}}]
    assert _message("scheduleTimeJobMessage", job, rows) == "결과를 확인하는 중…"
    rows[0]["event_ids"] = ["event-1"]
    assert _message("scheduleTimeJobMessage", job, rows) == "확인한 시작 시각을 일정표에 표시했어요."


@pytest.mark.parametrize("paused,expected", [
    (True, "조사를 멈추고 종료했어요. 다음에 이어서 할 수 있어요."),
    (False, "프로그램을 종료했어요. 이 창을 닫아도 됩니다."),
])
def test_shutdown_does_not_claim_unsent_notes_were_saved(paused, expected):
    """종료 응답의 실제 중지 여부만 설명하고 미입력 내용을 저장했다고 말하지 않는다.

    Args:
        paused: 종료 응답의 활성 작업 중지 여부.
        expected: 실제 결과에 맞는 종료 안내.
    """
    value = _message("scheduleShutdownMessage", paused)
    assert value == expected
    assert "기록을 저장" not in value
