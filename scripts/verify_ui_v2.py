"""전용 브라우저로 실제 명부 사본과 별도 가상 자료의 조회·인쇄를 검증한다."""

import asyncio
import base64
from datetime import datetime
import hashlib
import json
import re
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Thread
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.api import create_app
from app.config import load_settings
from app.exporting import render_export
from app.launcher import bind_server
import uvicorn

HELPER = str(Path.home() / ".agents/skills/browser-verification/scripts/playwright-session.mjs")
SOURCE = ROOT / "runtime/improvements-20261021/sources-integrated/20261021T023854465442+0000"
SEASON = "ba3daa8c-f911-4aae-ae89-f454a946d72a"
SCHOOL = "dccaa9d9-9618-49a6-bd35-bf2eca00e1e2"


class IsolatedRunner:
    """화면의 실제 작업 계약만 실행하고 외부 서비스는 호출하지 않는다."""

    calls = []

    def __init__(self, settings, meter=None):
        """검사 실행의 계량기 연결을 유지한다.

        Args:
            settings: 격리 앱 설정.
            meter: 코드 기반 작업 계량기.
        """
        self.meter = meter

    async def run(self, institution, season, identifiers, *, purpose="date_count", target_event=None, routes=None, existing_evidence=None):
        """정확한 대상과 목적을 기록하고 합성 사실 없이 반환한다.

        Args:
            institution: 실제 큐의 학교.
            season: 조사 시즌.
            identifiers: 학교 식별자.
            purpose: 날짜·인원 또는 시각 조사.
            target_event: 시각 조사 대상.
            routes: 재사용 경로.
            existing_evidence: 기존 공개 근거.
        Returns:
            외부 조사 성공으로 계산하지 않는 빈 결과.
        """
        self.calls.append({"institution_id": institution["id"], "season_id": season["id"],
                           "purpose": purpose, "target_event_id": target_event.get("id") if target_event else None})
        await asyncio.sleep(.04)
        return {"documents": [], "candidates": [], "counts": [], "errors": [], "purpose": purpose}


class LocalApp:
    """읽기 전용 원본 백업에서 이 검사만의 서버를 만들고 정확히 종료한다."""

    def __init__(self, root):
        """별도 DB 사본과 무작위 포트 앱을 준비한다.

        Args:
            root: 검사 소유 artifact 폴더.
        """
        path = root / "data/live/graduation.sqlite3"
        path.parent.mkdir(parents=True)
        with sqlite3.connect((SOURCE / "data/live/graduation.sqlite3").as_uri()+"?mode=ro", uri=True) as source:
            with sqlite3.connect(path) as target:
                source.backup(target)
        settings = load_settings(dotenv_path=root/"absent.env", process_values={
            "APP_DATA_DIR": str(root/"data"), "AUTO_OPEN_BROWSER": "false", "SEARCH_PROVIDER": "none"})
        self.app = create_app(settings, runner_factory=IsolatedRunner)
        self.runtime = self.app.state.runtime
        self.socket = bind_server("127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.socket.getsockname()[1]}/"
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level="error", access_log=False))
        self.runtime.shutdown_callback = lambda: setattr(self.server, "should_exit", True)
        self.thread = Thread(target=lambda: self.server.run(sockets=[self.socket]), daemon=True)

    def start(self):
        """이 검사 서버가 실제 준비될 때까지 제한적으로 기다린다."""
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return
            time.sleep(.05)
        raise RuntimeError("검사 서버 시작 실패")

    def stop(self):
        """이 객체가 시작한 서버만 종료하고 반환을 확인한다."""
        self.server.should_exit = True
        if self.thread.ident:
            self.thread.join(timeout=15)
        self.socket.close()
        if self.thread.is_alive():
            raise RuntimeError("검사 서버 종료 실패")


