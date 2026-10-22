"""격리 합성 명부로 첫 사용 안내·목록 복구·시즌 조사·파일 받기를 검증한다."""

import asyncio
import base64
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from threading import Event
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.integrations.providers import ProviderError
from checks.browser.periods import ControlledRunner, HELPER, LocalApp


class SyntheticRegistry:
    """외부 응답만 대신하고 실제 작업·preview·commit 경로를 통과하는 합성 명부."""

    def __init__(self):
        """첫 작업의 실패와 재시도 성공을 독립적으로 제어한다."""
        self.release = Event()
        self.fail = True
        self.calls = []

    async def sync(self, settings, provider, meter=None):
        """실제 명부 수집기 위치에서 검사 전용 두 학교를 반환한다.

        Args:
            settings: 사용자 설정과 분리한 검사 설정.
            provider: 화면이 요청한 명부 자료원.
            meter: 실제 저장 작업에 연결된 계량기.
        Returns:
            실제 학교 사실로 사용하지 않는 RegistryImport.
        Raises:
            ProviderError: 실패 화면 확인을 위해 첫 시도에 주입한 오류.
        """
        self.calls.append({"provider": provider, "job_id": meter.job_id})
        while not self.release.is_set():
            await asyncio.sleep(.05)
        if self.fail:
            raise ProviderError("provider_error", "합성 검사에서 주입한 명부 불러오기 실패", provider)
        elementary = provider == "neis"
        kind = "elementary" if elementary else "university"
        row = {"external_key": "F10:9990001" if elementary else "synthetic-campus",
            "name": "첫사용합성초등학교" if elementary else "첫사용합성대학교",
            "type": kind, "district": "북구", "address": "광주광역시 북구 합성검사로 1",
            "campus_label": None if elementary else "합성 캠퍼스", "phone": None,
            "homepage_url": None, "operation_status": "unknown",
            "operation_effective_date": None, "operation_evidence_url": None}
        return {"schema_version": "4.0", "source": {"provider": provider,
            "source_name": "첫 사용 브라우저 검사 전용 합성 명부",
            "official_url": "https://open.neis.go.kr/hub" if elementary else "https://www.data.go.kr/",
            "as_of": "2026-10-21", "scope_types": [kind], "scope_districts": ["북구"],
            "coverage_complete": False, "completeness_note": "합성 화면 검사이며 실제 공식 명부가 아님",
            "expected_total": 1, "source_hash": hashlib.sha256(provider.encode()).hexdigest()}, "rows": [row]}


