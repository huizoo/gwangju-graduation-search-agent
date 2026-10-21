"""전용 브라우저와 원본 DB 복사본에서 앱·저장 HTML의 실제 동작을 검사한다."""

import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Thread
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.api import create_app
from app.config import load_settings
from app.exporting import render_export
from app.launcher import bind_server
import uvicorn

HELPER = str(Path.home() / ".agents/skills/browser-verification/scripts/playwright-session.mjs")


class IsolatedRunner:
    """화면의 작업 요청 경로만 검사하며 외부 제공자를 호출하지 않는다."""

    def __init__(self, settings, meter=None):
        """검사 실행기의 영속 계량기를 연결한다."""
        self.meter = meter

    async def run(self, institution, season, identifiers):
        """실자료 정확성 검증으로 계산하지 않는 빈 검사 결과를 반환한다."""
        return {"documents": [], "candidates": [], "errors": []}


def fixture_season(service, jobs):
    """페이지 경계 검사용 가상 45기관을 별도 시즌에 격리한다.

    Args:
        service: 복사본 DB 서비스.
        jobs: 같은 복사본의 작업 관리자.
    Returns:
        가상 시즌과 고정 대상 ID.
    """
    season = service.create_season({"label":"화면 검사 전용 가상 시즌", "academic_year":2026,
        "search_start":"2026-12-01", "search_end":"2027-03-31", "export_key":"ui-fixture",
        "timezone":"Asia/Seoul","region_profile":"gwangju_5_gu"})
    ids = []
    for n in range(45):
        inst = service.create_institution({"name":f"화면검사{n+1:02}학교","type":"elementary",
            "district":"북구","address":"검사용 주소","campus_label":None,"phone":None,"homepage_url":None})
        ids.append(inst["id"])
        if n >= 43:
            continue
        day = f"2027-01-{7 if n < 25 else 8:02}"
        common = {"action":"set","basis":"phone_confirmed","confirmed_at":"2026-10-19T01:00:00+00:00",
                  "note":"브라우저 검사용 가상값, 원본 DB에 없음","source_url":None,"resolves_fingerprint":None}
        ops = [{**common,"field":"ceremony_status","value":"scheduled"},
               {**common,"field":"event_date","value":{"precision":"day","date":day,"month":None}}]
        if n % 2:
            ops.append({**common,"field":"start_time","value":"10:00","bound_date":day})
        service.create_event({"institution_id":inst["id"],"season_id":season["id"],"title":"졸업식",
            "event_type":"graduation_ceremony","audience":None,"campus_label":None,
            "allow_separate_event":False,"operations":ops})
    job = jobs.create({"kind":"schedule_scan","season_id":season["id"],"mode":"recheck",
                      "selection":{"scope":"ids","institution_ids":ids},"limits":None},str(uuid4()))
    jobs.pause(job["id"])
    return season, ids


