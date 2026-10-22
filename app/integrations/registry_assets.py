"""서버가 검토한 공식 명부와 일반 사용자 파일의 신뢰 범위를 구분한다."""

from app.core.domain import ApiError, fingerprint
from app.core.paths import ROOT
from app.integrations.official_registry import IMPORT_FILES, SOURCE_PROFILES, load_official_registry


def official_bundle(filename: str):
    """승인된 고정 파일 이름의 원문·정규값을 검사한다.

    Args:
        filename: 서버에 포함된 명부 JSON 이름.
    Returns:
        공식 원문과 관계가 검증된 목록·채널·검증 범위.
    Raises:
        ApiError: 없는 이름이거나 승인 바이트와 달라진 경우.
    """
    if filename not in IMPORT_FILES:
        raise ApiError("not_found", "준비된 공식 목록을 찾을 수 없습니다.", 404)
    return load_official_registry(
        ROOT / "assets/registry/source-manifest.json", filename,
        artifact_dir=ROOT / "assets/registry",
    )


def matching_authorities(payload: dict) -> tuple:
    """검토된 전체 입력과 정확히 일치할 때만 서버의 공식 관계를 반환한다.

    Args:
        payload: 미리보기에서 저장한 RegistryImport 원본.
    Returns:
        서버 검토 관계 목록. 일반 사용자 파일이나 변경 파일은 빈 tuple.
    """
    provider = payload["source"]["provider"]
    filename = next((name for name, profile in SOURCE_PROFILES.items() if profile[0] == provider), None)
    if filename is None:
        return ()
    verified = official_bundle(filename)
    if fingerprint(payload) != fingerprint(verified.registry):
        return ()
    return verified.authorities
