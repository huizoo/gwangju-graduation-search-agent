# 기존 커밋 메시지 한국어 변경안

현재 브랜치의 기존 커밋 40개는 병합 커밋이 없는 단일 이력이다. 영어 메시지 35개를 한국어로 옮기고, 기존 한국어 메시지 5개는 그대로 유지한다. 본문이 있는 커밋 2개도 함께 번역한다.

이 문서는 변경안을 기록하며 이력 변경 자체를 수행하지 않는다. 이력을 변경할 때 작성자·작성일·커미터·커밋일은 보존한다. 메시지와 부모 커밋이 달라지므로 커밋 ID는 바뀐다. 아래 ID는 변경 전 ID다.

## 제목

| 기존 ID | 기존 제목 | 새 제목 |
|---|---|---|
| `7e4c428` | chore: preserve v4 handoff and validate runtime configuration | v4 인계 자료 보존 및 실행 설정 검증 |
| `d8e25ae` | feat: persist evidence and protected manual records with unified publication | 근거와 보호된 수기 기록을 저장하고 게시 기준 통합 |
| `542226a` | feat: research official sources through MCP and validate versioned evidence rules | MCP로 공식 자료를 조사하고 버전별 근거 규칙 검증 |
| `ff35cad` | fix: propagate required source recheck failures without losing confirmed history | 확인 이력을 보존하면서 필수 원문 재검사 실패 반영 |
| `96f75d4` | feat: import reviewed official registries with pinned source provenance | 출처가 고정된 검토 완료 공식 명부 가져오기 |
| `cd3d7d2` | chore: preserve captured source bytes without text normalization | 수집 원문을 텍스트 정규화 없이 바이트 그대로 보존 |
| `b1ec1cb` | feat: apply verified calendar date changes while preserving prior evidence | 기존 근거를 보존하면서 확인된 학사일정 날짜 변경 반영 |
| `f09ef0f` | feat: run durable jobs through a local API with safe snapshot exports | 로컬 API로 작업을 영속 실행하고 안전한 스냅샷 내보내기 제공 |
| `ab36417` | feat: add Korean research and manual-entry screens for daily use | 일상 사용을 위한 한국어 조사·수기 입력 화면 추가 |
| `dd590bf` | docs: record P0 acceptance, live findings, and standalone schedule deliverables | P0 인수 검증·실제 조사 결과·독립 일정표 산출물 기록 |
| `a00d4bf` | fix: bind ceremony evidence across sources and guard job resume | 서로 다른 출처의 행사 근거를 연결하고 작업 재개 보호 |
| `f650ee0` | feat: provide one current-only schedule with offline filters and paging | 현재 유효한 정보의 단일 일정표에 오프라인 필터·페이지 이동 제공 |
| `93c7667` | feat: record natural confirmations and simplify the local app | 자연어 확인 내용을 기록하고 로컬 앱 단순화 |
| `47fbca7` | fix: preserve distinct ceremonies and remove invalid school links | 서로 다른 행사를 구분하고 잘못된 학교 링크 제거 |
| `a2286eb` | fix: hold prior schedule when the same ceremony is postponed | 같은 행사의 연기가 확인되면 이전 일정 사용 보류 |
| `403d3bf` | docs: publish simplified schedule and verified app evidence | 단순화한 일정표와 앱 검증 근거 문서화 |
| `aad8036` | feat: define date-count and event-time research contracts | 날짜·인원 조사와 행사 시각 조사 계약 정의 |
| `e85f3d5` | feat: verify school counts and grounded ceremony evidence with durable history | 학교 인원·원문 기반 행사 근거를 검증하고 이력 영속 저장 |
| `bf11d64` | feat: simplify schedule views and separate whole-school and event-time jobs | 일정표 화면을 단순화하고 전체 학교 조사와 행사 시각 조사 분리 |
| `82fe697` | fix: keep explicit date-count jobs scoped to every target school | 명시적 날짜·인원 조사에서 전체 대상 학교 범위 유지 |
| `aeefdae` | fix: reconcile university ceremony synonyms without losing count evidence | 인원 근거를 보존하면서 대학 행사 동의어 연결 |
| `1d19f41` | feat: run school research through evidence-driven guarded model actions | 근거 기반 검증 제약을 적용한 모델 동작으로 학교 조사 실행 |
| `51cb1e5` | docs: publish verified five-column schedule and live agent evidence | 검증한 5열 일정표와 실제 Agent 실행 근거 문서화 |
| `a4a2de4` | feat(ui): show durable time-search progress and period-based seasons | 시간 찾기 진행 상태를 복원하고 기간 기반 시즌 표시 |
| `333d144` | fix(ui): retain paused time-search feedback after checkpoints | 체크포인트 이후에도 일시정지된 시간 찾기 안내 유지 |
| `d3e3643` | feat(research): separate date-range seasons from evidence academic years | 날짜 범위 시즌과 근거 자료의 학년도 분리 |
| `f225f7d` | docs: publish period workflow and separate live and browser verification | 기간별 사용 흐름을 문서화하고 실제 연동·브라우저 검증 구분 |
| `4b70f1b` | 구분된 작업 이력으로 시간 찾기와 전체 조사 안내 제공 | 구분된 작업 이력으로 시간 찾기와 전체 조사 안내 제공 |
| `c9714ab` | 자동 HTML 저장의 DB 잠금 복구와 동시 쓰기 검증 | 자동 HTML 저장의 DB 잠금 복구와 동시 쓰기 검증 |
| `ece9f7b` | 개발 GIST 검증 자료를 보관하고 운영 근거에서 분리 | 개발 GIST 검증 자료를 보관하고 운영 근거에서 분리 |
| `c213e50` | 일정 조회에 시간 찾기 결과와 전체 조사 안내 연결 | 일정 조회에 시간 찾기 결과와 전체 조사 안내 연결 |
| `8e0fb45` | feat: automatically refresh standalone schedules and preserve failed saves | 독립 일정표를 자동 갱신하고 저장 실패 시 기존 파일 보존 |
| `22235c2` | 재개 불가 조사 중단 안내를 실제 다시 조사 동선과 일치 | 재개 불가 조사 중단 안내를 실제 다시 조사 동선과 일치 |
| `4230b1f` | refactor(ui): simplify schedule actions and show precise research feedback | 일정표 조작을 단순화하고 조사 결과를 정확하게 안내 |
| `7ab061d` | docs: record verified usability changes and development-data separation | 검증한 사용성 개선과 개발 데이터 분리 결과 기록 |
| `4adc458` | fix(ui): download schedules with the server-provided Korean filename | 서버가 제공한 한국어 파일명으로 일정표 내려받기 |
| `6fc7341` | feat: use Korean period filenames and clear previous schedule exports | 기간을 포함한 한국어 파일명 적용 및 기존 일정표 산출물 정리 |
| `61e4952` | fix: require eligible school roster before scheduling research | 조사 작업 생성 전에 적격 학교 명부 확보 요구 |
| `936cc8c` | test: verify first-use roster recovery and season download in browser | 브라우저에서 첫 사용 명부 복구와 시즌 일정표 다운로드 검증 |
| `5b0516a` | fix: guide first use through school roster loading before research | 첫 사용 시 조사 전에 학교 목록을 불러오도록 안내 |

## 본문

### `d3e3643`

백업을 포함한 마이그레이션으로 기간이 겹쳐도 기준 행사와 수기 기록을 보존한다. 실제 날짜로 NEIS를 조회하고, 인원 자료를 확인된 원문 학년도에 연결하며, 작업 재개와 저장 시 시작 시점에 고정한 조사 범위를 준수한다. 모호한 연결·취소·공통 시각·기존 데이터를 검증하고 회귀 검사 483개를 통과했다.

### `f225f7d`

원문 학년도 처리, 마이그레이션과 수기 기록 보호, 지원하는 화면 상태를 문서화한다. 실제 과거·미래 API 응답, 성과를 얻지 못한 모델 조사, 격리된 브라우저 검증 근거와 기간별 독립 HTML 예제를 보존한다.
