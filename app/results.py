"""내부 이력에서 현재 허용 필드만 골라 단순 사용자 결과를 구성한다."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .identity import scope
from .providers import official_channel

PRESENTATION_VERSION = "1.0"


def human_url(value: object) -> str | None:
    """인증정보와 기계용 API 주소를 사용자 링크에서 제외한다.

    Args:
        value: 공개 출처 URL.
    Returns:
        사람이 읽을 HTTP(S) 주소 또는 None.
    """
    try:
        raw = str(value or "")
        url = urlsplit(raw)
        if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password:
            return None
        if any(ord(c) < 32 for c in raw):
            return None
        path = url.path.lower()
        if (url.hostname == "open.neis.go.kr" or path.endswith((".xml", ".json"))
                or any(part in {"api", "apis", "rest", "hub"} for part in path.split("/"))):
            return None
        query = parse_qsl(url.query, keep_blank_values=True)
        if any(k.lower() in {"format", "type", "output"} and v.lower() in {"json", "xml"} for k, v in query):
            return None
        sensitive = {"key", "api_key", "apikey", "servicekey", "token", "access_token", "authorization"}
        return urlunsplit((url.scheme, url.netloc, url.path,
                          urlencode([(k, v) for k, v in query if k.lower() not in sensitive]), ""))
    except ValueError:
        return None


def school_link(institution: dict, documents: list[dict]) -> str | None:
    """공식 읽기 페이지를 우선하고 확인된 학교 홈페이지로 대체한다.

    Args:
        institution: 공식 관계가 저장된 기관.
        documents: 해당 행사에 연결된 근거.
    Returns:
        사용자용 공식 URL. API 근거는 내부에만 남긴다.
    """
    for document in sorted(documents, key=lambda d: d.get("retrieved_at", ""), reverse=True):
        url = human_url(document.get("display_url"))
        reading = document.get("reading", {})
        if (url and document.get("source_kind") == "html" and document.get("authority") == "official"
                and document.get("status") == "ok" and reading.get("institution_match") == "matched"
                and reading.get("content_match") == "expected" and official_channel(institution, url)):
            return url
    home = human_url(institution.get("homepage_url"))
    return home if home and official_channel(institution, home) else None


def result_rows(snapshot: dict) -> list[dict]:
    """요청 기관마다 현재 허용 값 또는 미확인 행을 만든다.

    Args:
        snapshot: 같은 SQLite 읽기 시점의 대상 기관·행사·근거.
    Returns:
        날짜·시간순의 네 열용 행. 검증 상세·사적 메모는 포함하지 않는다.
    Raises:
        ValueError: 게시 판정이 없거나 지원하지 않는 정책일 때.
    """
    if snapshot.get("publication_policy_version", snapshot.get("policy_version", "4.0")) != "4.0":
        raise ValueError("지원하지 않는 내부 게시 정책입니다.")
    events = snapshot.get("events", [])
    for event in events:
        if event.get("publication", {}).get("policy_version") != "4.0":
            raise ValueError("서버 게시 판정이 필요합니다.")
    institutions = snapshot.get("targets")
    if institutions is None:
        institutions = list({e["institution"]["id"]: e["institution"] for e in events}.values())
    result = []
    for institution in institutions:
        selected = [e for e in events if e["institution"]["id"] == institution["id"]]
        groups = {}
        for event in selected:
            groups.setdefault(scope(event, institution), []).append(event)
        if len(groups) > 1:
            # 과거의 대상 미분리 후보는 '별도 행사'의 증거가 아니다. 실제 과정
            # 행이 있으면 지원도 수기도 없는 generic 후보를 추가 행사로 표시하지 않는다.
            groups = {key: group for key, group in groups.items() if not (
                key[1] in {"", "졸업 대상 학생"} and all(
                    not e["publication"]["current_date_allowed"] and e["publication"].get("state") != "excluded"
                    and not any(f.get("manual") for f in e.get("fields", {}).values()) for e in group))}
        if not groups:
            groups[None] = []
        for key, group in groups.items():
            date_values, time_values, statuses = set(), set(), set()
            documents = []
            for event in group:
                pub = event["publication"]
                permissions = pub["field_permissions"]
                if pub.get("current_date_allowed") and pub.get("calendar_date"):
                    date_values.add(pub["calendar_date"])
                    if permissions["start_time"]["current_allowed"] and pub.get("calendar_time"):
                        time_values.add((pub["calendar_date"], pub["calendar_time"]))
                status_field = event.get("fields", {}).get("ceremony_status", {})
                manual = status_field.get("manual")
                automatic = status_field.get("automatic", {})
                # excluded는 서버가 상태 근거를 검증한 결과다. 후보의 취소 단어를 표시하지 않는다.
                if pub.get("state") == "excluded":
                    state = manual.get("value") if manual else automatic.get("value")
                    if state in {"cancelled", "not_held"}:
                        statuses.add(state)
                documents.extend(snapshot.get("documents", {}).get(event["id"], event.get("documents", [])))
            status = "cancelled" if "cancelled" in statuses else "not_held" if statuses else None
            day = next(iter(date_values)) if len(date_values) == 1 and not status else None
            time = next(iter(time_values))[1] if len(time_values) == 1 and next(iter(time_values))[0] == day else None
            address = institution.get("address") if institution.get("source_checked_at") else None
            # 수기 프로필 수정은 원래의 공식 주소 증명을 자동 상속하지 않는다.
            if "address" in institution.get("profile_overrides", {}):
                address = None
            if group and group[0].get("campus_label") not in (None, institution.get("campus_label")):
                address = None
            label = ""
            if len(groups) > 1 and group:
                label = " · ".join(filter(None, [group[0].get("audience"), group[0].get("campus_label"),
                                                  f"제{key[3]}회" if key[3] else None]))
            elif institution.get("campus_label"):
                label = institution["campus_label"]
            result.append({"institution_id": institution["id"], "event_ids": [e["id"] for e in group],
                           "name": institution["name"], "event_label": label,
                           "date": day, "start_time": time, "address": address or None,
                           "url": school_link(institution, documents), "status": status})
    return sorted(result, key=lambda r: (r["date"] or "9999", r["start_time"] or "99:99", r["name"], r["event_label"]))
