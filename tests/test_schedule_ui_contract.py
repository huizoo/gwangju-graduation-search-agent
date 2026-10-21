"""앱과 저장 HTML의 다섯 열·필터·인쇄 계약을 검사한다."""

from html.parser import HTMLParser
from pathlib import Path

from app.exporting import MARKER, current_export, schedule_markup


class ScheduleParser(HTMLParser):
    """요소의 속성과 텍스트를 실행 없이 읽는다."""

    def __init__(self, html):
        """일정표 HTML을 읽어 태그와 표 머리를 수집한다.

        Args:
            html: 검사할 일정표 문자열.
        """
        super().__init__()
        self.tags = []
        self.headings = []
        self.in_heading = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        """태그를 수집하고 표 머리의 시작을 표시한다.

        Args:
            tag: 태그명.
            attrs: 속성 목록.
        """
        self.tags.append((tag, dict(attrs)))
        self.in_heading = tag == "th"

    def handle_data(self, data):
        """표 머리의 표시 문구를 수집한다.

        Args:
            data: 요소의 텍스트.
        """
        if self.in_heading:
            self.headings.append(data)

    def handle_endtag(self, tag):
        """표 머리의 끝을 표시한다.

        Args:
            tag: 닫힌 태그명.
        """
        if tag == "th":
            self.in_heading = False


def _row(**changes):
    """공통 사용자 결과 행을 구성한다.

    Args:
        **changes: 검사에서 덮어쓸 값.
    Returns:
        현재 결과 계약의 행.
    """
    return {"institution_id": "school", "event_ids": ["event"], "name": "광주학교",
            "event_label": "", "date": "2027-01-07", "dates": ["2027-01-07", "2027-01-08"],
            "start_time": None, "graduation_count": 0, "address": None, "district": "북구",
            "url": "https://school.example", "status": None, **changes}


def test_app_and_export_share_columns_and_independent_single_day_filters():
    """앱과 파일은 같은 열 순서와 독립된 세 필터를 제공한다."""
    app = Path("app/static/index.html").read_text()
    for html in (app, schedule_markup([_row()])):
        parser = ScheduleParser(html)
        assert parser.headings == ["날짜", "학교명", "시간", "졸업인원수", "장소"]
        controls = {attrs["data-control"]: (tag, attrs) for tag, attrs in parser.tags if "data-control" in attrs}
        assert controls["query"][1]["type"] == "search"
        assert controls["district"][0] == "select"
        assert controls["date"][1]["type"] == "date"
        assert "date-mode" not in controls and "to-date" not in controls
        assert "print" in controls


def test_export_retains_all_event_days_and_zero_count_with_blank_unknown_cells():
    """복수 날짜를 필터 데이터에 남기고 0명과 미확인을 구별한다."""
    html = schedule_markup([_row()])
    assert 'data-dates="2027-01-07 2027-01-08"' in html
    assert 'data-district="북구"' in html
    assert 'data-label="졸업인원수">0</td>' in html
    assert 'data-label="시간"></td>' in html and 'data-label="장소"></td>' in html
    assert "시간 찾기" not in html and "fetch(" not in html
    assert "미확인" not in html


def test_export_attributes_and_count_cannot_inject_markup():
    """지역·복수 날짜·인원도 HTML 경계를 벗어나지 않는다."""
    parser = ScheduleParser(schedule_markup([_row(
        district='북구" onclick="oops()', dates=['<img src=x>'], graduation_count="<svg onload=x>")]))
    assert not any(tag in {"img", "svg"} for tag, _ in parser.tags)
    assert not any(name.startswith("on") for _, attrs in parser.tags for name in attrs)


def test_presentation_version_requires_new_output(tmp_path):
    """이전 네 열 파일은 현재 결과로 재사용하지 않는다.

    Args:
        tmp_path: 격리 출력 폴더.
    """
    old = tmp_path / "old.html"
    old.write_text('<meta name="schedule-presentation" content="1.0">')
    assert not current_export(old)
    new = tmp_path / "new.html"
    new.write_text(MARKER)
    assert current_export(new)
