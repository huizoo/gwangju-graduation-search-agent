"""최종 저장 HTML의 실제 통합 정보와 필터를 전용 브라우저에서 확인한다."""

import base64
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
HELPER = "$HOME/.agents/skills/browser-verification/scripts/playwright-session.mjs"


def main():
    """서버 없이 최종 파일 바이트를 검증하고 브라우저 소유 자원을 정리한다."""
    source = ROOT / "output/광주 졸업식 일정표 (2026.12.01~2027.03.31).html"
    content = source.read_bytes()
    out = ROOT / "runtime/improvements-20261021" / datetime.now().strftime("final-html-browser-%H%M%S")
    out.mkdir(parents=True)
    evidence = {
        "source": str(source),
        "source_sha256": hashlib.sha256(content).hexdigest(),
        "original_user_data_written": False,
        "navigation": "같은 HTML 바이트의 data URL. 전용 CLI의 file:// 차단은 앞선 검증에서 확인함.",
        "server_started": False,
        "assertions": {},
        "operations": [],
    }
    lease = None

    def invoke(arguments):
        """동일 helper lease로 명령을 보내고 관찰 결과를 보존한다.

        Args:
            arguments: helper에 전달할 명령 인자.
        Returns:
            완료된 프로세스 결과.
        Raises:
            RuntimeError: 브라우저 명령이 실패한 경우.
        """
        process = subprocess.run([HELPER, *arguments], cwd=out, text=True, capture_output=True, timeout=55)
        # 명령 인자의 긴 HTML은 접고 원본 경로·해시와 도구 응답을 보존한다.
        display = ["HTML data URL" if value.startswith("data:") else value for value in arguments]
        evidence["operations"].append({"command": display, "returncode": process.returncode,
                                       "stdout": process.stdout, "stderr": process.stderr})
        if process.returncode:
            raise RuntimeError(process.stderr or process.stdout[:1500])
        return process

    def evaluate(code):
        """실제 페이지 실행 결과에서 JSON 관찰값을 추출한다.

        Args:
            code: DOM을 읽거나 필터 이벤트를 실행할 함수.
        Returns:
            브라우저에서 반환한 관찰값.
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
        """관찰값과 판정을 보존하고 실패를 즉시 드러낸다.

        Args:
            name: 검사 이름.
            passed: 요구 조건 만족 여부.
            actual: 브라우저에서 읽은 실제 값.
        Raises:
            AssertionError: 요구 조건을 만족하지 못한 경우.
        """
        evidence["assertions"][name] = {"passed": bool(passed), "actual": actual}
        if not passed:
            raise AssertionError(f"{name}: {actual}")

    try:
        started = invoke(["start", "--url", "about:blank", "--deadline", "180"])
        lease = json.loads(started.stdout)["lease"]
        invoke(["run", "--lease", lease, "--", "goto",
                "data:text/html;base64," + base64.b64encode(content).decode()])
        invoke(["run", "--lease", lease, "--", "resize", "1440", "1050"])
        observed = evaluate("""() => {
          const rows=[...document.querySelectorAll('tbody tr')];
          const read=name=>rows.filter(r=>r.dataset.name===name).map(r=>({
            cells:[...r.cells].map(c=>c.textContent.trim()),href:r.querySelector('a')?.href,
            dates:r.dataset.dates}));
          return {rows:rows.length,headers:[...document.querySelectorAll('th')].map(n=>n.textContent),
            school:read('건국초등학교'),university:read('광주대학교'),
            external:performance.getEntriesByType('resource').filter(r=>/^https?:/.test(r.name)).length};
        }""")
        check("final_339_offline_columns", observed["rows"] == 339 and observed["external"] == 0
              and observed["headers"] == ["날짜", "학교명", "시간", "졸업인원수", "장소"],
              {key: observed[key] for key in ["rows", "headers", "external"]})
        school = observed["school"]
        check("geonkuk_date_count_homepage", len(school) == 1 and school[0]["cells"][0] == "2027-01-06"
              and school[0]["cells"][2] == "" and school[0]["cells"][3] == "188"
              and school[0]["href"] == "http://geonkuk.gen.es.kr/", school)
        university = observed["university"]
        check("gwangju_university_date_unknown_time_count_homepage", len(university) == 1
              and university[0]["cells"][0] == "2027-02-19"
              and university[0]["cells"][2:4] == ["", ""]
              and university[0]["href"] == "https://www.gwangju.ac.kr/", university)
        filtered = evaluate("""() => {
          const get=k=>document.querySelector(`[data-control="${k}"]`);
          for(const [key,value] of Object.entries({query:'광주대학교',date:'2027-02-19'})){
            get(key).value=value;get(key).dispatchEvent(new Event('input'));
          }
          return {count:get('count').textContent,page:get('page').textContent,
            visible:[...document.querySelectorAll('tbody tr:not([hidden])')].map(r=>({
              name:r.dataset.name,cells:[...r.cells].map(c=>c.textContent.trim())}))};
        }""")
        check("university_name_date_and_filter", len(filtered["visible"]) == 1
              and filtered["visible"][0]["name"] == "광주대학교"
              and filtered["visible"][0]["cells"][0] == "2027-02-19"
              and filtered["count"] == "1건 / 전체 339건" and filtered["page"] == "1 / 1 페이지", filtered)
        screenshot = out / "final-university-filter.png"
        invoke(["run", "--lease", lease, "--", "screenshot", f"--filename={screenshot}"])
        evidence["screenshot"] = str(screenshot)
        evidence["source_bytes_executed_unchanged"] = True
        evidence["status"] = "passed"
    except Exception as error:
        evidence["status"] = "failed"
        evidence["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        if lease:
            process = subprocess.run([HELPER, "finish", "--lease", lease], cwd=out,
                                     text=True, capture_output=True, timeout=55)
            cleanup = json.loads(process.stdout)
            evidence["cleanup"] = {key: cleanup.get(key)
                                   for key in ["state", "cleanupMode", "finalResidualCount"]}
            if cleanup["state"] != "FINISHED" or cleanup["finalResidualCount"] != 0:
                evidence["status"] = "failed"
        (out / "result.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"status": evidence.get("status"), "out": str(out),
                          "cleanup": evidence.get("cleanup")}, ensure_ascii=False))
        assert evidence.get("cleanup", {}).get("state") == "FINISHED"
        assert evidence.get("cleanup", {}).get("finalResidualCount") == 0


if __name__ == "__main__":
    main()
