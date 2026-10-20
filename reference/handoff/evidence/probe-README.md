# 졸업식 일정 수집 기능 검증

Supervisor와 분기 그래프를 구현하지 않고, 실제 자료를 다루는 작업을 독립적으로 실행하는 작은 검증 폴더다. 기존 프로젝트 코드는 변경하지 않는다.

## 확인하는 작업

| 작업 | 실행·판정 내용 |
|---|---|
| 검색 | 기존 프로젝트의 OpenAI 설정으로 기관명 검색. API 호출 성공과 해당 기관 공식 출처 확보를 구분한다. |
| HTTP 수집 | 학교·공공기관 원문 요청, 응답·원문·해시 저장. HTTP 200이어도 기대 본문이 없으면 `content_mismatch`다. |
| 브라우저 읽기 | 전용 자동화 브라우저에서 통합 학사일정·어린이집 공시 DOM을 읽고 정리 상태까지 확인한다. |
| PDF | 실제 PDF 다운로드, 파일 시그니처 확인, `pdftotext -layout`으로 추출한다. |
| HWP | 실제 공지에서 첨부 링크를 발견해 다운로드하고 HWP5 `PrvText` 미리보기 스트림을 읽는다. 전체 HWP 변환을 보장하지 않는다. |
| 이미지 | 공지에 연결된 실제 안내 이미지 2개를 다운로드하고 로컬 이미지 모델로 해석한다. |
| 나이스 API | 학교 코드 검색, 5건 샘플, 이미 알려진 졸업식 날짜의 행사 조회를 수행한다. |
| 일정 해석 | 원문 날짜·행사 시작 시간·목표 학년도·인용 근거를 로컬 모델로 추출하고 수동 확인 기준과 대조한다. |

판정용 기대값은 모델 입력에 전달하지 않는다. 본문 대신 검색 색인을 넣거나, 실패한 요청을 목업 응답으로 대체하지 않는다.

## 준비

Python 3.11 이상, `uv`, `pdftotext`가 필요하다. 이 폴더에는 전용 `.venv`가 준비되어 있다.

```sh
cd $HOME/workspace/week15/graduation_probe
uv venv --cache-dir .uv-cache --python python3 .venv
uv pip install --python .venv/bin/python --cache-dir .uv-cache -r requirements.txt
```

검색은 인접한 `all_total_vue_student/.env`의 기존 `OPENAI_API_KEY`를 메모리로 읽는다. 키는 결과에 기록하지 않는다. 검색 실행에는 API 비용이 발생한다. 검색 요청에는 기관명·검색어만 보내며 문서 원문은 보내지 않는다.

문서·이미지의 기본 해석 경로는 **로컬 Ollama**다. 현재 환경의 `qwen3:8b`(텍스트), `qwen3.5:2b`(이미지)를 사용한다. 서버를 임의로 시작하거나 다른 모델을 설치하지 않는다.

## 개별 실행

각 명령은 고정된 작업을 실행한다. 자동 분기·자동 재계획은 없다. 내려받은 파일이 필요한 작업은 파일이 없으면 `blocked`로 기록한다.

```sh
.venv/bin/python probe.py fetch --run live
.venv/bin/python probe.py documents --run live
.venv/bin/python probe.py attachments --run live
.venv/bin/python hwp_probe.py
.venv/bin/python probe.py neis --run live
.venv/bin/python probe.py neis-graduation --run live
.venv/bin/python probe.py search --run live
.venv/bin/python probe.py search-filtered --run live
.venv/bin/python model_probe.py --backend ollama
.venv/bin/python summarize.py
```

특정 출처만 다시 요청하려면 다음처럼 실행한다. 전체 수집 결과를 덮어쓰지 않고 별도 결과를 저장한다.

```sh
.venv/bin/python probe.py fetch --run live --only cic_current_detail
```

브라우저 확인은 이 머신에 설치된 `browser-verification` 스킬의 전용 런타임만 사용한다. 사용자 브라우저를 사용하지 않는다. 고정된 런타임이 없는 머신에서는 이 항목은 실행할 수 없다.

```sh
.venv/bin/python browser_probe.py
```

최초 모델 테스트 후 날짜 형식·시간 미기재·학년도 해석 지침을 명확히 한 대표 사례 4건을 추가 확인한다. 초기 결과는 보존한다.

