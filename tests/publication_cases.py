"""인계에서 보존한 게시 양성·반례를 주입한 운영 함수에 적용한다."""
from copy import deepcopy
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "tests" / "fixtures" / "contracts"
NOW = "2026-10-20T07:00:00Z"
SEASON = {"id": "00000000-0000-0000-0000-000000000003", "academic_year": 2026, "search_start": "2026-12-01", "search_end": "2027-03-31"}
SCHEMA = json.loads((ROOT / "app" / "core" / "domain.schema.json").read_text())
PUBLICATION_SCHEMA = {"$schema": SCHEMA["$schema"], "$defs": SCHEMA["$defs"], "$ref": "#/$defs/Publication"}
VALIDATOR = Draft202012Validator(PUBLICATION_SCHEMA, format_checker=FormatChecker())


def load(name):
    """가상 기관으로 구성한 고정 예제를 읽는다.

    Args:
        name: 확장자를 제외한 예제 이름.
    Returns:
        저장된 예제 객체.
    """
    return json.loads((EXAMPLES / f"{name}.example.json").read_text())


def base():
    """각 반례가 서로 영향을 주지 않는 독립 기준 문맥을 만든다.

    Returns:
        행사·시즌·게시 검증 문맥.
    """
    event, document, observation = load("publication-ready"), load("document"), load("observation")
    context = {"documents": {document["id"]: document}, "observations": {observation["id"]: observation}, "registered_rules": {"explicit_notice": "1.0.0"}, "now": NOW, "event_blockers": [], "use_fixture_rules": True}
    return event, deepcopy(SEASON), context


def manual(event, field, value, confirmed="2026-10-20T06:55:00Z", bound=None):
    """지정한 필드에 가상 확인값을 넣어 보호·충돌 조건을 검사한다.

    Args:
        event: 시험 중인 행사.
        field: 확인값을 지정할 필드.
        value: 가상 확인값.
        confirmed: 실제 확인시점 조건으로 쓸 가상 시각.
        bound: 시각·장소가 종속되는 행사일.
    """
    snapshot = event["fields"][field]
    snapshot["automatic"]["conflict_detected_at"] = snapshot["automatic"]["checked_at"]
    snapshot["manual"] = {"id": "00000000-0000-0000-0000-000000000099", "mode": "set", "value": value, "basis": "phone_confirmed", "confirmed_at": confirmed, "recorded_at": NOW, "note": "실제 확인을 표현하는 가상 시험 기록", "bound_date": bound, "acknowledged_fingerprint": None, "resolved_fingerprint": snapshot["automatic"]["fingerprint"], "source_url": None, "source_document_id": None}


