"""서버 검토 명부를 실제 앱의 미리보기·적재 API로 가져온다."""

import json
from pathlib import Path
from uuid import uuid4

import httpx


def main():
    """중복은 자동 병합하지 않고 검토할 수 있도록 중단한다."""
    results = []
    with httpx.Client(base_url="http://127.0.0.1:8600", trust_env=False, timeout=60) as client:
        token = client.get("/api/bootstrap").raise_for_status().json()["token"]
        files = client.get("/api/registry/official-files").raise_for_status().json()["items"]
        for entry in files:
            payload = client.get("/api/registry/official-files/" + entry["filename"]).raise_for_status().json()
            headers = {"X-App-Token": token, "Idempotency-Key": str(uuid4())}
            preview = client.post("/api/registry/imports/preview", headers=headers, json=payload).raise_for_status().json()
            if preview["duplicate_candidates"] or preview["rejected_rows"]:
                print(json.dumps({"filename": entry["filename"], "preview": preview}, ensure_ascii=False))
                raise RuntimeError("중복·거절 기관을 화면에서 검토해야 합니다.")
            headers["Idempotency-Key"] = str(uuid4())
            committed = client.post(f"/api/registry/imports/{preview['id']}/commit", headers=headers, json={
                "expected_preview_hash": preview["preview_hash"], "expected_content_revision": preview["content_revision"],
                "allow_partial": False, "mappings": [],
            }).raise_for_status().json()
            results.append({"filename": entry["filename"], "snapshot": committed, "provenance": entry["provenance"]})
            print(json.dumps({"filename": entry["filename"], "loaded": committed["loaded_count"],
                              "complete": committed["source"]["coverage_complete"]}, ensure_ascii=False), flush=True)
    output = Path(__file__).resolve().parents[1] / "runtime/live-app/official-file-imports.json"
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
