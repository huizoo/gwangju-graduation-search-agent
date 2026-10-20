# 데이터와 API 계약 4.0

## 단일 정의와 적용 순서

`domain.schema.json`(JSON Schema draft 2020-12)의 `$defs`가 JSON 형식의 기준이다. OpenAPI 3.1은 같은 정의를 참조하고 SQLite `schema.sql`은 저장 구조를 정의한다. `checks/publication_policy.py`는 게시 규칙의 실행 가능한 **설계 참조 구현**이다. 실제 FastAPI/LangGraph/MCP 앱은 이 인계 패키지에 구현되어 있지 않다. Pydantic 모델과 SQL JSON 값도 동일 계약으로 구현하고 검사해야 한다.

`ValidatedContext`는 서버가 같은 read snapshot에서 조립하는 내부 계약이며 documents/observations/등록 규칙/판정시점/미해결 행사 사유를 포함한다. API나 LLM이 이 객체를 제출하지 않는다. use_fixture_rules는 사양 시험 전용이다.

형식 검사는 사실 검증이 아니다. 서버만 `ValueCheck`, 관측 채택 상태, `Publication`을 만든다. 모델/클라이언트의 `official`, `supported`, 게시 상태를 저장 명령으로 신뢰하지 않는다. 활성 규칙은 ID와 버전으로 등록하며 live `auto_rule_ids=[]`가 초기값이다. 가상 규칙 시험을 실제 자료의 검증 완료로 취급하지 않는다.

모든 ID는 서버 UUID, 날짜는 실제 달력 날짜, 시각은 HH:mm, 기록 시점은 timezone을 포함한 ISO 8601이다. 저장 시 UTC로 정규화하고 행사 시간은 Asia/Seoul로 해석한다. null은 미확인이다. 원문에 자정이 명시된 00:00만 유효하며 누락 기본값으로 생성하지 않는다.

## 하나의 판정, 세 가지 조회

`calculate_publication(event, season, validated_context) → Publication`을 한 번 계산하고 API, 화면, HTML 내보내기와 집계가 같은 결과를 사용한다. 입력의 기존 `publication`이나 `effective_value`를 정답으로 신뢰하지 않고 검증된 자동 평가 또는 유효한 수기 확인에서 출력값을 생성한다. 제공된 결과의 무결성 검사는 재계산 결과와 전체 객체를 비교한다.

| 조회 | 데이터와 표시 조건 |
| --- | --- |
| `view=calendar` 일반 조회 | `calendar_eligible=true`인 검증 이력의 일 단위 날짜. 과거에 유효하게 확인한 값을 확인시점과 함께 표시한다. 필수 근거의 실제 재확인에 실패했으면 필드별 경고를 표시한다. |
| `view=sales` 판매 준비 | 같은 판정의 `sales_ready=true`. 현재 사용이 허용된 날짜와 그 날짜에 연결된 실제 시작 시각이 모두 있어야 한다. |
| `view=all` 관리 기록 | 후보, 월 단위 계획, 확인시점 없는 메모, 기각 근거와 이력을 상태와 함께 표시한다. 정상 확인값처럼 표시하지 않는다. |

`Publication.calendar_date/time/venue`는 일반 조회의 검증값이다. `state=date_and_time/date_only/hold/excluded`는 일반 조회의 형태이고 현재 판매 허가와 같지 않다. `current_date_allowed/current_time_allowed`는 필드별 현재 허용값이며 `sales_ready`는 현재 예정 상태·날짜·시각을 함께 충족한 결과다. `field_permissions`는 네 필드의 `general_visible/current_allowed/confirmed_at/recheck_status/reason_codes`를 제공한다. 판매 준비 표의 장소 등 선택 필드도 해당 current_allowed가 true일 때만 현재값으로 표시한다. 일반 조회의 과거값을 경고 없이 판매 표로 복사하지 않는다. `withheld_fields`는 일반 조회에서 가려진 필드이며 현재 제한 이유는 각 permission과 최상위 reason_codes에도 남긴다.

