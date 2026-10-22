"""기존 DB 확장·중복 연결이 원문과 수기 이력을 보존하는지 검사한다."""

import json
import sqlite3
from pathlib import Path

from app.core.domain import fingerprint, new_id, utcnow
from app.records.storage import Service, Store
from test_review_regressions import source_context, save_notice


def test_additive_schema_backs_up_old_database_before_any_change(tmp_path):
    """4.0 파일을 덮어쓰지 않고 backup API로 확장 전 사본을 남긴다."""
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(Path("app/records/schema.sql").read_text())
        conn.execute("INSERT INTO app_meta VALUES('preserved_user_value','original')")
    store = Store(path)
    backups = list((tmp_path / "backups").glob("before-experience-2-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='natural_records'").fetchone()
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    with store.transaction() as conn:
        assert conn.execute("SELECT value FROM app_meta WHERE key='preserved_user_value'").fetchone()[0] == "original"
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='natural_records'").fetchone()
        assert conn.execute("SELECT value FROM app_meta WHERE key='experience_schema'").fetchone()[0] == "2"
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='count_observations'").fetchone()
    Store(path)
    assert len(list((tmp_path / "backups").glob("before-experience-2-*.sqlite3"))) == 1


def test_duplicate_link_keeps_event_and_observation_history(source_context):
    """이전 generic 행사와 과정 행사를 연결해도 UUID·원문·이전 행사 행을 삭제하지 않는다."""
    service, school, season = source_context
    saved = save_notice(source_context, "2026학년도 검증초등학교 졸업식은 2027년 1월 7일 오전 10시에 시작합니다.")
    original_id = saved["event_ids"][0]
    with service.store.transaction(True) as conn:
        canonical_id = service._insert_event(conn, {"institution_id":school["id"],"season_id":season["id"],
            "title":"졸업식","academic_year":2026,"event_type":"graduation_ceremony","audience":"초등학교 6학년","campus_label":None})
        before = [(r["id"], r["document_id"]) for r in conn.execute("SELECT * FROM observations")]
        conn.execute("DELETE FROM app_meta WHERE key='assessed_rules'")
    reopened = Service(Store(service.store.path), {"auto_rule_ids":["explicit_notice","neis_ceremony_row"]})
    assert [e["id"] for e in reopened.events(season["id"])["items"]] == [canonical_id]
    assert reopened.event_detail(canonical_id)["event"]["publication"]["calendar_time"] == "10:00"
    assert reopened.event_detail(original_id)["event"]["id"] == original_id
    with reopened.store.transaction() as conn:
        assert [(r["id"], r["document_id"]) for r in conn.execute("SELECT * FROM observations")] == before
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 2
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
