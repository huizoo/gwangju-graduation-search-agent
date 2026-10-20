"""별도 실제 앱 프로세스로 중복 실행·포트 충돌·수기 보존·취소 복구를 검사한다.

시험 저장소는 runtime 아래에 격리한다. 기관 식별자는 실제 NEIS 목록을 사용하며
수기 기록은 확인시점 미상인 시험 메모로 저장한다. 일정 정확성 성과로 세지 않는다.
"""

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]


class ProcessApp:
    """이 검사에서 시작한 앱 프로세스와 HTTP 연결만 관리한다."""

    def __init__(self, folder: Path, port: int = 8604):
        self.folder, self.port = folder, port
        self.process = None
        self.client = None
        self.log = None

    def start(self):
        """고유 저장소의 준비 완료와 새 실행 식별자를 확인한다."""
        self.folder.mkdir(parents=True, exist_ok=True)
        self.env = {**os.environ, "APP_MODE": "live", "APP_DATA_DIR": str(self.folder),
                    "APP_PORT": str(self.port), "AUTO_OPEN_BROWSER": "false"}
        self.log = (self.folder / "launcher.log").open("a", encoding="utf-8")
        self.process = subprocess.Popen([sys.executable, "-m", "app"], cwd=ROOT, env=self.env,
                                        stdout=self.log, stderr=self.log)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("격리 앱 시작 실패")
            try:
                metadata = json.loads((self.folder / "live/instance.lock").read_text())
                client = httpx.Client(base_url=metadata["url"], timeout=15, trust_env=False)
                info = client.get("/api/instance").raise_for_status().json()
                if metadata["pid"] == self.process.pid and info["instance_id"] == metadata["instance_id"]:
                    self.client = client
                    self.url = metadata["url"]
                    self.bootstrap = self.get("/api/bootstrap")
                    self.token = self.bootstrap["token"]
                    return self
                client.close()
            except (OSError, ValueError, KeyError, httpx.HTTPError):
                pass
            time.sleep(0.1)
        raise RuntimeError("격리 앱 준비 시간 초과")

    def get(self, path, **params):
        """검사 대상 앱의 JSON 결과를 읽는다."""
        return self.client.get(path, params=params).raise_for_status().json()

    def write(self, path, body=None, method="POST"):
        """실행 토큰과 새 의도 키를 사용하며 토큰을 파일에 기록하지 않는다."""
        response = self.client.request(method, path, json=body, headers={
            "X-App-Token": self.token, "Idempotency-Key": str(uuid4()),
        })
        response.raise_for_status()
        return response.json()

    def stop(self, crash=False):
        """이 객체가 생성한 프로세스만 정상 종료하거나 명시적으로 충돌시킨다."""
        if self.process and self.process.poll() is None:
            if crash:
                self.process.kill()
            else:
                try:
                    self.write("/api/app/shutdown", {"pause_active_job": True})
                except (httpx.HTTPError, RuntimeError):
                    # 검사 실패 정리에서도 이 객체가 만든 프로세스만 종료한다.
                    self.process.terminate()
            self.process.wait(timeout=30)
        if self.client:
            self.client.close()
        if self.log:
            self.log.close()


