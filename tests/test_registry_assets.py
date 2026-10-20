"""공식 파일 검토 경계가 실제 API와 SQLite 채널 연결에도 적용되는지 검사한다."""

from copy import deepcopy
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api import create_app
from app.registry_assets import matching_authorities, official_bundle
from test_jobs_api import settings_for


def test_modified_payload_cannot_self_assert_official_homepage():
    """발행자 이름과 원문 해시가 같아도 행을 바꾸면 공식 승인이 없다."""
    bundle = official_bundle("universities-registry.json")
    assert len(matching_authorities(bundle.registry)) == 18
    changed = deepcopy(bundle.registry)
    changed["rows"][0]["homepage_url"] = "https://attacker.example.org"
    assert matching_authorities(changed) == ()


def test_prepared_file_preview_commit_attaches_verified_relationship(tmp_path):
    """준비된 파일도 일반 preview·commit을 거쳐 원문에 연결한 채널만 저장한다."""
    with TestClient(create_app(settings_for(tmp_path)), base_url="http://127.0.0.1") as client:
        token = client.get("/api/bootstrap").json()["token"]
        def post(path, payload):
            response = client.post(path, json=payload, headers={
                "X-App-Token": token, "Idempotency-Key": str(uuid4()),
            })
            assert response.is_success, response.text
            return response.json()
        available = client.get("/api/registry/official-files")
        assert available.status_code == 200 and len(available.json()["items"]) == 4
        source = client.get("/api/registry/official-files/universities-registry.json").json()
        preview = post("/api/registry/imports/preview", source)
        result = post(f"/api/registry/imports/{preview['id']}/commit", {
            "expected_preview_hash": preview["preview_hash"],
            "expected_content_revision": preview["content_revision"], "allow_partial": False, "mappings": [],
        })
        assert result["loaded_count"] == 18 and not result["source"]["coverage_complete"]
        universities = client.get("/api/institutions").json()["items"]
        assert all(len(row["official_channels"]) == 1 for row in universities)
        assert all(row["official_channels"][0]["url"] == row["homepage_url"] for row in universities)
        assert client.get("/api/registry/official-files/unapproved.json").status_code == 404
