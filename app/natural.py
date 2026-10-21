"""사적 입력을 외부에 보내지 않고 확인 진술과 불확실한 부분을 나눠 기록한다."""

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .domain import ApiError, dumps, fingerprint, new_id, utcnow, validate
from .storage import _audit, empty_automatic

KST = ZoneInfo("Asia/Seoul")
DATE = re.compile(r"(?:(\d{4})\s*(?:년|[-.])\s*)?(\d{1,2})\s*(?:월|[-.])\s*(\d{1,2})\s*일?")
TIME = re.compile(r"(오전|오후)?\s*(\d{1,2})(?:시(?:\s*(반|\d{1,2})분?)?|:(\d{2}))")


def school_matches(text: str, institutions: list[dict]) -> list[dict]:
    """공식 이름과 통상 축약명을 찾고 병설 기관의 긴 이름을 우선한다.

    Args:
        text: 자연어 요청.
        institutions: 등록 기관 목록.
    Returns:
        이름에 명시적으로 대응하는 기관. 같은 축약명의 복수 캠퍼스는 모두 남긴다.
    """
    hits = []
    compact = re.sub(r"\s+", "", text)
    for item in institutions:
        name = re.sub(r"\s+", "", item["name"])
        aliases = {name}
        for full, short in (("초등학교", "초"), ("중학교", "중"), ("고등학교", "고"), ("대학교", "대")):
            if name.endswith(full):
                aliases.add(name[:-len(full)] + short)
        for alias in aliases:
            for match in re.finditer(re.escape(alias), compact):
                if name.endswith("초등학교") and re.match(r"(?:등학교)?(?:병설|부설)유치원", compact[match.end():]):
                    continue
                hits.append((match.start(), match.end(), item))
    # 긴 기관명에 포함된 부모 학교 이름만 제거한다. 서로 떨어진 두 학교는 합치지 않는다.
    return list({item["id"]: item for start, end, item in hits if not any(
        a <= start and b >= end and b - a > end - start for a, b, _ in hits)}.values())


def clock_value(text: str) -> tuple[str | None, str | None]:
    """시각 형식과 오전·오후의 모호함을 구분한다.

    Args:
        text: 시각이 포함된 구절.
    Returns:
        정규 시각과 필요한 짧은 질문.
    """
    found = list(TIME.finditer(text))
    if not found:
        return None, None
    if len(found) != 1:
        return None, "졸업식이 실제로 시작하는 시각은 몇 시인가요?"
    ampm, raw_hour, raw_minute, colon = found[0].groups()
    hour, minute = int(raw_hour), 30 if raw_minute == "반" else int(raw_minute or colon or 0)
    if minute > 59 or hour > 23 or (ampm and not 1 <= hour <= 12):
        return None, "시각을 다시 적어 주세요. 예: 오전 10시 30분"
    if not ampm and not colon and 1 <= hour <= 12:
        return None, "오전인가요, 오후인가요? 예: 오전 10시"
    if ampm:
        hour = hour % 12 + (12 if ampm == "오후" else 0)
    return f"{hour:02d}:{minute:02d}", None


def event_day(text: str, season: dict) -> tuple[str | None, str | None]:
    """생략 연도는 조사 기간 안에서 유일할 때만 채운다.

    Args:
        text: 행사 날짜 구절.
        season: 목표 조사 기간.
    Returns:
        유효한 날짜와 필요한 질문.
    """
    found = list(DATE.finditer(text))
    if not found:
        return None, None
    if len(found) != 1:
        return None, "졸업식 날짜를 하나로 알려 주세요."
    year, month, day = found[0].groups()
    years = [int(year)] if year else range(int(season["search_start"][:4]), int(season["search_end"][:4]) + 1)
    valid = []
    for y in years:
        try:
            value = date(y, int(month), int(day)).isoformat()
            if season["search_start"] <= value <= season["search_end"]:
                valid.append(value)
        except ValueError:
            pass
    if len(valid) != 1:
        return None, f"날짜를 확인해 주세요. 이번 조사 기간은 {season['search_start']}부터 {season['search_end']}까지예요."
    return valid[0], None