def main():
    """한 lease 안에서 화면·오프라인 동작을 검증하고 정확히 정리한다."""
    out = ROOT / "runtime/improvements-20261020" / datetime.now().strftime("browser-%H%M%S")
    out.mkdir(parents=True)
    clone = out / "data/live/graduation.sqlite3"
    clone.parent.mkdir(parents=True)
    with sqlite3.connect((ROOT/"data/live/graduation.sqlite3").as_uri()+"?mode=ro",uri=True) as source:
        with sqlite3.connect(clone) as target:
            source.backup(target)
    settings = load_settings(dotenv_path=out/"absent.env",process_values={
        "APP_DATA_DIR":str(out/"data"),"AUTO_OPEN_BROWSER":"false","SEARCH_PROVIDER":"none"})
    app = create_app(settings, runner_factory=IsolatedRunner)
    runtime = app.state.runtime
    live_season = "ba3daa8c-f911-4aae-ae89-f454a946d72a"
    clean_html = render_export(runtime.service.snapshot(live_season))
    (out/"source-result.html").write_text(clean_html)
    fixture, ids = fixture_season(runtime.service, runtime.jobs)
    fixture_html = render_export(runtime.service.snapshot(fixture["id"]))
    (out/"fixture-result.html").write_text(fixture_html)
    sock = bind_server("127.0.0.1", 0)
    server = uvicorn.Server(uvicorn.Config(app,log_level="error",access_log=False))
    runtime.shutdown_callback = lambda: setattr(server, "should_exit", True)
    thread = Thread(target=lambda:server.run(sockets=[sock]),daemon=True)
    thread.start()
    evidence = {"scope":"원본 DB 복사본의 실제 앱 UI + 별도 가상 45기관의 페이지 경계. 외부 제공자 호출 없음.",
                "operations":[],"assertions":{}}
    lease = None

    def invoke(arguments, *, optional=False):
        """모든 브라우저 조작을 같은 helper와 lease에서 실행한다."""
        process = subprocess.run([HELPER,*arguments],cwd=out,text=True,capture_output=True,timeout=55)
        info = {"command":arguments[:5],"returncode":process.returncode,"stdout":process.stdout,"stderr":process.stderr}
        # data URL의 HTML 전체는 별도 파일에 보존하며 로그에 다시 복제하지 않는다.
        if any(a.startswith("data:") for a in arguments):
            info["command"] = ["run","goto","standalone HTML bytes"]
        evidence["operations"].append(info)
        if process.returncode and not optional:
            raise RuntimeError(process.stderr or process.stdout[:700])
        return process

    def evaluate(code):
        """브라우저 DOM과 실제 이벤트 처리 후 관찰값을 읽는다."""
        response = invoke(["run","--lease",lease,"--","eval",code])
        outer = json.loads(response.stdout)
        raw = outer["stdout"]
        if "### Result\n" not in raw:
            raise RuntimeError(raw[:1500])
        value = raw.split("### Result\n",1)[1].split("\n###",1)[0].strip()
        for _ in range(3):
            if isinstance(value,str):
                try:
                    value = json.loads(value)
                except ValueError:
                    break
        return value

    def shot(name):
        """검사 시점의 전체 화면을 task 소유 경로에 보존한다."""
        invoke(["run","--lease",lease,"--","screenshot",f"--filename={out/name}"])

    def check(key, condition, actual):
        """실패 관찰도 기록하고 기준을 낮추지 않고 중단한다."""
        evidence["assertions"][key] = {"passed":bool(condition),"actual":actual}
        if not condition:
            raise AssertionError(f"{key}: {actual}")

    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.05)
        assert server.started
        print("browser: starting dedicated lease",flush=True)
        started = invoke(["start","--url",f"http://127.0.0.1:{sock.getsockname()[1]}/","--deadline","600"])
        lease = json.loads(started.stdout)["lease"]
        invoke(["run","--lease",lease,"--","resize","1440","1050"])
        initial = evaluate("""async () => {await new Promise(r=>setTimeout(r,900));return {
          rows:document.querySelectorAll('#results tbody tr').length,
          headers:[...document.querySelectorAll('#results th')].map(n=>n.textContent),
          body:document.body.innerText, links:[...document.querySelectorAll('#results tbody a')].map(a=>a.href),
          dates:[...document.querySelectorAll('#results tbody tr')].filter(r=>r.dataset.date).length
        };}""")
        check("actual_stored_results", initial["rows"]==17 and initial["dates"]==10, {k:initial[k] for k in ["rows","headers","dates"]})
        check("no_technical_main_ui",not any(x in initial["body"] for x in ["판매 준비 가능","일반 일정표","HTTP","토큰","revision","검증 상태"]),True)
        check("school_links",len(initial["links"])==16 and not any("open.neis.go.kr" in u or "://none" in u for u in initial["links"]),initial["links"])
        shot("app-desktop.png")
        before = runtime.service.list_institutions(q="건국초등학교")["items"][0]
        original_events = [e for e in runtime.service.events(live_season)["items"] if e["institution"]["id"]==before["id"]]
        evaluate("""() => {document.querySelector('#record-text').value='건국초 졸업식은 1월 6일 오전 10시, 학교 강당이래. 방금 학교에 전화해서 확인했어. 기록해줘.';document.querySelector('#record-form button[type=submit]').click();return true;}""")
        saved = evaluate("""async () => {await new Promise(r=>setTimeout(r,900));return {reply:document.querySelector('#conversation').innerText,rows:[...document.querySelectorAll('#results tbody tr')].filter(r=>r.dataset.name==='건국초등학교').map(r=>r.innerText)};}""")
        after_events = [e for e in runtime.service.events(live_season)["items"] if e["institution"]["id"]==before["id"]]
        check("natural_existing_update",len(after_events)==len(original_events)==1 and after_events[0]["id"]==original_events[0]["id"] and after_events[0]["publication"]["calendar_time"]=="10:00",saved)
        check("address_not_venue","학교 강당" not in saved["rows"][0],saved["rows"])
        evaluate("""() => {document.querySelector('#save-html').click();return true;}""")
        download = evaluate("""async () => {await new Promise(r=>setTimeout(r,800));return document.querySelector('#message').textContent;}""")
        check("html_save_control","HTML 파일을 저장" in download,download)
        invoke(["run","--lease",lease,"--","resize","390","844"])
        mobile = evaluate("() => ({width:innerWidth,scroll:document.documentElement.scrollWidth,table:document.querySelector('#results table').getBoundingClientRect().width,overflow:[...document.querySelectorAll('body *')].filter(n=>n.getBoundingClientRect().right>innerWidth).map(n=>({tag:n.tagName,id:n.id,right:n.getBoundingClientRect().right}))})")
        shot("app-mobile.png")
        check("app_mobile_no_overflow",mobile["width"]>=mobile["scroll"],mobile)
        print("browser: checking combined filters and paging",flush=True)
        evaluate("() => {const s=document.querySelector('#season-select');s.value="+json.dumps(fixture["id"])+";s.dispatchEvent(new Event('change'));return true;}")
        count = evaluate("""async () => {await new Promise(r=>setTimeout(r,850));return {
          all:document.querySelectorAll('#results tbody tr').length,visible:document.querySelectorAll('#results tbody tr:not([hidden])').length};}""")
        check("app_20_per_page",count=={"all":45,"visible":20},count)
        paging = evaluate("""() => {const root=document.querySelector('#results'), get=id=>root.querySelector(`[data-control="${id}"]`);
          get('next').click();const second=get('page').textContent;
          get('query').value='화면검사';get('query').dispatchEvent(new Event('input'));
          get('from-date').value='2027-01-07';get('from-date').dispatchEvent(new Event('input'));
          const first={page:get('page').textContent,count:get('count').textContent,visible:root.querySelectorAll('tbody tr:not([hidden])').length};
          get('next').click();return {second,first,last:{page:get('page').textContent,visible:root.querySelectorAll('tbody tr:not([hidden])').length}};}""")
        check("combined_filter_then_page",paging["first"]["visible"]==20 and paging["last"]["visible"]==5 and paging["first"]["page"].startswith("1 /"),paging)
        empty = evaluate("""() => {const q=document.querySelector('#results [data-control=query]');q.value='없는학교';q.dispatchEvent(new Event('input'));return {visible:document.querySelectorAll('#results tbody tr:not([hidden])').length,empty:document.querySelector('#results [data-control=empty]').hidden,page:document.querySelector('#results [data-control=page]').textContent};}""")
        check("empty_result",empty["visible"]==0 and not empty["empty"],empty)
        # 조사 버튼은 가상 runner에서만 확인한다. 네트워크 정확성 검사와 구분한다.
        evaluate("""() => {const q=document.querySelector('#school-query');q.value='화면검사01';q.dispatchEvent(new Event('input'));return true;}""")
        requested = evaluate("""async () => {await new Promise(r=>setTimeout(r,350));document.querySelector('#school-options button').click();document.querySelector('#research-submit').click();await new Promise(r=>setTimeout(r,700));return document.querySelector('#job-message').textContent;}""")
        check("research_request_ui","마쳤" in requested or "확인하고" in requested,requested)
        print("browser: stopping app before standalone HTML",flush=True)
        server.should_exit=True
        thread.join(timeout=15)
        assert not thread.is_alive()
        # file:// 제한을 우회하지 않는다. 동일 바이트 data 문서는 도구가 허용하는 독립 페이지다.
        file_result = invoke(["run","--lease",lease,"--","goto",(out/"source-result.html").as_uri()],optional=True)
        evidence["file_navigation"] = {"returncode":file_result.returncode,"stdout":file_result.stdout[-1800:]}
        invoke(["run","--lease",lease,"--","goto","data:text/html;base64,"+base64.b64encode(fixture_html.encode()).decode()])
        offline = evaluate("""() => {
          const root=document.querySelector('[data-schedule]'),get=id=>root.querySelector(`[data-control="${id}"]`);
          const initial=root.querySelectorAll('tbody tr:not([hidden])').length;
          get('query').value='화면검사';get('query').dispatchEvent(new Event('input'));
          get('date-mode').value='range';get('date-mode').dispatchEvent(new Event('input'));
          get('from-date').value='2027-01-07';get('from-date').dispatchEvent(new Event('input'));
          get('to-date').value='2027-01-08';get('to-date').dispatchEvent(new Event('input'));
          get('next').click();get('next').click();
          return {initial,count:get('count').textContent,page:get('page').textContent,last:root.querySelectorAll('tbody tr:not([hidden])').length,
          external:performance.getEntriesByType('resource').filter(r=>/^https?:/.test(r.name)).length,width:innerWidth,scroll:document.documentElement.scrollWidth};
        }""")
        check("standalone_range_and_pages",offline["initial"]==20 and offline["last"]==3 and offline["page"].startswith("3 /") and offline["external"]==0,offline)
        check("html_mobile_no_overflow",offline["width"]>=offline["scroll"],offline)
        shot("html-fixture-mobile.png")
        invoke(["run","--lease",lease,"--","goto","data:text/html;base64,"+base64.b64encode(clean_html.encode()).decode()])
        invoke(["run","--lease",lease,"--","resize","1440","1050"])
        shot("html-source-desktop.png")
        standalone = evaluate("""() => ({rows:document.querySelectorAll('tbody tr').length,
          links:document.querySelectorAll('tbody a').length,external:performance.getEntriesByType('resource').filter(r=>/^https?:/.test(r.name)).length,
          columns:[...document.querySelectorAll('th')].map(n=>n.textContent)})""")
        check("stored_actual_html",standalone["rows"]==17 and standalone["links"]==16 and standalone["external"]==0,standalone)
        invoke(["run","--lease",lease,"--","resize","390","844"])
        shot("html-source-mobile.png")
        evidence["status"]="passed"
    finally:
        if lease:
            finished = subprocess.run([HELPER,"finish","--lease",lease],cwd=out,text=True,capture_output=True,timeout=55)
            cleanup = json.loads(finished.stdout)
            evidence["cleanup"]={k:cleanup.get(k) for k in ("state","cleanupMode","finalResidualCount")}
            assert cleanup["state"]=="FINISHED" and cleanup["finalResidualCount"]==0
        server.should_exit=True
        thread.join(timeout=15)
        sock.close()
        evidence["app_stopped"]=not thread.is_alive()
        (out/"result.json").write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+"\n")
        print(json.dumps({"status":evidence.get("status"),"out":str(out),"cleanup":evidence.get("cleanup")},ensure_ascii=False),flush=True)


if __name__ == "__main__":
    main()
