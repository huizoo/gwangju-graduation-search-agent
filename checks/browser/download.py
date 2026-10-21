"""격리 합성 앱에서 한국어 파일 다운로드와 file:// 오프라인 조회를 검증한다."""

from datetime import datetime
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.exporting import schedule_filename
from checks.browser.periods import HELPER, LocalApp


def seed(service):
    """파일 조회에 필요한 25개 학교를 검사 전용 DB에 넣는다.

    Args:
        service: 새 격리 DB의 서비스.
    Returns:
        과거 기간의 사용자 시즌.
    """
    season = service.create_season({"search_start": "2025-12-01", "search_end": "2026-03-31"})
    for number in range(25):
        school = service.create_institution({"name": f"파일검사{number + 1:02}학교", "type": "elementary",
            "district": "북구", "address": "브라우저 검사 합성 주소", "campus_label": None,
            "phone": None, "homepage_url": None})
        day = "2026-01-08" if number < 23 else "2026-01-20"
        basis = {"action": "set", "basis": "phone_confirmed", "confirmed_at": "2025-12-20T01:00:00Z",
                 "note": "다운로드 브라우저 검사 합성 자료", "source_url": None, "resolves_fingerprint": None}
        service.create_event({"institution_id": school["id"], "season_id": season["id"], "title": "졸업식",
            "event_type": "graduation_ceremony", "audience": None, "campus_label": None,
            "allow_separate_event": False, "operations": [
                {**basis, "field": "ceremony_status", "value": "scheduled"},
                {**basis, "field": "event_date", "value": {"precision": "day", "date": day, "month": None}}]})
    return season


