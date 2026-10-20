"""같은 게시 스냅샷으로 오프라인에서 열 수 있는 안전한 HTML을 만든다."""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime
from html import escape
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo


TYPE_NAMES = {
    "childcare": "어린이집", "kindergarten": "유치원", "elementary": "초등학교",
    "middle": "중학교", "high": "고등학교", "special_school": "특수학교",
    "other_school": "기타학교", "university": "대학교", "junior_college": "전문대학",
}
BASIS_NAMES = {
    "official": "공식 자료", "phone_confirmed": "전화 확인",
    "document_confirmed": "문서 직접 확인", "user_recorded": "직접 기록", "none": "미확인",
}
RECHECK_NAMES = {
    "not_attempted": "재확인 미수행", "succeeded": "재확인 성공",
    "failed": "재확인 실패 · 현재 사용 보류", "not_applicable": "재확인 대상 아님",
}
_STYLE = """
:root{font-family:system-ui,-apple-system,"Malgun Gothic",sans-serif;color:#1d322c;background:#f5f7f4}
*{box-sizing:border-box}body{margin:0;font-size:18px;line-height:1.6}main{max-width:1300px;margin:auto;padding:32px}
h1{font-size:32px;line-height:1.3;margin:0 0 8px}h2{font-size:22px}.muted,small{color:#566760}
.notice{padding:16px 20px;background:#fff5da;border-left:5px solid #a66b00;margin:20px 0}
.meta{margin:10px 0}.tools{display:flex;flex-wrap:wrap;gap:14px;margin:24px 0;align-items:end}
label{display:grid;gap:5px;font-weight:650}input,select,button{font:inherit;border:1px solid #a8b8b0;border-radius:8px;padding:10px;background:#fff;color:inherit}
button{cursor:pointer;background:#1d654e;color:#fff;border-color:#1d654e}#filter-count{margin-left:auto}
.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;background:#fff}th,td{text-align:left;vertical-align:top;padding:16px;border-bottom:1px solid #dce5df}
th{background:#e6eee8;white-space:nowrap}td:first-child{min-width:180px}.schedule{white-space:nowrap;font-weight:750}
.badge{display:inline-block;border-radius:6px;font-size:15px;padding:3px 9px;background:#e5f0e8;color:#175238}
.hold{background:#fff0d2;color:#805400}.source{display:block;font-size:15px;overflow-wrap:anywhere}a{color:#075f8b}
details{margin-top:7px}summary{cursor:pointer;color:#175c47}.check{font-size:15px;margin-top:8px;min-width:210px}
.private{white-space:pre-wrap;overflow-wrap:anywhere}footer{margin-top:26px;font-size:16px}tr[hidden]{display:none!important}
@media(max-width:650px){main{padding:18px}h1{font-size:27px}.tools>*{width:100%}#filter-count{margin-left:0}}
@media print{body{font-size:11px;background:#fff}main{padding:0;max-width:none}.tools,.no-print{display:none!important}h1{font-size:22px}th,td{padding:8px}.check{font-size:10px;min-width:0}a{color:inherit}tr{break-inside:avoid}.notice{padding:8px}details>div{display:block!important}}
"""
_SCRIPT = """
(() => {
  const query = document.getElementById('query');
  const district = document.getElementById('district');
  const kind = document.getElementById('kind');
  const view = document.getElementById('view');
  const rows = Array.from(document.querySelectorAll('tbody tr[data-search]'));
  function filter() {
    let count = 0;
    for (const row of rows) {
      const visible = row.dataset.search.includes(query.value.trim().toLocaleLowerCase())
        && (!district.value || row.dataset.district === district.value)
        && (!kind.value || row.dataset.type === kind.value)
        && (view.value !== 'sales' || row.dataset.sales === 'true');
      row.hidden = !visible;
      if (visible) count++;
    }
    document.getElementById('filter-count').textContent = `${count}건 표시 / 전체 ${rows.length}건`;
  }
  for (const element of [query, district, kind, view]) element.addEventListener('input', filter);
  document.getElementById('print').addEventListener('click', () => window.print());
  filter();
})();
"""


def _text(value: object) -> str:
    """값을 HTML 텍스트와 속성에 안전하게 넣을 문자열로 바꾼다.

    Args:
        value: 표시할 값.
    Returns:
        따옴표까지 이스케이프한 문자열.
    """
    return escape("" if value is None else str(value), quote=True)