def parse_turn(text: str, season: dict, moment: str, *, confirmation_reply=False) -> dict:
    """문장 파싱을 사실 검증과 분리하고 명시한 확인시점만 정규화한다.

    Args:
        text: 원래 입력 문장.
        season: 조사 기간.
        moment: 실제 수신 시각. '방금'이라는 명시적 상대 표현에만 사용한다.
        confirmation_reply: 직전 질문이 실제 확인시점에 관한 것인지 여부.
    Returns:
        정규 필드·확인 진술·질문. 원문은 호출자가 별도로 보존한다.
    """
    result = {"questions": [], "non_ceremony": False}
    if re.search(r"졸업식\s*(?:준비\s*)?(?:설명회|예행연습|준비\s*모임)", text):
        return {"intent": "record", "non_ceremony": True,
                "questions": ["설명회·준비 일정과 졸업식 본식은 달라요. 본식 날짜와 시각을 알려 주세요."]}
    if re.search(r"아마|추측|추정|예상|같아|같아요|것\s*같|확실하지|모르겠|일지도", text):
        result["intent"] = "guess"
    elif re.search(r"조사해|알아봐|찾아봐|찾아줘|확인해\s*줘", text) and not re.search(r"확인했|확인한|통화했", text):
        result["intent"] = "research"
    else:
        result["intent"] = "record"
    method = ("phone_confirmed" if re.search(r"전화|통화", text) else
              "document_confirmed" if re.search(r"안내문|문서|공지|홈페이지", text) else None)
    if method and re.search(r"확인했|확인한|전화했|통화했|문의했|직접\s*봤", text):
        result["basis"] = method
    # 행사 날짜와 전화한 날짜가 한 문장에 섞일 때 확인 구절을 먼저 분리한다.
    parts = re.split(r"[,.。]\s*(?!\d)|\n", text)
    confirmation_parts = [p for p in parts if re.search(r"전화|통화|확인했|확인한|직접\s*봤", p)
                          and re.search(r"방금|오늘|어제|\d{4}\s*(?:년|[-.])", p)]
    if confirmation_reply:
        confirmation_parts = [text]
    confirmation_text = " ".join(confirmation_parts)
    now = datetime.fromisoformat(moment).astimezone(KST)
    if "방금" in confirmation_text:
        result.update(confirmed_at=moment, confirmation_expression="방금")
    elif confirmation_text:
        clock, issue = clock_value(confirmation_text)
        confirmed_day = None
        if "오늘" in confirmation_text:
            confirmed_day = now.date()
        elif "어제" in confirmation_text:
            confirmed_day = now.date() - timedelta(days=1)
        else:
            matches = list(DATE.finditer(confirmation_text))
            if len(matches) == 1 and matches[0][1]:
                try:
                    confirmed_day = date(*map(int, matches[0].groups()))
                except ValueError:
                    pass
        if confirmed_day and clock:
            value = datetime.combine(confirmed_day, datetime.strptime(clock, "%H:%M").time(), KST)
            if value <= now:
                result.update(confirmed_at=value.isoformat(), confirmation_expression=confirmation_text)
            else:
                result["questions"].append("확인시각이 미래로 적혔어요. 실제로 확인한 때를 알려 주세요.")
        elif issue:
            result["questions"].append(issue)
    event_text = " ".join(p for p in parts if p not in confirmation_parts) if confirmation_parts else text
    # '...학교 강당이래. 방금 학교에 전화...'처럼 마침표 없는 한국어 문장도 분리한다.
    event_text = re.split(r"(?:방금|오늘|어제)\s*(?:학교에?\s*)?(?:전화|통화)", event_text)[0]
    day, issue = event_day(event_text, season)
    if day:
        result["date"] = day
    if issue:
        result["questions"].append(issue)
        result["invalid_date"] = True
    elif day:
        result["invalid_date"] = False
    clock, issue = clock_value(event_text)
    if clock:
        # 입장·사진·식전 공연은 본식이라고 명시하지 않으면 시각 질문으로 남긴다.
        if re.search(r"입장|식전|촬영|준비|설명회|등교", event_text) and not re.search(r"(?:본식|실제\s*시작)", event_text):
            result["questions"].append("그 시각이 졸업식 본식 시작 시각인가요? 본식 시각을 알려 주세요.")
        else:
            result["start_time"] = clock
    if issue:
        result["questions"].append(issue)
    venue = re.search(r"(학교\s*강당|[가-힣A-Za-z0-9 ]{1,30}(?:강당|체육관|대강당))", event_text)
    if venue:
        result["venue"] = venue[0].strip()
    if re.search(r"취소(?:됐|되었|했|입니다)|미실시|실시하지\s*않", event_text):
        result["ceremony_status"] = "cancelled" if "취소" in event_text else "not_held"
    elif re.search(r"졸업식|학위수여식", event_text):
        result["ceremony_status"] = "scheduled"
        result["event_type"] = "degree_award_ceremony" if "학위수여식" in event_text else "graduation_ceremony"
    audience = re.search(r"[가-힣A-Za-z]+학과|학부|대학원|(?:초등학교|중학교|고등학교|전공과|유치원)\s*\d학년", event_text)
    sequence = re.search(r"제\s*(\d+)\s*회", event_text)
    campus = re.search(r"[가-힣A-Za-z]+캠퍼스", event_text)
    academic_years = set(re.findall(r"(20\d{2})\s*학년도", event_text))
    terms = set(re.findall(r"(?:(?<![가-힣])|(?<=학년도))(전기|후기)(?=\s|학위\s*수여식|졸업식|$)", event_text))
    if len(academic_years) == 1:
        result["academic_year"] = int(next(iter(academic_years)))
    elif len(academic_years) > 1:
        result["questions"].append("어느 학년도의 졸업식인지 알려 주세요.")
    if len(terms) == 1:
        result["ceremony_term"] = next(iter(terms))
    elif len(terms) > 1:
        result["questions"].append("전기와 후기 중 어느 졸업식인지 알려 주세요.")
    if audience:
        result["audience"] = audience[0]
    if sequence:
        result["sequence"] = sequence[1]
    if campus:
        result["campus_label"] = campus[0]
    return result