def add_fixture(service):
    """별도 사본에 45개 합성 기관을 추가해 페이지·구·복수일 경계를 만든다.

    Args:
        service: 별도 가상 자료용 DB 사본 서비스.
    Returns:
        합성 기관 UUID 목록.
    """
    ids = []
    common = {"action": "set", "basis": "phone_confirmed", "confirmed_at": "2026-10-19T01:00:00+00:00",
              "note": "화면 검사 전용 합성 자료", "source_url": None, "resolves_fingerprint": None}
    for n in range(45):
        school = service.create_institution({"name": f"화면검사{n+1:02}학교", "type": "elementary",
            "district": "남구" if 35 <= n <= 42 else "북구", "address": "검사 주소", "campus_label": None,
            "phone": None, "homepage_url": None})
        ids.append(school["id"])
        if n == 43:
            continue
        day = "2027-01-08" if 25 <= n <= 34 else "2027-01-07"
        dates = {"precision": "day", "date": day, "month": None}
        if n == 44:
            dates["dates"] = ["2027-01-07", "2027-01-08"]
        ops = [{**common, "field": "ceremony_status", "value": "scheduled"},
               {**common, "field": "event_date", "value": dates}]
        if n % 2 and n != 44:
            ops.append({**common, "field": "start_time", "value": "10:00", "bound_date": day})
        service.create_event({"institution_id": school["id"], "season_id": SEASON, "title": "졸업식",
            "event_type": "graduation_ceremony", "audience": None, "campus_label": None,
            "allow_separate_event": False, "operations": ops})
    return ids


