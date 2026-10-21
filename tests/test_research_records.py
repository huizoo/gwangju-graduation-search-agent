"""인원·학교별 경로·실행 이력의 실제 SQLite 지속성과 현재 권한을 검사한다."""

import json

import pytest

from app.domain import dumps, utcnow
from app.providers import empty_document
from app.research_records import current_counts, reusable_routes, save_supplement
from test_grounded_claims import grounded_context, _count, candidate, NORMAL, _save
from test_rules import notice


def _document(school, text, path="students"):
    """공식 관계와 취득 주소가 일치하는 격리 원문을 만든다.

    Args:
        school: 등록 학교.
        text: 읽은 본문.
        path: 동일 원문 경로를 구분할 URL 경로.
    Returns:
        저장 가능한 DocumentRead.
    """
    doc = notice(school, text)
    doc["source_key"] = doc["display_url"] = doc["acquisition"]["final_url"] = f"https://school.example/{path}"
    channel = school["official_channels"][0]
    doc["authority_evidence"] = {"channel_url": channel["url"], "verified_by_url": channel["verified_by_url"], "verified_at": channel["verified_at"]}
    return doc


def _grade_context(ctx):
    """같은 격리 저장소의 기관을 초등학교로 전환한다.

    Args:
        ctx: 대학 기본 검사 문맥.
    Returns:
        서비스, 초등학교, 시즌.
    """
    service, school, season = ctx
    with service.store.transaction(True) as conn:
        conn.execute("UPDATE institutions SET type='elementary',name='검증초등학교' WHERE id=?", (school["id"],))
    return service, service.get_institution(school["id"]), season


def _write(ctx, documents, *, counts=None, routes=None, **extra):
    """문서 UUID 재매핑을 포함한 보조 기록의 저장 경로를 실행한다.

    Args:
        ctx: 서비스·학교·시즌.
        documents: 새 원문 목록.
        counts: 인원 후보 목록.
        routes: 읽기 경로 시도 목록.
        **extra: 실행 추적 등 추가 결과.
    """
    service, school, season = ctx
    result = {"counts": counts or [], "routes": routes or [], **extra}
    with service.store.transaction(True) as conn:
        ids = {doc["id"]: service._save_document(conn, school["id"], doc) for doc in documents}
        save_supplement(service, conn, school, season, result, ids)


def _read(ctx):
    """현재 행사와 인원 판정을 동일 읽기 거래에서 계산한다.

    Args:
        ctx: 서비스·학교·시즌.
    Returns:
        게시 가능한 인원 사전.
    """
    service, _, season = ctx
    with service.store.transaction() as conn:
        events = [service._project(conn, row[0])[0] for row in conn.execute("SELECT id FROM events WHERE season_id=?", (season["id"],))]
        return current_counts(service, conn, season, events)


def _enrollment(ctx, amount=188, path="students"):
    """현재 학년도 6학년 인원과 정확한 후보를 구성한다.

    Args:
        ctx: 초등학교 검사 문맥.
        amount: 원문 인원.
        path: 공식 원문 경로.
    Returns:
        문서와 인원 후보.
    """
    _, school, _ = ctx
    doc = _document(school, f"2026학년도 검증초등학교 학생 현황\n6학년 학생 수 {amount}명", path)
    return doc, _count(doc, value=amount, kind="graduating_grade_enrollment", grade=6)


def test_count_remaps_document_and_preserves_rejection_and_idempotency(grounded_context):
    """허용·기각 인원은 모두 보존하고 같은 원문 후보는 중복 적재하지 않는다."""
    ctx = _grade_context(grounded_context)
    service, school, _ = ctx
    doc, claim = _enrollment(ctx)
    bad = {**claim, "value": 189}
    _write(ctx, [doc], counts=[claim, bad])
    _write(ctx, [doc], counts=[claim, bad])
    assert _read(ctx) == {school["id"]: 188}
    with service.store.transaction() as conn:
        rows = conn.execute("SELECT * FROM count_observations").fetchall()
        assert len(rows) == 2 and {json.loads(row["proof_json"])["allowed"] for row in rows} == {True, False}
        assert all(row["document_id"] != doc["id"] for row in rows)
        assert all(json.loads(row["claim_json"])["document_id"] == row["document_id"] for row in rows)
        traces = conn.execute("SELECT trace_json FROM investigation_traces").fetchall()
        assert len(traces) == 2 and all(json.loads(row[0])["agent_trace"] for row in traces)


