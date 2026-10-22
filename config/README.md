# 설정 파일의 역할

`.env.example`은 백엔드 선택·연결 정보와 자료원 키 입력 칸을 제공합니다. 기본 모델 이름은 실행 설정이며 모든 학교의 일정 추출 정확성을 보장하는 추천값은 아닙니다. 실제 사용하는 모델의 설치·호출 가능 여부를 확인해야 합니다.

| 파일 | 역할 |
| --- | --- |
| `.env` | 실제 키·모델 백엔드 선택·이 PC의 Ollama 연결 정보. Git에서 제외합니다. |
| `.env.example` | 비밀값이 없는 사용자 입력 양식. 전체 설정 목록이 아닙니다. |
| `config/defaults.json` | 실행 한도·재시도·기능 활성화·시즌 초기값 등 공유할 수 있는 모든 기본값. 비밀값은 넣지 않습니다. |
| `config/env-map.json` | 지원하는 전체 선택적 환경 변수와 타입·대상 경로. `user_setup=true`인 항목만 현재 예제에 표시합니다. 모든 항목을 입력할 필요는 없습니다. |

먼저 최종 `LLM_BACKEND`를 프로세스 환경 변수 > `.env` > 기본값 우선순위로 결정합니다. 이후 기본값 → 선택한 `llm_profiles`의 전송 설정 → 비어 있지 않은 `.env` 값 → 비어 있지 않은 프로세스 환경 변수 순으로 결합합니다. 빈 문자열은 이전 값을 지우지 않습니다. 명시한 `false`·숫자 `0`은 보존한 뒤 실제 앱의 범위 검사를 적용합니다. 등록되지 않은 `.env` 키는 값 없이 키 이름만 알리는 오류로 처리하고, 등록되지 않은 일반 프로세스 환경 변수는 무시합니다.

API 키는 실행 중 메모리에서만 결합하고 설정 JSON, 작업 snapshot, 로그, HTML에 저장하지 않습니다. 기존 시즌의 기간과 역사 학년도는 DB가 보존하며 기본 설정 변경이 기존 시즌을 덮어쓰지 않습니다. 새 시즌은 포함 날짜 범위로 생성하고 자료 학년도는 행사별로 검증합니다. 조회·수기 입력은 외부 서비스 키 없이 사용할 수 있어야 하고, 누락된 연결은 해당 서비스 호출 단계에서 따로 알립니다. 설정 변경은 앱 재시작 후 새 작업에 적용합니다. 이전 작업 재개는 저장된 `source_config_hash`와 현재 비밀이 아닌 수집·모델 설정 해시가 같을 때만 허용합니다. backend/model/관련 Ollama endpoint/전송 정책/게시 정책 버전/활성 규칙 ID는 포함하고 키·UI 포트·브라우저 열기는 제외합니다. 불일치면 provider 초기화·호출 전에 409 `settings_changed`로 거절하며 이전 설정을 복원하거나 기록을 유지한 새 작업을 만듭니다. 해시만으로 이전 설정을 복구하지 않으며 중단 작업의 모델 전환은 P0에서 지원하지 않습니다.

## 입력할 곳