def main():
    """한 browser lease에서 실제 앱과 독립 파일을 검증하고 잔여 없이 정리한다."""
    out = ROOT / "runtime/improvements-20261021" / datetime.now().strftime("browser-%H%M%S")
    out.mkdir(parents=True)
    real = LocalApp(out/"actual")
    synthetic = LocalApp(out/"synthetic")
    fixture_ids = add_fixture(synthetic.runtime.service)
    real_html = render_export(real.runtime.service.snapshot(SEASON))
    fake_html = render_export(synthetic.runtime.service.snapshot(SEASON))
    (out/"source-result.html").write_text(real_html)
    (out/"fixture-result.html").write_text(fake_html)
    evidence = {"source": str(SOURCE), "original_user_data_written": False,
        "fixture_scope": "실제 원문 사본과 별도 사본의 합성 45기관. 수기10시는 합성 입력이며 사실 검증 성과에 포함하지 않음.",
        "assertions": {}, "operations": [], "screenshots": [], "servers": [real.url, synthetic.url]}
    lease = None

    def invoke(arguments, *, optional=False):
        """전용 helper의 같은 lease로 모든 브라우저 명령을 전달한다."""
        process = subprocess.run([HELPER, *arguments], cwd=out, text=True, capture_output=True, timeout=55)
        info = {"command": arguments[:5], "returncode": process.returncode, "stdout": process.stdout, "stderr": process.stderr}
        if any(value.startswith("data:") for value in arguments):
            info["command"] = ["run", "goto", "standalone HTML bytes"]
        evidence["operations"].append(info)
        if process.returncode and not optional:
            raise RuntimeError(process.stderr or process.stdout[:1500])
        return process

    def parse(response):
        """CLI 결과에서 관찰 JSON만 읽는다."""
        raw = json.loads(response.stdout)["stdout"]
        if "### Result\n" not in raw:
            raise RuntimeError(raw[:1500])
        value = raw.split("### Result\n", 1)[1].split("\n###", 1)[0].strip()
        for _ in range(3):
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    break
        return value

    def evaluate(code):
        """실제 DOM과 이벤트 처리 결과를 읽는다."""
        return parse(invoke(["run", "--lease", lease, "--", "eval", code]))

    def run_code(code):
        """동일 페이지의 Playwright 인쇄 매체 전환만 수행한다."""
        return invoke(["run", "--lease", lease, "--", "run-code", code])

    def shot(name):
        """현재 화면을 검사 artifact에 저장한다."""
        invoke(["run", "--lease", lease, "--", "screenshot", f"--filename={out/name}"])
        evidence["screenshots"].append(name)

    def check(name, passed, actual):
        """실패 관찰도 보존하고 검사 기준을 바꾸지 않는다."""
        evidence["assertions"][name] = {"passed": bool(passed), "actual": actual}
        if not passed:
            raise AssertionError(f"{name}: {actual}")

    def filter_code(query="", district="", day=""):
        """독립 입력에 실제 input 이벤트를 전달한다."""
        return """() => {const root=document.querySelector('[data-schedule]'),get=k=>root.querySelector(`[data-control="${k}"]`);
          const inputs="""+json.dumps({"query":query,"district":district,"date":day},ensure_ascii=False)+""";
          for(const [key,value] of Object.entries(inputs)){get(key).value=value;get(key).dispatchEvent(new Event('input'));}
          return {count:get('count').textContent,page:get('page').textContent,visible:root.querySelectorAll('tbody tr:not([hidden])').length,
          matches:root.querySelectorAll('tbody tr[data-match=true]').length};} """

    def print_check(name, expected):
        """현재 화면 페이지와 무관하게 필터 전체행만 인쇄되는지 관찰한다."""
        viewport = evaluate("() => ({width:innerWidth,height:innerHeight})")
        invoke(["run","--lease",lease,"--","resize","1440","1050"])
        run_code("async (page) => { await page.emulateMedia({media:'print'}); }")
        result = evaluate("""() => {const root=document.querySelector('[data-schedule]');return {
          shown:[...root.querySelectorAll('tbody tr')].filter(r=>getComputedStyle(r).display!=='none').length,
          mismatch:[...root.querySelectorAll('tbody tr')].filter(r=>(getComputedStyle(r).display!=='none')!==(r.dataset.match==='true')).length,
          controls:[...document.querySelectorAll('button,input,select,textarea,h1,h2,header,footer')].filter(n=>n.getClientRects().length).map(n=>n.tagName),
          blankTime:[...root.querySelectorAll('tr[data-match=true] .find-time')].every(n=>getComputedStyle(n).display==='none' && n.parentElement.innerText.trim()===''),
          borders:[...root.querySelectorAll('tr.date-group-start td:first-child')].map(n=>getComputedStyle(n).borderTopWidth)};}""")
        shot(name+".png")
        run_code("async (page) => { await page.pdf({path:"+json.dumps(str(out/(name+".pdf")))+",landscape:true,format:'A4',preferCSSPageSize:true,printBackground:false}); }")
        from pypdf import PdfReader
        pdf = PdfReader(out/(name+".pdf"))
        pdf_text = re.sub(r"\s+", "", "\n".join(page.extract_text() for page in pdf.pages))
        printed_schools = set(re.findall(r"화면검사\d{2}학교", pdf_text))
        result["pdf_pages"] = len(pdf.pages)
        result["pdf_synthetic_school_count"] = len(printed_schools)
        check(name, len(printed_schools)==expected and result["shown"]==expected and result["mismatch"]==0 and not result["controls"] and result["blankTime"], result)
        run_code("async (page) => { await page.emulateMedia({media:'screen'}); }")
        invoke(["run","--lease",lease,"--","resize",str(viewport["width"]),str(viewport["height"])])

    try:
        real.start(); synthetic.start()
        print("browser-v2: start dedicated helper", flush=True)
        started = invoke(["start", "--url", real.url, "--deadline", "1200"])
        lease = json.loads(started.stdout)["lease"]
        invoke(["run", "--lease", lease, "--", "resize", "1440", "1050"])
        initial = evaluate("""async () => {await new Promise(r=>setTimeout(r,900));return {
          rows:document.querySelectorAll('#results tbody tr').length,headers:[...document.querySelectorAll('#results th')].map(n=>n.textContent),
          school:[...document.querySelectorAll('#results tbody tr')].filter(r=>r.dataset.name==='건국초등학교').map(r=>r.innerText),
          links:[...document.querySelectorAll('#results tbody a')].map(a=>a.href),body:document.body.innerText};}""")
        check("actual_339_and_188", initial["rows"]==339 and len(initial["school"])==1 and "188" in initial["school"][0] and "2027-01-06" in initial["school"][0], {k:initial[k] for k in ["rows","headers","school"]})
        check("five_columns", initial["headers"]==["날짜","학교명","시간","졸업인원수","장소"], initial["headers"])
        from app.results import human_url
        from app.providers import official_channel
        schools = real.runtime.service.snapshot(SEASON)["targets"]
        expected_links = [human_url(s.get("homepage_url")) for s in schools
                          if human_url(s.get("homepage_url")) and official_channel(s, human_url(s["homepage_url"]))]
        canonical = evaluate("() => "+json.dumps(expected_links,ensure_ascii=False)+".map(url=>new URL(url).href)")
        matches = set(initial["links"]) == set(canonical)
        check("homepage_only_links", matches and not any("open.neis" in u or "SchoolSchedule" in u for u in initial["links"]),
              {"count":len(initial["links"]),"matches_official_registry_homepages":matches})
        check("simple_main_ui", not any(w in initial["body"] for w in ["검증 상태","revision","HTTP","토큰","판매 준비 가능","일반 일정표"]), True)
        shot("app-actual-desktop.png")
        evaluate("""() => {window.__jobBodies=[];const original=window.fetch;window.fetch=(url,options)=>{if(url==='/api/jobs'&&options?.body)window.__jobBodies.push(JSON.parse(options.body));return original(url,options);};return true;}""")
        evaluate(filter_code("건국초", "북구", "2027-01-06"))
        target_event = next(e for e in real.runtime.service.events(SEASON)["items"] if e["institution"]["id"]==SCHOOL)["id"]
        request = evaluate("""async () => {document.querySelector('#results tr[data-match=true] .find-time').click();await new Promise(r=>setTimeout(r,750));return window.__jobBodies.at(-1);}""")
        check("time_request_exact_event", request["purpose"]=="time" and request["target_event_id"]==target_event and request["selection"]=={"scope":"ids","institution_ids":[SCHOOL]}, request)
        evaluate("""() => {document.querySelector('#record-text').value='건국초 졸업식은 1월 6일 오전 10시, 학교 강당이래. 방금 학교에 전화해서 확인했어. 기록해줘.';document.querySelector('#record-form button[type=submit]').click();return true;}""")
        recorded = evaluate("""async () => {await new Promise(r=>setTimeout(r,800));return {reply:document.querySelector('#conversation').innerText,rows:[...document.querySelectorAll('#results tbody tr')].filter(r=>r.dataset.name==='건국초등학교').map(r=>r.innerText)};}""")
        events = [e for e in real.runtime.service.events(SEASON)["items"] if e["institution"]["id"]==SCHOOL]
        check("natural_existing_event_update", len(events)==1 and events[0]["id"]==target_event and events[0]["publication"]["calendar_time"]=="10:00", recorded)
        check("school_address_not_venue", "학교 강당" not in recorded["rows"][0], recorded["rows"])
        request = evaluate("""async () => {document.querySelector('#research-submit').click();await new Promise(r=>setTimeout(r,500));return window.__jobBodies.at(-1);}""")
        with real.runtime.service.store.transaction() as conn:
            latest = dict(conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone())
        check("default_all_scope_ignores_display_filters", request["purpose"]=="date_count" and request["selection"]=={"scope":"all"} and latest["target_count"]==339, {"request":request,"targets":latest["target_count"]})
        real.runtime.jobs.pause(latest["id"])
        evaluate("""() => {document.querySelector('#save-html').click();return true;}""")
        saved = evaluate("""async () => {await new Promise(r=>setTimeout(r,650));return document.querySelector('#message').textContent;}""")
        check("html_save_button", "HTML 파일을 저장" in saved, saved)
        invoke(["run","--lease",lease,"--","resize","390","844"])
        mobile = evaluate("() => ({width:innerWidth,scroll:document.documentElement.scrollWidth})")
        check("app_mobile_width", mobile["scroll"]<=mobile["width"], mobile);shot("app-actual-mobile.png")
        print("browser-v2: fixture filtering and print", flush=True)
        invoke(["run","--lease",lease,"--","goto",synthetic.url])
        evaluate("async () => {await new Promise(r=>setTimeout(r,700));return true;}")
        first = evaluate(filter_code("화면검사", "북구", "2027-01-07"))
        check("app_three_filters_and_20", first["matches"]==26 and first["visible"]==20 and first["page"].startswith("1 /"), first)
        colors = evaluate("""() => {const root=document.querySelector('[data-schedule]'),get=k=>root.querySelector(`[data-control="${k}"]`);const row=root.querySelector('tr[data-name="화면검사01학교"]');const before=getComputedStyle(row).backgroundColor;get('next').click();const last={visible:root.querySelectorAll('tbody tr:not([hidden])').length,page:get('page').textContent};get('previous').click();return {before,after:getComputedStyle(row).backgroundColor,last};}""")
        check("paging_and_color_stability", colors["last"]["visible"]==6 and colors["before"]==colors["after"], colors)
        evaluate("() => {document.querySelector('[data-control=next]').click();return true;}")
        print_check("app_print_all_filtered_26",26)
        empty = evaluate(filter_code("존재하지않는학교", "북구", "2027-01-07"))
        check("empty_filter_and_reset_page", empty["matches"]==0 and empty["visible"]==0 and empty["page"].startswith("1 /"), empty)
        multi = [evaluate(filter_code("화면검사45학교", "북구", day)) for day in ["2027-01-07","2027-01-08","2027-01-09"]]
        check("multiple_ceremony_days", [r["matches"] for r in multi]==[1,1,0], multi)
        evaluate(filter_code("화면검사", "북구", "2027-01-07"));shot("app-fixture-mobile.png")
        print("browser-v2: stop own servers before standalone HTML",flush=True)
        real.stop();synthetic.stop()
        result = invoke(["run","--lease",lease,"--","goto",(out/"source-result.html").as_uri()],optional=True)
        evidence["file_navigation"]={"returncode":result.returncode,"stdout":result.stdout[-1600:]}
        invoke(["run","--lease",lease,"--","goto","data:text/html;base64,"+base64.b64encode(fake_html.encode()).decode()])
        first = evaluate(filter_code("화면검사", "북구", "2027-01-07"))
        evaluate("() => {document.querySelector('[data-control=next]').click();return true;}")
        last = evaluate("() => ({visible:document.querySelectorAll('tbody tr:not([hidden])').length,page:document.querySelector('[data-control=page]').textContent,buttons:[...document.querySelectorAll('button')].map(n=>n.textContent),external:performance.getEntriesByType('resource').filter(r=>/^https?:/.test(r.name)).length})")
        check("standalone_filters_and_paging", first["matches"]==26 and first["visible"]==20 and last["visible"]==6 and last["external"]==0 and "시간 찾기" not in last["buttons"], {"first":first,"last":last})
        print_check("standalone_print_all_filtered_26",26)
        multi = [evaluate(filter_code("화면검사45학교", "북구", day)) for day in ["2027-01-07","2027-01-08"]]
        check("standalone_multiple_days", [r["matches"] for r in multi]==[1,1], multi)
        mobile = evaluate("() => ({width:innerWidth,scroll:document.documentElement.scrollWidth})")
        check("standalone_mobile_width", mobile["scroll"]<=mobile["width"], mobile);shot("html-fixture-mobile.png")
        invoke(["run","--lease",lease,"--","goto","data:text/html;base64,"+base64.b64encode(real_html.encode()).decode()])
        invoke(["run","--lease",lease,"--","resize","1440","1050"])
        actual = evaluate("() => ({rows:document.querySelectorAll('tbody tr').length,school:[...document.querySelectorAll('tbody tr')].filter(r=>r.dataset.name==='건국초등학교').map(r=>r.innerText),resources:performance.getEntriesByType('resource').length})")
        check("standalone_actual_339_188",actual["rows"]==339 and "188" in actual["school"][0] and actual["resources"]==0,actual)
        shot("html-actual-desktop.png")
        invoke(["run","--lease",lease,"--","resize","390","844"]);shot("html-actual-mobile.png")
        evidence["html_sha256"]={"actual":hashlib.sha256(real_html.encode()).hexdigest(),"fixture":hashlib.sha256(fake_html.encode()).hexdigest()}
        evidence["source_bytes_executed_unchanged"]=True
        evidence["runner_calls"] = IsolatedRunner.calls
        evidence["status"]="passed"
    except Exception as error:
        evidence["status"]="failed";evidence["error"]={"type":type(error).__name__,"message":str(error)}
        raise
    finally:
        if lease:
            result = subprocess.run([HELPER,"finish","--lease",lease],cwd=out,text=True,capture_output=True,timeout=55)
            cleanup=json.loads(result.stdout)
            evidence["cleanup"]={k:cleanup.get(k) for k in ["state","cleanupMode","finalResidualCount"]}
            assert cleanup["state"]=="FINISHED" and cleanup["finalResidualCount"]==0
        real.stop();synthetic.stop()
        evidence["own_servers_stopped"]=True
        (out/"result.json").write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+"\n")
        print(json.dumps({"status":evidence.get("status"),"out":str(out),"cleanup":evidence.get("cleanup")},ensure_ascii=False),flush=True)


if __name__ == "__main__":
    main()
