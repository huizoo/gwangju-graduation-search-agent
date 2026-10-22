"""사용자 파일명이 실제 저장·API·브라우저 응답에서 일치하는지 검사한다."""

from urllib.parse import unquote

from fastapi.testclient import TestClient
import pytest

from app.web.api import create_app
from app.web.exporting import schedule_filename
from tests.test_jobs_api import settings_for


@pytest.mark.parametrize("start,end,expected", [
    ("2025-12-01", "2026-03-31", "광주 졸업식 일정표 (2025.12.01~2026.03.31).html"),
    ("2024-02-29", "2024-08-28", "광주 졸업식 일정표 (2024.02.29~2024.08.28).html"),
])
def test_saved_file_download_metadata_and_http_name_agree(tmp_path, start, end, expected):
    """자동 저장과 직접 내보내기가 같은 한국어 파일명을 사용한다."""
    app = create_app(settings_for(tmp_path))
    runtime = app.state.runtime
    season = runtime.service.create_season({"search_start": start, "search_end": end})
    assert runtime._refresh_exports(force=True)
    latest = runtime.data_dir / "exports" / expected
    assert latest.is_file()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        token = client.get("/api/bootstrap").json()["token"]
        response = client.post("/api/exports/html", headers={"X-App-Token": token}, json={
            "season_id": season["id"], "include_notes": False, "publication_policy_version": "4.0"})
        assert response.status_code == 202, response.text
        assert response.json()["filename"] == expected
        html = client.get(response.json()["url"])
        header = html.headers["content-disposition"]
        assert unquote(header.split("filename*=utf-8''", 1)[1]) == expected
        assert html.content == latest.read_bytes()
        with runtime.store.transaction() as conn:
            version = conn.execute("SELECT file_ref FROM exports WHERE season_id=?", (season["id"],)).fetchone()[0]
        from pathlib import Path
        assert Path(version).name == expected and Path(version).parent.parent.name == "versions"
    assert not list((runtime.data_dir / "exports").glob("graduation-schedule-*.html"))


@pytest.mark.parametrize("invalid", ["2025-02-30", "../2025-12-01"])
def test_filename_rejects_invalid_dates_instead_of_using_path_text(invalid):
    """날짜가 아닌 입력을 사용자 파일 경로로 조합하지 않는다."""
    with pytest.raises(ValueError):
        schedule_filename({"search_start": invalid, "search_end": "2026-03-31"})
