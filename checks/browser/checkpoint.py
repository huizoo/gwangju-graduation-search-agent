"""중간 저장된 시간과 실제 작업 완료를 구별하는 화면 경계를 검사한다."""

import asyncio
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
from threading import Event

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.jobs import BudgetStop
from checks.browser.periods import HELPER, LocalApp, fixtures


class CheckpointRunner:
    """유효한 합성 시간을 먼저 저장하고 실제 작업은 별도 신호까지 유지한다."""

    release = Event()
    publish = True

    def __init__(self, settings, meter=None):
        """실제 작업 상태를 확인할 검사 계량기를 연결한다.

        Args:
            settings: 격리 앱 설정.
            meter: 실제 작업 관리자 계량기.
        """
        self.meter = meter

    async def run(self, institution, season, identifiers, *, purpose="date_count", target_event=None, **kwargs):
        """중간 값이 보이는 DB 상태와 아직 실행 중인 작업 상태를 함께 만든다.

        Args:
            institution: 검사 학교.
            season: 고정 조사 조건.
            identifiers: 기관 식별자.
            purpose: 시간 조사 목적.
            target_event: 정확한 대상 행사.
            **kwargs: 실제 실행기가 받는 부가 조건.
        Returns:
            외부 수집으로 계산하지 않는 빈 결과.
        Raises:
            BudgetStop: 검사 중 실제 일시정지 또는 취소가 요청된 경우.
        """
        service = self.meter.manager.service
        if self.publish:
            event = service.event_detail(target_event["id"])["event"]
            service.apply_overrides(event["id"], {"expected_revision": event["revision"], "operations": [{
                "field": "start_time", "action": "set", "value": "10:30", "bound_date": "2027-01-08",
                "basis": "phone_confirmed", "confirmed_at": "2026-10-20T01:00:00Z",
                "note": "작업 중간 저장을 재현하는 합성 검사값", "source_url": None, "resolves_fingerprint": None}]})
        while not self.release.is_set():
            status = self.meter.manager.get(self.meter.job_id)["status"]
            if status != "running":
                raise BudgetStop("user_cancel" if status in {"cancelling", "cancelled"} else "user_pause")
            await asyncio.sleep(.05)
        return {"documents": [], "candidates": [], "counts": [], "errors": [], "purpose": purpose}


