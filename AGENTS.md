# 프로젝트 작업 규칙

- 커밋 제목은 한국어로 작성한다. 기술명·명령·식별자는 영어를 함께 사용할 수 있다.
- `.env`, 운영 DB, 수기 기록, 수집 원문, 로그, 내보낸 HTML과 백업은 로컬 실행 경로에 보관하고 커밋하지 않는다.
- 개발용 고정 자료가 필요하면 공개에 필요한 최소 사본을 만들고 토큰·개인정보·개인 경로를 검토한다. 원본은 Git 제외 경로에 보존한다.
- 검토한 자료만 `config/publication-data-manifest.json`에 정확한 SHA-256과 목적을 기록한다. 검사 통과만을 목적으로 검토 없이 해시를 갱신하지 않는다.
- 변경한 파일만 선택해 스테이징하고 `python3 scripts/check_publication_safety.py --staged`로 검사한다.
- 개발 환경의 Git hook은 `git config --local core.hooksPath .githooks`로 활성화한다. hook을 우회해 공개 제한을 해제하지 않는다.
- 이미 공개한 이력 재작성과 강제 푸시는 대상 브랜치·원격 상태를 확인하고 사용자의 명시적 승인을 받은 후 수행한다.

공개 자료 검토·이력 정리·검사의 범위와 한계는 [Git 공개 관리](docs/GIT_PUBLICATION.md)를 따른다.
