"""Schema-level guard on staff decisions (adversarial-review fix, PR167).

DB-free: exercises the Pydantic model directly. A DECLINE must carry at least
one reason_code at the SCHEMA level — the endpoint's directory validation
(active reject codes) remains the second gate on top.
"""
import pytest
from pydantic import ValidationError

from app.api.v1.endpoints.admin_actions import DecisionBody


def test_declined_without_reason_codes_fails_at_schema():
    with pytest.raises(ValidationError, match="at least one reason_code"):
        DecisionBody(outcome="declined", reason_codes=[])


def test_declined_default_empty_reason_codes_also_fails():
    with pytest.raises(ValidationError):
        DecisionBody(outcome="declined")


def test_declined_with_reason_codes_parses():
    body = DecisionBody(outcome="declined", reason_codes=["dti_too_high"])
    assert body.reason_codes == ["dti_too_high"]


@pytest.mark.parametrize("outcome", ["approved", "refer"])
def test_non_decline_outcomes_do_not_require_reason_codes(outcome):
    body = DecisionBody(outcome=outcome)
    assert body.reason_codes == []
