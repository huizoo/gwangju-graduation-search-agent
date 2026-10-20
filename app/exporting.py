"""조회에 필요한 네 열만 담은 독립 HTML을 안전하게 생성한다."""

import base64
import hashlib
from html import escape
from pathlib import Path

from .results import result_rows

STATIC = Path(__file__).with_name("static")
MARKER = '<meta name="schedule-presentation" content="1.0">'


def table_body(rows: list[dict]) -> str:
    """필터용 값과 표시 텍스트를 모두 HTML 이스케이프한다.

    Args:
        rows: 서버의 현재 허용 결과 행.
    Returns:
        네 칸으로 구성된 tbody 내용.
    """
    output = []
    for row in rows:
        name = escape(row["name"])
        if row["url"]:
            name = f'<a href="{escape(row["url"], quote=True)}" target="_blank" rel="noopener noreferrer">{name}</a>'
        if row["event_label"]:
            name += f'<span class="event-label">{escape(row["event_label"])}</span>'
        if row["status"]:
            name += '<span class="cancelled"> · ' + ("취소" if row["status"] == "cancelled" else "미실시") + "</span>"
        cells = [name, escape(row["date"] or "미확인"), escape(row["start_time"] or "미확인"),
                 escape(row["address"] or "미확인")]
        labels = ["학교명", "졸업식 날짜", "실제 시작 시각", "학교 위치(주소)"]
        output.append(f'<tr data-name="{escape(row["name"], quote=True)}" data-date="{row["date"] or ""}">' +
                      "".join(f'<td data-label="{label}">{value}</td>' for label, value in zip(labels, cells)) + "</tr>")
    return "".join(output)


def schedule_markup(rows: list[dict]) -> str:
    """앱과 HTML에서 같은 필터·표·페이지 구조를 제공한다.

    Args:
        rows: 네 열 결과.
    Returns:
        외부 요청 없이 작동하는 일정표 영역.
    """
    return f"""<section data-schedule aria-label="졸업식 일정표">
<div class="schedule-tools">
<label class="schedule-search">학교명 검색<input type="search" data-control="query" placeholder="학교 이름" autocomplete="off"></label>
<label>날짜 선택<select data-control="date-mode"><option value="day">하루</option><option value="range">기간</option></select></label>
<label>날짜 / 시작일<input type="date" data-control="from-date"></label>
<label data-control="to-wrap" hidden>종료일<input type="date" data-control="to-date"></label>
<button type="button" data-control="reset">전체 보기</button>
</div>
<table class="schedule-table"><thead><tr><th scope="col">학교명</th><th scope="col">졸업식 날짜</th><th scope="col">실제 시작 시각</th><th scope="col">학교 위치(주소)</th></tr></thead>
<tbody>{table_body(rows)}</tbody></table>
<p class="empty" data-control="empty" hidden>조건에 맞는 학교가 없습니다.</p>
<div class="schedule-footer"><span data-control="count" role="status" aria-live="polite"></span>
<div class="paging"><button type="button" data-control="previous">이전</button><span data-control="page"></span><button type="button" data-control="next">다음</button></div></div>
</section>"""


def render_export(snapshot: dict, *, include_notes: bool = False) -> str:
    """현재 허용 값으로 학교 주소 중심의 오프라인 결과를 만든다.

    Args:
        snapshot: 조회와 같은 대상 기관·게시 판정.
        include_notes: 이전 API와 호환되는 입력. 단순 결과에는 사적 메모를 싣지 않는다.
    Returns:
        CSS·JS를 내장하고 외부 리소스를 요청하지 않는 HTML.
    """
    rows = result_rows(snapshot)
    style = (STATIC / "schedule.css").read_text(encoding="utf-8")
    script = (STATIC / "schedule.js").read_text(encoding="utf-8")
    script_hash = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    style_hash = base64.b64encode(hashlib.sha256(style.encode()).digest()).decode()
    csp = f"default-src 'none'; script-src 'sha256-{script_hash}'; style-src 'sha256-{style_hash}'; base-uri 'none'; form-action 'none'"
    title = escape(snapshot.get("season", {}).get("label", "광주") + " 졸업식 일정표")
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{MARKER}<meta http-equiv="Content-Security-Policy" content="{escape(csp, quote=True)}">
<title>{title}</title><style>{style}</style></head>
<body><main><h1>{title}</h1>{schedule_markup(rows)}</main><script>{script}</script></body></html>"""


def current_export(path: str | Path) -> bool:
    """이전의 경고 기반 HTML을 새 사용자 결과로 재사용하지 않는다.

    Args:
        path: 저장 파일 경로.
    Returns:
        현재 표현 계약의 표식이 있는 파일인지 여부.
    """
    try:
        with Path(path).open(encoding="utf-8") as stream:
            return MARKER in stream.read(1024)
    except (OSError, UnicodeError):
        return False
