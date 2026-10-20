# 설정 파일의 역할

현재 선택은 Ollama이며 `.env.example`은 OpenAI 전환용 키 칸을 포함한 여섯 항목만 제공합니다. `qwen3:8b`는 기존 실험 재현값이고 OpenAI 기본값 `gpt-6.1-sol`도 실제 일정 추출 정확성을 검증한 추천 모델을 뜻하지 않습니다. 선택 모델에서 정상·반례 자료를 검증합니다.

| 파일 | 역할 |
| --- | --- |
| `.env` | 실제 키·모델 백엔드 선택·이 PC의 Ollama 연결 정보. Git에서 제외합니다. |
| `.env.example` | 비밀값이 없는 사용자 입력 양식. 전체 설정 목록이 아닙니다. |
| `config/defaults.json` | 실행 한도·재시도·기능 활성화·시즌 초기값 등 공유할 수 있는 모든 기본값. 비밀값은 넣지 않습니다. |
| `config/env-map.json` | 지원하는 전체 선택적 환경 변수와 타입·대상 경로. `user_setup=true`인 항목만 현재 예제에 표시합니다. 모든 항목을 입력할 필요는 없습니다. |

먼저 최종 `LLM_BACKEND`를 프로세스 환경 변수 > `.env` > 기본값 우선순위로 결정합니다. 이후 기본값 → 선택한 `llm_profiles`의 전송 설정 → 비어 있지 않은 `.env` 값 → 비어 있지 않은 프로세스 환경 변수 순으로 결합합니다. 빈 문자열은 이전 값을 지우지 않습니다. 명시한 `false`·숫자 `0`은 보존한 뒤 실제 앱의 범위 검사를 적용합니다. 등록되지 않은 `.env` 키는 값 없이 키 이름만 알리는 오류로 처리하고, 등록되지 않은 일반 프로세스 환경 변수는 무시합니다.

API 키는 실행 중 메모리에서만 결합하고 설정 JSON, 작업 snapshot, 로그, HTML에 저장하지 않습니다. 기존 시즌의 학년도·기간은 DB가 관리하며 기본 설정 변경이 기존 시즌을 덮어쓰지 않습니다. 조회·수기 입력은 외부 서비스 키 없이 사용할 수 있어야 하고, 누락된 연결은 해당 서비스 호출 단계에서 따로 알립니다. 설정 변경은 앱 재시작 후 새 작업에 적용합니다. 이전 작업 재개는 저장된 `source_config_hash`와 현재 비밀이 아닌 수집·모델 설정 해시가 같을 때만 허용합니다. backend/model/관련 Ollama endpoint/전송 정책/게시 정책 버전/활성 규칙 ID는 포함하고 키·UI 포트·브라우저 열기는 제외합니다. 불일치면 provider 초기화·호출 전에 409 `settings_changed`로 거절하며 이전 설정을 복원하거나 기록을 유지한 새 작업을 만듭니다. 해시만으로 이전 설정을 복구하지 않으며 중단 작업의 모델 전환은 P0에서 지원하지 않습니다.

## 입력할 곳

- `LLM_BACKEND`: `ollama` 또는 `openai`를 선택합니다. 초기값은 `ollama`입니다.
- `OLLAMA_BASE_URL`: 같은 PC의 기본 주소는 `http://127.0.0.1:11434`입니다. 별도로 변경한 주소가 없다면 그대로 둡니다.
- `OLLAMA_TEXT_MODEL`: Ollama의 설치된 모델 목록에서 정확한 이름과 태그를 확인합니다. `ollama list`로 확인할 수 있습니다. 모델 설치 여부와 호출 성공은 일정 추출 정확성의 증거가 아닙니다.
- `OPENAI_API_KEY`: OpenAI를 선택할 때만 https://platform.openai.com/api-keys 에서 발급한 키를 `.env`에 입력합니다. Ollama 사용 중에는 비워 둡니다.
- `NEIS_API_KEY`: https://open.neis.go.kr/portal/guide/apiGuidePage.do 의 인증키 신청 절차로 발급받아 `.env`에만 입력합니다.
- `TAVILY_API_KEY`: https://app.tavily.com 에서 가입·로그인 후 대시보드의 API 키를 `.env`에만 입력합니다.

OpenAI 키를 입력한 뒤 `LLM_BACKEND=openai`로 바꾸면 OpenAI 경로를 선택합니다. `OPENAI_FAST_MODEL`은 config 기본 모델의 선택적 재정의이며 `OPENAI_QUALITY_MODEL`이 비어 있으면 fast 모델을 재사용합니다. 선택한 백엔드만 초기화·검사하므로 OpenAI 선택에는 Ollama가 필요하지 않습니다. 키만 입력해서는 선택이 바뀌지 않으며 키 누락·장애 시 자동 fallback도 하지 않습니다.

`llm_profiles`는 텍스트·이미지 전송 허용값만 포함합니다. Ollama/fake의 기본 외부 전송값은 모두 false이고 OpenAI 선택은 공개 문서의 필요한 텍스트 구간을 OpenAI로 보내는 선택입니다. openai profile의 텍스트만 true이며 이미지와 사적 수기 메모는 포함하지 않습니다. 명시한 `ALLOW_EXTERNAL_DOCUMENT_TEXT=false`는 profile보다 우선하고 실행환경의 별도 승인/거절도 적용합니다. 사용자 지정 원격 Ollama 주소 역시 외부 전송 검사 대상입니다.

Mac도 Ollama 앱이 백그라운드 서비스를 실행하면 터미널에서 `ollama serve`를 별도로 띄우지 않아도 됩니다. GUI 없이 CLI 서버로 실행할 때만 해당 명령을 사용하며 같은 포트의 중복 서버는 피합니다. 유치원·어린이집·LangSmith 관련 환경 변수는 해당 연동을 선택할 때만 추가합니다.

이 파일들은 개발 전 설정 자료입니다. 실제 앱의 설정 로더와 서비스 연결은 아직 구현 대상입니다. 인계본의 `checks/configuration_reference.py`와 `check_configuration.py`는 합성 입력으로 로딩 규칙을 검증하는 참조 사양입니다.