```sh
.venv/bin/python model_probe.py --backend ollama --clarified --only jnu_calendar gist_notice childcare_month_only gwangju_rental_table
```

`model_probe.py --backend openai`는 별도의 선택 경로이며 이번 검증에서 실행하지 않았다. 문서·이미지 원문을 외부 API로 보내는 작업은 자동 승인 검토에서 거절되어 로컬 모델로 검증했다. 해당 외부 전송을 다시 실행하려면 구체적인 전송 범위에 대한 승인이 필요하다.

## 결과 위치와 해석

2026-10-20 실행에서 확인한 핵심 내용:

- HTTP 대상 URL 10개 중 6개에서 기대 내용을 확보했다. 1개는 HTTP 200이지만 게시물 부재 메시지였고, 3개는 TLS/연결 오류였다. PDF 다운로드도 이 URL 수에 포함한다.
- 전용 브라우저에서는 통합 일정 목록과 어린이집 공시 DOM을 읽었다. `FINISHED`, 잔여 프로세스 0으로 정리를 확인했다.
- PDF 2개, HWP 미리보기 1개, 안내 이미지 2개의 실제 다운로드·읽기 경로를 확인했다. PDF 관련 페이지는 렌더링해서 표의 월·행 관계도 시각 확인했다.
- 나이스 API에서 첨단고등학교의 `20270120`, `EVENT_NM=졸업식` 레코드를 받았다. 이 조회에는 이미 알려진 날짜를 사용했다.
- 일반 검색은 전남대 질문에 전북대 출처를 반환했고 어린이집 검색은 출처를 확보하지 못했다. 공식 도메인 필터 요청 2개는 현재 API 설정에서 HTTP 400이었다.
- 로컬 모델은 호출·응답 생성은 가능했지만, 초기 8건과 지침을 명확히 한 4건 모두 날짜·시간·분류·근거를 포함한 전체 검사에서는 오류가 남았다. 일부 사례의 날짜·시간 자체는 맞았으므로 원시 출력과 필드별 실패를 함께 봐야 한다.
- 실제 오류에는 다른 학년도 선택, 시간 미기재 자료에 `00:00`/`10:00` 생성, 행사 시간 대신 대여 시간 선택, 떨어진 문장을 이어 붙인 인용이 있다. 이 결과를 일반 모델 정확도로 확대 해석하지 않는다.

- `artifacts/sandbox/`: 기본 샌드박스의 연결 실패 기록.
- `artifacts/live/fetch/`: 실제 HTTP 응답, 본문, 발견한 링크, 개별 상태.
- `artifacts/live/documents/`: PDF에서 추출한 텍스트.
- `artifacts/live/attachments/`: HWP·안내 이미지와 HWP 미리보기.
- `artifacts/live/browser/`: 브라우저 DOM과 종료·잔여 프로세스 검사.
- `artifacts/live/search_results.json`: 검색 결과와 기관 출처 검사.
- `artifacts/live/search_filtered_results.json`: 도메인 필터 요청의 실제 오류 기록.
- `artifacts/live/neis_graduation_result.json`: 알려진 날짜의 졸업식 레코드 조회.
- `artifacts/live/model/local_results.json`: 초기 로컬 모델 출력과 개별 실패 항목.
- `artifacts/live/model/clarified/local_results.json`: 지침을 명확히 한 대표 사례 결과.
- `artifacts/live/summary.json`: 전체 결과를 모은 JSON.

`search_results_initial.json`은 최초 호출 직후의 원본이다. 당시 `passed`는 도구 실행·인용 반환만 의미했다. 검토에서 전남대 질문에 전북대 출처가 반환된 것을 발견하여 `search_results.json`에서는 `wrong_source`로 분리했다. 수집 성공과 결과 정확성을 혼동하지 않도록 원본과 판정을 함께 남겼다.

이 결과는 선택한 소수 자료에서 각 작업을 실행할 수 있는지 확인한 것이다. 광주 전체 기관 수집률, 모든 파일 형식 지원, 반복 실행 안정성, 모델의 일반 정확도를 의미하지 않는다. 검색 실패, TLS 오류, 원문 삭제, 불명확한 일정은 각각 다른 실패로 유지한다.