def run(calculate_publication, validate_supplied_publication):
    """실제 게시 함수에 기존 양성·반례를 적용하고 결과를 반환한다.

    Args:
        calculate_publication: 운영 게시값 계산 함수.
        validate_supplied_publication: 제공된 게시값의 위조 검사 함수.
    Returns:
        통과한 사례 이름과 개수.
    """
    def calculate(event, season, context):
        """계산 결과의 스키마와 날짜·시간 사용 관계를 함께 검사한다."""
        publication = calculate_publication(event, season, context)
        VALIDATOR.validate(publication)
        assert publication["current_time_allowed"] <= publication["current_date_allowed"]
        assert publication["sales_ready"] == bool(publication["current_date_allowed"] and publication["current_time_allowed"])
        return publication

    checks = []
    def passed(name):
        checks.append(name)

    e, s, c = base()
    p = calculate(e, s, c)
    assert p["sales_ready"] and p["calendar_date"] == "2027-01-07" and p["calendar_time"] == "11:00"
    e["publication"] = deepcopy(p)
    validate_supplied_publication(e, s, c)
    passed("positive_date_and_time")

    e, s, c = base()
    e["fields"]["start_time"]["automatic"]["status"] = "missing"
    p = calculate(e, s, c)
    assert p["current_date_allowed"] and p["state"] == "date_only" and not p["sales_ready"]
    passed("positive_date_only")

    for reason in ("event_match_required", "cancelled_notice"):
        e, s, c = base()
        e["reason_codes"] = [reason]
        e["review_required"] = False
        p = calculate(e, s, c)
        assert p["state"] == "hold" and not p["calendar_eligible"]
        passed("counterexample_" + reason)

    e, s, c = base()
    c["event_blockers"] = ["event_match_required"]
    assert calculate(e, s, c)["state"] == "hold"
    passed("server_context_event_blocker")

    e, s, c = base()
    manual(e, "start_time", "10:00", confirmed=None, bound="2027-01-07")
    e["fields"]["start_time"]["review_required"] = True
    p = calculate(e, s, c)
    assert p["calendar_date"] and not p["calendar_time"] and not p["sales_ready"]
    passed("counterexample_manual_missing_confirmed_at")

    e, s, c = base()
    manual(e, "venue", "본관 강당")
    p = calculate(e, s, c)
    assert p["calendar_venue"] == "본관 강당" and p["sales_ready"]
    e["publication"] = deepcopy(p)
    e["publication"]["calendar_venue"] = "다른 장소"
    try:
        validate_supplied_publication(e, s, c)
    except ValueError:
        pass
    else:
        raise AssertionError("altered venue was accepted")
    assert calculate(e, s, c)["calendar_venue"] == "본관 강당"
    passed("counterexample_altered_published_venue")

    e, s, c = base()
    manual(e, "start_time", "10:00", bound="2027-01-07")
    e["fields"]["start_time"]["review_required"] = True
    e["fields"]["start_time"]["has_web_difference"] = True
    e["fields"]["start_time"]["reason_codes"] = ["manual_difference"]
    e["review_required"] = True
    p = calculate(e, s, c)
    assert p["sales_ready"] and p["calendar_time"] == "10:00"
    passed("positive_manual_reconfirmation")

    for mutation in ("old_time", "old_fingerprint", "notification_only", "missing_note", "future_time"):
        e, s, c = base()
        manual(e, "start_time", "10:00", bound="2027-01-07")
        snapshot = e["fields"]["start_time"]
        snapshot["review_required"] = True
        m = snapshot["manual"]
        if mutation == "old_time": m["confirmed_at"] = "2026-10-19T06:00:00Z"
        if mutation == "old_fingerprint": m["resolved_fingerprint"] = "outdated"
        if mutation == "notification_only": m.update(resolved_fingerprint=None, acknowledged_fingerprint=snapshot["automatic"]["fingerprint"])
        if mutation == "missing_note": m["note"] = ""
        if mutation == "future_time": m["confirmed_at"] = "2026-10-21T06:00:00Z"
        p = calculate(e, s, c)
        assert p["current_date_allowed"] and not p["calendar_time"]
        passed("manual_" + mutation)

    e, s, c = base()
    e["review_required"] = True
    e["fields"]["start_time"]["review_required"] = True
    e["fields"]["start_time"]["reason_codes"] = ["conflict"]
    p = calculate(e, s, c)
    assert p["state"] == "date_only" and p["current_date_allowed"]
    passed("time_conflict_preserves_date")

    e, s, c = base()
    manual(e, "event_date", {"precision": "day", "date": "2027-01-08", "month": None})
    e["fields"]["event_date"]["review_required"] = True
    p = calculate(e, s, c)
    assert p["calendar_date"] == "2027-01-08" and p["calendar_time"] is None and p["current_date_allowed"]
    passed("date_change_invalidates_bound_time_only")

    e, s, c = base()
    document = next(iter(c["documents"].values()))
    document["recheck"] = {"status": "failed", "attempted_at": "2026-10-20T06:58:00Z", "reason_codes": ["connection_reset"]}
    p = calculate(e, s, c)
    assert p["calendar_date"] and p["calendar_time"] and not p["sales_ready"]
    assert p["field_permissions"]["event_date"]["recheck_status"] == "failed"
    passed("failed_recheck_preserves_history_restricts_current")

    for name in ("ceremony_status", "event_date", "start_time"):
        manual(e, name, e["fields"][name]["automatic"]["value"], bound="2027-01-07" if name == "start_time" else None)
    assert calculate(e, s, c)["sales_ready"]
    passed("phone_reconfirmation_independent_of_failed_source")

    e, s, c = base()
    document = next(iter(c["documents"].values()))
    observation = next(iter(c["observations"].values()))
    document["recheck"] = {"status": "failed", "attempted_at": "2026-10-20T06:58:00Z", "reason_codes": ["connection_reset"]}
    second_document, second_observation = deepcopy(document), deepcopy(observation)
    second_document["id"] = "00000000-0000-0000-0000-000000000044"
    second_document["recheck"] = {"status": "succeeded", "attempted_at": "2026-10-20T06:59:00Z", "reason_codes": []}
    second_observation["id"] = "00000000-0000-0000-0000-000000000055"
    second_observation["document_id"] = second_document["id"]
    c["documents"][second_document["id"]] = second_document
    c["observations"][second_observation["id"]] = second_observation
    for field in ("ceremony_status", "event_date", "start_time"):
        automatic = e["fields"][field]["automatic"]
        automatic["evidence_ids"].append(second_observation["id"])
        proof = automatic["value_check"]
        proof["evidence_ids"].append(second_observation["id"])
        proof["document_ids"].append(second_document["id"])
        proof["document_hashes"][second_document["id"]] = second_document["content_hash"]
    assert calculate(e, s, c)["sales_ready"]
    passed("alternative_valid_support_retains_current_permission")
    second_observation["disposition"] = "rejected"
    p = calculate(e, s, c)
    assert p["calendar_date"] and not p["sales_ready"]
    passed("rejected_alternative_cannot_restore_permission")

    mutations = ("rejected_observation", "wrong_institution", "wrong_event", "wrong_season", "wrong_hash", "wrong_rule_version", "wrong_field_link", "fabricated_quote", "wrong_normalized_time", "cross_unit_quote", "unverified_authority", "preview_only", "empty_registry", "fixture_rules_disabled")
    for mutation in mutations:
        e, s, c = base()
        document = next(iter(c["documents"].values()))
        observation = next(iter(c["observations"].values()))
        if mutation == "rejected_observation": observation["disposition"] = "rejected"
        if mutation == "wrong_institution": document["institution_id"] = "other"
        if mutation == "wrong_event": observation["event_id"] = "other"
        if mutation == "wrong_season": observation["season_id"] = "other"
        if mutation == "wrong_hash":
            for f in ("ceremony_status", "event_date", "start_time"): e["fields"][f]["automatic"]["value_check"]["document_hashes"][document["id"]] = "0" * 64
        if mutation == "wrong_rule_version":
            for f in ("ceremony_status", "event_date", "start_time"): e["fields"][f]["automatic"]["value_check"]["rule_version"] = "99.0"
        if mutation == "wrong_field_link": e["fields"]["event_date"]["automatic"]["value_check"]["field"] = "venue"
        if mutation == "fabricated_quote": observation["candidate"]["context_evidence"][0]["quote"] = "원문에 없는 합성 문장"
        if mutation == "wrong_normalized_time":
            observation["candidate"]["start_time"] = "10:30"
            e["fields"]["start_time"]["automatic"]["value"] = "10:30"
        if mutation == "cross_unit_quote":
            extra = deepcopy(document["segments"][0]); extra.update(id="s2", unit_id="other-ceremony")
            document["segments"].append(extra)
            observation["candidate"]["date_evidence"][0]["segment_id"] = "s2"
        if mutation == "unverified_authority": document["authority_evidence"] = None
        if mutation == "preview_only": document["reading"]["scope"] = "hwp_preview"
        if mutation == "empty_registry": c["registered_rules"] = {}
        if mutation == "fixture_rules_disabled": c["use_fixture_rules"] = False
        p = calculate(e, s, c)
        if mutation == "wrong_normalized_time": assert p["calendar_date"] and p["calendar_time"] is None
        else: assert p["calendar_date"] is None
        passed(mutation)

    e, s, c = base()
    month = {"precision": "month", "date": None, "month": "2027-01"}
    e["fields"]["event_date"]["automatic"]["value"] = month
    observation = next(iter(c["observations"].values()))
    observation["candidate"]["event_date"] = deepcopy(month)
    observation["candidate"]["date_evidence"][0]["quote"] = "2027년 1월"
    p = calculate(e, s, c)
    assert not p["calendar_eligible"] and not p["current_date_allowed"] and not p["sales_ready"]
    passed("month_only_never_fabricates_day")

    e, s, c = base()
    e["fields"]["event_date"]["effective_value"]["date"] = "2027-01-30"
    e["fields"]["start_time"]["effective_value"] = "09:00"
    p = calculate(e, s, c)
    assert p["calendar_date"] == "2027-01-07" and p["calendar_time"] == "11:00"
    passed("untrusted_effective_values_do_not_control_output")

    e, s, c = base()
    manual(e, "venue", "별관", bound="2027-01-08")
    p = calculate(e, s, c)
    assert p["calendar_venue"] is None and p["sales_ready"]
    passed("date_bound_venue_mismatch_hides_venue_only")

    e, s, c = base()
    doc = next(iter(c["documents"].values()))
    for field in ("ceremony_status", "event_date", "start_time"):
        manual(e, field, e["fields"][field]["automatic"]["value"], bound="2027-01-07" if field == "start_time" else None)
        e["fields"][field]["manual"].update(basis="document_confirmed", source_url=doc["display_url"])
    doc["recheck"] = {"status":"failed", "attempted_at":"2026-10-20T06:58:00Z", "reason_codes":["connection_reset"]}
    p = calculate(e, s, c)
    assert p["calendar_time"] == "11:00" and not p["sales_ready"]
    passed("manual_document_recheck_failure_preserves_history_only")
    for field in ("ceremony_status", "event_date", "start_time"):
        e["fields"][field]["manual"]["confirmed_at"] = "2026-10-20T06:59:00Z"
    assert calculate(e, s, c)["sales_ready"]
    passed("new_human_document_confirmation_after_failed_attempt")
    doc["status"] = "content_mismatch"
    assert not calculate(e, s, c)["calendar_eligible"]
    passed("known_invalid_manual_document_not_restored_by_new_timestamp")

    for kind in ("degree_award_ceremony", "graduation_and_completion"):
        e, s, c = base()
        e["event_type"] = kind
        next(iter(c["observations"].values()))["candidate"]["event_type"] = kind
        assert calculate(e, s, c)["sales_ready"]
        passed("positive_event_type_" + kind)
    for kind in ("completion_ceremony", "unknown"):
        e, s, c = base()
        e["event_type"] = kind
        next(iter(c["observations"].values()))["candidate"]["event_type"] = kind
        assert not calculate(e, s, c)["calendar_eligible"]
        passed("excluded_event_type_" + kind)

    for closed_on, expected in (("2027-01-01", False), ("2027-02-01", True), (None, True)):
        e, s, c = base()
        e["institution"].update(operation_status="closed_confirmed", operation_effective_date=closed_on)
        assert calculate(e, s, c)["sales_ready"] is expected
        passed("operation_end_date_" + str(closed_on))

    for rule in ("neis_ceremony_row", "academic_calendar_row"):
        e, s, c = base()
        c["registered_rules"] = {rule: "1.0.0"}
        for field in ("ceremony_status", "event_date", "start_time"):
            e["fields"][field]["automatic"]["value_check"]["rule_id"] = rule
        assert not calculate(e, s, c)["sales_ready"]
        passed("unimplemented_rule_cannot_reuse_notice_normalizer_" + rule)

    # 시 문자열의 접두사만 일치해 11시 30분을 11시로 승인하지 않아야 한다.
    e, s, c = base()
    doc = next(iter(c["documents"].values()))
    obs = next(iter(c["observations"].values()))
    for segment in doc["segments"]:
        segment["text"] = segment["text"].replace("오전 11시", "오전 11시 30분")
    for evidences in (obs["candidate"]["context_evidence"], obs["candidate"]["time_evidence"]):
        for evidence in evidences:
            evidence["quote"] = evidence["quote"].replace("오전 11시", "오전 11시 30분")
    assert calculate(e, s, c)["calendar_time"] is None
    passed("zero_minutes_does_not_match_half_hour_prefix")

    e, s, c = base()
    manual(e, "venue", "체육관")
    e["fields"]["venue"]["manual"].update(note="", resolved_fingerprint=None)
    assert calculate(e, s, c)["calendar_venue"] == "체육관"
    passed("ordinary_manual_confirmation_does_not_require_conflict_note")

    e, s, c = base()
    manual(e, "start_time", "10:00", bound="2027-01-07")
    snapshot = e["fields"]["start_time"]
    snapshot.update(review_required=True, has_web_difference=True)
    snapshot["automatic"]["checked_at"] = "2026-10-20T06:59:00Z"
    snapshot["automatic"]["value_check"]["checked_at"] = "2026-10-20T06:59:00Z"
    assert calculate(e, s, c)["calendar_time"] == "10:00"
    passed("same_fingerprint_refresh_preserves_prior_resolution")
    snapshot["automatic"].update(fingerprint="new-conflict", conflict_detected_at="2026-10-20T06:59:00Z")
    assert calculate(e, s, c)["calendar_time"] is None
    passed("new_fingerprint_requires_fresh_resolution")

    e, s, c = base()
    good = next(iter(c["documents"].values()))
    old = deepcopy(good)
    old.update(id="00000000-0000-0000-0000-000000000088", status="content_mismatch", retrieved_at="2026-10-19T06:00:00Z")
    c["documents"][old["id"]] = old
    for field in ("ceremony_status", "event_date", "start_time"):
        manual(e, field, e["fields"][field]["automatic"]["value"], bound="2027-01-07" if field == "start_time" else None)
        e["fields"][field]["manual"].update(basis="document_confirmed", source_url=good["display_url"], source_document_id=good["id"])
    assert calculate(e, s, c)["sales_ready"]
    passed("old_bad_document_version_does_not_poison_selected_good_version")
    for field in ("ceremony_status", "event_date", "start_time"):
        e["fields"][field]["manual"]["source_document_id"] = None
    assert calculate(e, s, c)["sales_ready"]
    passed("url_only_legacy_confirmation_selects_relevant_version")
    e["fields"]["event_date"]["manual"]["source_document_id"] = old["id"]
    assert not calculate(e, s, c)["calendar_eligible"]
    passed("selected_bad_document_version_cannot_confirm_date")

    result = {"policy_version": "4.0", "passed": len(checks), "cases": checks, "scope": "fixture specification checks; not live service accuracy"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result