class NaturalRecorder:
    """질문 중인 입력도 로컬 DB에 남기고 검증 가능한 필드만 기존 행사에 보완한다."""

    def __init__(self, service):
        """기관·행사 저장 서비스만 사용하며 모델 제공자를 초기화하지 않는다."""
        self.service = service

    def record(self, payload: dict) -> dict:
        """입력·정규값·추가 질문·수정 이력을 하나의 트랜잭션으로 저장한다.

        Args:
            payload: NaturalRecordRequest.
        Returns:
            기록 결과와 짧은 추가 질문. 원문은 HTML 결과에 노출하지 않는다.
        Raises:
            ApiError: 없는 대화, 다른 시즌, 오래된 revision 또는 잘못된 입력.
        """
        import json
        data = validate("NaturalRecordRequest", payload)
        service = self.service
        moment = utcnow()
        with service.store.transaction(True) as conn:
            season = service._season(conn, data["season_id"])
            identity = data.get("conversation_id") or new_id()
            previous = conn.execute("SELECT * FROM natural_records WHERE id=?", (identity,)).fetchone()
            if data.get("conversation_id") and not previous:
                raise ApiError("not_found", "이전 입력을 찾을 수 없습니다.", 404)
            if previous and (previous["season_id"] != season["id"] or previous["revision"] != data["expected_revision"]):
                raise ApiError("revision_conflict", "기록이 바뀌었습니다. 입력 내용을 다시 열어 주세요.", 409)
            turns = json.loads(previous["turns_json"]) if previous else []
            state = json.loads(previous["state_json"]) if previous else {}
            text = data["text"].strip()
            last_questions = state.get("questions", [])
            confirmation_reply = any("언제" in q or "확인시각" in q for q in last_questions)
            parsed = parse_turn(text, season, moment, confirmation_reply=confirmation_reply)
            turns.append({"text": text, "received_at": moment, "parsed": parsed})
            all_text = "\n".join(t["text"] for t in turns)
            for key, value in parsed.items():
                if key not in {"questions", "intent"}:
                    state[key] = value
            prior_intent = state.get("intent")
            state["intent"] = parsed["intent"]
            if prior_intent == "research" and parsed["intent"] == "record" and not any(
                key in parsed for key in ("basis", "date", "start_time", "confirmed_at")
            ):
                state["intent"] = "research"
            questions = list(parsed["questions"])
            schools = school_matches(text, service.list_institutions()["items"])
            if len(schools) > 1:
                campuses = [school for school in schools if school.get("campus_label") and school["campus_label"] in text]
                if len(campuses) == 1:
                    schools = campuses
            if not schools and state.get("institution_id"):
                schools = [service._institution(conn, state["institution_id"])]
            if len(schools) != 1:
                questions.insert(0, "어느 학교인가요? 학교의 전체 이름과 캠퍼스를 알려 주세요.")
                state.pop("institution_id", None)
            else:
                state["institution_id"] = schools[0]["id"]
            event = None
            scope_ambiguous = False
            if len(schools) == 1 and state["intent"] == "record":
                school = schools[0]
                matches = [e for e in service._events(conn, season["id"]) if e["institution"]["id"] == school["id"]]
                fixed = next((e for e in matches if e["id"] == state.get("event_id")), None)
                if state.get("academic_year") is not None:
                    matches = [e for e in matches if e.get("academic_year") in (None, state["academic_year"])]
                if state.get("ceremony_term"):
                    matches = [e for e in matches if e.get("ceremony_term") in (None, state["ceremony_term"])]
                if state.get("event_id"):
                    matches = [e for e in matches if e["id"] == state["event_id"]]
                    if fixed and not matches:
                        scope_ambiguous = True
                        questions.append("앞서 기록한 행사와 학년도 또는 전후기가 달라요. 어느 행사를 수정할지 확인해 주세요.")
                if matches and any(state.get(k) for k in ("audience","sequence","campus_label")):
                    scoped = [e for e in matches if
                        (not state.get("audience") or state["audience"] == e.get("audience")) and
                        (not state.get("campus_label") or state["campus_label"] in {e.get("campus_label"),school.get("campus_label")}) and
                        (not state.get("sequence") or re.search(r"제\s*"+re.escape(state["sequence"])+r"\s*회",e["title"]))]
                    if scoped:
                        matches = scoped
                    elif not re.search(r"같은\s*행사|기존\s*행사", text):
                        scope_ambiguous = True
                        questions.append("적어 주신 과정·회차·캠퍼스가 기존 행사와 같나요? '기존 행사' 또는 '별도 행사'라고 알려 주세요.")
                        if re.search(r"별도\s*행사",text):
                            matches = []
                            scope_ambiguous = False
                            questions.pop()
                if len(matches) > 1:
                    dated = [e for e in matches if state.get("date") and e["publication"]["current_date_allowed"]
                             and state["date"] in (e["fields"]["event_date"]["effective_value"].get("dates")
                                                   or [e["publication"]["calendar_date"]])]
                    if len(dated) == 1:
                        matches = dated
                if len(matches) > 1:
                    narrowed = [e for e in matches if any(label and label in all_text
                                for label in (e.get("audience"), e.get("campus_label")))]
                    if len(narrowed) == 1:
                        matches = narrowed
                    else:
                        questions.append("어느 행사인가요? " + " / ".join(dict.fromkeys(e.get("audience") or e["title"] for e in matches))[:180])
                if len(matches) == 1:
                    event = matches[0]
                    state["event_id"] = event["id"]
            if state["intent"] == "research":
                # 서로 다른 이름을 명시한 복수 조사 요청과 같은 이름의 캠퍼스
                # 식별 모호성을 구분한다. 수기 사실 입력은 여전히 한 기관씩 받는다.
                if len(schools) > 1 and len({s["name"] for s in schools}) == len(schools):
                    questions = [q for q in questions if not q.startswith("어느 학교")]
                status = "question" if questions else "research_request"
                message = "조사할 학교를 확인할게요." if questions else "선택한 학교를 조사할게요."
            elif state["intent"] == "guess":
                status, message = "noted", "추측으로 기록했어요. 확인된 일정에는 반영하지 않았어요."
            else:
                if not state.get("basis"):
                    questions.append("직접 확인하셨나요? 전화 또는 문서로 확인한 방법을 알려 주세요.")
                if not state.get("confirmed_at"):
                    questions.append("실제로 언제 확인하셨나요? 예: 오늘 오전 9시, 방금")
                day = state.get("date") or (event and event["publication"]["calendar_date"] if event and event["publication"]["current_date_allowed"] else None)
                if state.get("start_time") and not day:
                    questions.append("어느 날짜의 졸업식인가요?")
                if not day and not state.get("ceremony_status") in {"cancelled", "not_held"}:
                    if not any("날짜" in q or "조사 기간" in q for q in questions):
                        questions.append("졸업식 날짜를 알려 주세요.")
                fields = {}
                if state.get("date"):
                    fields["event_date"] = {"precision": "day", "date": state["date"], "month": None}
                if state.get("start_time") and day:
                    fields["start_time"] = state["start_time"]
                if state.get("venue") and day:
                    fields["venue"] = state["venue"]
                if state.get("ceremony_status"):
                    fields["ceremony_status"] = state["ceremony_status"]
                elif event and fields:
                    fields["ceremony_status"] = "scheduled"
                operations, conflicts = [], []
                can_save = (not scope_ambiguous and not state.get("non_ceremony") and not state.get("invalid_date")
                            and len(schools) == 1 and (event or not matches) and state.get("basis")
                            and state.get("confirmed_at") and (day or state.get("ceremony_status") in {"cancelled", "not_held"}))
                for name, value in fields.items():
                    current = event["fields"][name] if event else None
                    if current:
                        manual, automatic = current["manual"], current["automatic"]
                        saved_value = manual["value"] if manual else automatic["value"]
                        different = saved_value is not None and saved_value != value
                        unresolved = current["review_required"] or current["has_web_difference"]
                        if (different or unresolved) and not re.search(r"다시\s*확인|재확인", text):
                            conflicts.append(name)
                            continue
                        if different or unresolved:
                            # 충돌 인지 뒤 확인시점과 근거가 필요하다. 처음 제안된 변경을 먼저
                            # 이력으로 남긴 뒤 질문하며 기존 정상 수기를 조용히 덮어쓰지 않는다.
                            detected = automatic.get("conflict_detected_at") or state.get("conflict_detected_at")
                            if not detected or not state.get("confirmed_at") or datetime.fromisoformat(state["confirmed_at"]) < datetime.fromisoformat(detected):
                                conflicts.append(name)
                                continue
                        if saved_value == value and manual and manual["confirmed_at"] == state.get("confirmed_at"):
                            continue
                    op = {"field": name, "action": "set", "value": value, "basis": state.get("basis", "user_recorded"),
                          "confirmed_at": state.get("confirmed_at"), "note": all_text, "source_url": None,
                          "resolves_fingerprint": None}
                    if name in {"start_time", "venue"}:
                        op["bound_date"] = day
                    if current and (current["review_required"] or current["has_web_difference"]):
                        op["resolves_fingerprint"] = current["automatic"]["fingerprint"]
                    operations.append(op)
                changed = []
                if can_save and event and conflicts:
                    for name in conflicts:
                        current = event["fields"][name]
                        old = current["automatic"]
                        digest = fingerprint({"record_id": identity, "field": name, "proposed": fields[name]})
                        if old.get("fingerprint") == digest:
                            continue
                        held = empty_automatic()
                        held.update(assessment_id=new_id(), status="conflict", fingerprint=digest,
                                    checked_at=moment, conflict_detected_at=moment,
                                    evidence_ids=old["evidence_ids"], reason_codes=["conflict", "manual_difference"])
                        conn.execute("INSERT INTO assessments(id,event_id,field,status,automatic_json,created_at) VALUES(?,?,?,'conflict',?,?)",
                                     (held["assessment_id"], event["id"], name, dumps(held), moment))
                        _audit(conn, "event", event["id"], "natural_conflict", before=old,
                               after={"record_id": identity, "proposed": fields[name],
                                      "assessment_fingerprint": digest}, field=name,
                               summary="직접 확인 진술과 기존 값의 차이 보류")
                    service.store.touch(conn, season["id"])
                    event = service._project(conn, event["id"], season_id=season["id"])[0]
                if can_save and operations:
                    if event:
                        if any(event.get(key) is None and state.get(key) is not None
                               for key in ("academic_year", "ceremony_term")):
                            service.enrich_event_identity(
                                conn, event["id"], state.get("academic_year"), state.get("ceremony_term"))
                        updated = service.apply_overrides(event["id"], {"expected_revision": event["revision"], "operations": operations})
                    else:
                        updated = service.create_event({
                            "institution_id": schools[0]["id"], "season_id": season["id"],
                            "title": (f"제{state['sequence']}회 " if state.get("sequence") else "") + ("학위수여식" if state.get("event_type")=="degree_award_ceremony" else "졸업식"),
                            "event_type": state.get("event_type", "graduation_ceremony"), "audience": state.get("audience"),
                            "academic_year": state.get("academic_year"), "ceremony_term": state.get("ceremony_term"),
                            "campus_label": state.get("campus_label", schools[0].get("campus_label")),
                            "allow_separate_event": False, "operations": operations})
                    service.link_event_periods(conn, updated["id"])
                    state["event_id"] = updated["id"]
                    changed = [op["field"] for op in operations]
                if conflicts:
                    state.setdefault("conflict_detected_at", moment)
                    labels = {"event_date": "날짜", "start_time": "시각", "venue": "장소", "ceremony_status": "개최 여부"}
                    questions.append("기존 " + "·".join(labels[name] for name in conflicts) + "과 달라요. 다시 확인한 값과 실제 확인시각을 알려 주세요.")
                status = "question" if questions else "saved"
                message = ("확실한 내용은 기록했어요." if changed else "입력 내용을 보관했어요.") if questions else "기존 행사에 기록했어요." if event else "기록했어요."
                state["changed_fields"] = changed
            state["questions"] = list(dict.fromkeys(questions))
            revision = previous["revision"] + 1 if previous else 1
            conn.execute("INSERT INTO natural_records VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,turns_json=excluded.turns_json,state_json=excluded.state_json,updated_at=excluded.updated_at",
                         (identity, season["id"], revision, dumps(turns), dumps(state), previous["created_at"] if previous else moment, moment))
            _audit(conn, "natural_record", identity, "input", after={"revision": revision, "normalized": state}, summary="자연어 원문·정규값·추가 질문 보관")
            return {"id": identity, "revision": revision, "status": status, "message": message,
                    "questions": state["questions"], "event_id": state.get("event_id"),
                    "institution_ids": [s["id"] for s in schools], "changed_fields": state.get("changed_fields", [])}
