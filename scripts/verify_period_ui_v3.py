"""격리된 합성 자료로 시간 작업·기간 시즌·색상·인쇄를 실제 브라우저에서 검증한다."""

import asyncio
import base64
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import sys
from threading import Event, Thread
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.api import create_app
from app.config import load_settings
from app.exporting import render_export
from app.jobs import BudgetStop
from app.launcher import bind_server
import uvicorn

HELPER = str(Path.home() / ".agents/skills/browser-verification/scripts/playwright-session.mjs")


class ControlledRunner:
    """실제 작업 관리자에 연결하되 외부 API·모델·사실 수집은 대신하지 않는 화면 검사기."""

    release = Event()
    mode = "empty"
    calls = []

    def __init__(self, settings, meter=None):
        """검사 소유 작업 계량기를 연결한다.

        Args:
            settings: 격리 앱 설정.
            meter: 실제 작업 관리자 계량기.
        """
        self.meter = meter

    async def run(self, institution, season, identifiers, *, purpose="date_count", target_event=None, **kwargs):
        """정확한 작업 조건을 기록하고 검사 신호나 사용자 중지 때까지 기다린다.

        Args:
            institution: 고정 큐의 학교.
            season: 고정 조사 조건.
            identifiers: 기관 식별자.
            purpose: 날짜·인원 또는 시간.
            target_event: 시간 대상 행사.
            **kwargs: 경로와 기존 근거 등의 실제 실행기 입력.
        Returns:
            외부 검증 성과로 계산하지 않는 빈 조사 결과.
        Raises:
            BudgetStop: 실제 일시정지·취소가 요청된 경우.
            RuntimeError: 실패 화면을 검사하기 위해 주입한 오류.
        """
        self.calls.append({"purpose": purpose, "season": season, "institution_id": institution["id"],
                           "target_event_id": target_event.get("id") if target_event else None})
        while not self.release.is_set():
            job = self.meter.manager.get(self.meter.job_id)
            if job["status"] != "running":
                raise BudgetStop("user_cancel" if job["status"] in {"cancelling", "cancelled"} else "user_pause")
            await asyncio.sleep(.05)
        if self.mode == "failure":
            raise RuntimeError("합성 화면 검사에서 주입한 조사 실패")
        if self.mode == "time" and target_event:
            service = self.meter.manager.service
            event = service.event_detail(target_event["id"])["event"]
            service.apply_overrides(event["id"], {"expected_revision": event["revision"], "operations": [{
                "field": "start_time", "action": "set", "value": "10:30", "bound_date": "2027-01-08",
                "basis": "phone_confirmed", "confirmed_at": "2026-10-20T01:00:00Z",
                "note": "브라우저 검사의 합성 확인값이며 실제 학교 사실이 아님", "source_url": None,
                "resolves_fingerprint": None}]})
        return {"documents": [], "candidates": [], "counts": [], "errors": [], "purpose": purpose}


class LocalApp:
    """사용자 DB나 서버에 접근하지 않는 신규 검사 DB와 서버를 소유한다."""

    def __init__(self, root):
        """검사 전용 앱과 포트를 준비한다.

        Args:
            root: 이 실행의 자료 폴더.
        """
        settings = load_settings(dotenv_path=root / "absent.env", process_values={
            "APP_DATA_DIR": str(root / "data"), "AUTO_OPEN_BROWSER": "false", "SEARCH_PROVIDER": "none"})
        self.app = create_app(settings, runner_factory=ControlledRunner)
        self.runtime = self.app.state.runtime
        self.socket = bind_server("127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.socket.getsockname()[1]}/"
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level="error", access_log=False))
        self.thread = Thread(target=lambda: self.server.run(sockets=[self.socket]), daemon=True)

    def start(self):
        """자신이 준비한 서버만 시작하고 준비 상태를 확인한다."""
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return
            time.sleep(.05)
        raise RuntimeError("검사 서버 시작 실패")

    def stop(self):
        """이 객체가 시작한 서버만 종료하고 잔여 실행을 확인한다."""
        self.server.should_exit = True
        if self.thread.ident:
            self.thread.join(timeout=15)
        self.socket.close()
        if self.thread.is_alive():
            raise RuntimeError("검사 서버 종료 실패")


