# 게시·API 회귀검사 자료

기존 인계 자료에서 실제 자동 검사에 쓰는 다섯 예제만 내용 변경 없이 옮겼다. 기관명·전화 확인·날짜·시각은 합성 값이며 실제 조사 성과가 아니다.

- `event-create`, `manual-patch`: API의 직접 기록·중복 요청·수정 충돌·재시작 보존 검사.
- `publication-ready`, `document`, `observation`: `tests/publication_cases.py`의 정상 게시와 원문·기관·행사·학년도·시각·수기 충돌 반례.

자료를 사용하는 검사는 현재 `app/domain.schema.json`과 운영 코드로 실행한다. 원본 인계 자료와 과거 작업 기록은 로컬 보관본으로 유지하며 앱 실행에는 사용하지 않는다.