def main():
    """하나의 전용 lease에서 파일 이름·바이트·오프라인 필터를 확인하고 정리한다."""
    out = ROOT / "runtime/verification/browser/download" / datetime.now().strftime("browser-%Y%m%d-%H%M%S")
    out.mkdir(parents=True)
    local = LocalApp(out)
    season = seed(local.runtime.service)
    expected = "광주 졸업식 일정표 (2025.12.01~2026.03.31).html"
    downloaded = out / "download" / expected
    downloaded.parent.mkdir()
    report = {"synthetic_only": True, "user_database_accessed": False, "external_api_or_model_called": False,
              "assertions": {}, "operations": [], "screenshots": []}
    lease = None

    def invoke(arguments, allow_error=False):
        """한 helper lease로만 브라우저 작업을 실행하고 원본 출력을 남긴다."""
        result = subprocess.run([HELPER, *arguments], cwd=out, text=True, capture_output=True, timeout=55)
        report["operations"].append({"command": ["HTML data URL" if value.startswith("data:") else value for value in arguments], "returncode": result.returncode,
                                     "stdout": result.stdout, "stderr": result.stderr})
        if result.returncode and not allow_error:
            raise RuntimeError(result.stderr or result.stdout[:1600])
        return result

    def evaluate(code):
        """실제 브라우저 DOM과 다운로드 관찰값을 읽는다."""
        raw = json.loads(invoke(["run", "--lease", lease, "--", "eval", code]).stdout)["stdout"]
        value = raw.split("### Result\n", 1)[1].split("\n###", 1)[0].strip()
        for _ in range(3):
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    break
        return value

    def run(code):
        """화면 버튼과 다운로드 이벤트를 같은 페이지에서 조작한다."""
        invoke(["run", "--lease", lease, "--", "run-code", code])

    def wait(condition):
        """실제 화면 조건을 최대 15초 기다린다."""
        return evaluate("async()=>{const end=Date.now()+15000;while(Date.now()<end){if(" + condition +
                        ")return true;await new Promise(r=>setTimeout(r,100));}return false;}")

    def check(name, passed, actual):
        """성공과 실패 관찰을 모두 검증 자료로 남긴다."""
        report["assertions"][name] = {"passed": bool(passed), "actual": actual}
        if not passed:
            raise AssertionError(f"{name}: {actual}")
        print("filename-ui: " + name, flush=True)

    try:
        check("single_server_filename", schedule_filename(season) == expected, schedule_filename(season))
        local.start()
        lease = json.loads(invoke(["start", "--url", local.url, "--deadline", "420"]).stdout)["lease"]
        invoke(["run", "--lease", lease, "--", "resize", "1440", "1000"])
        assert wait("document.querySelector('#season-select')?.options.length===2")
        run("async(page)=>{await page.locator('#season-select').selectOption(" + json.dumps(season["id"]) + ");}")
        assert wait("document.querySelector('#season-select').value===" + json.dumps(season["id"]) +
                    " && document.querySelectorAll('#results tbody tr').length===25")
        latest = local.runtime.data_dir / "exports" / expected
        for _ in range(100):
            if latest.exists():
                break
            time.sleep(.1)
        check("automatic_file_has_same_korean_name", latest.exists(), str(latest))
        run("async(page)=>{const popupPromise=page.waitForEvent('popup');await page.locator('#open-html').click();"
            "const popup=await popupPromise;await popup.waitForLoadState('domcontentloaded');"
            "await popup.waitForFunction(()=>document.querySelectorAll('tbody tr').length===25);"
            "const opened=await popup.evaluate(()=>({rows:document.querySelectorAll('tbody tr').length,title:document.title,serverTools:!!document.querySelector('.find-time,#shutdown')}));"
            "await page.evaluate(value=>window.__openedFile=value,opened);await popup.close();}")
        opened = evaluate("()=>window.__openedFile")
        check("open_schedule_uses_actual_export", opened["rows"] == 25 and not opened["serverTools"], opened)
        run("async(page)=>{const pending=page.waitForEvent('download');await page.locator('#save-html').click();"
            "const download=await pending;const filename=download.suggestedFilename();await download.saveAs(" +
            json.dumps(str(downloaded)) + ");const failure=await download.failure();"
            "await page.evaluate(value=>window.__savedFile=value,{filename,failure});}")
        actual = evaluate("()=>window.__savedFile")
        check("actual_browser_download_name", actual == {"filename": expected, "failure": None}, actual)
        check("download_matches_automatic_file", downloaded.read_bytes() == latest.read_bytes(),
              {"download_sha256": hashlib.sha256(downloaded.read_bytes()).hexdigest(),
               "automatic_sha256": hashlib.sha256(latest.read_bytes()).hexdigest()})
        # 서버를 먼저 종료한 뒤 방금 받은 실제 파일 경로를 연다.
        local.stop()
        navigation = invoke(["run", "--lease", lease, "--", "goto", downloaded.as_uri()], allow_error=True)
        protocol = "file:"
        if navigation.returncode:
            if 'protocol is blocked' not in navigation.stdout or 'file:' not in navigation.stdout:
                raise RuntimeError(navigation.stderr or navigation.stdout[:1600])
            # 전용 도구의 파일 접근 제한을 우회하지 않고 동일 바이트 검사를 별도로 기록한다.
            report["file_url_verification"] = {"status": "blocked", "reason": "전용 브라우저 CLI가 file: 프로토콜을 차단함"}
            protocol = "data:"
            invoke(["run", "--lease", lease, "--", "goto",
                    "data:text/html;base64," + base64.b64encode(downloaded.read_bytes()).decode()])
        else:
            report["file_url_verification"] = {"status": "passed"}
        assert wait("document.querySelectorAll('tbody tr').length===25")
        offline = evaluate("()=>({protocol:location.protocol,rows:document.querySelectorAll('tbody tr').length,"
            "visible:[...document.querySelectorAll('tbody tr')].filter(r=>getComputedStyle(r).display!=='none').length,"
            "resources:performance.getEntriesByType('resource').length,serverTools:!!document.querySelector('.find-time,#shutdown')})")
        check("downloaded_bytes_work_after_server_shutdown", offline == {
            "protocol": protocol, "rows": 25, "visible": 20, "resources": 0, "serverTools": False}, offline)
        run("async(page)=>{await page.locator('[data-control=\"next\"]').click();}")
        paging = evaluate("()=>({visible:[...document.querySelectorAll('tbody tr')].filter(r=>getComputedStyle(r).display!=='none').length,page:document.querySelector('[data-control=\"page\"]').textContent})")
        check("offline_second_page", paging["visible"] == 5 and "2" in paging["page"], paging)
        run("async(page)=>{await page.locator('[data-control=\"query\"]').fill('파일검사2');"
            "await page.locator('[data-control=\"date\"]').fill('2026-01-20');}")
        filtered = evaluate("()=>({names:[...document.querySelectorAll('tbody tr')].filter(r=>getComputedStyle(r).display!=='none').map(r=>r.dataset.name),page:document.querySelector('[data-control=\"page\"]').textContent})")
        check("offline_search_and_date_and_reset_page", filtered["names"] == ["파일검사24학교", "파일검사25학교"] and filtered["page"].startswith("1"), filtered)
        screenshot = out / "downloaded-file-offline.png"
        invoke(["run", "--lease", lease, "--", "screenshot", f"--filename={screenshot}"])
        report["screenshots"].append(str(screenshot))
        report["status"] = "passed" if protocol == "file:" else "passed_with_file_url_blocked"
    except Exception as error:
        report["status"] = "failed"
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        if lease:
            closed = subprocess.run([HELPER, "finish", "--lease", lease], cwd=out,
                                    text=True, capture_output=True, timeout=55)
            cleanup = json.loads(closed.stdout)
            report["cleanup"] = {key: cleanup.get(key) for key in ["state", "cleanupMode", "finalResidualCount"]}
            assert cleanup["state"] == "FINISHED" and cleanup["finalResidualCount"] == 0
        local.stop()
        report["own_server_stopped"] = True
        (out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"status": report.get("status"), "out": str(out),
                          "checks": len(report["assertions"]), "cleanup": report.get("cleanup")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