def fixtures(service):
    """페이지 경계의 같은 날짜와 인접 날짜가 있는 45개 합성 학교를 만든다.

    Args:
        service: 검사 전용 서비스.
    Returns:
        저장된 시즌과 첫 행사.
    """
    season = service.list_seasons()["items"][0]
    first = None
    for n in range(45):
        school = service.create_institution({"name": f"화면검사{n + 1:02}학교", "type": "elementary",
            "district": "북구" if n < 35 else "남구", "address": "합성 검사 주소", "campus_label": None,
            "phone": None, "homepage_url": None})
        if n >= 43:
            continue
        day = "2027-01-08" if n < 23 else "2027-01-20" if n < 33 else "2027-01-21"
        common = {"action": "set", "basis": "phone_confirmed", "confirmed_at": "2026-10-20T01:00:00Z",
                  "note": "브라우저 합성 자료", "source_url": None, "resolves_fingerprint": None}
        event = service.create_event({"institution_id": school["id"], "season_id": season["id"],
            "title": "졸업식", "event_type": "graduation_ceremony", "audience": None, "campus_label": None,
            "allow_separate_event": False, "operations": [
                {**common, "field": "ceremony_status", "value": "scheduled"},
                {**common, "field": "event_date", "value": {"precision": "day", "date": day, "month": None}}]})
        if first is None:
            first = event
    return season, first