def test_failed_source_does_not_remove_independent_equal_count(grounded_context):
    """한 경로 실패는 해당 근거만 보류하며 독립적인 같은 값은 유지한다."""
    ctx = _grade_context(grounded_context)
    _, school, _ = ctx
    doc, claim = _enrollment(ctx)
    second, same = _enrollment(ctx, path="other-students")
    _write(ctx, [doc, second], counts=[claim, same])
    failed = empty_document(school, doc["display_url"])
    failed["recheck"] = {"status": "failed", "attempted_at": utcnow(), "reason_codes": ["provider_error"]}
    _write(ctx, [failed])
    assert _read(ctx) == {school["id"]: 188}
    failed = empty_document(school, second["display_url"])
    failed["recheck"] = {"status": "failed", "attempted_at": utcnow(), "reason_codes": ["provider_error"]}
    _write(ctx, [failed])
    assert _read(ctx) == {}


def test_conflicting_counts_are_held_and_same_source_revision_replaces_support(grounded_context):
    """다른 지원 값은 충돌로 보류하고 같은 URL의 새 값은 옛 근거를 갱신한다."""
    ctx = _grade_context(grounded_context)
    _, school, _ = ctx
    first, a = _enrollment(ctx)
    _write(ctx, [first], counts=[a])
    changed, b = _enrollment(ctx, 189)
    _write(ctx, [changed], counts=[b])
    assert _read(ctx) == {school["id"]: 189}
    independent, c = _enrollment(ctx, 188, "different")
    _write(ctx, [independent], counts=[c])
    assert _read(ctx) == {}


def test_count_rechecks_official_relationship_and_stays_in_its_season(grounded_context):
    """현재 공식 관계가 사라지거나 학년도가 다르면 과거 인원을 보여주지 않는다."""
    ctx = _grade_context(grounded_context)
    service, school, season = ctx
    doc, claim = _enrollment(ctx)
    _write(ctx, [doc], counts=[claim])
    with service.store.transaction(True) as conn:
        assert current_counts(service, conn, {**season, "academic_year": 2027}, []) == {}
        conn.execute("UPDATE institutions SET official_channels_json='[]' WHERE id=?", (school["id"],))
    assert _read(ctx) == {}


def test_university_count_links_to_unique_current_event_only(grounded_context):
    """같은 날짜의 대표 행사가 둘이면 인원을 임의 행사에 붙이지 않는다."""
    service, school, season = grounded_context
    text = NORMAL + "\n졸업생 총 132명"
    doc = _document(school, text)
    saved = _save(grounded_context, text, candidate(text), document=doc)
    _write(grounded_context, [doc], counts=[_count(doc)])
    assert _read(grounded_context) == {saved["event_ids"][0]: 132}
    second_text = text.replace("검증대학교", "검증대학교 제2캠퍼스")
    second_doc = _document(school, second_text, "second-campus")
    second = candidate(second_text, campus_label="제2캠퍼스")
    _save(grounded_context, second_text, second, document=second_doc)
    ambiguous = _count(doc, value=132)
    _write(grounded_context, [doc], counts=[ambiguous])
    with service.store.transaction() as conn:
        rows = conn.execute("SELECT event_id,proof_json FROM count_observations ORDER BY created_at").fetchall()
        assert len(rows) == 2 and rows[-1]["event_id"] is None
        assert not json.loads(rows[-1]["proof_json"])["allowed"]


def _route(doc, *, purpose="date_count", **changes):
    """실행기의 경로 관찰 결과를 구성한다.

    Args:
        doc: 읽은 문서.
        purpose: 조사 목적.
        **changes: 실패·목록 링크 등 추가 입력.
    Returns:
        경로 시도 객체.
    """
    return {"url": doc["display_url"], "purpose": purpose, "reading_method": "html_parser",
            "checked_at": utcnow(), "status": "succeeded", "failure_reason": None,
            "document_id": doc["id"], "route_id": None, "reusable": True, **changes}


def test_reusable_home_and_listing_require_verified_document_and_real_links(grounded_context):
    """홈페이지·공식 링크가 있는 목록만 등록하고 개별 공지는 시도만 남긴다."""
    service, school, _ = grounded_context
    home = _document(school, "검증대학교 홈페이지", "")
    listing = _document(school, "공지 목록", "notices")
    listing["title"] = "학사공지"
    individual = _document(school, NORMAL, "notice?mode=V&no=123")
    individual["title"] = "학사공지 - 2026학년도 학위수여식"
    unlinked = _document(school, "공지 목록", "unlinked")
    unlinked["title"] = "공지사항"
    links = ["https://school.example/notice/1", "https://school.example/notice/2"]
    _write(grounded_context, [home, listing, individual, unlinked], routes=[
        _route(home), _route(listing, purpose="start_time", links=links),
        _route(individual, links=links), _route(unlinked, links=[links[0], "https://other.example/a"])])
    with service.store.transaction() as conn:
        dates = reusable_routes(conn, school["id"], "date_count")
        times = reusable_routes(conn, school["id"], "time")
        assert [row["url"] for row in dates] == [home["display_url"]]
        assert all(row["verified"] is True for row in dates + times)
        assert [row["url"] for row in times] == [listing["display_url"]]
        assert conn.execute("SELECT count(*) FROM route_attempts").fetchone()[0] == 4