def _safe_url(value: object) -> str | None:
    """공개 출처 링크에서 실행 스킴, 인증정보와 인증 쿼리를 제거한다.

    Args:
        value: 저장된 공개 URL.
    Returns:
        안전한 HTTP(S) URL 또는 허용할 수 없을 때 None.
    """
    try:
        url = urlsplit(str(value or ""))
        if url.scheme.lower() not in {"https", "http"} or not url.hostname:
            return None
        if url.username or url.password or any(ord(c) < 32 for c in str(value)):
            return None
        sensitive = {"key", "api_key", "apikey", "servicekey", "token", "access_token", "authorization"}
        query = urlencode([(k, v) for k, v in parse_qsl(url.query, keep_blank_values=True)
                           if k.lower() not in sensitive])
        return urlunsplit((url.scheme, url.netloc, url.path, query, ""))
    except ValueError:
        return None


def _moment(value: object) -> str:
    """확인 시각을 한국 표준시로 표시하고 미상은 그대로 남긴다.

    Args:
        value: ISO 시각 또는 None.
    Returns:
        한국 표준시 표시 문자열.
    """
    if not value:
        return "확인 시점 미상"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return str(value)
        return parsed.astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M KST")
    except ValueError:
        return str(value)


def _field_summary(event: dict, field: str) -> str:
    """게시 판정에 저장된 필드의 확인방법·시점·재확인 상태를 표시한다.

    Args:
        event: 이미 게시 판정이 끝난 행사.
        field: 날짜 또는 실제 시작 시각 필드.
    Returns:
        안전한 HTML 조각.
    """
    permission = event["publication"].get("field_permissions", {}).get(field, {})
    projection = event.get("fields", {}).get(field, {})
    basis = BASIS_NAMES.get(projection.get("effective_basis"), "확인방법 기록 없음")
    recheck = RECHECK_NAMES.get(permission.get("recheck_status"), "재확인 상태 미상")
    allowed = "현재 사용 가능" if permission.get("current_allowed") else "현재 사용 보류"
    label = "날짜" if field == "event_date" else "시각"
    return (f'<div class="check"><strong>{label}</strong> · {_text(basis)}<br>'
            f'{_text(_moment(permission.get("confirmed_at")))}<br>'
            f'{_text(recheck)} · {_text(allowed)}</div>')


def _source_links(event: dict, documents: list[dict]) -> str:
    """사적 메모나 원문을 포함하지 않고 공개 출처 링크만 출력한다.

    Args:
        event: 수기 근거 URL을 포함할 수 있는 행사.
        documents: 행사와 연결된 문서 목록.
    Returns:
        중복을 제거한 안전한 출처 HTML.
    """
    links: dict[str, str] = {}
    for document in documents:
        url = _safe_url(document.get("display_url"))
        if url:
            links[url] = document.get("title") or "공개 근거 원문"
    for projection in event.get("fields", {}).values():
        manual = projection.get("manual") or {}
        url = _safe_url(manual.get("source_url"))
        if url:
            links.setdefault(url, "직접 확인한 문서")
    if not links:
        return '<span class="source muted">온라인 근거 링크 없음 · 확인방법과 시점을 확인하세요.</span>'
    return "".join(f'<a class="source" href="{_text(url)}" target="_blank" rel="noopener noreferrer">'
                   f'{_text(title)}</a>' for url, title in links.items())