def main():
    """하나의 전용 lease와 신규 DB에서 첫 사용 흐름을 확인하고 정확히 종료한다."""
    out = ROOT / "runtime/verification/browser/first_use" / datetime.now().strftime("browser-%Y%m%d-%H%M%S")
    out.mkdir(parents=True)
    local = LocalApp(out)
    registry = SyntheticRegistry()
    ControlledRunner.release.clear()
    ControlledRunner.calls.clear()
    ControlledRunner.mode = "empty"
    report = {"synthetic_only": True, "user_database_accessed": False,
        "external_api_or_model_called": False, "assertions": {}, "operations": [], "screenshots": []}
    lease = None

    def invoke(arguments):
        """같은 실행 문맥의 단일 helper lease에서 명령과 원 관찰을 보관한다."""
        result = subprocess.run([HELPER, *arguments], cwd=out, text=True, capture_output=True, timeout=55)
        report["operations"].append({"command": [
            "downloaded HTML bytes as data URL" if value.startswith("data:") else value for value in arguments],
            "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout[:2000])
        return result

    def evaluate(code):
        """실제 브라우저 DOM 또는 로컬 API 관찰을 JSON으로 읽는다."""
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
        """동일 페이지에서 사용자 버튼과 입력 이벤트를 실행한다."""
        invoke(["run", "--lease", lease, "--", "run-code", code])

    def wait(expression):
        """임의 고정 지연 대신 화면 상태를 최대 15초 기다린다."""
        return evaluate("async()=>{const end=Date.now()+15000;while(Date.now()<end){if(" + expression +
            ")return true;await new Promise(r=>setTimeout(r,100));}return false;}")

    def check(name, passed, actual):
        """실패를 숨기지 않고 검사 결과와 실제 관찰을 남긴다."""
        report["assertions"][name] = {"passed": bool(passed), "actual": actual}
        if not passed:
            raise AssertionError(f"{name}: {actual}")
        print("first-use-ui: " + name, flush=True)

    def screenshot(name):
        """사용자 데이터가 없는 현재 화면을 검사 폴더에 보존한다."""
        invoke(["run", "--lease", lease, "--", "screenshot", f"--filename={out / name}"])
        report["screenshots"].append(str(out / name))

    def screen():
        """첫 사용 안내와 실제 버튼의 상태를 함께 읽는다."""
        return evaluate("""()=>({guide:!document.querySelector('#roster-guide').hidden,
            guideText:document.querySelector('#roster-guide').innerText,
            loadDisabled:document.querySelector('#load-roster').disabled,
            loadText:document.querySelector('#load-roster').textContent,
            researchDisabled:document.querySelector('#research-submit').disabled,
            newDisabled:document.querySelector('#add-season').disabled,
            summaryHidden:document.querySelector('#research-summary').hidden,
            message:document.querySelector('#message').textContent,
            jobText:document.querySelector('#job-message').textContent,
            rows:document.querySelectorAll('#results tbody tr').length})""")

    try:
        with patch("app.integrations.registry.sync_registry", registry.sync):
            local.start()
            # helper의 시작 검사가 전용 runtime 버전·캐시·전체 Chromium 실행 파일을 고정한다.
            lease = json.loads(invoke(["start", "--url", local.url, "--deadline", "420"]).stdout)["lease"]
            invoke(["run", "--lease", lease, "--", "resize", "1440", "1000"])
            assert wait("document.querySelector('#season-select')?.options.length===1 && !document.querySelector('#roster-guide').hidden")
            initial = screen()
            check("empty_roster_guidance_and_disabled_research", initial["guide"] and initial["researchDisabled"]
                and initial["newDisabled"] and not initial["loadDisabled"] and initial["summaryHidden"]
                and initial["rows"] == 0 and "학교 목록이 없어요" in initial["guideText"], initial)
            screenshot("01-empty-roster.png")
            run("async(page)=>{await page.locator('#load-roster').click();}")
            check("registry_request_accepted", wait("document.querySelector('#load-roster').disabled"), screen())
            loading = screen()
            check("registry_job_blocks_duplicate_and_research", loading["loadDisabled"]
                and loading["researchDisabled"] and loading["newDisabled"]
                and "불러오는 중" in loading["loadText"], loading)
            invoke(["run", "--lease", lease, "--", "reload"])
            assert wait("document.querySelector('#load-roster').disabled && !document.querySelector('#roster-guide').hidden")
            check("reload_restores_registry_progress", "불러오는 중" in screen()["loadText"], screen())
            registry.release.set()
            assert wait("!document.querySelector('#load-roster').disabled && document.querySelector('#job-message').textContent.includes('가져오지 못')")
            failed = screen()
            check("failed_registry_restores_retry_without_enabling_research", failed["guide"]
                and not failed["loadDisabled"] and failed["researchDisabled"] and failed["newDisabled"], failed)
            registry.fail = False
            run("async(page)=>{await page.locator('#load-roster').click();}")
            assert wait("document.querySelector('#roster-guide').hidden && !document.querySelector('#add-season').disabled && document.querySelectorAll('#results tbody tr').length===2")
            ready = screen()
            check("successful_registry_enables_season_flow", not ready["guide"] and not ready["researchDisabled"]
                and not ready["newDisabled"] and ready["rows"] == 2 and "학교 목록을 불러왔어요" in ready["jobText"], ready)
            with local.runtime.store.transaction() as conn:
                registry_saved = {"snapshots": conn.execute("SELECT count(*) FROM registry_snapshots").fetchone()[0],
                    "previews": conn.execute("SELECT count(*) FROM import_previews WHERE status='committed'").fetchone()[0],
                    "jobs": conn.execute("SELECT count(*) FROM jobs WHERE kind='registry_sync'").fetchone()[0]}
            check("registry_used_real_job_preview_and_commit", registry_saved == {"snapshots": 2, "previews": 2, "jobs": 2},
                {"saved": registry_saved, "synthetic_source_calls": registry.calls})
            run("async(page)=>{await page.locator('[data-control=\"query\"]').fill('검색결과가없는합성학교');}")
            no_matches = evaluate("()=>({empty:!document.querySelector('[data-control=\"empty\"]').hidden,researchDisabled:document.querySelector('#research-submit').disabled,newDisabled:document.querySelector('#add-season').disabled,guideHidden:document.querySelector('#roster-guide').hidden})")
            check("empty_display_filter_does_not_disable_school_research",
                no_matches == {"empty": True, "researchDisabled": False, "newDisabled": False, "guideHidden": True}, no_matches)
            run("async(page)=>{await page.locator('[data-control=\"reset\"]').click();}")
            run("""async(page)=>{await page.locator('#add-season').click();
                await page.locator('#season-start').fill('2025-12-01');
                await page.locator('#season-end').fill('2026-03-31');
                await page.locator('#season-submit').click();}""")
            assert wait("document.querySelector('#research-submit').disabled && document.querySelector('#job-message').textContent.includes('확인하고')")
            jobs = evaluate("async()=>await (await fetch('/api/jobs?limit=100')).json()")
            job = next(item for item in jobs["items"] if item["kind"] == "schedule_scan")
            frozen = job["scope_snapshot"]
            check("new_season_has_fixed_period_and_all_school_targets",
                frozen["season"]["search_start"] == "2025-12-01" and frozen["season"]["search_end"] == "2026-03-31"
                and frozen["purpose"] == "date_count" and job["target_count"] == 2,
                {"season": frozen["season"], "purpose": frozen["purpose"], "targets": job["target_count"]})
            ControlledRunner.release.set()
            assert wait("!document.querySelector('#research-submit').disabled")
            check("actual_runner_receives_same_period", len(ControlledRunner.calls) == 2 and all(
                call["season"]["id"] == job["season_id"] and call["season"]["search_start"] == "2025-12-01"
                and call["season"]["search_end"] == "2026-03-31" for call in ControlledRunner.calls), ControlledRunner.calls)
            invoke(["run", "--lease", lease, "--", "reload"])
            assert wait("document.querySelector('#season-select').value===" + json.dumps(job["season_id"]) +
                " && !document.querySelector('#research-submit').disabled")
            preserved = evaluate("()=>({id:document.querySelector('#season-select').value,seasons:document.querySelector('#season-select').options.length,guideHidden:document.querySelector('#roster-guide').hidden,rows:document.querySelectorAll('#results tbody tr').length})")
            check("reload_keeps_roster_selected_season_and_results", preserved ==
                {"id": job["season_id"], "seasons": 2, "guideHidden": True, "rows": 2}, preserved)
            screenshot("02-roster-and-season-ready.png")
            invoke(["run", "--lease", lease, "--", "resize", "390", "844"])
            narrow = evaluate("()=>({width:innerWidth,body:document.body.scrollWidth,guideHidden:document.querySelector('#roster-guide').hidden})")
            check("narrow_screen_has_no_page_overflow", narrow["body"] <= narrow["width"], narrow)
            screenshot("03-narrow-screen.png")
            expected = "광주 졸업식 일정표 (2025.12.01~2026.03.31).html"
            downloaded = out / expected
            run("async(page)=>{const pending=page.waitForEvent('download');await page.locator('#save-html').click();"
                "const download=await pending;await download.saveAs(" + json.dumps(str(downloaded)) + ");"
                "await page.evaluate(value=>window.__download=value,{filename:download.suggestedFilename(),failure:await download.failure()});}")
            observed = evaluate("()=>window.__download")
            check("download_uses_selected_season_name", observed == {"filename": expected, "failure": None}, observed)
            latest = local.runtime.data_dir / "exports" / expected
            check("download_matches_automatically_saved_html", downloaded.read_bytes() == latest.read_bytes(),
                {"download_sha256": hashlib.sha256(downloaded.read_bytes()).hexdigest(),
                 "automatic_sha256": hashlib.sha256(latest.read_bytes()).hexdigest()})
            local.stop()
            # 도구가 file: 접근을 금지하므로 파일 열기 성공을 주장하지 않고 동일 바이트만 확인한다.
            invoke(["run", "--lease", lease, "--", "goto",
                "data:text/html;base64," + base64.b64encode(downloaded.read_bytes()).decode()])
            assert wait("document.querySelectorAll('tbody tr').length===2")
            offline = evaluate("()=>({protocol:location.protocol,rows:document.querySelectorAll('tbody tr').length,resources:performance.getEntriesByType('resource').length,serverControls:!!document.querySelector('#load-roster,#save-html,.find-time')})")
            check("downloaded_html_has_no_server_dependency", offline ==
                {"protocol": "data:", "rows": 2, "resources": 0, "serverControls": False}, offline)
            report["file_url_verification"] = {"status": "not_attempted",
                "reason": "전용 CLI의 알려진 file: 제한. 저장 파일과 동일 바이트를 data URL로 열어 서버 독립성만 검사함"}
            report["status"] = "passed"
    except Exception as error:
        report["status"] = "failed"
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        if lease:
            try:
                report["failure_screen"] = screen()
                screenshot("failure.png")
            except Exception as capture_error:
                report["failure_capture_error"] = str(capture_error)
        raise
    finally:
        registry.release.set()
        ControlledRunner.release.set()
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
