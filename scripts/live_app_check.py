"""로컬 실제 앱 API를 통해 조사·조회·HTML 결과 증빙을 저장한다.

네트워크를 수행하므로 일반 pytest 모음에 포함하지 않는다.
API 키와 로컬 실행 토큰은 출력하거나 결과에 저장하지 않는다.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from uuid import uuid4

import httpx


def run(url: str, action: str, output: Path, names: list[str], job_id: str | None = None) -> None:
    """앱의 공개 API를 호출해 실제 결과와 원문 연결을 저장한다.

    Args:
        url: 이미 실행 중인 loopback 앱 주소.
        action: registry, scan 또는 report.
        output: 프로젝트 내부 증빙 JSON 경로.
        names: 조사할 등록 기관의 정확한 이름; 날짜와 정답 URL은 받지 않는다.
        job_id: 기존 작업을 마지막 상태까지 관찰해 보고할 선택 식별자.
    """
    client = httpx.Client(base_url=url, timeout=30, trust_env=False)
    bootstrap = client.get("/api/bootstrap").raise_for_status().json()
    token = bootstrap["token"]
    season_id = bootstrap["selected_season_id"]
    institutions = []
    cursor = None
    while True:
        page = client.get("/api/institutions", params={"limit": 500, **({"cursor": cursor} if cursor else {})}).raise_for_status().json()
        institutions.extend(page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    job = client.get(f"/api/jobs/{job_id}").raise_for_status().json() if job_id else None
    if job is None and action in {"registry", "scan"}:
        if action == "registry":
            payload = {"kind": "registry_sync", "providers": ["neis"], "limits": None}
        else:
            selected = [item for item in institutions if item["name"] in names]
            if {item["name"] for item in selected} != set(names):
                raise RuntimeError("선택한 기관이 공식 목록에 모두 존재하지 않습니다.")
            payload = {"kind": "schedule_scan", "season_id": season_id, "mode": "recheck",
                       "selection": {"scope": "ids", "institution_ids": [item["id"] for item in selected]}, "limits": None}
        response = client.post("/api/jobs", headers={"X-App-Token": token, "Idempotency-Key": str(uuid4())}, json=payload)
        response.raise_for_status()
        job = response.json()
        print(json.dumps({"job_id": job["id"], "target_count": job["target_count"], "action": action}), flush=True)
    if job:
        while job["status"] in {"queued", "running", "pausing", "cancelling"}:
            time.sleep(2)
            job = client.get(f"/api/jobs/{job['id']}").raise_for_status().json()
        print(json.dumps({"status": job["status"], "counts": job["counts"], "usage": job["usage"]}), flush=True)
    events = client.get("/api/events", params={"season_id": season_id, "view": "all", "limit": 500}).raise_for_status().json()
    overview = client.get("/api/overview", params={"season_id": season_id}).raise_for_status().json()
    details = [client.get(f"/api/events/{item['id']}").raise_for_status().json() for item in events["items"]]
    result = {
        "recorded_at": datetime.now(timezone.utc).isoformat(), "evidence_level": "live_app_api",
        "app_mode": bootstrap["status"]["app_mode"], "discovery_mode": "blind_window",
        "input_institution_names": names, "seeded_date": False, "seeded_notice_url": False,
        "job": job, "items": client.get(f"/api/jobs/{job['id']}/items", params={"limit": 500}).raise_for_status().json() if job else None,
        "overview": overview, "events": events,
        "details": details,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="실제 앱 종단 검증")
    parser.add_argument("action", choices=["registry", "scan", "report"])
    parser.add_argument("--url", default="http://127.0.0.1:8600")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--names", nargs="*", default=[])
    parser.add_argument("--job-id")
    args = parser.parse_args()
    run(args.url, args.action, args.output, args.names, args.job_id)
