# 광주 졸업식 일정 도우미 · 통합 기획·설계 4.0

어머니의 졸업식 꽃다발 판매 준비를 돕기 위해 광주 교육기관의 졸업식 날짜와 실제 시작 시각을 공개 자료에서 조사하고 근거와 함께 정리하는 AI Agent의 구현 인계 패키지입니다. **실제 앱의 구현 완료본이 아닙니다.**

두 안의 날짜·근거 표현과 게시 검사를 통합했습니다. 일반 조회에는 과거의 유효한 검증값과 확인시점·재확인 경고를, 판매 준비 목록에는 현재 허용된 날짜·시간을, 관리 기록에는 후보와 메모를 표시합니다. 모든 경로는 하나의 Publication 판정 결과를 사용합니다.

| 파일 | 용도 |
| --- | --- |
| 00_revision_notes.md | 채택 사항과 이유, 남은 한계 |
| 01_product_plan.md | 주제·사용자·핵심 기능·P0 |
| 02_technical_design.md | 흐름·서버 검증·상태 복구 |
| 03_setup_guide.md / .env.example / config | 실행 목표와 설정 계약 |
| 04_codex_handoff.txt | 구현 순서와 완료 조건 |
| 05_sources.md / 07_execution_basis.md / evidence | 자료원 및 실제 개별 시험의 범위·한계 |
| 06_acceptance.md | 정상/반례 및 실제 앱의 후속 인수 기준 |
| 08_release_policy.md / contracts | 단일 판정·스키마·API·DB·가상 예제 |
| checks | 실행 가능한 사양 검사, 문서 생성기와 패키지 검사기 |

## 사용자 설정 준비

현재 선택은 Ollama입니다. `.env.example`은 OpenAI 전환용 키 칸까지 여섯 항목만 제공합니다. OpenAI 키를 입력한 뒤 `LLM_BACKEND=openai`로 바꾸면 해당 모델 경로와 공개 문서 텍스트 전송 profile을 선택합니다. 이미지·사적 메모는 포함하지 않으며 키 입력만으로 전환하지 않습니다. 나머지는 `config/defaults.json`을 사용하고 전체 환경 변수 입력은 필수가 아닙니다. 입력 위치·Mac Ollama 백그라운드 실행·설정 우선순위는 `03_setup_guide.md`, `config/README.md`를 확인합니다. 실제 키는 `.env`에만 넣고 저장소·인계 ZIP에는 포함하지 않습니다. 실제 앱과 두 Adapter의 연결은 개발 대상입니다.

## 로컬 사양 검사와 재생성

Python 3.11 이상에서 별도 가상환경을 만들고 `pip install -r checks/requirements.txt`로 검사 의존성을 설치합니다. 실제 앱 설치 명령과 구분합니다.

```sh
python checks/run_spec_checks.py
python checks/build_html.py --template checks/document-template.html --output ../graduation-agent-plan-integrated.html --zip-name graduation-agent-handoff-integrated.zip
python checks/check_bundle.py --html ../graduation-agent-plan-integrated.html --zip ../graduation-agent-handoff-integrated.zip
```

검사 결과는 checks/validation-results.json에, 배포 파일별 해시는 checks/package-manifest.json에 남깁니다. HTML과 ZIP은 같은 원본으로 생성합니다. 변경 후 검사를 다시 실행하고 결과를 갱신한 뒤 재생성해야 합니다. 계약 검사 통과는 실제 사이트 조사 정확성, Windows 배포 성공 또는 광주 전체 수집 완료를 뜻하지 않습니다.
