"""실제 근거에서 만든 두 기간 HTML을 서버 없이 전용 브라우저로 검증한다."""

import base64
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
HELPER = "$HOME/.agents/skills/browser-verification/scripts/playwright-session.mjs"
CASES = [
    {"key": "past", "path": "output/광주 졸업식 일정표 (2025.12.01~2026.03.15).html",
     "schools": [{"name": "첨단고등학교", "date": "2026-01-21", "count": "355", "district": "광산구",
                  "home": "http://cheomdan.gen.hs.kr/"}]},
    {"key": "existing", "path": "output/광주 졸업식 일정표 (2026.12.01~2027.03.31).html",
     "schools": [{"name": "건국초등학교", "date": "2027-01-06", "count": "188", "district": "북구",
                  "home": "http://geonkuk.gen.es.kr/"},
                 {"name": "광주대학교", "date": "2027-02-19", "count": "", "district": "남구",
                  "home": "https://www.gwangju.ac.kr/"}]},
]


def main():
    """한 lease에서 두 파일 원 바이트를 읽고 별도 서버 없이 조회 기능을 검사한다."""
    out = ROOT / "runtime/period-ui-20261021" / datetime.now().strftime("final-html-%H%M%S")
    out.mkdir(parents=True)
    report = {"sources": {}, "server_started": False, "new_external_collection": False,
              "original_user_data_written": False, "assertions": {}, "operations": [], "screenshots": [],
              "navigation": "파일과 동일한 바이트의 data URL. OS file:// 더블클릭 검증은 아님."}
    lease = None

    def invoke(arguments):
        """전용 helper의 같은 lease에 명령을 전달하고 원 출력을 보관한다.

        Args:
            arguments: helper 명령 인자.
        Returns:
            명령 실행 결과.
        Raises:
            RuntimeError: 전용 브라우저 명령이 실패한 경우.
        """
        result = subprocess.run([HELPER, *arguments], cwd=out, text=True, capture_output=True, timeout=55)
        report["operations"].append({"command": ["HTML data URL" if value.startswith("data:") else value for value in arguments],
            "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout[:1800])
        return result

    def evaluate(code):
        """실제 브라우저의 DOM 관찰값을 JSON으로 읽는다.

        Args:
            code: 실행할 DOM 검사 함수.
        Returns:
            페이지가 반환한 값.
        """
        raw = json.loads(invoke(["run", "--lease", lease, "--", "eval", code]).stdout)["stdout"]
        value = raw.split("### Result\n", 1)[1].split("\n###", 1)[0].strip()
        for _ in range(3):
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    break
        return value

    def check(name, passed, actual):
        """실제 관찰값과 판정을 보존한다.

        Args:
            name: 검사 이름.
            passed: 요구 만족 여부.
            actual: 원 관찰값.
        Raises:
            AssertionError: 요구를 만족하지 못한 경우.
        """
        report["assertions"][name] = {"passed": bool(passed), "actual": actual}
        if not passed:
            raise AssertionError(f"{name}: {actual}")
        print("period-html: " + name, flush=True)

    def filter_rows(school, district):
        """독립 세 입력의 실제 이벤트로 필터를 적용한다.

        Args:
            school: 이름과 날짜를 가진 기대 학교.
            district: 양성 또는 반례 구 조건.
        Returns:
            필터 후 표시 행과 페이지.
        """
        values = {"query": school["name"], "date": school["date"], "district": district}
        return evaluate("() => {const values=" + json.dumps(values, ensure_ascii=False) + ";const get=k=>document.querySelector(`[data-control=\"${k}\"]`);"
            "for(const [key,value] of Object.entries(values)){get(key).value=value;get(key).dispatchEvent(new Event('input'));}"
            "return {rows:[...document.querySelectorAll('tbody tr:not([hidden])')].map(r=>r.dataset.name),count:get('count').textContent,page:get('page').textContent};}")

    try:
        lease = json.loads(invoke(["start", "--url", "about:blank", "--deadline", "350"]).stdout)["lease"]
        invoke(["run", "--lease", lease, "--", "resize", "1440", "1050"])
        for case in CASES:
            source = ROOT / case["path"]
            content = source.read_bytes()
            report["sources"][case["key"]] = {"path": str(source), "sha256": hashlib.sha256(content).hexdigest(),
                                             "identical_bytes_executed": True}
            invoke(["run", "--lease", lease, "--", "goto", "data:text/html;base64," + base64.b64encode(content).decode()])
            actual = evaluate("""() => {const rows=[...document.querySelectorAll('tbody tr')];return {
              rows:rows.length,headers:[...document.querySelectorAll('th')].map(n=>n.textContent),
              version:document.querySelector('meta[name="schedule-presentation"]')?.content,
              external:performance.getEntriesByType('resource').filter(r=>/^https?:/.test(r.name)).length,
              buttons:[...document.querySelectorAll('button')].map(n=>n.textContent),forms:document.forms.length,
              scripts:[...document.scripts].map(s=>s.src||s.textContent),
              schools:rows.map(r=>({name:r.dataset.name,cells:[...r.cells].map(c=>c.textContent.trim()),href:r.querySelector('a')?.href}))};}""")
            key = case["key"]
            check(key + "_339_five_columns_v3", actual["rows"] == 339 and actual["version"] == "3.0" and
                actual["headers"] == ["날짜", "학교명", "시간", "졸업인원수", "장소"],
                {name: actual[name] for name in ["rows", "version", "headers"]})
            check(key + "_no_server_functions_or_external_requests", actual["external"] == 0 and actual["forms"] == 0 and
                not any(word in " ".join(actual["buttons"]) for word in ["조사", "찾기", "기록"]) and
                not any(word in " ".join(actual["scripts"]) for word in ["fetch(", "/api/", "XMLHttpRequest", "WebSocket"]),
                {name: actual[name] for name in ["external", "forms", "buttons"]})
            for school in case["schools"]:
                found = [row for row in actual["schools"] if row["name"] == school["name"]]
                check(key + "_" + school["name"] + "_verified_values_homepage", len(found) == 1 and
                    found[0]["cells"][0] == school["date"] and found[0]["cells"][2:4] == ["", school["count"]] and
                    found[0]["href"] == school["home"], found)
                positive = filter_rows(school, school["district"])
                negative = filter_rows(school, "동구" if school["district"] != "동구" else "서구")
                restored = filter_rows(school, school["district"])
                check(key + "_" + school["name"] + "_and_filter_empty_reset", positive["rows"] == [school["name"]] and
                    negative["rows"] == [] and restored["rows"] == positive["rows"] and
                    restored["page"] == "1 / 1 페이지", {"positive": positive, "negative": negative, "restored": restored})
            shot = out / (key + "-filtered-desktop.png")
            invoke(["run", "--lease", lease, "--", "screenshot", f"--filename={shot}"])
            report["screenshots"].append(str(shot))
            paging = evaluate("""() => {const get=k=>document.querySelector(`[data-control="${k}"]`);get('reset').click();
              const count=()=>document.querySelectorAll('tbody tr:not([hidden])').length;const first=count();get('next').click();const second=count();
              let turns=0;while(!get('next').disabled&&turns++<25)get('next').click();return {first,second,last:count(),page:get('page').textContent,count:get('count').textContent};}""")
            check(key + "_offline_20_row_pagination", paging == {"first": 20, "second": 20, "last": 19,
                "page": "17 / 17 페이지", "count": "339건 / 전체 339건"}, paging)
            check(key + "_source_bytes_preserved", hashlib.sha256(source.read_bytes()).hexdigest() == report["sources"][key]["sha256"], True)
        report["status"] = "passed"
    except Exception as error:
        report["status"] = "failed"; report["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        if lease:
            result = subprocess.run([HELPER, "finish", "--lease", lease], cwd=out, text=True, capture_output=True, timeout=55)
            cleanup = json.loads(result.stdout)
            report["cleanup"] = {name: cleanup.get(name) for name in ["state", "cleanupMode", "finalResidualCount"]}
            if cleanup["state"] != "FINISHED" or cleanup["finalResidualCount"] != 0:
                report["status"] = "failed"
        (out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"status": report.get("status"), "out": str(out), "checks": len(report["assertions"]), "cleanup": report.get("cleanup")}, ensure_ascii=False), flush=True)
        assert report.get("cleanup", {}).get("state") == "FINISHED" and report.get("cleanup", {}).get("finalResidualCount") == 0


if __name__ == "__main__":
    main()
