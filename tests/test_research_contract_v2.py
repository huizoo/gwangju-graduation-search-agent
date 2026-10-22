"""조사 목적과 복수 행사일의 입력 계약을 검증한다."""

from uuid import uuid4

import pytest

from app.core.domain import ApiError, validate
from app.validation.scope import eligible_school


def test_time_request_requires_single_school_and_event():
    request = {"kind": "schedule_scan", "season_id": str(uuid4()), "mode": "recheck",
               "selection": {"scope": "all"}, "limits": None, "purpose": "time"}
    with pytest.raises(ApiError):
        validate("ScheduleJobCreate", request)
    request.update(target_event_id=str(uuid4()), selection={"scope": "ids", "institution_ids": [str(uuid4())]})
    assert validate("ScheduleJobCreate", request)["purpose"] == "time"
    request["selection"]["institution_ids"].append(str(uuid4()))
    with pytest.raises(ApiError):
        validate("ScheduleJobCreate", request)


def test_date_count_rejects_unrelated_event_target():
    with pytest.raises(ApiError):
        validate("ScheduleJobCreate", {"kind": "schedule_scan", "season_id": str(uuid4()), "mode": "recheck",
            "selection": {"scope": "all"}, "limits": None, "purpose": "date_count", "target_event_id": str(uuid4())})


def test_multiple_days_have_one_ordered_representative():
    claim = {"precision": "day", "date": "2027-01-06", "month": None,
             "dates": ["2027-01-06", "2027-01-08"]}
    assert validate("DateClaim", claim)["dates"] == claim["dates"]
    for dates in [["2027-01-08", "2027-01-06"], ["2027-01-08"], ["2027-02-30"]]:
        with pytest.raises(ApiError):
            validate("DateClaim", {**claim, "dates": dates})


def test_shared_school_scope():
    assert eligible_school({"type": "university", "district": "북구"})
    assert eligible_school({"type": "elementary", "district": "남구"})
    assert not eligible_school({"type": "university", "district": "다른 지역"})
    assert not eligible_school({"type": "kindergarten", "district": "북구"})
