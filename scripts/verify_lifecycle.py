"""격리한 실제 start.command 프로세스로 기록·파일·정상 종료를 검사한다.

합성 기관과 수기 입력만 사용하며 API·모델 조사를 실행하지 않는다.
사용자 DB와 브라우저를 열지 않고 결과는 실행마다 새 runtime 경로에 보관한다.
"""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]


class ProcessApp:
    """자신이 생성한 실제 앱 프로세스와 로컬 연결만 관리한다."""

    def __init__(self, folder: Path, port: int = 8604):
        """새 저장소와 loopback 포트만 지정하며 사용자 설정 파일은 수정하지 않는다."""
        self.folder, self.port = folder, port
        self.process = self.client = self.log = None
        self.cleanups = []

    def start(self):
        """start.command가 준비한 PID·실행 ID·loopback 주소를 함께 확인한다.

        Returns:
            준비된 검사 앱.
        Raises:
            RuntimeError: 시작 실패 또는 준비 제한시간 초과.
        """
        self.folder.mkdir(parents=True, exist_ok=True)
        self.env = {
            **os.environ, "APP_MODE": "live", "APP_DATA_DIR": str(self.folder),
            "APP_HOST": "127.0.0.1", "APP_PORT": str(self.port), "AUTO_OPEN_BROWSER": "false",
        }
        self.log = (self.folder / "launcher.log").open("a", encoding="utf-8")
        self.process = subprocess.Popen(
            [str(ROOT / "start.command")], cwd=ROOT, env=self.env,
            stdout=self.log, stderr=self.log,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("격리 앱 시작 실패: launcher.log를 확인하세요.")
            client = None
            try:
                metadata = json.loads((self.folder / "live/instance.lock").read_text())
                if metadata["pid"] != self.process.pid:
                    continue
                client = httpx.Client(base_url=metadata["url"], timeout=15, trust_env=False)
                info = client.get("/api/instance").raise_for_status().json()
                if info["instance_id"] == metadata["instance_id"]:
                    self.client, self.url = client, metadata["url"]
                    self.token = self.get("/api/bootstrap")["token"]
                    return self
            except (OSError, ValueError, KeyError, httpx.HTTPError):
                pass
            if client:
                client.close()
            time.sleep(0.1)
        raise RuntimeError("격리 앱 준비 시간 초과")

    def get(self, path, **params):
        """검사 앱에서만 JSON 응답을 읽는다."""
        return self.client.get(path, params=params).raise_for_status().json()

    def write(self, path, body=None):
        """실행 토큰을 메모리에서만 사용해 새 쓰기 의도를 전달한다."""
        response = self.client.post(path, json=body, headers={
            "X-App-Token": self.token, "Idempotency-Key": str(uuid4()),
        })
        response.raise_for_status()
        return response.json()

    def stop(self, *, require_graceful=False):
        """API 정상 종료를 검사하고 실패 정리는 이 객체의 자식에만 적용한다.

        Args:
            require_graceful: 강제 정리를 검사 통과로 취급하지 않을지 여부.
        Raises:
            RuntimeError: 정상 종료 검사가 실패한 경우.
        """
        graceful, failure = True, None
        if self.process and self.process.poll() is None:
            try:
                response = self.write("/api/app/shutdown", {"pause_active_job": True})
                assert response["accepted"] and not response["paused_active_job"]
                self.process.wait(timeout=20)
                assert self.process.returncode == 0
            except (httpx.HTTPError, AssertionError, subprocess.TimeoutExpired, AttributeError) as error:
                graceful, failure = False, type(error).__name__
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        if self.client:
            self.client.close()
            self.client = None
        if self.log:
            self.log.close()
            self.log = None
        self.cleanups.append({"graceful": graceful, "failure_type": failure})
        if require_graceful and not graceful:
            raise RuntimeError("격리 앱이 정상 종료되지 않았습니다.")


def natural(app, season, text):
    """합성 문장을 일반 자연어 API에 새 기록으로 전달한다."""
    return app.write("/api/records/natural", {
        "season_id": season, "text": text, "conversation_id": None, "expected_revision": None,
    })


def run():
    """수집 정확성과 구분한 프로세스·영속 기록 검증 결과를 저장한다.

    Returns:
        검사 결과 파일의 경로.
    Raises:
        Exception: 실행·기록·백업·종료의 실제 검사가 실패한 경우.
    """
    folder = ROOT / "runtime/verification/lifecycle" / uuid4().hex
    app, collision = ProcessApp(folder / "app"), None
    evidence = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "evidence_level": "actual_start_command_process_http_with_synthetic_records",
        "schedule_accuracy_test": False, "research_jobs_requested": 0,
        "browser_opened": False, "status": "failed", "checks": {},
    }
    try:
        app.start()
        original_instance = app.get("/api/instance")
        duplicate = subprocess.run(
            [str(ROOT / "start.command")], cwd=ROOT, env=app.env,
            capture_output=True, text=True, timeout=15,
        )
        assert duplicate.returncode == 0 and "이미 실행 중" in duplicate.stdout
        assert app.get("/api/instance") == original_instance
        evidence["checks"]["duplicate_reuses_existing_process"] = True

        collision = ProcessApp(folder / "port-collision", int(app.url.rsplit(":", 1)[1])).start()
        assert collision.url != app.url
        collision.stop(require_graceful=True)
        evidence["checks"]["occupied_port_uses_new_loopback_port"] = True

        school = app.write("/api/institutions", {
            "name": "수명검증가상고등학교", "type": "high", "district": "북구",
            "address": "광주광역시 북구 합성검증로 1", "campus_label": None,
            "phone": None, "homepage_url": None,
        })
        season = app.write("/api/seasons", {"search_start": "2026-12-01", "search_end": "2027-03-31"})
        assert app.write("/api/seasons", {
            "search_start": season["search_start"], "search_end": season["search_end"],
        })["id"] == season["id"]
        app.write("/api/selected-season", {"season_id": season["id"]})
        first = natural(app, season["id"],
                        "수명검증가상고등학교 졸업식은 2027년 1월 7일이야. 방금 학교에 전화해서 확인했어.")
        assert first["status"] == "saved"
        second = natural(app, season["id"],
                         "수명검증가상고등학교 졸업식은 오전 10시야. 방금 학교에 전화해서 확인했어.")
        assert second["status"] == "saved" and second["event_id"] == first["event_id"]
        event = app.get(f"/api/events/{first['event_id']}")["event"]
        assert event["institution"]["id"] == school["id"]
        assert event["publication"]["calendar_date"] == "2027-01-07"
        assert event["publication"]["calendar_time"] == "10:00"
        assert app.get("/api/events", season_id=season["id"], view="all")["total"] == 1
        manual = deepcopy({name: event["fields"][name]["manual"] for name in ("event_date", "start_time")})
        evidence["checks"]["natural_time_updates_same_event"] = True

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            exported = app.get(f"/api/exports/{season['id']}")
            if exported["status"] == "current":
                html = app.client.get(exported["url"]).raise_for_status().text
                if "수명검증가상고등학교" in html and "10:00" in html:
                    break
            time.sleep(0.2)
        else:
            raise AssertionError("수기 변경 후 HTML 자동 저장을 확인하지 못했습니다.")
        filename = "광주 졸업식 일정표 (2026.12.01~2027.03.31).html"
        assert (app.folder / "live/exports" / filename).is_file()
        evidence["checks"]["html_automatically_saved_with_period_filename"] = True

        backup = app.write("/api/backups")
        raw = app.client.get(backup["download_url"]).raise_for_status().content
        assert raw.startswith(b"SQLite format 3\0")
        backup_path = folder / "downloaded-backup.sqlite3"
        backup_path.write_bytes(raw)
        with sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True) as conn:
            assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert not conn.execute("PRAGMA foreign_key_check").fetchall()
            assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM natural_records").fetchone()[0] == 2
            assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        evidence["checks"]["downloaded_backup_contains_records_and_passes_integrity"] = True
        evidence["backup_sha256"] = hashlib.sha256(raw).hexdigest()

        app.stop(require_graceful=True)
        app.start()
        assert app.get("/api/instance") != original_instance
        assert app.get("/api/bootstrap")["selected_season_id"] == season["id"]
        restored = app.get(f"/api/events/{first['event_id']}")["event"]
        assert {name: restored["fields"][name]["manual"] for name in manual} == manual
        assert app.get("/api/events", season_id=season["id"], view="all")["total"] == 1
        assert app.get("/api/jobs")["total"] == 0
        assert next(item for item in app.get("/api/seasons")["items"] if item["id"] == season["id"]) == season
        evidence["checks"]["restart_preserves_season_selection_event_and_manual_evidence"] = True
        app.stop(require_graceful=True)
        evidence["checks"]["api_shutdown_exits_owned_process_with_zero_status"] = True
        evidence["status"] = "passed"
    except Exception as error:
        evidence["failure_type"] = type(error).__name__
        raise
    finally:
        app.stop()
        if collision:
            collision.stop()
        evidence["owned_processes_stopped"] = all(
            not instance or not instance.process or instance.process.poll() is not None
            for instance in (app, collision)
        )
        evidence["cleanup"] = app.cleanups + (collision.cleanups if collision else [])
        folder.mkdir(parents=True, exist_ok=True)
        result = folder / "result.json"
        result.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": evidence["status"], "checks": len(evidence["checks"]),
                      "result": str(result.relative_to(ROOT))}, ensure_ascii=False))
    return result


if __name__ == "__main__":
    run()
