"""실제 JSON Schema와 Pydantic 경계의 일치·거절을 검사한다."""
import pytest
from pydantic import ValidationError
from app.domain import ApiError, model_for, validate


def test_calendar_dates_and_unknown_fields_rejected():
    for value in ({"precision": "day", "date": "2027-02-30", "month": None}, {"precision": "month", "date": "2027-02-01", "month": "2027-02"}):
        with pytest.raises(ApiError):
            validate("DateValue", value)
    with pytest.raises(ValidationError):
        model_for("InstitutionCreate").model_validate({"official": True})


def test_root_model_preserves_contract():
    value = {"precision": "month", "date": None, "month": "2027-02"}
    assert model_for("DateValue").model_validate(value).model_dump() == value
    assert "oneOf" in model_for("DateValue").model_json_schema()
    with pytest.raises(ApiError):
        validate("SeasonCreate", {"label": "비밀값은 오류에서 출력 금지"})
    try:
        validate("DateValue", {"precision": "private-content"})
    except ApiError as error:
        assert "private-content" not in str(error)