def render_export(snapshot: dict, include_notes: bool = False) -> str:
    """하나의 Publication 스냅샷을 자급형 HTML 일정표로 렌더링한다.

    Args:
        snapshot: season, events, overview, content_revision, generated_at을 담은 스냅샷.
            events는 EventProjection 배열 또는 items를 가진 페이지다. documents는
            행사 ID를 키로 하는 문서 목록이며 생략할 수 있다.
        include_notes: 사용자가 명시적으로 선택한 경우에만 사적 수기 메모를 포함한다.
    Returns:
        외부 자원 없이 필터와 인쇄가 동작하는 HTML.
    Raises:
        ValueError: 게시 정책 버전이 4.0이 아니거나 행사 판정이 누락된 경우.
    """
    overview = snapshot.get("overview", {})
    policy = snapshot.get("publication_policy_version", snapshot.get("policy_version",
                          overview.get("publication_policy_version", "4.0")))
    if policy != "4.0":
        raise ValueError("현재 일정표는 게시 정책 4.0의 스냅샷만 지원합니다.")
    events = snapshot.get("events", snapshot.get("items", []))
    if isinstance(events, dict):
        events = events.get("items", [])
    if any(not isinstance(e.get("publication"), dict) for e in events):
        raise ValueError("게시 판정이 없는 행사로 일정표를 생성할 수 없습니다.")
    if any(e["publication"].get("policy_version") != "4.0" for e in events):
        raise ValueError("행사의 게시 정책 버전이 스냅샷과 다릅니다.")
    # 내보내기는 재판정하지 않는다. 같은 스냅샷의 일반 조회 허용 결과만 소비한다.
    visible = [event for event in events if event["publication"].get("calendar_eligible")]
    visible.sort(key=lambda e: (e["publication"].get("calendar_date") or "",
                               e["publication"].get("calendar_time") or "99:99",
                               e.get("institution", {}).get("name", "")))
    rows = []
    for event in visible:
        publication, institution = event["publication"], event.get("institution", {})
        ready = publication.get("sales_ready") is True
        badge = '<span class="badge">판매 준비 가능</span>' if ready else '<span class="badge hold">추가 확인 필요</span>'
        if not publication.get("current_date_allowed"):
            badge += '<br><span class="badge hold">과거 확인값 · 현재 사용 보류</span>'
        documents = event.get("documents") or snapshot.get("documents", {}).get(event.get("id"), [])
        notes = ""
        if include_notes:
            private = [(key, (value.get("manual") or {}).get("note"))
                       for key, value in event.get("fields", {}).items()]
            labels = {"event_date": "날짜", "start_time": "시각", "venue": "장소", "ceremony_status": "행사 상태"}
            notes = "".join(f'<div class="private">{_text(labels.get(key, key))}: {_text(note)}</div>'
                            for key, note in private if note)
            if notes:
                notes = f'<details><summary>포함하기로 선택한 수기 메모</summary><div>{notes}</div></details>'
        search = " ".join(str(v or "") for v in [
            institution.get("name"), institution.get("address"), event.get("title"),
            event.get("audience"), event.get("campus_label"), institution.get("campus_label"),
            publication.get("calendar_date"), publication.get("calendar_venue"),
        ]).lower()
        identity = " · ".join(str(value) for value in (
            event.get("audience"), event.get("campus_label") or institution.get("campus_label"),
        ) if value)
        rows.append(
            f'<tr data-search="{_text(search)}" data-district="{_text(institution.get("district"))}" '
            f'data-type="{_text(institution.get("type"))}" data-sales="{str(ready).lower()}">'
            f'<td><strong>{_text(institution.get("name"))}</strong><br>{_text(event.get("title"))}'
            f'<br><small>{_text(identity)}</small>'
            f'<br><small>{_text(institution.get("district"))} · '
            f'{_text(TYPE_NAMES.get(institution.get("type"), institution.get("type", "")))}</small>'
            f'<br><small>{_text(institution.get("address"))}</small>'
            f'<br><small>{_text(institution.get("phone") or "전화번호 미등록")}</small></td>'
            f'<td class="schedule">{_text(publication.get("calendar_date"))}<br>'
            f'{_text(publication.get("calendar_time") or "시각 미확인")}</td>'
            f'<td>{_text(publication.get("calendar_venue") or "장소 미확인")}</td>'
            f'<td>{badge}{_field_summary(event, "event_date")}{_field_summary(event, "start_time")}</td>'
            f'<td>{_source_links(event, documents)}{notes}</td></tr>'
        )
    season = snapshot.get("season", {})
    title = f'{season.get("label", "광주 졸업식")} · 졸업식 일정표'
    generated = snapshot.get("generated_at") or datetime.now(ZoneInfo("Asia/Seoul")).isoformat()
    revision = snapshot.get("content_revision", snapshot.get("revision", overview.get("content_revision", 0)))
    coverage = "공식 목록 확보 완료" if overview.get("registry_complete") else "등록 기관 기준 · 공식 전체 목록 확보 미완료"
    registry_rows = []
    for registry in overview.get("registry_snapshots", []):
        source = registry.get("source", {})
        scope_types = ", ".join(TYPE_NAMES.get(kind, kind) for kind in source.get("scope_types", []))
        districts_text = ", ".join(source.get("scope_districts", []))
        source_url = _safe_url(source.get("official_url"))
        source_link = (f'<a href="{_text(source_url)}" target="_blank" rel="noopener noreferrer">공식 목록 출처</a>'
                       if source_url else "출처 링크 없음")
        scope_status = "해당 범위 전체 확보" if source.get("coverage_complete") else "일부 확보"
        registry_rows.append(
            f'<li><strong>{_text(source.get("source_name", "공식 목록"))}</strong> · {_text(scope_status)}<br>'
            f'{_text(scope_types)} / {_text(districts_text)} · 기준일 {_text(source.get("as_of") or "미상")}<br>'
            f'등록 {_text(registry.get("loaded_count", 0))}, 명부 연결 {_text(registry.get("linked_count", 0))}, '
            f'제외 {_text(registry.get("rejected_count", 0))}, 명부 전체 행수 {_text(source.get("expected_total") if source.get("expected_total") is not None else "미상")} · {source_link}</li>'
        )
    registry_html = (f'<details><summary>공식 기관 목록 확보 범위</summary><ul>{"".join(registry_rows)}</ul></details>'
                     if registry_rows else '<p class="muted">공식 기관 목록 확보 이력이 없습니다.</p>')
    held_count = len(events) - len(visible)
    types = "".join(f'<option value="{_text(key)}">{_text(label)}</option>' for key, label in TYPE_NAMES.items())
    districts = "".join(f'<option>{district}</option>' for district in ["동구", "서구", "남구", "북구", "광산구"])
    digest = base64.b64encode(hashlib.sha256(_SCRIPT.encode()).digest()).decode()
    csp = f"default-src 'none'; script-src 'sha256-{digest}'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
    notes_notice = " 이 파일에는 선택한 사적 수기 메모가 포함되어 있습니다." if include_notes else ""
    body = "".join(rows) or '<tr><td colspan="5">이 스냅샷에 일반 일정표로 제공할 확인값이 없습니다.</td></tr>'
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{_text(csp)}">
<title>{_text(title)}</title><style>{_STYLE}</style></head><body><main>
<h1>{_text(title)}</h1>
<p class="muted">꽃다발 판매 준비를 위한 날짜·실제 시작 시각과 확인 근거</p>
<p class="meta">생성: {_text(_moment(generated))} · 저장 내용 버전(revision): {_text(revision)} · 게시 정책: {_text(policy)}<br>
{_text(coverage)} · 등록 대상 {_text(overview.get("eligible_institutions", "미상"))}개 기관</p>
<p>현재 날짜 확인 {_text(overview.get("date_confirmed_institutions", 0))}개 기관 ·
현재 시작 시각 확인 {_text(overview.get("time_confirmed_institutions", 0))}개 기관 ·
추가 확인 필요 {_text(overview.get("review_institutions", 0))}개 기관</p>
{registry_html}
<div class="notice">이 파일은 저장 당시의 일정입니다. 프로그램 없이 열 수 있으며 자동으로 갱신되지 않습니다.
판매 준비 목록은 현재 사용 가능한 날짜와 실제 시작 시각이 함께 확인된 행사만 표시합니다.
일반 일정표에는 재확인에 실패한 과거 확인값이 경고와 함께 남을 수 있습니다.
일정표에 포함하지 않은 보류·제외 기록: {held_count}건.{_text(notes_notice)}</div>
<div class="tools"><label>기관·날짜 검색<input id="query" type="search" placeholder="기관 이름 또는 날짜"></label>
<label>지역<select id="district"><option value="">전체 지역</option>{districts}</select></label>
<label>기관 종류<select id="kind"><option value="">전체 종류</option>{types}</select></label>
<label>목록<select id="view"><option value="calendar">일반 일정표</option><option value="sales">판매 준비 가능</option></select></label>
<button id="print" type="button">인쇄하기</button><span id="filter-count" role="status">{len(visible)}건</span></div>
<div class="table-wrap"><table><thead><tr><th>기관 · 행사</th><th>날짜 · 실제 시작</th><th>장소</th>
<th>검증 상태 · 확인 시점</th><th>확인 근거</th></tr></thead><tbody>{body}</tbody></table></div>
<footer>미확인 시각은 임의로 채우지 않습니다. 출처 링크는 인터넷 연결이 있을 때 열립니다.
전화·문서 확인의 시점은 실제 확인한 시각이며 입력한 시각과 다를 수 있습니다.</footer>
</main><script>{_SCRIPT}</script></body></html>"""
