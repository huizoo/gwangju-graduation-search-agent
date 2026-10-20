"""HTML 내보내기의 게시 일관성과 불신 입력 경계를 검사한다."""

from html.parser import HTMLParser

import pytest

from app.exporting import render_export


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
        "id": "one", "institution": {"name": name, "district": "북구", "type": "high"},
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


def test_uses_publication_and_keeps_recheck_warning():
    """후보값을 출력하지 않고 과거 확인값과 현재 보류를 함께 보존한다."""
    html = render_export({"events": [_event(ready=False), _event(eligible=False)],
                          "content_revision": 7, "generated_at": "2026-10-20T01:00:00Z"})
    assert "2027-01-08" in html and "2099-12-31" not in html
    assert "과거 확인값 · 현재 사용 보류" in html
    assert "재확인 실패" in html
    assert "보류·제외 기록: 1건" in html
    assert 'data-sales="false"' in html
    assert "2026-10-20 10:00 KST" in html
    assert "저장 내용 버전(revision): 7" in html
    assert "사적 메모 비밀" not in html


def test_untrusted_values_cannot_create_active_content():
    """스크립트 종료 문자열과 속성 탈출을 실행 요소로 만들지 않는다."""
    event = _event(name='</script><img src=x onerror="alert(1)">')
    event["audience"] = "초등학교 6학년 <svg onload='alert(9)'>"
    event["campus_label"] = "본교"
    event["documents"] = [
        {"title": '<svg onload="alert(2)">', "display_url": "https://school.example/notice?KEY=secret&a=1"},
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
    assert "https://school.example/notice?a=1" in html
    assert "fetch(" not in html and "https://cdn" not in html
    assert "script-src &#x27;sha256-" in html
    assert "초등학교 6학년" in html and "본교" in html


def test_private_notes_require_explicit_inclusion():
    """사적 메모는 명시적으로 선택할 때만 이스케이프해서 포함한다."""
    event = _event()
    event["fields"]["event_date"]["manual"]["note"] = "<script>private()</script>"
    html = render_export({"events": [event]}, include_notes=True)
    assert "&lt;script&gt;private()&lt;/script&gt;" in html
    assert "사적 수기 메모가 포함" in html
    assert "<script>private()" not in html


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
    assert "일반 일정표로 제공할 확인값이 없습니다" in html
    assert "등록 대상 12개 기관" in html
    assert "등록 기관 기준" in html
