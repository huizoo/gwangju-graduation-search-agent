"""실제 화면 함수가 목록 확보와 시즌 조사의 API 계약을 구분하는지 검사한다."""

import json
from pathlib import Path
import shutil
import subprocess

from app.core.domain import validate


def _request(kind, roster_ready):
    """외부 연결 없이 앱의 실제 요청 함수를 실행한다.

    Args:
        kind: 목록 확보 또는 일정 조사 종류.
        roster_ready: 화면에서 받은 학교 목록 준비 상태.
    Returns:
        전송한 요청과 전송 전에 표시한 오류.
    """
    source = Path("app/static/app.js").read_text()
    function = "async function startJob(" + source.split("  async function startJob(", 1)[1].split(
        "\n  async function jobs()", 1)[0]
    node = shutil.which("node")
    assert node, "화면 요청 계약 검사에 Node.js가 필요합니다."
    script = """
      const observed={body:null,error:null};
      const state={rosterReady:READY,season:"a628c405-cb9a-4284-ae0c-c18bafbdc1ae",jobs:[]};
      const activeStatuses=new Set(["queued","running"]);
      const renderTimeStates=()=>{},message=()=>{},jobs=async()=>{},results=async()=>{};
      const api=async(path,options)=>{observed.body=options.body;return{id:"job",status:"queued"};};
      FUNCTION
      startJob({kind:KIND}).catch(error=>{observed.error=error.message;})
        .finally(()=>console.log(JSON.stringify(observed)));
    """.replace("READY", json.dumps(roster_ready)).replace("FUNCTION", function).replace("KIND", json.dumps(kind))
    return json.loads(subprocess.run([node, "-e", script], check=True, text=True, capture_output=True).stdout)


def test_load_roster_button_sends_registry_contract_without_season_fields():
    """목록 버튼이 시즌 조사 전용 필드를 섞어 서버에서 거절되지 않는다."""
    sent = _request("registry_sync", False)
    assert sent["error"] is None
    validate("JobCreate", sent["body"])
    assert sent["body"]["providers"] == ["neis", "higher_education"]


def test_research_requires_roster_but_loaded_roster_uses_selected_season():
    """학교 목록 준비 여부를 확인하되 준비 후에는 선택 기간 조사를 요청한다."""
    empty = _request("schedule_scan", False)
    assert empty["body"] is None and "학교 목록" in empty["error"]
    ready = _request("schedule_scan", True)
    assert ready["error"] is None
    validate("JobCreate", ready["body"])
    assert ready["body"]["season_id"] == "a628c405-cb9a-4284-ae0c-c18bafbdc1ae"
    assert ready["body"]["purpose"] == "date_count"
    assert ready["body"]["selection"] == {"scope": "all"}
