"""단순화한 앱 안내와 자동 파일 저장 경계를 실제 브라우저·격리 합성 DB로 확인한다."""

import argparse
import asyncio
import base64
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
from threading import Event
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.jobs import BudgetStop
from app.exporting import schedule_filename
from verify_period_ui_v3 import HELPER, LocalApp, fixtures


class FeedbackRunner:
    """실제 작업 수명에 네 가지 합성 결과를 주입한다."""

    mode = "not_found"
    release = Event()

    def __init__(self, settings, meter=None):
        """검사 소유 작업의 상태를 연결한다.

        Args:
            settings: 격리 앱 설정.
            meter: 실제 작업 관리자 계량기.
        """
        self.meter = meter

    async def run(self, institution, season, identifiers, *, purpose="date_count", target_event=None, **kwargs):
        """근거 미발견·원문 실패·서비스 실패·한도 종료를 각각 재현한다.

        Args:
            institution: 합성 학교.
            season: 저장된 조사 조건.
            identifiers: 기관 식별자.
            purpose: 실제 요청의 목적.
            target_event: 시간 조사 대상.
            **kwargs: 실제 실행기 부가 입력.
        Returns:
            실제 외부 수집 성과로 계산하지 않는 합성 결과.
        Raises:
            RuntimeError: 합성 서비스 실패.
            BudgetStop: 합성 한도 또는 실제 중지 요청.
        """
        while not self.release.is_set():
            status = self.meter.manager.get(self.meter.job_id)["status"]
            if status != "running":
                raise BudgetStop("user_cancel" if status in {"cancelling", "cancelled"} else "user_pause")
            await asyncio.sleep(.04)
        if self.mode == "service_error":
            raise RuntimeError("합성 서비스 실패")
        if self.mode == "incomplete":
            raise BudgetStop("item_budget")
        errors = [{"provider": "reader", "code": "access_failed", "message": "합성 원문 읽기 실패"}] if self.mode == "source_unavailable" else []
        return {"documents": [], "candidates": [], "counts": [], "errors": errors, "purpose": purpose}