근거가 잘못됐다고 판명되면 관측을 `rejected`로 전환하고 의존 평가와 출력을 재계산한다. 해당 관측만으로 유지하던 값은 일반 조회에서도 정상 확인값으로 남기지 않는다. 독립적인 유효 근거가 있으면 그것으로 재평가할 수 있다. 단순 연결 실패는 원문이 틀렸다는 증명이 아니므로 과거 검증값과 원문 버전을 보존한다.

## 날짜·시간·행사 문맥

`DateClaim`은 day/month/unknown, `DateValue`는 day/month다. 후보·수기 입력·projection 모두 동일 객체를 사용한다. 월만 알면 관리 기록에 월을 보관하고 임의 일자를 만들지 않는다. 월 단위는 일자 확인율이나 판매 목록에 포함하지 않는다.

졸업식·졸업/수료 통합식·실제 학위수여식은 대상 행사입니다. 수료식 단독, 단순 학위수여일과 unknown은 자동 판매 목록으로 승격하지 않습니다. 공식 운영 종료일이 확인된 경우 해당 일 이후의 행사는 제외하며, 종료일 미상은 자동 조사 대상에서 조용히 제외하지 않고 검토합니다.

행사별 `event_key`는 서버가 만드는 불변 키다. 기관/시즌이 같아도 학과·회차·대상·캠퍼스가 다르면 다른 행사일 수 있다. 기관/시즌만으로 합치지 않는다. 행사 식별이 불명확한 관측은 `event_id=null`로 보관한다. 행사 전체의 미해결 식별 문제·취소 충돌은 전체를 보류하고, 시간만 충돌하면 날짜를 유지한다. 상위 `review_required=true` 자체로 전체를 숨기지 않는다.

시간은 `bound_date`와 연결하며 게시 날짜가 일 단위이고 같은 값일 때만 표시한다. 날짜 변경 시 과거 시간을 새 날짜에 자동 연결하지 않는다. 장소도 `bound_date`가 있으면 일치해야 한다. 최종 장소를 포함한 모든 출력은 확인값에서 생성하므로 임의 대체값을 허용하지 않는다. 시즌 범위 밖 날짜는 관리 기록으로 남기고 일반/판매 일정에서 제외한다.

## 자동 평가와 근거

수집 순서는 등록된 정형 매핑 → 필요한 경우 LLM 후보 추출 → 서버 근거 검증 → 게시 판정이다. NEIS의 명확한 레코드를 모델 두 역할로 매번 재해석하지 않는다. 비정형 후보가 형식상 올바르더라도 의미를 검증할 규칙이 없으면 reference_only로 보관한다.

- `DocumentRead`는 불변 원문 버전의 해시·segments·취득(`acquisition`)·읽은 범위(`reading`)를 분리한다. 성공 HTTP만으로 본문/기관 일치를 인정하지 않는다. 미리보기·이미지 후보를 원문 전체로 승격하지 않는다.
- `authority_evidence`는 공식 채널 URL, 그 관계를 확인한 URL, 확인시점을 보존한다. 공식 도메인처럼 보인다는 이유만으로 official을 채우지 않는다. 첨부파일 CDN은 연결한 공식 게시물로 관계를 검증한다.
- `Observation.disposition`은 pending/accepted/reference/rejected이고 사유를 남긴다. LLM 후보를 accepted로 바로 저장하지 않는다.
- `ValueCheck`는 대상 field, 등록 rule_id/rule_version, 문서별 content_hash, 관측 ID(`evidence_ids`), 문서 ID, 정책 버전, 각 검사 결과와 검사시점을 연결한다. 관측의 기관·시즌·event_id, 원문 버전·공식성, 같은 행사 unit, 실제 인용과 정규값을 서버가 검사한다.
- institution/content/season/event_identity/literal_presence/normalized_value는 pass여야 한다. time_role은 start_time에서 pass, 나머지 필드에서만 not_applicable이다. 시간 역할은 행사 시작이며 입장/하교/대여/게시 시각과 구별한다.
- 가상 참조 규칙의 문자열 정규화는 일반 한국어 문서의 의미 검증기가 아니다. 참조 규칙은 같은 공식 채널 안의 literal 본문만 다루며 CDN 첨부 연결·이미지·HWP·원격 의미 판독은 구현 범위에 없다. 해당 경로는 앱에서 별도 원문 연결/읽기 규칙을 구현해야 한다. 실제 자료 형식별 정형 매핑의 양성·음성 시험을 통과한 뒤에만 해당 ID/버전을 live 활성화한다.

