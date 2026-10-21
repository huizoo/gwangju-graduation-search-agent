"""조회·필터·인쇄에 필요한 다섯 열의 독립 HTML을 안전하게 생성한다."""

import base64
import hashlib
from datetime import date
from html import escape
from pathlib import Path

from .results import result_rows

STATIC = Path(__file__).with_name("static")
MARKER = '<meta name="schedule-presentation" content="3.0">'


def schedule_filename(season: dict) -> str:
    """저장·다운로드에 사용할 한국어 기간 파일명을 한곳에서 구성한다.

    Args:
        season: 검증된 search_start와 search_end를 포함한 시즌.
    Returns:
        광주 졸업식 일정표 (YYYY.MM.DD~YYYY.MM.DD).html 형식의 이름.
    Raises:
        ValueError: 저장된 날짜가 올바른 ISO 날짜가 아닌 경우.
    """
    start = date.fromisoformat(season["search_start"]).strftime("%Y.%m.%d")
    end = date.fromisoformat(season["search_end"]).strftime("%Y.%m.%d")
    return f"광주 졸업식 일정표 ({start}~{end}).html"


def table_body(rows: list[dict]) -> str:
    """필터용 값과 표시 텍스트를 모두 HTML 이스케이프한다.

    Args:
        rows: 서버의 현재 허용 결과 행.
    Returns:
        다섯 칸으로 구성된 tbody 내용.
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
        dates = row.get("dates") or ([row["date"]] if row["date"] else [])
        count = row.get("graduation_count")
        cells = [escape("\n".join(dates)), name, escape(row["start_time"] or ""),
                 escape(str(count)) if count is not None else "", escape(row["address"] or "")]
        labels = ["날짜", "학교명", "시간", "졸업인원수", "장소"]
        attrs = {"name": row["name"], "date": row["date"] or "", "dates": " ".join(dates),
                 "district": row.get("district") or ""}
        attributes = " ".join(f'data-{key}="{escape(value, quote=True)}"' for key, value in attrs.items())
        output.append(f"<tr {attributes}>" + "".join(
            f'<td data-label="{label}"' + (' class="date-list"' if label == "날짜" else "") + f'>{value}</td>'
            for label, value in zip(labels, cells)) + "</tr>")
    return "".join(output)


def schedule_markup(rows: list[dict]) -> str:
    """앱과 HTML에서 같은 필터·표·페이지 구조를 제공한다.

    Args:
        rows: 다섯 열 결과.
    Returns:
        외부 요청 없이 작동하는 일정표 영역.
    """
    return f"""<section data-schedule aria-label="졸업식 일정표">
<div class="schedule-tools">
<label class="schedule-search">학교명 검색<input type="search" data-control="query" placeholder="학교 이름" autocomplete="off"></label>
<label>구<select data-control="district"><option value="">전체 구</option></select></label>
<label>날짜<input type="date" data-control="date"></label>
<button type="button" data-control="reset">전체 보기</button>
<button type="button" data-control="print">인쇄</button>
</div>
<table class="schedule-table"><thead><tr><th scope="col">날짜</th><th scope="col">학교명</th><th scope="col">시간</th><th scope="col">졸업인원수</th><th scope="col">장소</th></tr></thead>
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
    """현재 표현 계약의 HTML인지 확인해 오래된 표 구조의 재사용을 막는다.

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
