"""학교 명부를 남기고 조사 기록을 백업한 뒤 비우는 관리 작업."""

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from .launcher import InstanceLock


PRESERVED_TABLES = (
    "institutions", "institution_identifiers", "registry_snapshots", "registry_memberships",
)
# 자식부터 비워 외래키 검사를 계속 켠 채로 초기화한다.
RESET_TABLES = (
    "call_reservations", "job_segments", "job_items", "jobs", "natural_records",
    "count_observations", "investigation_traces", "route_attempts", "collection_routes",
    "manual_overrides", "assessments", "observations", "event_aliases", "event_seasons",
    "exports", "events", "documents", "seasons", "import_previews", "request_keys", "audit_log",
)


class ResetError(RuntimeError):
    """원본을 보존한 채 초기화를 중단해야 하는 조건."""


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    """없는 DB를 생성하지 않는 SQLite 연결을 연다.

    Args:
        path: 기존 DB 경로.
        readonly: 변경하지 않는 연결인지 여부.
    Returns:
        외래키 검사가 활성화된 연결.
    """
    connection = sqlite3.connect(
        path.resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
        uri=True, isolation_level=None, timeout=5,
    )
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _integrity(connection: sqlite3.Connection) -> None:
    """백업과 현재 DB의 물리·참조 무결성을 검사한다.

    Args:
        connection: 검사할 DB 연결.
    Raises:
        ResetError: SQLite 검사 또는 외래키 검사를 통과하지 못한 경우.
    """
    if [tuple(row) for row in connection.execute("PRAGMA quick_check")] != [("ok",)]:
        raise ResetError("DB 무결성 검사에 실패했습니다. 원본을 보존합니다.")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise ResetError("DB 참조 검사에 실패했습니다. 원본을 보존합니다.")


def _table_state(connection: sqlite3.Connection) -> dict:
    """순서에 의존하지 않는 전체 행 해시와 행 수를 반환한다.

    Args:
        connection: 같은 snapshot에서 읽을 DB 연결.
    Returns:
        테이블별 행 수와 SHA-256. 내용 자체는 출력하지 않는다.
    """
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    result = {}
    for (table,) in tables:
        name = '"' + table.replace('"', '""') + '"'
        rows = sorted(
            json.dumps(tuple(row), ensure_ascii=False, separators=(",", ":"), default=repr)
            for row in connection.execute(f"SELECT * FROM {name}")
        )
        result[table] = {
            "count": len(rows),
            "sha256": hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest(),
        }
    return result


def _artifact_state(folder: Path) -> dict:
    """내보낸 일정표를 이동하기 전후로 파일 내용을 대조한다.

    Args:
        folder: 앱이 관리하는 exports 디렉터리.
    Returns:
        상대 경로별 SHA-256.
    Raises:
        ResetError: 예상하지 못한 파일 또는 심볼릭 링크가 있는 경우.
    """
    if folder.is_symlink() or not folder.is_dir():
        raise ResetError("일정표 경로가 일반 디렉터리가 아닙니다. 자동 초기화를 중단합니다.")
    result = {}
    for entry in sorted(folder.rglob("*")):
        if entry.is_symlink() or not (entry.is_file() or entry.is_dir()):
            raise ResetError("일정표 폴더에 특수 파일이 있어 자동 초기화를 중단합니다.")
        if entry.is_file():
            result[str(entry.relative_to(folder))] = hashlib.sha256(entry.read_bytes()).hexdigest()
    return result


def _backup(source: Path, destination: Path) -> dict:
    """SQLite 백업 API로 전체 기록을 보존하고 내용을 대조한다.

    Args:
        source: 현재 DB.
        destination: 아직 존재하지 않는 백업 경로.
    Returns:
        백업한 테이블별 행 수와 해시.
    Raises:
        ResetError: 백업 내용이 원본과 다르거나 무결성이 깨진 경우.
        FileExistsError: 백업 파일이 이미 존재하는 경우.
    """
    # exclusive 생성으로 같은 이름의 기존 백업을 덮어쓸 수 없게 한다.
    with destination.open("xb"):
        pass
    with closing(_connect(source, readonly=True)) as original:
        _integrity(original)
        with closing(sqlite3.connect(destination)) as saved:
            original.backup(saved)
            _integrity(saved)
            before, after = _table_state(original), _table_state(saved)
            if before != after:
                raise ResetError("백업과 원본 내용이 달라 초기화를 중단했습니다.")
    return before