`automatic.official_confirmed`는 null 값/근거/검사시점을 허용하지 않는다. conflict/unchecked/missing 값은 null이고 reference_only는 후보를 보관할 수 있다. projection의 effective 값은 관리 화면 표시용 파생값이며 게시 권한을 대신하지 않는다. API 최종 일정은 Publication 값만 사용한다.

## 재확인과 원문 버전

`DocumentRead.recheck`는 해당 원문 지원 경로를 실제 재확인한 최신 시도의 결과다. `not_attempted`는 attempted_at=null, succeeded/failed는 실제 attempted_at을 요구한다. 과거 원문 본문/해시를 실패 응답으로 덮어쓰지 않는다. 원문 변경을 발견하면 새 버전을 저장하고 재평가한다.

보조 검색 실패, 재확인 미수행, 저장 HTML의 오프라인 열람은 failed로 바꾸지 않는다. 여러 근거 중 일부만 실패했으면 유효한 다른 지원 경로를 확인한다. 필요한 지원 경로가 모두 실제 재확인에 실패했고 독립적인 사람의 재확인도 없을 때에만 그 필드의 현재 사용을 보류한다. 전화 확인은 웹 연결 장애와 독립적이다. 수기 문서 확인은 `source_document_id`로 저장된 원문 버전을 연결한다. 종이·오프라인 기록은 null을 허용한다. URL만 있는 이전 기록은 confirmed_at 이전에 취득한 같은 기관·URL의 최신 버전을 선택하고, 무관한 옛 오류 버전을 함께 묶지 않는다. 연결된 지원 버전에서 사람의 확인 이후에 발생한 실제 재확인 실패를 현재 사용에 반영한다. 그 뒤 실제 사람이 다시 확인했거나 새 정상 지원 경로가 확인되면 재평가한다. 알려진 잘못된 기관/원문의 연결은 확인시점만 갱신해 정상화할 수 없다.

임의의 미검증 TTL은 두지 않는다. `confirmed_at`은 그 값의 확인시점, last_attempt_at은 시도, last_successful_source_check_at은 원문 점검 성공이다. 신규 본문 확인 없이 과거 확인시점을 현재 시각으로 갱신하지 않는다. 현재 허용은 설정된 근거 조건의 충족을 뜻하며 실세계의 무변경을 보장하지 않는다.

## 수기 보호와 충돌 해결

수기 set은 null이 아닌 DateValue/시각/문자열을 받고 clear는 보호된 null, release는 사용자 지정 해제다. user_recorded 메모와 confirmed_at=null은 저장할 수 있지만 확인값으로 게시할 수 없다. 입력 시점 recorded_at을 실제 확인시점 confirmed_at 대신 넣지 않는다. 유효한 전화/문서 확인에는 실제 확인시점이 필요하며 미래 시점은 거절한다. 일반 신규 확인의 메모는 선택 입력이고 충돌 재확인에는 필수다. 종이·오프라인 문서의 사람 확인은 저장된 웹 원문 없이도 기록할 수 있지만 실제 사람이 확인했다는 입력을 신뢰하는 경계가 남는다.