- `LLM_BACKEND`: `ollama` 또는 `openai`를 선택합니다. 초기값은 `ollama`입니다.
- `OLLAMA_BASE_URL`: 같은 PC의 기본 주소는 `http://127.0.0.1:11434`입니다. 별도로 변경한 주소가 없다면 그대로 둡니다.
- `OLLAMA_TEXT_MODEL`: Ollama의 설치된 모델 목록에서 정확한 이름과 태그를 확인합니다. `ollama list`로 확인할 수 있습니다. 모델 설치 여부와 호출 성공은 일정 추출 정확성의 증거가 아닙니다.
- `OPENAI_API_KEY`: OpenAI를 선택할 때만 https://platform.openai.com/api-keys 에서 발급한 키를 `.env`에 입력합니다. Ollama 사용 중에는 비워 둡니다.
- `NEIS_API_KEY`: https://open.neis.go.kr/portal/guide/apiGuidePage.do 의 인증키 신청 절차로 발급받아 `.env`에만 입력합니다.
- `SCHOOLINFO_API_KEY`: 학교알리미의 학년별 학생 수 공개 API 인증키를 `.env`에만 입력합니다. 앱은 학교 소재 구에 맞는 필수 `sggCode`와 공식 시도 코드를 전달합니다. 키가 없는 경우 공개 공시 페이지 경로도 시도하며, 전체 학생 수를 졸업 학년 인원으로 대체하지 않습니다.
- `TAVILY_API_KEY`: https://app.tavily.com 에서 가입·로그인 후 대시보드의 API 키를 `.env`에만 입력합니다.
- `LANGSMITH_API_KEY`: 선택적 실행 추적을 위한 키 준비란입니다. `.env.example`의 이 항목만 기존 `.env`에 추가하면 됩니다. 현재 추적 연동은 미구현이며, 키만 입력해도 앱의 기존 동작과 외부 전송은 바뀌지 않습니다. 연동·검증이 끝나기 전에는 `LANGSMITH_TRACING=true`를 설정하지 마세요. 프로젝트 이름의 기본값 `graduation-agent`는 `config/defaults.json`에 유지합니다.

OpenAI 키를 입력한 뒤 `LLM_BACKEND=openai`로 바꾸면 OpenAI 경로를 선택합니다. `OPENAI_FAST_MODEL`은 config 기본 모델의 선택적 재정의이며 `OPENAI_QUALITY_MODEL`이 비어 있으면 fast 모델을 재사용합니다. 선택한 백엔드만 초기화·검사하므로 OpenAI 선택에는 Ollama가 필요하지 않습니다. 키만 입력해서는 선택이 바뀌지 않으며 키 누락·장애 시 자동 fallback도 하지 않습니다.

`llm_profiles`는 텍스트·이미지 전송 허용값만 포함합니다. Ollama/fake의 기본 외부 전송값은 모두 false이고 OpenAI 선택은 공개 문서의 필요한 텍스트 구간을 OpenAI로 보내는 선택입니다. openai profile의 텍스트만 true이며 이미지와 사적 수기 메모는 포함하지 않습니다. 명시한 `ALLOW_EXTERNAL_DOCUMENT_TEXT=false`는 profile보다 우선하고 실행환경의 별도 승인/거절도 적용합니다. 사용자 지정 원격 Ollama 주소 역시 외부 전송 검사 대상입니다.

Mac도 Ollama 앱이 백그라운드 서비스를 실행하면 터미널에서 `ollama serve`를 별도로 띄우지 않아도 됩니다. GUI 없이 CLI 서버로 실행할 때만 해당 명령을 사용하며 같은 포트의 중복 서버는 피합니다.

실제 설정 로더는 `app/config.py`, 병합·해시는 `app/configuration.py`, 지연 생성하는 선택 백엔드는 `app/providers.py`에 구현되어 있습니다. 로더·API·재개 경로는 `tests/test_config.py`, `tests/test_jobs_api.py`로 검사합니다.

`publication.auto_rule_ids`는 검사한 `규칙ID@버전`만 활성화합니다. 규칙 구현은 `app/rules.py`와 `app/claims.py`, 원문 관계와 현재 출력 허용 검사는 `app/publication.py`에 있습니다. 이전 버전과 이름이 같아도 새 버전을 자동 승인하지 않습니다. `tracing.enabled=true`는 아직 지원하지 않아 시작 시 명시적으로 거절합니다. 브라우저 보조 읽기·HWP·이미지 OCR은 수집 경로에 포함하지 않습니다.

`item_max_replans=6`은 한 학교에서 관찰 후 선택하는 다음 행동의 최대 횟수입니다. 기존의 두 계획 묶음(각 최대 세 행동)을 단일 행동 반복으로 바꾸면서 동일한 최대 도구 행동 수를 유지했습니다. HTTP·검색·토큰·학교별 시간 한도도 별도로 적용됩니다. 조사 계약 3.0 이전 작업은 기간·자료 학년도·행사 연결 조건이 다르므로 이력을 보존한 새 작업을 시작합니다. 3.0 작업은 저장한 기간·revision·대상 행사 날짜가 일치할 때만 재개합니다.