def run():
    """실제 HTTP 경로의 상태 전이와 재시작 후 영속 상태를 기록한다."""
    folder = ROOT / "runtime/lifecycle" / uuid4().hex
    app, collision = ProcessApp(folder), None
    evidence = {"checked_at": datetime.now(timezone.utc).isoformat(),
                "evidence_level": "actual_process_http", "schedule_accuracy_test": False,
                "isolated_storage": str(folder.relative_to(ROOT)), "checks": {}}
    try:
        app.start()
        first_instance = app.get("/api/instance")
        duplicate = subprocess.run([sys.executable, "-m", "app"], cwd=ROOT, env=app.env,
                                   capture_output=True, text=True, timeout=10)
        assert duplicate.returncode == 0 and app.get("/api/instance") == first_instance
        evidence["checks"]["duplicate_reuses_existing_instance"] = True

        collision = ProcessApp(folder / "port-collision", port=int(app.url.rsplit(":", 1)[1])).start()
        assert collision.url != app.url
        collision.stop()
        evidence["checks"]["occupied_port_uses_new_loopback_port"] = True

        payload = json.loads((ROOT / "runtime/provider-live/neis-registry.json").read_text())
        payload["rows"] = [row for row in payload["rows"] if row["name"] == "첨단고등학교"]
        assert len(payload["rows"]) == 1
        payload["source"].update(coverage_complete=False, expected_total=None,
                                 completeness_note="실제 공식 목록 중 프로세스 수명 검사용 기관 1곳")
        preview = app.write("/api/registry/imports/preview", payload)
        app.write(f"/api/registry/imports/{preview['id']}/commit", {
            "expected_preview_hash": preview["preview_hash"],
            "expected_content_revision": preview["content_revision"], "allow_partial": False, "mappings": [],
        })
        institution = app.get("/api/institutions")["items"][0]
        season = app.bootstrap["selected_season_id"]
        event_payload = json.loads((ROOT / "reference/handoff/contracts/examples/event-create.example.json").read_text())
        event_payload.update(institution_id=institution["id"], season_id=season,
                             title="수기 보존 검사 기록", audience="프로세스 검증", allow_separate_event=True)
        event_payload["operations"] = [dict(event_payload["operations"][1],
            value={"precision": "day", "date": "2027-01-20", "month": None},
            confirmed_at=None, note="프로세스 수명 검사. 실제 전화 확인을 주장하지 않는 시점 미상 메모.")]
        manual = app.write("/api/events", event_payload)
        original = deepcopy(manual["fields"]["event_date"]["manual"])
        assert not manual["publication"]["current_date_allowed"]

        job = app.write("/api/jobs", {"kind": "schedule_scan", "season_id": season, "mode": "recheck",
            "selection": {"scope": "ids", "institution_ids": [institution["id"]]}, "limits": None})
        app.write(f"/api/jobs/{job['id']}/pause")
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            paused = app.get(f"/api/jobs/{job['id']}")
            if paused["status"] == "paused":
                break
            time.sleep(0.2)
        assert paused["status"] == "paused"
        before_usage = paused["usage"]
        app.stop()
        app.start()
        restored = app.get(f"/api/jobs/{job['id']}")
        assert restored["status"] == "paused" and restored["usage"] == before_usage
        assert restored["scope_snapshot"] == job["scope_snapshot"]
        evidence["checks"]["pause_restart_preserves_queue_and_usage"] = True
        assert app.get(f"/api/events/{manual['id']}")["event"]["fields"]["event_date"]["manual"] == original
        evidence["checks"]["manual_unknown_time_record_survives_restart"] = True

        app.write(f"/api/jobs/{job['id']}/resume", {"expected_revision": restored["revision"], "increase_limits": None})
        time.sleep(0.5)
        cancelling = app.write(f"/api/jobs/{job['id']}/cancel")
        assert cancelling["status"] in {"cancelling", "cancelled"}
        app.stop(crash=True)
        app.start()
        cancelled = app.get(f"/api/jobs/{job['id']}")
        assert cancelled["status"] == "cancelled"
        assert all(value >= cancelling["usage"][key] for key, value in cancelled["usage"].items())
        refused = app.client.post(f"/api/jobs/{job['id']}/resume", headers={
            "X-App-Token": app.token, "Idempotency-Key": str(uuid4())},
            json={"expected_revision": cancelled["revision"], "increase_limits": None})
        assert refused.status_code == 409
        assert app.get(f"/api/events/{manual['id']}")["event"]["fields"]["event_date"]["manual"] == original
        evidence["checks"].update(cancel_crash_recovers_cancelled=True, cancelled_resume_refused=True,
                                   manual_record_survives_research_and_crash=True)
        evidence.update(status="passed", before_crash_status=cancelling["status"],
                        after_crash_status=cancelled["status"], usage_after_crash=cancelled["usage"])
    finally:
        app.stop()
        if collision:
            collision.stop()
        output = ROOT / "docs/evidence/process-lifecycle.json"
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False))


if __name__ == "__main__":
    run()