`acknowledge`는 알림 읽음이다. 게시 보류를 해제하지 않는다. 충돌을 해결하려면 새로운 set에 실제 방법, 확인시점, 메모와 현재 `resolves_fingerprint`를 제출한다. 해당 fingerprint의 안정적인 최초 충돌 인지시점 `automatic.conflict_detected_at` 이후의 재확인인지 검사한다. 내용과 fingerprint가 같은 재수집에서 checked_at만 갱신해 기존 해결을 무효화하지 않는다. fingerprint가 새로 바뀌면 conflict_detected_at을 새 인지시점으로 설정한다. fingerprint가 바뀌면 이전 해결 기록은 새 충돌을 해결하지 못한다. 수기 값을 자동 조사로 덮어쓰지 않는다.

fingerprint는 기관/시즌/event_key/필드/정규값/관련 문서 내용 해시와 관측에서 계산하고 fetched_at/checked_at은 제외한다. 최신 assessment는 증가 seq로 선택한다. 행사 제목/대상 변경으로 근거 문맥이 달라지면 새 검토가 필요하다.

## API·저장·집계

선언하지 않은 키를 거절한다. Error가 오류 응답의 단일 객체이며 빈 성공 배열로 인증/파싱 실패를 숨기지 않는다. 쓰기는 LocalToken, revision 및 요청 멱등성 규칙을 적용한다. 단일 operations에서 동일 field 두 번 지정은 422다. 중복 행사 후보가 있고 별도 생성 의도가 없으면 409/candidate_ids를 반환한다.

페이지 limit 기본 100/최대 500, total은 필터 전체 수다. cursor는 동일 필터·정렬·snapshot에만 유효하다. 일반/판매 정렬은 각각 허용된 날짜·시각/기관명/id다. 관리 조회는 보관 후보를 포함한다. review queue는 행사 0건 기관도 포함한다. empty ids는 거절하고 all로 해석하지 않는다.

기관 등록은 공식 목록 import preview → 중복 mapping(create/update/skip) → revision/preview_hash 확인 commit이다. 원본/출처/기준일/해시와 제외/실패 페이지를 보존한다. 일부 import나 한 페이지 응답을 광주 전체 완료로 표시하지 않는다. 기관 등록, 조사 시도, 날짜/시간 확인 완료는 별개다. 각 source 범위의 전체 페이지 성공을 확인한 범위만 complete로 기록한다. 일부 구의 실패를 다른 구의 폐원 근거로 사용하지 않는다.

Overview의 날짜 확인 수는 `current_date_allowed`, 시간 확인 수는 `current_time_allowed`인 행사의 유일 기관 수다. 날짜만 있어도 날짜 지표에 포함할 수 있고 sales_ready는 false다. 일반 조회의 과거 값·월 단위·후보는 포함하지 않는다. 분모는 해당 시즌 조사 대상의 유일 기관 수, 0이면 비율 null이다. 공식/수기 출처별 수는 중복 가능하며 더해서 전체 수로 쓰지 않는다. `events_with_usable_*`도 현재 허용 기준이다. 집계와 응답의 content_revision/publication_policy_version을 일치시킨다.

## 작업·재시작·예산

