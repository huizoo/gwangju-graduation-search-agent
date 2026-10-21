# 검토한 공식 명부 자산

2026-10-20 공개 공식 사이트에서 확보한 자료의 배포 사본과 정규 파일이다. 목록 기준일과 취득일을 구분하며 모든 자료의 운영상태를 추정하지 않는다.

- `imports/`: 앱의 준비된 공식 명부에서 제공하는 네 개의 `RegistryImport` 파일.
- 상위 폴더의 HTML·BIN·CSV: 정규 행·학교코드·홈페이지 연결을 검토하는 고정 사본.
- `homepage-authority-provenance.json`: 원문 행과 공식 홈페이지 관계의 대응 근거.
- [검토 manifest](../../docs/evidence/non-school-registry.json): 원문 해시와 수집 범위. 코드가 해시를 고정하므로 파일만 바꿔 승인 관계를 추가할 수 없다.

공개 저장소에는 `gen-kindergarten-directory.html`의 CSRF 값 2개와 `hana-summary.html`의 원장명 1개를 비식별화한 사본을 제공한다. 기관 행·코드·주소·전화·홈페이지는 바꾸지 않았다. manifest의 `public_copy`는 취득 원문 해시와 정제 범위를 구분하며, 정제 시점을 원문 취득 시점으로 사용하지 않는다. 정제 전 파일은 로컬 `runtime/git-publication-cleanup/original-assets/`에 별도로 보관하고 Git에 포함하지 않는다.

유치원 원본 분석은 명부 256행 확보를 기록하지만 기준일이 없다. 앱용 파일은 전체 확보를 주장하지 않는다. 대학은 대학원 행을 제외한 18곳이며 별도 캠퍼스의 완전성이 미확인이다. 어린이집 250행은 일부 명부이고 대표 공시 1곳을 별도로 제공한다.

파일 해시 검증은 최신성을 증명하지 않는다. 새 명부를 승인할 때에는 원문·정규 행·관계·manifest·서버 검증과 검사를 함께 갱신해야 한다. 상세 범위는 [명부 설명](../../docs/registry-coverage.md)을 참조한다.
