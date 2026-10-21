"""서버의 파일명을 앱 다운로드까지 그대로 전달하고 누락 시 중단하는지 검사한다."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


def _download(filename):
    """앱의 실제 내려받기 함수를 외부 저장 없이 실행한다.

    Args:
        filename: 내보내기 API가 전달한 파일명.
    Returns:
        다운로드 이름, 파일 요청 횟수, 오류 메시지.
    """
    node = shutil.which("node")
    assert node, "다운로드 동작 검사에 Node.js가 필요합니다."
    source = Path("app/static/app.js").read_text()
    function = source.split("  async function exportHtml(download) {", 1)[1].split(
        "\n  async function seasonChanged()", 1)[0]
    script = """
    const observed={name:null,fetches:0,error:null};
    const state={season:"selected-season"};
    const api=async()=>(RESPONSE);
    const fetch=async()=>{observed.fetches++;return{ok:true,blob:async()=>({})};};
    const URL={createObjectURL:()=>"blob:test",revokeObjectURL:()=>{}};
    const document={createElement:()=>({click(){observed.name=this.download;}})};
    const setTimeout=()=>{};
    const message=()=>{};
    async function exportHtml(download) {BODY
    exportHtml(true).catch(error=>{observed.error=error.message;}).finally(()=>{
      console.log(JSON.stringify(observed));
    });
    """.replace("RESPONSE", json.dumps({"status": "current", "url": "/export/result", "filename": filename})).replace("BODY", function)
    result = subprocess.run([node, "-e", script], text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def test_download_uses_api_filename_without_rebuilding_it_from_selected_season():
    """클라이언트는 다른 명명 규칙을 만들지 않고 서버 이름을 그대로 쓴다."""
    name = "광주 졸업식 일정표 (2025.12.01~2026.03.31).html"
    assert _download(name) == {"name": name, "fetches": 1, "error": None}


@pytest.mark.parametrize("filename", [None, "", "   ", 123])
def test_missing_download_name_stops_before_fetch_or_download(filename):
    """이름 계약이 빠지면 다른 이름의 파일로 묵시 대체하지 않는다.

    Args:
        filename: 누락되거나 유효한 문자열이 아닌 응답 값.
    """
    actual = _download(filename)
    assert actual["name"] is None and actual["fetches"] == 0
    assert "파일 이름을 확인하지 못했어요" in actual["error"]