def main():
    """한 전용 브라우저 lease에서 실제 화면을 확인하고 정리 증거를 남긴다."""
    out = ROOT / "runtime/period-ui-20261021" / datetime.now().strftime("browser-%H%M%S")
    out.mkdir(parents=True)
    local = LocalApp(out)
    season, event = fixtures(local.runtime.service)
    evidence = {"status": "running", "synthetic_only": True, "user_database_accessed": False,
        "external_api_or_model_called": False, "assertions": {}, "operations": [], "screenshots": []}
    lease = None

    def invoke(arguments, optional=False):
        """같은 helper와 lease에 명령을 보내고 실패 출력도 보존한다."""
        result = subprocess.run([HELPER, *arguments], cwd=out, text=True, capture_output=True, timeout=55)
        saved = {"command": arguments[:5], "returncode": result.returncode,
                 "stdout": result.stdout, "stderr": result.stderr}
        if any(value.startswith("data:") for value in arguments):
            saved["command"] = ["run", "goto", "standalone HTML bytes"]
        evidence["operations"].append(saved)
        if result.returncode and not optional:
            raise RuntimeError(result.stderr or result.stdout[:2000])
        return result

    def evaluate(code):
        """실제 페이지에서 DOM과 이벤트 처리 결과를 JSON으로 읽는다."""
        raw = json.loads(invoke(["run", "--lease", lease, "--", "eval", code]).stdout)["stdout"]
        if "### Result\n" not in raw:
            raise RuntimeError(raw[:2000])
        value = raw.split("### Result\n", 1)[1].split("\n###", 1)[0].strip()
        for _ in range(3):
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    break
        return value

    def run(code):
        """수명 제어 없이 같은 브라우저의 상호작용과 매체 전환을 실행한다."""
        return invoke(["run", "--lease", lease, "--", "run-code", code])

    def wait(expression):
        """임의 지연 대신 화면 조건 충족을 최대 15초 기다린다."""
        return evaluate("async () => {const end=Date.now()+15000;while(Date.now()<end){if(" + expression +
                        ")return true;await new Promise(r=>setTimeout(r,80));}return false;}")

    def check(name, passed, actual):
        """실패 원인과 원 관찰을 바꾸지 않고 검사 결과를 기록한다."""
        evidence["assertions"][name] = {"passed": bool(passed), "actual": actual}
        if not passed:
            raise AssertionError(f"{name}: {actual}")
        print(f"browser-v3: {name}", flush=True)

    def shot(name):
        """검사 폴더에 화면을 저장한다."""
        invoke(["run", "--lease", lease, "--", "screenshot", f"--filename={out / name}"])
        evidence["screenshots"].append(name)

    def filter_rows(query="", district="", day=""):
        """표시 필터를 실제 input 이벤트로 적용한다."""
        return evaluate("() => {const values=" + json.dumps({"query": query, "district": district, "date": day}, ensure_ascii=False) +
            ";for(const [key,value] of Object.entries(values)){const input=document.querySelector(`[data-control=\"${key}\"]`);input.value=value;input.dispatchEvent(new Event('input'));}"
            "return {matches:document.querySelectorAll('tr[data-match=true]').length,visible:document.querySelectorAll('tbody tr:not([hidden])').length,page:document.querySelector('[data-control=page]').textContent};}")

    def row_state(name="화면검사01학교"):
        """특정 행의 현재 시간 칸과 작업 표시를 읽는다."""
        return evaluate("() => {const row=document.querySelector('tr[data-name=" + json.dumps(name, ensure_ascii=False) + "]');"
            "return {text:row.children[2].innerText,state:row.querySelector('.time-research')?.dataset.state,spinner:!!row.querySelector('.time-spinner'),disabled:row.querySelector('button')?.disabled};}")

    def click_time(name="화면검사01학교"):
        """화면에 보이는 단일 행을 필터로 고른 뒤 실제 버튼을 누른다."""
        filter_rows(name)
        run("async (page) => {await page.locator('#results tr[data-match=true] .find-time').click();}")

    def print_all(name, expected):
        """현재 필터 전체 행을 흑백 표로 인쇄하고 연구 상태 칸을 비운다."""
        run("async (page) => {await page.emulateMedia({media:'print'});}")
        result = evaluate("""() => {const rows=[...document.querySelectorAll('tbody tr')];return {
          shown:rows.filter(r=>getComputedStyle(r).display!=='none').length,
          controls:[...document.querySelectorAll('button,input,select,textarea,header,footer,h1,h2')].filter(n=>n.getClientRects().length).length,
          hiddenResearch:[...document.querySelectorAll('.time-research')].every(n=>getComputedStyle(n).display==='none'&&n.parentElement.innerText.trim()===''),
          boundaries:[...document.querySelectorAll('tr.date-group-start td:first-child')].map(n=>getComputedStyle(n).borderTopWidth)};}""")
        check(name, result["shown"] == expected and result["controls"] == 0 and result["hiddenResearch"] and
              all(value == "2px" for value in result["boundaries"]), result)
        shot(name + ".png")
        run("async (page) => {await page.pdf({path:" + json.dumps(str(out / (name + '.pdf'))) + ",format:'A4',landscape:true,preferCSSPageSize:true});await page.emulateMedia({media:'screen'});}")
        from pypdf import PdfReader
        pdf = PdfReader(out / (name + ".pdf"))
        text = re.sub(r"\s+", "", "\n".join(page.extract_text() for page in pdf.pages))
        names = set(re.findall(r"화면검사\d{2}학교", text))
        check(name + "-pdf", len(names) == expected and not any(value in text for value in ["찾는중", "시간찾기", "확인하지못"]),
              {"pages": len(pdf.pages), "unique_schools": len(names), "expected": expected})

    try:
        local.start()
        lease = json.loads(invoke(["start", "--url", local.url, "--deadline", "1100"]).stdout)["lease"]
        invoke(["run", "--lease", lease, "--", "resize", "1440", "1050"])
        check("initial_45", wait("document.querySelectorAll('#results tbody tr').length===45"), 45)
        evaluate("""() => {window.__jobs=[];const original=window.fetch;window.fetch=(url,options)=>{if(url==='/api/jobs'&&options?.body)window.__jobs.push(JSON.parse(options.body));return original(url,options);};return true;}""")
        click_time()
        check("target_running", wait("document.querySelector('tr[data-name=\"화면검사01학교\"] .time-research')?.dataset.state==='running'"), row_state())
        other = row_state("화면검사02학교")
        check("other_row_disabled_without_progress", other["disabled"] and not other["spinner"] and other["text"] == "시간 찾기", other)
        duplicate = evaluate("""() => {const row=document.querySelector('tr[data-name="화면검사01학교"]');row.querySelector('button')?.click();return window.__jobs;}""")
        check("single_exact_time_request", len(duplicate) == 1 and duplicate[0]["target_event_id"] == event["id"] and duplicate[0]["season_id"] == season["id"], duplicate)
        filter_rows()
        evaluate("() => {document.querySelector('[data-control=next]').click();return true;}")
        evaluate("() => {document.querySelector('[data-control=previous]').click();return true;}")
        check("poll_and_page_preserve_target", wait("document.querySelector('tr[data-name=\"화면검사01학교\"] .time-research')?.dataset.state==='running'"), row_state())
        invoke(["run", "--lease", lease, "--", "reload"])
        check("browser_reload_restores_target", wait("document.querySelector('tr[data-name=\"화면검사01학교\"] .time-research')?.dataset.state==='running'"), row_state())
        shot("running-desktop.png")
        run("async (page) => {await page.getByRole('button',{name:'잠시 멈추기',exact:true}).click();}")
        check("paused_same_cell", wait("document.querySelector('tr[data-name=\"화면검사01학교\"] .time-research')?.dataset.state==='paused'"), row_state())
        click_time()
        check("resume_same_job", wait("document.querySelector('tr[data-name=\"화면검사01학교\"] .time-research')?.dataset.state==='running'"), row_state())
        run("async (page) => {await page.getByRole('button',{name:'조사 취소',exact:true}).click();}")
        check("cancelled_same_cell", wait("document.querySelector('tr[data-name=\"화면검사01학교\"] .time-research')?.dataset.state==='cancelled'"), row_state())
        ControlledRunner.release.set(); click_time()
        check("not_found_not_unpublished", wait("document.querySelector('tr[data-name=\"화면검사01학교\"] .time-research')?.dataset.state==='completed'"), row_state())
        check("not_found_wording", row_state()["text"] == "이번 조사에서 시간을 찾지 못했어요.\n다시 찾기", row_state())
        ControlledRunner.mode = "failure"; click_time()
        check("failed_same_cell", wait("document.querySelector('tr[data-name=\"화면검사01학교\"] td:nth-child(3)').innerText.includes('조사 중 오류가 났어요.')"), row_state())
        ControlledRunner.mode = "time"; click_time()
        check("completed_replaced_by_time", wait("document.querySelector('tr[data-name=\"화면검사01학교\"] td:nth-child(3)').innerText==='10:30'"), row_state())
        ControlledRunner.mode = "empty"; ControlledRunner.release.clear(); click_time("화면검사02학교")
        check("second_target_running", wait("document.querySelector('tr[data-name=\"화면검사02학교\"] .time-research')?.dataset.state==='running'"), row_state("화면검사02학교"))
        filter_rows()
        colors = evaluate("""() => {const rows=[...document.querySelectorAll('tbody tr[data-date]')];const groups={};for(const row of rows){if(row.dataset.date)(groups[row.dataset.date]??=new Set()).add(getComputedStyle(row).backgroundColor);}const before=rows.filter(r=>r.dataset.date==='2027-01-08').map(r=>r.className.match(/date-color-\d/)[0]);document.querySelector('[data-control=next]').click();return {groups:Object.fromEntries(Object.entries(groups).map(([d,c])=>[d,[...c]])),sameDayColors:[...new Set(before)],secondPage:[...document.querySelectorAll('tbody tr:not([hidden])')].filter(r=>r.dataset.date==='2027-01-08').map(r=>getComputedStyle(r).backgroundColor)};}""")
        group_colors = [value[0] for value in colors["groups"].values()]
        check("adjacent_date_groups_and_page_boundary", all(len(value) == 1 for value in colors["groups"].values()) and len(set(group_colors)) == 3 and len(colors["sameDayColors"]) == 1 and colors["secondPage"] == [group_colors[0]] * 3, colors)
        filtered = filter_rows("", "남구")
        reset_color = evaluate("() => document.querySelector('tbody tr[data-match=true]').className")
        check("filter_reassigns_group_palette", "date-color-0" in reset_color and filtered["page"].startswith("1 /"), {"filter": filtered, "firstClass": reset_color})
        empty = filter_rows("없는학교")
        check("empty_results", empty["matches"] == 0 and empty["visible"] == 0, empty)
        filter_rows()
        evaluate("() => {document.querySelector('[data-control=next]').click();return true;}")
        print_all("app-print-all-45", 45)
        # 활성 작업으로 비활성화된 버튼과 활성 일반 버튼의 실제 계산 스타일을 구별한다.
        styles = evaluate("() => ({normal:getComputedStyle(document.querySelector('#open-html')).backgroundColor,disabled:getComputedStyle(document.querySelector('#research-submit')).backgroundColor})")
        run("async (page) => {await page.locator('#open-html').hover();}")
        styles["hover"] = evaluate("() => getComputedStyle(document.querySelector('#open-html')).backgroundColor")
        run("async (page) => {await page.locator('#open-html').focus();await page.keyboard.press('Tab');}")
        styles["focus"] = evaluate("() => ({outline:getComputedStyle(document.activeElement).outlineWidth,visible:document.activeElement.matches(':focus-visible')})")
        check("button_hover_focus_disabled", styles["normal"] != styles["hover"] and styles["normal"] != styles["disabled"] and styles["focus"]["visible"] and styles["focus"]["outline"] == "3px", styles)
        run("async (page) => {await page.getByRole('button',{name:'조사 취소',exact:true}).click();}")
        check("second_cancelled", wait("document.querySelector('tr[data-name=\"화면검사02학교\"] .time-research')?.dataset.state==='cancelled'"), row_state("화면검사02학교"))
        primary = {"normal": evaluate("() => getComputedStyle(document.querySelector('#research-submit')).backgroundColor")}
        run("async (page) => {await page.locator('#research-submit').hover();await page.waitForTimeout(160);}")
        primary["hover"] = evaluate("() => getComputedStyle(document.querySelector('#research-submit')).backgroundColor")
        run("async (page) => {await page.mouse.down();await page.waitForTimeout(160);}")
        primary["active"] = evaluate("() => getComputedStyle(document.querySelector('#research-submit')).backgroundColor")
        run("async (page) => {await page.mouse.move(5,5);await page.mouse.up();}")
        check("primary_hover_and_active", len(set(primary.values())) == 3, primary)
        # 학년도 입력 없이 실제 API로 기간을 생성하고 기본 전체 큐와 같은 조건인지 확인한다.
        run("async (page) => {await page.locator('#add-season').click();}")
        evaluate("""() => {document.querySelector('#season-start').value='2024-08-31';document.querySelector('#season-start').dispatchEvent(new Event('input'));document.querySelector('#season-end').value='2025-02-28';document.querySelector('#season-end').dispatchEvent(new Event('input'));return true;}""")
        invalid = evaluate("() => ({valid:document.querySelector('#season-form').checkValidity(),max:document.querySelector('#season-end').max,academic:!!document.querySelector('[name=academic_year]')})")
        check("season_exclusive_clamped_boundary", not invalid["valid"] and invalid["max"] == "2025-02-27" and not invalid["academic"], invalid)
        evaluate("""() => {document.querySelector('#season-start').value='2024-12-01';document.querySelector('#season-start').dispatchEvent(new Event('input'));document.querySelector('#season-end').value='2025-03-31';document.querySelector('#season-end').dispatchEvent(new Event('input'));return true;}""")
        run("async (page) => {await page.locator('#season-submit').click();}")
        check("new_period_started", wait("document.querySelector('#season-select').selectedOptions[0].textContent.includes('2024-12-01') && document.querySelector('#research-summary').textContent.includes('조사하고')"), evaluate("() => document.querySelector('#season-select').selectedOptions[0].textContent"))
        newest = local.runtime.jobs.list()[0]
        snapshot = newest["scope_snapshot"]
        created = next(s for s in local.runtime.service.list_seasons()["items"] if s["id"] == newest["season_id"])
        check("new_period_actual_job_all_schools", newest["target_count"] == 45 and created["search_start"] == "2024-12-01" and created["search_end"] == "2025-03-31", {"season": created, "snapshot": snapshot})
        run("async (page) => {await page.getByRole('button',{name:'조사 취소',exact:true}).click();}")
        check("new_period_cancelled", wait("document.querySelector('#research-submit').disabled===false"), True)
        old_id = created["id"]
        run("async (page) => {await page.locator('#research-submit').click();}")
        check("existing_period_refresh_started", wait("document.querySelector('#research-summary').textContent.includes('조사하고')"), True)
        refreshed = local.runtime.jobs.list()[0]
        check("existing_period_id_preserved", refreshed["season_id"] == old_id and len(local.runtime.service.list_seasons()["items"]) == 2, refreshed["scope_snapshot"])
        run("async (page) => {await page.getByRole('button',{name:'조사 취소',exact:true}).click();}")
        wait("document.querySelector('#research-submit').disabled===false")
        run("async (page) => {await page.locator('#season-select').selectOption(" + json.dumps(season["id"]) + ");}")
        check("original_manual_records_preserved", wait("document.querySelector('tr[data-name=\"화면검사01학교\"] td:nth-child(3)')?.innerText==='10:30'"), row_state())
        invoke(["run", "--lease", lease, "--", "resize", "390", "844"])
        mobile = evaluate("() => ({width:innerWidth,scroll:document.documentElement.scrollWidth})")
        check("mobile_app_no_horizontal_overflow", mobile["scroll"] <= mobile["width"], mobile); shot("app-mobile.png")
        evaluate("() => {window.__downloadName=null;const original=HTMLAnchorElement.prototype.click;HTMLAnchorElement.prototype.click=function(){window.__downloadName=this.download;original.call(this);};document.querySelector('#save-html').click();return true;}")
        check("download_filename_has_period", wait("window.__downloadName?.includes('2026-12-01_2027-03-31')"), evaluate("() => window.__downloadName"))
        html = render_export(local.runtime.service.snapshot(season["id"]))
        (out / "offline-fixture.html").write_text(html)
        local.stop()
        local = LocalApp(out); local.start()
        invoke(["run", "--lease", lease, "--", "goto", local.url])
        check("app_restart_preserves_periods_and_manual_time", wait("document.querySelectorAll('#season-select option').length===2 && document.querySelector('tr[data-name=\"화면검사01학교\"] td:nth-child(3)')?.innerText==='10:30'"), {"season_id": season["id"], "row": row_state()})
        local.stop()
        invoke(["run", "--lease", lease, "--", "goto", "data:text/html;base64," + base64.b64encode(html.encode()).decode()])
        offline = filter_rows()
        contents = evaluate("() => ({colors:[...document.querySelectorAll('tr.date-group-start')].map(r=>r.className),buttons:[...document.querySelectorAll('button')].map(b=>b.textContent),resources:performance.getEntriesByType('resource').length})")
        check("offline_group_palette_without_server", offline["matches"] == 45 and len(set(value.split(' ')[0] for value in contents["colors"][:3])) == 3 and contents["resources"] == 0 and not any('찾기' in value for value in contents["buttons"]), contents)
        filters = filter_rows("화면검사", "북구", "2027-01-08")
        evaluate("() => {document.querySelector('[data-control=next]').click();return true;}")
        last_page = evaluate("() => document.querySelectorAll('tbody tr:not([hidden])').length")
        check("offline_filters_and_page_boundary", filters["matches"] == 23 and filters["visible"] == 20 and last_page == 3, {"filter": filters, "last": last_page})
        invoke(["run", "--lease", lease, "--", "resize", "1440", "1050"])
        print_all("offline-print-filtered-23", 23)
        shot("offline-desktop.png")
        evidence["runner_calls"] = ControlledRunner.calls
        evidence["status"] = "passed"
    except Exception as error:
        evidence["status"] = "failed"
        evidence["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        if lease:
            result = subprocess.run([HELPER, "finish", "--lease", lease], cwd=out, text=True, capture_output=True, timeout=55)
            cleanup = json.loads(result.stdout)
            evidence["cleanup"] = {key: cleanup.get(key) for key in ["state", "cleanupMode", "finalResidualCount"]}
            assert cleanup["state"] == "FINISHED" and cleanup["finalResidualCount"] == 0
        local.stop()
        evidence["own_server_stopped"] = True
        (out / "result.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"status": evidence["status"], "out": str(out), "checks": len(evidence["assertions"]), "cleanup": evidence.get("cleanup")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