def main():
    """한 새 lease로 중간 저장·인쇄·완료·재개 불가 안내를 확인한다."""
    out = ROOT / "runtime/verification/browser/checkpoint" / datetime.now().strftime("checkpoint-%H%M%S")
    out.mkdir(parents=True)
    local = LocalApp(out)
    season, event = fixtures(local.runtime.service)
    local.runtime.jobs.runner_factory = CheckpointRunner
    report = {"synthetic_only": True, "external_api_or_model_called": False, "checks": {}, "operations": []}
    lease = None

    def invoke(arguments):
        """같은 실행 맥락의 전용 helper에 명령을 보내고 원 출력을 보관한다."""
        result = subprocess.run([HELPER, *arguments], cwd=out, text=True, capture_output=True, timeout=55)
        report["operations"].append({"command": arguments[:5], "stdout": result.stdout, "stderr": result.stderr,
                                     "returncode": result.returncode})
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout[:2000])
        return result

    def evaluate(code):
        """현재 페이지에서 DOM과 API 결과를 실제로 읽는다."""
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
        """같은 페이지의 실제 버튼·인쇄 매체를 조작한다."""
        invoke(["run", "--lease", lease, "--", "run-code", code])

    def wait(condition):
        """짧은 실제 상태 확인을 최대 15초까지만 기다린다."""
        return evaluate("async () => {const end=Date.now()+15000;while(Date.now()<end){if(" + condition +
            ")return true;await new Promise(r=>setTimeout(r,80));}return false;}")

    def check(name, passed, actual):
        """실패한 관찰도 변경 없이 결과에 남긴다."""
        report["checks"][name] = {"passed": bool(passed), "actual": actual}
        if not passed:
            raise AssertionError(f"{name}: {actual}")
        print("checkpoint-ui: " + name, flush=True)

    try:
        local.start()
        lease = json.loads(invoke(["start", "--url", local.url, "--deadline", "400"]).stdout)["lease"]
        invoke(["run", "--lease", lease, "--", "resize", "1440", "1050"])
        assert wait("document.querySelectorAll('#results tbody tr').length===45")
        run("async (page) => {await page.locator('tr[data-name=\"화면검사01학교\"] .find-time').click();}")
        assert wait("document.querySelector('tr[data-name=\"화면검사01학교\"] .time-research')?.dataset.state==='running'")
        actual = evaluate("""async () => {const data=await (await fetch('/api/results?season_id='+document.querySelector('#season-select').value)).json();
          const row=data.rows.find(r=>r.name==='화면검사01학교');const target=document.querySelector('tr[data-name="화면검사01학교"]');const other=document.querySelector('tr[data-name="화면검사02학교"]');
          return {saved_time:row.start_time,job_status:row.time_search.status,target:target.children[2].innerText,spinner:!!target.querySelector('.time-spinner'),other:other.children[2].innerText,other_spinner:!!other.querySelector('.time-spinner'),other_disabled:other.querySelector('button').disabled};} """)
        check("saved_time_does_not_end_running_indicator", actual["saved_time"] == "10:30" and actual["job_status"] == "running" and actual["spinner"] and actual["target"] == "찾는 중…", actual)
        check("other_school_is_only_disabled", actual["other"] == "시간 찾기" and actual["other_disabled"] and not actual["other_spinner"], actual)
        invoke(["run", "--lease", lease, "--", "reload"])
        check("reload_restores_running_over_saved_time", wait("document.querySelector('tr[data-name=\"화면검사01학교\"] .time-research')?.dataset.state==='running'"), True)
        invoke(["run", "--lease", lease, "--", "screenshot", f"--filename={out / 'checkpoint-running.png'}"])
        run("async (page) => {await page.emulateMedia({media:'print'});}")
        printed = evaluate("() => {const row=document.querySelector('tr[data-name=\"화면검사01학교\"]');return {text:row.children[2].innerText,notice:getComputedStyle(row.querySelector('.time-research')).display};}")
        check("print_keeps_inflight_time_cell_blank", printed["text"] == "" and printed["notice"] == "none", printed)
        run("async (page) => {await page.emulateMedia({media:'screen'});}")
        running_id = local.runtime.jobs.list()[0]["id"]
        run("async (page) => {await page.getByRole('button',{name:'잠시 멈추기',exact:true}).click();}")
        assert wait("document.querySelector('tr[data-name=\"화면검사01학교\"] .time-research')?.dataset.state==='paused'")
        paused_partial = evaluate("""async () => {const data=await (await fetch('/api/results?season_id='+document.querySelector('#season-select').value)).json();
          const row=data.rows.find(r=>r.name==='화면검사01학교');return {saved_time:row.start_time,status:row.time_search.status,
          text:document.querySelector('tr[data-name="화면검사01학교"] td:nth-child(3)').innerText};}""")
        check("paused_checkpoint_keeps_notice_and_resume", paused_partial["saved_time"] == "10:30" and
              paused_partial["status"] == "paused" and "잠시 멈췄어요." in paused_partial["text"] and
              "이어서 찾기" in paused_partial["text"] and "10:30" not in paused_partial["text"], paused_partial)
        invoke(["run", "--lease", lease, "--", "reload"])
        check("paused_checkpoint_survives_reload", wait("document.querySelector('tr[data-name=\"화면검사01학교\"] .time-research')?.dataset.state==='paused'"), True)
        invoke(["run", "--lease", lease, "--", "screenshot", f"--filename={out / 'checkpoint-paused.png'}"])
        run("async (page) => {await page.emulateMedia({media:'print'});}")
        paused_print = evaluate("() => document.querySelector('tr[data-name=\"화면검사01학교\"] td:nth-child(3)').innerText")
        check("print_keeps_paused_checkpoint_time_blank", paused_print == "", paused_print)
        run("async (page) => {await page.emulateMedia({media:'screen'});await page.locator('tr[data-name=\"화면검사01학교\"] .find-time').click();}")
        resumed = wait("document.querySelector('tr[data-name=\"화면검사01학교\"] .time-research')?.dataset.state==='running'")
        check("paused_checkpoint_resumes_original_job", resumed and local.runtime.jobs.list()[0]["id"] == running_id,
              {"resumed": resumed, "job_id": local.runtime.jobs.list()[0]["id"], "original_job_id": running_id})
        CheckpointRunner.release.set()
        check("actual_completion_replaces_with_saved_time", wait("document.querySelector('tr[data-name=\"화면검사01학교\"] td:nth-child(3)').innerText==='10:30' && !document.querySelector('tr[data-name=\"화면검사01학교\"] .time-research')"), True)
        job = local.runtime.jobs.list()[0]
        check("completion_is_persisted_job_state", job["status"] == "completed", {"status": job["status"], "id": job["id"]})
        # 설정 변경으로 멈춘 작업은 재개 대신 같은 행의 새 조사로 연결한다.
        CheckpointRunner.publish = False; CheckpointRunner.release.clear()
        run("async (page) => {await page.locator('tr[data-name=\"화면검사02학교\"] .find-time').click();}")
        assert wait("document.querySelector('tr[data-name=\"화면검사02학교\"] .time-research')?.dataset.state==='running'")
        run("async (page) => {await page.getByRole('button',{name:'잠시 멈추기',exact:true}).click();}")
        assert wait("document.querySelector('tr[data-name=\"화면검사02학교\"] .time-research')?.dataset.state==='paused'")
        job = local.runtime.jobs.list()[0]
        with local.runtime.store.transaction(write=True) as conn:
            conn.execute("UPDATE jobs SET paused_reason='configuration_required',revision=revision+1 WHERE id=?", (job["id"],))
        invoke(["run", "--lease", lease, "--", "reload"])
        assert wait("document.querySelector('tr[data-name=\"화면검사02학교\"] .time-research')?.dataset.state==='paused'")
        paused = evaluate("() => ({row:document.querySelector('tr[data-name=\"화면검사02학교\"] td:nth-child(3)').innerText,controls:document.querySelector('#job-actions').innerText,message:document.querySelector('#job-message').innerText})")
        check("configuration_pause_offers_new_research", "다시 찾기" in paused["row"] and "이어서" not in paused["row"] + paused["controls"] and "조건이 바뀌었어요" in paused["message"], paused)
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
        print(json.dumps({"status": report.get("status"), "out": str(out), "checks": len(report["checks"]), "cleanup": report.get("cleanup")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