def reset_research_data(data_dir: Path, *, confirmed: bool = False) -> dict:
    """앱이 종료된 저장소의 명부만 남기고 조사 기록을 초기화한다.

    Args:
        data_dir: graduation.sqlite3와 instance.lock이 있는 모드별 데이터 경로.
        confirmed: 시즌·수기·조사 기록 초기화를 명시적으로 승인했는지 여부.
    Returns:
        백업 위치와 보존·초기화 행 수를 담은 비밀 없는 결과.
    Raises:
        ResetError: 승인·종료·지원 스키마·백업·무결성 조건을 충족하지 못한 경우.
        OSError: 백업이나 파일 이동에 실패한 경우. DB 거래와 이동은 복구한다.
    """
    if not confirmed:
        raise ResetError("초기화에는 --confirm-reset 확인 옵션이 필요합니다.")
    directory = Path(data_dir)
    database = directory / "graduation.sqlite3"
    if directory.is_symlink() or database.is_symlink() or not database.is_file():
        raise ResetError("기존 일반 DB 파일을 찾을 수 없습니다. 설정과 경로를 확인해 주세요.")
    if (directory / "instance.lock").is_symlink():
        raise ResetError("앱 잠금 경로가 심볼릭 링크입니다. 자동 초기화를 중단합니다.")
    lock = InstanceLock(directory / "instance.lock")
    try:
        if not lock.acquire():
            raise ResetError("앱이 실행 중입니다. 화면의 프로그램 종료를 누른 뒤 다시 실행해 주세요.")
        with closing(_connect(database, readonly=True)) as conn:
            before = _table_state(conn)
            expected = set(PRESERVED_TABLES + RESET_TABLES + ("app_meta",))
            if set(before) != expected:
                raise ResetError("지원하는 DB 테이블 구성과 다릅니다. 자동 초기화를 중단합니다.")
            version = conn.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone()
            if version != ("4.0",):
                raise ResetError("지원하는 DB 버전이 아닙니다. 원본을 보존합니다.")
        exports = directory / "exports"
        artifact_state = _artifact_state(exports) if exports.exists() or exports.is_symlink() else None
        backups = directory / "backups"
        if backups.is_symlink():
            raise ResetError("백업 경로가 심볼릭 링크입니다. 자동 초기화를 중단합니다.")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_dir = backups / f"research-reset-{stamp}-{uuid4().hex[:12]}"
        backup_dir.mkdir(parents=True, exist_ok=False)
        saved_db = backup_dir / "graduation.sqlite3"
        backed_up = _backup(database, saved_db)
        moved = False
        saved_exports = backup_dir / "exports"
        with closing(_connect(database)) as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                if _table_state(conn) != backed_up:
                    raise ResetError("백업 이후 DB가 변경되어 초기화를 중단합니다.")
                for table in RESET_TABLES:
                    conn.execute(f'DELETE FROM "{table}"')
                conn.execute(
                    "DELETE FROM app_meta WHERE key IN ('selected_season_id','development_archive_reassess')"
                )
                conn.execute(
                    "UPDATE app_meta SET value=CAST(value AS INTEGER)+1 WHERE key='content_revision'"
                )
                after = _table_state(conn)
                if any(after[table] != backed_up[table] for table in PRESERVED_TABLES):
                    raise ResetError("학교 목록 보존 검사에 실패했습니다.")
                if any(after[table]["count"] for table in RESET_TABLES):
                    raise ResetError("일부 조사 기록이 남아 초기화를 중단합니다.")
                _integrity(conn)
                if artifact_state is not None:
                    exports.rename(saved_exports)
                    moved = True
                    if _artifact_state(saved_exports) != artifact_state:
                        raise ResetError("일정표 파일 백업 검증에 실패했습니다.")
                result = {
                    "status": "completed",
                    "backup_directory": str(backup_dir.resolve()),
                    "backup_database": str(saved_db.resolve()),
                    "preserved_counts": {table: after[table]["count"] for table in PRESERVED_TABLES},
                    "cleared_counts": {table: backed_up[table]["count"] for table in RESET_TABLES},
                    "export_files_archived": len(artifact_state or {}),
                }
                receipt = {**result, "before_tables": backed_up, "after_tables": after,
                           "export_files": artifact_state or {}}
                with (backup_dir / "reset-receipt.json").open("x", encoding="utf-8") as stream:
                    json.dump(receipt, stream, ensure_ascii=False, indent=2)
                conn.commit()
            except BaseException:
                conn.rollback()
                if moved:
                    saved_exports.rename(exports)
                # 완료 영수증이 남아 성공한 것처럼 읽히지 않도록 실패 자료를 구분한다.
                receipt = backup_dir / "reset-receipt.json"
                if receipt.exists():
                    receipt.rename(backup_dir / "not-applied-receipt.json")
                raise
        return result
    finally:
        lock.close()