def test_route_failure_retains_success_history_and_exact_trace(grounded_context):
    """실패 원인을 갱신해도 마지막 성공 시각과 관찰·종료 이력을 보존한다."""
    service, school, _ = grounded_context
    home = _document(school, "검증대학교 홈페이지", "")
    _write(grounded_context, [home], routes=[_route(home)])
    failed = empty_document(school, home["display_url"])
    failed["recheck"] = {"status": "failed", "attempted_at": utcnow(), "reason_codes": ["provider_error"]}
    trace = [{"step": 1, "kind": "decision", "action": "read_document"},
             {"step": 2, "kind": "observation", "outcome": "access_failed"},
             {"step": 3, "kind": "decision", "action": "search"}]
    _write(grounded_context, [failed], routes=[_route(failed, status="failed", failure_reason="access_failed", reusable=False)],
           agent_trace=trace, stop_reason="budget_reached")
    with service.store.transaction() as conn:
        route = reusable_routes(conn, school["id"], "date_count")[0]
        assert route["last_success_at"] and route["last_attempt_at"] >= route["last_success_at"]
        assert route["failure_reason"] == "access_failed"
        saved = json.loads(conn.execute("SELECT trace_json FROM investigation_traces ORDER BY created_at DESC LIMIT 1").fetchone()[0])
        assert saved["agent_trace"] == trace and saved["stop_reason"] == "budget_reached"


def test_missing_count_document_is_preserved_as_rejected_trace(grounded_context):
    """원문 연결이 없는 잘못된 후보도 정상 인원으로 만들지 않고 원문 후보를 남긴다."""
    service, school, _ = grounded_context
    doc = _document(school, NORMAL + "\n졸업생 총 132명")
    claim = _count(doc)
    _write(grounded_context, [], counts=[claim])
    with service.store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM count_observations").fetchone()[0] == 0
        trace = json.loads(conn.execute("SELECT trace_json FROM investigation_traces").fetchone()[0])
        assert trace["rejected_counts"][0]["claim"] == claim
        assert not trace["rejected_counts"][0]["proof"]["allowed"]


def test_successful_recheck_adds_new_proof_without_erasing_previous_rejection(grounded_context):
    """같은 값이 실제 재확인되면 새 허용 판정을 보존하고 과거 기각도 남긴다."""
    ctx = _grade_context(grounded_context)
    service, school, _ = ctx
    doc, claim = _enrollment(ctx)
    doc["recheck"] = {"status": "failed", "attempted_at": utcnow(), "reason_codes": ["provider_error"]}
    _write(ctx, [doc], counts=[claim])
    assert _read(ctx) == {}
    fresh = json.loads(dumps(doc))
    fresh["retrieved_at"] = fresh["last_checked_at"] = utcnow()
    fresh["recheck"] = {"status": "succeeded", "attempted_at": fresh["retrieved_at"], "reason_codes": []}
    _write(ctx, [fresh], counts=[claim])
    assert _read(ctx) == {school["id"]: 188}
    with service.store.transaction() as conn:
        proofs = [json.loads(row[0]) for row in conn.execute("SELECT proof_json FROM count_observations")]
        assert len(proofs) == 2 and {proof["allowed"] for proof in proofs} == {False, True}


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["api", "public"])
async def test_validated_schoolinfo_source_keeps_count_after_resave_and_restart(grounded_context, source):
    """공시 전용 관계를 검증한 원문은 동일 재조회·재시작에도 인원을 유지한다."""
    from app.storage import Service
    from test_student_provider import INSTITUTION, IDENTIFIERS, api_provider, public_provider
    service, school, season = grounded_context
    with service.store.transaction(True) as conn:
        conn.execute("UPDATE institutions SET name=?,type=?,district=?,address=?,official_channels_json='[]' WHERE id=?",
                     (INSTITUTION["name"], INSTITUTION["type"], INSTITUTION["district"], INSTITUTION["address"], school["id"]))
        for item in IDENTIFIERS:
            conn.execute("INSERT INTO institution_identifiers VALUES(?,?,?)", (item["provider"], item["external_key"], school["id"]))
    school = service.get_institution(school["id"])
    ctx = service, school, season
    provider, _ = api_provider() if source == "api" else public_provider()
    result = await provider.lookup(school, season, IDENTIFIERS)
    assert result["counts"][0]["value"] == 188
    for _ in range(2):
        service.save_research(school["id"], season["id"], result)
        assert _read(ctx) == {school["id"]: 188}
    restarted = Service(service.store, service.settings)
    assert _read((restarted, school, season)) == {school["id"]: 188}
    with service.store.transaction() as conn:
        assert conn.execute("SELECT count(*) FROM count_observations WHERE institution_id=?", (school["id"],)).fetchone()[0] == 1