def main():
    """하나의 lease에서 읽기 전용 실제 사본과 별도 합성 앱을 검사한다."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-url", help="root가 시작한 실제 DB 사본 앱. 이 스크립트는 종료하지 않는다.")
    parser.add_argument("--live-season-id", help="사본에서 조회할 과거 일반 시즌. 조사 작업은 시작하지 않는다.")
    options = parser.parse_args()
    out = ROOT / "runtime/usability-v4" / datetime.now().strftime("browser-%H%M%S")
    out.mkdir(parents=True)
    local = LocalApp(out)
    season, event = fixtures(local.runtime.service)
    local.runtime.jobs.runner_factory = FeedbackRunner
    local.runtime.shutdown_callback = lambda: setattr(local.server, "should_exit", True)
    report = {"synthetic_only_for_mutations": True, "external_api_or_model_called": False,
              "live_clone_url": options.live_url, "assertions": {}, "operations": [], "screenshots": []}
    lease = None
    original_export = local.runtime.export
    fail_export = False

    def injected_export(payload):
        """정상 URL이 남은 저장 실패를 합성해 화면의 오래된 파일 다운로드를 검사한다."""
        if fail_export:
            with local.runtime.store.transaction(write=True) as conn:
                conn.execute("UPDATE exports SET status='failed' WHERE season_id=?", (payload["season_id"],))
            return local.runtime.export_result(payload["season_id"])
        return original_export(payload)

    local.runtime.export = injected_export

    def invoke(arguments):
        """같은 전용 helper lease에서만 브라우저 명령을 실행한다."""
        result = subprocess.run([HELPER, *arguments], cwd=out, text=True, capture_output=True, timeout=55)
        report["operations"].append({"command": ["HTML data URL" if value.startswith("data:") else value for value in arguments],
            "stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode})
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout[:1600])
        return result

    def evaluate(code):
        """브라우저가 반환한 실제 DOM 관찰값을 읽는다."""
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
        """같은 페이지의 실제 버튼을 조작한다."""
        invoke(["run", "--lease", lease, "--", "run-code", code])

    def wait(condition):
        """실제 조건 충족까지 최대 20초 기다린다."""
        return evaluate("async () => {const end=Date.now()+20000;while(Date.now()<end){if(" + condition +
            ")return true;await new Promise(r=>setTimeout(r,100));}return false;}")

    def check(name, passed, actual):
        """실패 관찰도 바꾸지 않고 보관한다."""
        report["assertions"][name] = {"passed": bool(passed), "actual": actual}
        if not passed:
            raise AssertionError(f"{name}: {actual}")
        print("simple-ui: " + name, flush=True)

    def shot(name):
        """현재 화면을 검사 자료에 보관한다."""
        invoke(["run", "--lease", lease, "--", "screenshot", f"--filename={out / name}"])
        report["screenshots"].append(name)

    def source_clone():
        """root가 소유한 실제 데이터 사본은 조회만 하고 서버 수명을 변경하지 않는다."""
        invoke(["run", "--lease", lease, "--", "goto", options.live_url])
        ready = wait("document.querySelectorAll('#results tbody tr').length>0 && document.querySelector('#research-summary').textContent.length>0")
        actual = evaluate("() => ({rows:document.querySelectorAll('#results tbody tr').length,summary:document.querySelector('#research-summary').textContent,headers:[...document.querySelectorAll('th')].map(n=>n.textContent),buttons:[...document.querySelectorAll('button')].map(n=>n.textContent),duplicateRange:!!document.querySelector('#season-range')})")
        check("real_clone_read_only_ui", ready and not actual["duplicateRange"] and "일정표 열기" in actual["buttons"] and
              "파일 내려받기" in actual["buttons"] and "새로 보기" not in actual["buttons"], actual)
        shot("real-clone-desktop.png")
        if options.live_season_id:
            run("async (page) => {await page.locator('#season-select').selectOption(" + json.dumps(options.live_season_id) + ");}")
            assert wait("document.querySelector('#season-select').value===" + json.dumps(options.live_season_id))
            # 선택 변경의 결과 요청이 완료된 뒤 저장된 원문과 무관한 검증 학교 값이 남지 않는지 확인한다.
            assert wait("document.querySelector('#research-summary').hidden===false")
            rows = evaluate("() => [...document.querySelectorAll('#results tbody tr')].filter(r=>/광주과학기술원|GIST/i.test(r.dataset.name)).map(r=>({name:r.dataset.name,date:r.cells[0].textContent.trim(),time:r.cells[2].textContent.trim(),count:r.cells[3].textContent.trim()}))")
            check("live_clone_past_validation_school_blank", len(rows) == 1 and rows[0]["date"] == "" and rows[0]["count"] == "" and rows[0]["time"] in {"", "시간 찾기"}, rows)

    try:
        local.start()
        lease = json.loads(invoke(["start", "--url", "about:blank", "--deadline", "1100"]).stdout)["lease"]
        invoke(["run", "--lease", lease, "--", "resize", "1440", "1050"])
        if options.live_url:
            source_clone()
        invoke(["run", "--lease", lease, "--", "goto", local.url])
        assert wait("document.querySelectorAll('#results tbody tr').length===45")
        labels = evaluate("() => ({buttons:[...document.querySelectorAll('button')].map(n=>n.textContent),summary:document.querySelector('#research-summary').textContent,management:document.querySelector('footer summary').textContent,duplicateRange:!!document.querySelector('#season-range')})")
        check("simple_labels_and_initial_full_research_notice", not labels["duplicateRange"] and labels["management"] == "관리" and
              all(value in labels["buttons"] for value in ["일정표 열기", "파일 내려받기", "기록 백업", "학교 목록 갱신", "프로그램 종료"]) and
              not any(value in labels["buttons"] for value in ["새로 보기", "HTML 열기", "HTML 저장", "저장하고 프로그램 종료"]) and
              "아직 시작하지" in labels["summary"], labels)
        evaluate("() => {window.__shutdownCalls=0;window.__downloadName=null;const fetcher=window.fetch;window.fetch=(url,opts)=>{if(url==='/api/app/shutdown')window.__shutdownCalls++;return fetcher(url,opts);};const click=HTMLAnchorElement.prototype.click;HTMLAnchorElement.prototype.click=function(){window.__downloadName=this.download;click.call(this);};document.querySelector('#record-text').value='아직 기록하지 않은 합성 메모';return true;}")
        run("async (page) => {await page.locator('#shutdown').click();}")
        blocked = evaluate("() => ({calls:window.__shutdownCalls,message:document.querySelector('#message').textContent,text:document.querySelector('#record-text').value,focused:document.activeElement.id})")
        check("unsent_text_is_not_silently_saved_or_discarded", blocked["calls"] == 0 and "아직 기록하지 않은" in blocked["message"] and blocked["focused"] == "record-text", blocked)
        evaluate("() => {document.querySelector('#record-text').value='';return true;}")
        FeedbackRunner.release.set()
        messages = {"not_found": "이번 조사에서 시간을 찾지 못했어요.", "source_unavailable": "일부 자료를 읽지 못했어요.",
                    "service_error": "조사 중 오류가 났어요.", "incomplete": "찾기를 마치지 못했어요."}
        for mode, text in messages.items():
            FeedbackRunner.mode = mode
            run("async (page) => {await page.locator('tr[data-name=\"화면검사01학교\"] .find-time').click();}")
            check("time_feedback_" + mode, wait("document.querySelector('tr[data-name=\"화면검사01학교\"] td:nth-child(3)').innerText.includes(" + json.dumps(text) + ")"),
                evaluate("() => document.querySelector('tr[data-name=\"화면검사01학교\"] td:nth-child(3)').innerText"))
        check("time_only_jobs_do_not_claim_full_research", "아직 시작하지" in evaluate("() => document.querySelector('#research-summary').textContent"), True)
        # 같은 브라우저의 기록 버튼을 거치지 않은 저장도 revision 폴링으로 반영돼야 한다.
        current = local.runtime.service.event_detail(event["id"])["event"]
        local.runtime.service.apply_overrides(event["id"], {"expected_revision": current["revision"], "operations": [{
            "field": "start_time", "action": "set", "value": "11:15", "bound_date": "2027-01-08", "basis": "phone_confirmed",
            "confirmed_at": "2026-10-20T01:00:00Z", "note": "외부 창 수기 저장을 재현한 합성 값", "source_url": None, "resolves_fingerprint": None}]})
        check("automatic_results_without_refresh_button", wait("document.querySelector('tr[data-name=\"화면검사01학교\"] td:nth-child(3)').innerText==='11:15'"), True)
        latest = local.runtime.data_dir / "exports" / schedule_filename(season)
        for _ in range(100):
            if latest.exists() and "11:15" in latest.read_text():
                break
            time.sleep(.1)
        check("automatic_html_without_export_click", latest.exists() and "11:15" in latest.read_text(), str(latest))
        FeedbackRunner.mode = "not_found"; FeedbackRunner.release.clear()
        run("async (page) => {await page.locator('#research-submit').click();}")
        check("full_research_running_summary", wait("document.querySelector('#research-summary').textContent.includes('전체 학교를 조사하고')"), True)
        FeedbackRunner.release.set()
        check("full_research_done_is_not_all_information_verified", wait("document.querySelector('#research-summary').textContent.includes('모든 일정이 확인된 것은 아니')"),
              evaluate("() => document.querySelector('#research-summary').textContent"))
        fail_export = True
        # 내용 revision은 그대로 두고 저장 상태만 실패로 바꿔 경량 상태 폴링을 검사한다.
        with local.runtime.export_lock:
            injected_export({"season_id": season["id"]})
        check("save_failure_without_content_revision_change", wait("document.querySelector('#html-save-message').hidden===false"),
              evaluate("() => document.querySelector('#html-save-message').textContent"))
        old = local.runtime.export_result(season["id"])
        run("async (page) => {await page.locator('#save-html').click();}")
        failed_download = evaluate("() => ({name:window.__downloadName,message:document.querySelector('#message').textContent})")
        check("failed_export_old_url_is_not_downloaded", old["status"] == "failed" and bool(old["url"]) and failed_download["name"] is None and
              "파일을 만들지 못했어요" in failed_download["message"], failed_download)
        run("async (page) => {await page.locator('#shutdown').click();}")
        check("failed_file_save_does_not_close_app", wait("document.querySelector('#message').textContent.includes('종료하지 않았어요')") and local.thread.is_alive(), local.thread.is_alive())
        fail_export = False
        original_export({"season_id": season["id"], "include_notes": False, "publication_policy_version": "4.0"})
        check("save_failure_notice_recovers_without_reload", wait("document.querySelector('#html-save-message').hidden===true"), True)
        run("async (page) => {await page.locator('#save-html').click();}")
        check("file_download_uses_period_name", wait("window.__downloadName==='광주 졸업식 일정표 (2026.12.01~2027.03.31).html'"), evaluate("() => window.__downloadName"))
        invoke(["run", "--lease", lease, "--", "resize", "390", "844"])
        check("mobile_no_overflow", evaluate("() => document.documentElement.scrollWidth<=innerWidth"), True); shot("simple-mobile.png")
        invoke(["run", "--lease", lease, "--", "resize", "1440", "1050"])
        shot("simple-desktop.png")
        run("async (page) => {await page.emulateMedia({media:'print'});}")
        printed = evaluate("() => ({rows:[...document.querySelectorAll('tbody tr')].filter(r=>getComputedStyle(r).display!=='none').length,controls:[...document.querySelectorAll('button,input,select,textarea,#research-summary,#html-save-message')].filter(n=>n.getClientRects().length).length})")
        check("print_hides_app_management_and_keeps_all_rows", printed == {"rows": 45, "controls": 0}, printed)
        run("async (page) => {await page.emulateMedia({media:'screen'});}")
        FeedbackRunner.release.clear()
        run("async (page) => {await page.locator('tr[data-name=\"화면검사02학교\"] .find-time').click();}")
        assert wait("document.querySelector('tr[data-name=\"화면검사02학교\"] .time-research')?.dataset.state==='running'")
        run("async (page) => {await page.locator('#shutdown').click();}")
        check("active_shutdown_explains_resume", wait("document.querySelector('#message').textContent==='조사를 멈추고 종료했어요. 다음에 이어서 할 수 있어요.'"), True)
        paused_job = local.runtime.jobs.list()[0]
        check("shutdown_persists_resumable_job", paused_job["status"] == "paused", {"id": paused_job["id"], "status": paused_job["status"]})
        local.stop()
        html = latest.read_bytes()
        local = LocalApp(out); local.runtime.jobs.runner_factory = FeedbackRunner
        local.runtime.shutdown_callback = lambda: setattr(local.server, "should_exit", True)
        local.start(); invoke(["run", "--lease", lease, "--", "goto", local.url])
        assert wait("document.querySelectorAll('#results tbody tr').length===45")
        check("restart_offers_paused_time_resume", wait("document.querySelector('tr[data-name=\"화면검사02학교\"] td:nth-child(3)').innerText.includes('이어서 찾기')"), True)
        run("async (page) => {await page.locator('#shutdown').click();}")
        check("idle_shutdown_does_not_claim_saving_unsent_text", wait("document.querySelector('#message').textContent==='프로그램을 종료했어요. 이 창을 닫아도 됩니다.'"), True)
        local.stop()
        invoke(["run", "--lease", lease, "--", "goto", "data:text/html;base64," + base64.b64encode(html).decode()])
        offline = evaluate("() => ({rows:document.querySelectorAll('tbody tr').length,resources:performance.getEntriesByType('resource').length,management:!!document.querySelector('#research-summary,#html-save-message,#shutdown'),time:document.querySelector('tr[data-name=\"화면검사01학교\"] td:nth-child(3)').textContent})")
        check("automatic_file_opens_without_program", offline == {"rows": 45, "resources": 0, "management": False, "time": "11:15"}, offline)
        if options.live_url:
            invoke(["run", "--lease", lease, "--", "goto", options.live_url])
            assert wait("document.querySelectorAll('#results tbody tr').length>0")
        report["status"] = "passed"
    except Exception as error:
        report["status"] = "failed"; report["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        if lease:
            result = subprocess.run([HELPER, "finish", "--lease", lease], cwd=out, text=True, capture_output=True, timeout=55)
            cleanup = json.loads(result.stdout)
            report["cleanup"] = {key: cleanup.get(key) for key in ["state", "cleanupMode", "finalResidualCount"]}
            assert cleanup["state"] == "FINISHED" and cleanup["finalResidualCount"] == 0
        local.stop(); report["own_server_stopped"] = True
        (out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"status": report.get("status"), "out": str(out), "checks": len(report["assertions"]), "cleanup": report.get("cleanup")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
