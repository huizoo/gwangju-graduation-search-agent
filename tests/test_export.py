"""HTML 내보내기의 게시 일관성과 불신 입력 경계를 검사한다."""

from html.parser import HTMLParser

import pytest

from app.web.exporting import render_export


class _Tags(HTMLParser):
    """브라우저에 실행 요소로 전달되는 태그와 속성을 기록한다."""

    def __init__(self):
        """수집 목록을 준비한다."""
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        """파싱한 시작 태그를 보관한다.

        Args:
            tag: HTML 태그 이름.
            attrs: 속성 쌍.
        """
        self.tags.append((tag, dict(attrs)))


def _event(*, eligible=True, ready=True, name="광주 확인학교"):
    """동일 게시 결과를 소비하는지 확인할 최소 행사 스냅샷을 만든다.

    Args:
        eligible: 일반 조회 허용 여부.
        ready: 판매 준비 허용 여부.
        name: 기관 이름.
    Returns:
        내보내기용 행사.
    """
    return {
        "id": "one", "institution": {"id": "school", "name": name, "district": "북구", "type": "high",
            "address": "광주 북구 학교로 1", "source_checked_at": "2026-10-20T01:00:00Z",
            "homepage_url": "https://school.example", "official_channels": [{"url": "https://school.example",
            "verified_by_url": "https://directory.example", "verified_at": "2026-10-01T00:00:00Z"}]},
        "title": "졸업식", "fields": {
            "event_date": {"effective_value": {"precision": "day", "date": "2099-12-31"},
                           "effective_basis": "phone_confirmed",
                           "manual": {"note": "사적 메모 비밀", "source_url": "javascript:alert(1)"}},
            "start_time": {"effective_basis": "official"},
        },
        "publication": {
            "policy_version": "4.0", "calendar_eligible": eligible,
            "calendar_date": "2027-01-08" if eligible else None,
            "calendar_time": "10:30" if eligible else None,
            "calendar_venue": "확인 강당" if eligible else None,
            "current_date_allowed": ready, "sales_ready": ready,
            "field_permissions": {
                "event_date": {"confirmed_at": "2026-10-20T01:00:00Z",
                               "current_allowed": ready, "recheck_status": "failed" if not ready else "succeeded"},
                "start_time": {"confirmed_at": "2026-10-20T01:00:00Z",
                               "current_allowed": ready, "recheck_status": "not_attempted"},
            },
        },
    }


def test_current_result_masks_held_values_without_exposing_internal_metadata():
    """과거 확인값·내부 상태를 출력하지 않고 같은 행의 해당 칸을 비운다."""
    html = render_export({"events": [_event(ready=False), _event(eligible=False)],
                          "content_revision": 7, "generated_at": "2026-10-20T01:00:00Z"})
    assert "2027-01-08" not in html and "2099-12-31" not in html
    assert "미확인" not in html
    assert "과거 확인값" not in html and "재확인 실패" not in html
    assert "revision" not in html and "판매 준비" not in html
    assert "2026-10-20" not in html
    assert "광주 북구 학교로 1" in html and "확인 강당" not in html
    assert "사적 메모 비밀" not in html


def test_untrusted_values_cannot_create_active_content():
    """스크립트 종료 문자열과 속성 탈출을 실행 요소로 만들지 않는다."""
    event = _event(name='</script><img src=x onerror="alert(1)">')
    event["audience"] = "초등학교 6학년 <svg onload='alert(9)'>"
    event["campus_label"] = "본교"
    event["documents"] = [
        {"title": '<svg onload="alert(2)">', "display_url": "https://school.example/notice?KEY=secret&a=1",
         "status": "ok", "source_kind": "html", "authority": "official",
         "reading": {"institution_match": "matched", "content_match": "expected"}},
        {"title": "위험", "display_url": "javascript:alert(2)"},
        {"title": "인증", "display_url": "https://user:password@school.example/"},
    ]
    html = render_export({"events": [event]})
    parser = _Tags()
    parser.feed(html)
    assert sum(tag == "script" for tag, _ in parser.tags) == 1
    assert not any(tag in {"img", "svg", "iframe", "object"} for tag, _ in parser.tags)
    assert not any(key.startswith("on") for _, attrs in parser.tags for key in attrs)
    assert "javascript:" not in html and "password" not in html and "secret" not in html
    assert 'href="https://school.example"' in html
    assert "https://school.example/notice" not in html
    assert "fetch(" not in html and "https://cdn" not in html
    assert "script-src &#x27;sha256-" in html
    assert "초등학교 6학년" not in html  # 단일 행사는 불필요한 대상 설명을 반복하지 않는다.


def test_private_notes_stay_internal_even_for_legacy_include_flag():
    """구 API의 메모 옵션도 새 단순 결과에 사적 내용을 넣지 않는다."""
    event = _event()
    event["fields"]["event_date"]["manual"]["note"] = "<script>private()</script>"
    html = render_export({"events": [event]}, include_notes=True)
    assert "private()" not in html


def test_refuses_unknown_policy_and_unpublished_events():
    """구정책이나 판정 누락을 현재 일정표로 제공하지 않는다."""
    with pytest.raises(ValueError):
        render_export({"policy_version": "3.0", "events": [_event()]})
    with pytest.raises(ValueError):
        render_export({"events": [{"title": "후보"}]})
    event = _event()
    event["publication"]["policy_version"] = "3.0"
    with pytest.raises(ValueError):
        render_export({"events": [event]})


def test_empty_snapshot_explains_missing_schedule():
    """확인된 일정이 없는 경우 빈 정상 결과를 설명한다."""
    html = render_export({"events": [], "overview": {"eligible_institutions": 12}})
    assert "조건에 맞는 학교가 없습니다" in html
    assert "등록 기관" not in html


def test_partial_time_unknown_keeps_date_and_five_columns():
    """시간만 현재 사용할 수 없으면 정상 날짜와 주소를 유지한다."""
    event = _event()
    event["publication"]["field_permissions"]["start_time"]["current_allowed"] = False
    html = render_export({"events": [event]})
    parser = _Tags()
    parser.feed(html)
    assert "2027-01-08" in html and "10:30" not in html
    assert sum(tag == "th" for tag, _ in parser.tags) == 5
    assert sum(tag == "td" for tag, _ in parser.tags) == 5


def test_api_link_falls_back_to_school_and_missing_target_is_in_same_table():
    """API 근거와 행사장 대신 공식 학교 링크와 확인 주소를 사용한다."""
    event = _event()
    event["documents"] = [{"display_url": "https://open.neis.go.kr/hub/SchoolSchedule", "source_kind": "official_api"}]
    other = {**event["institution"], "id": "other", "name": "일정 없는 학교", "source_checked_at": None}
    html = render_export({"events": [event], "targets": [event["institution"], other]})
    assert "open.neis" not in html and 'href="https://school.example"' in html
    assert "일정 없는 학교" in html
    assert html.count('data-label="학교명"') == 2


def test_missing_homepage_is_not_replaced_by_fake_none_url():
    """홈페이지도 미확인인 학교에는 허위 링크를 만들지 않는다."""
    event = _event()
    event["institution"]["homepage_url"] = "https://none/"
    event["institution"]["official_channels"][0]["url"] = "https://none/"
    html = render_export({"events":[event]})
    assert 'href=' not in html
    assert "https://none" not in html and "광주 확인학교" in html