기관별 그래프를 실행하며 job scope는 시작 시 고정한다. initial/recheck/missing_only는 동일 허용값과 검토 사유를 기준으로 선택한다. registry_sync는 LLM 없이 같은 HTTP/시간 계량기를 쓴다. 최종 LLM_BACKEND를 defaults/.env/프로세스 환경변수 우선순위로 결정한 뒤 defaults → 해당 llm_profiles의 전송 설정 → 비어 있지 않은 .env → 비어 있지 않은 프로세스 환경변수를 병합한다. limits=null은 이렇게 얻은 실행 한도이며 명시 limits는 전체 ExecutionLimits다. 생략·빈 값은 직전 값을 유지하고 false·0은 보존한 뒤 허용 범위를 검사한다. env-map 전체는 필수 입력 목록이 아니며 .env.example은 여섯 준비 항목만 담는다. 설정 변경은 재시작 후 새 작업에 적용한다. `source_config_hash`는 정규화한 비밀이 아닌 수집·모델 실행 설정의 해시로 backend/model/관련 Ollama endpoint/전송 정책/게시 정책 버전/활성 규칙 ID를 포함하고 키·UI 포트·브라우저 열기는 제외한다. 재개 요청은 현재 해시와 같을 때만 허용하며 다르면 provider 초기화·호출 전에 409 `settings_changed`를 반환하고 기존 snapshot은 보존한다. 해시로 이전 설정을 복구하지 않으며 사용자는 이전 설정 복원 또는 기록을 유지한 새 작업을 선택한다. 키는 작업 snapshot·일반 JSON·로그·HTML에 보관하지 않는다. 시즌은 생성 후 DB가 소유하며 config의 시즌 값으로 기존 시즌을 덮어쓰지 않는다. 호출 전 예약하고 재시도도 예산에 포함한다. 응답 유실은 charged_unknown으로 보존하며 사용량을 0으로 되돌리지 않는다.

정확한 해시 대상은 `checks/configuration_reference.py`의 `source_configuration_hash`가 정의한다. 모드·선택 모델/검색 제공자·해당 endpoint·문서/검색 플래그·전송 권한·게시 정책/활성 규칙·이미지 후보를 포함하며 비선택 모델·비밀값·화면/추적 설정·시즌·실행 한도는 제외한다. 시즌/한도는 기존 작업 snapshot을 따로 유지한다. 재개 오류는 `Error.code=settings_changed`, `details.reason_codes=[configuration_required]`로 응답하며 `settings_changed`를 작업 paused_reason의 새 값으로 쓰지 않는다. 앱 시작 시 과거 작업의 백엔드를 일괄 접속 검사하지 않는다.

종료 상태는 유지한다. 취소 의도가 없던 running/pausing은 재시작 시 paused로 복구한다. 이미 모든 항목이 정상 종결됐다면 completed로 정리할 수 있지만 **cancelling은 취소를 마무리하여 cancelled로 복구**한다. cancelled는 resume 대상이 아니다. 취소 이후 늦게 도착한 응답이 작업을 running/completed로 되살리지 않게 revision/취소 상태를 검사한다. 이미 저장한 정상 결과와 사용량은 보존한다. pending/running 항목을 정산한 뒤 취소 우선으로 부모 작업을 종료한다.

## HTML 내보내기와 이전 버전

동일 DB read snapshot의 Publication과 permission 경고·확인시점을 사용해 저장한다. 일반/판매/관리 영역은 같은 판정의 투영이다. 미확인 후보는 관리임을 명확히 표시하고 최종 표의 확인값으로 섞지 않는다. 기본 메모 제외, 사용자 불신 텍스트/URL escape, 임의 스크립트 실행 금지, 외부 런타임 의존 없음이 요구사항이다. 필터·인쇄에 필요한 고정 신뢰 스크립트는 허용된다. 이 기획 문서 HTML은 실제 앱 export가 아니다.

2.0/2.1/3.0 DB와 export는 백업 후 명시 마이그레이션한다. 알 수 없는 DB 버전은 중단한다. 수기·월 값·원문·관측·이력을 보존하되 누락된 실제 확인시점·공식성·규칙 버전·해시를 만들어 채우지 않는다. 검증 연결이 부족한 자동 값은 reference_only/unchecked로 재평가하고 새 최종 표로 승격하지 않는다. 과거 export는 보관하되 정책 4.0으로 재생성하기 전 current URL로 제공하지 않는다. 새 파일 쓰기 실패 시 같은 4.0 정책/정합성으로 검증한 마지막 정상 파일만 시점을 표시하여 fallback할 수 있다.
