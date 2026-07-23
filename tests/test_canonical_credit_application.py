"""Unit tests for the canonical credit-application schema, status workflow, and
mock/test-fill helper.

These are DB-FREE: they exercise the pure data generator, the orchestrator's
status-transition functions (using lightweight stand-in objects), and the
schema-level invariants (no raw-SIN column; enum value coverage). The full
DB-backed suite runs in CI against the migrated Postgres schema.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.services import mock_application_filler as filler
from app.services.flow_orchestrator import (
    InvalidStateTransition,
    mark_manual_review,
    mark_origination,
    mark_underwriting,
    mark_verification,
)


# --- mock data generator ---------------------------------------------------


def test_generate_mock_data_is_deterministic_under_seed():
    a = filler.generate_mock_application_data(seed=42)
    b = filler.generate_mock_application_data(seed=42)
    assert a == b


def test_generate_mock_data_has_full_canonical_field_set():
    data = filler.generate_mock_application_data(seed=1)
    app = data["application"]
    expected = {
        # personal
        "first_name", "middle_name", "last_name", "date_of_birth", "marital_status",
        "number_of_dependents", "citizenship", "education", "main_phone",
        "alternative_phone", "email",
        # id
        "id_type", "id_number", "id_province_of_issue", "id_expiry",
        # residence
        "residence_street", "residence_unit", "residence_city", "residence_province",
        "residence_postal_code", "time_at_address_years", "time_at_address_months",
        "residential_status", "monthly_housing_payment_cents",
        # primary income
        "income_type", "net_monthly_income_cents", "next_pay_date", "pay_frequency",
        "employer_name", "hire_date", "job_title", "work_phone", "work_phone_ext",
        "ok_to_contact_at_work",
        # financial
        "number_of_credit_accounts", "car_ownership", "monthly_car_payment_cents",
        "non_discretionary_expenses_cents",
    }
    assert expected == set(app.keys())


def test_generate_mock_data_values_are_realistic_and_typed():
    data = filler.generate_mock_application_data(seed=7)
    app = data["application"]
    assert isinstance(app["date_of_birth"], date)
    assert isinstance(app["net_monthly_income_cents"], int) and app["net_monthly_income_cents"] > 0
    assert app["income_type"] in filler._INCOME_TYPES
    assert app["car_ownership"] in filler._CAR_OWNERSHIP
    assert 0 <= app["time_at_address_months"] <= 11
    assert app["non_discretionary_expenses_cents"] >= 0
    # secondary incomes are 0-2 and each carries a valid income_type
    assert 0 <= len(data["secondary_incomes"]) <= 2
    for line in data["secondary_incomes"]:
        assert line["income_type"] in filler._SECONDARY_INCOME_TYPES


def test_mock_sin_is_luhn_valid_and_masked():
    data = filler.generate_mock_application_data(seed=3)
    sin = data["raw_sin"]
    assert len(sin) == 9 and sin.isdigit()
    assert sin[0] != "0"
    assert filler._luhn_check_digit_ok(sin)
    assert data["sin_last3"] == sin[-3:]


def test_car_payment_zero_when_not_financing():
    # Find a seed producing a non-financing/leasing car_ownership and assert 0.
    for seed in range(50):
        app = filler.generate_mock_application_data(seed=seed)["application"]
        if app["car_ownership"] in ("fully_paid", "none"):
            assert app["monthly_car_payment_cents"] == 0
            return
    pytest.skip("no non-financing car_ownership seed found in range")


# --- SIN is never in a plaintext column ------------------------------------


def test_mock_data_carries_no_raw_sin_in_application_columns():
    """The application-column payload must not contain the raw SIN under any key."""
    data = filler.generate_mock_application_data(seed=9)
    raw_sin = data["raw_sin"]
    for key, value in data["application"].items():
        assert value != raw_sin, f"raw SIN leaked into application column {key!r}"
    # And the model must not declare a raw-SIN column.
    from app.models.platform.credit_application import PlatformCreditApplication

    cols = set(PlatformCreditApplication.__table__.columns.keys())
    assert "sin" not in cols
    assert "social_insurance_number" not in cols
    assert "sin_encrypted" not in cols  # SIN lives on the patient, not the application


# --- status workflow transitions (orchestrator-owned) ----------------------


def _fake_app(status: str):
    return SimpleNamespace(status=status, status_updated_at=None)


def test_named_workflow_transitions_set_expected_status():
    app = _fake_app("started")
    mark_origination(app)
    assert app.status == "origination"
    mark_verification(app)
    assert app.status == "verifying"
    mark_underwriting(app)
    assert app.status == "underwriting"
    mark_manual_review(app)
    assert app.status == "under_review"


@pytest.mark.parametrize("terminal", ["approved", "rejected", "withdrawn", "expired"])
@pytest.mark.parametrize("transition", [mark_origination, mark_verification, mark_underwriting])
def test_workflow_transitions_refuse_terminal_states(terminal, transition):
    app = _fake_app(terminal)
    with pytest.raises(InvalidStateTransition):
        transition(app)
    assert app.status == terminal  # unchanged


def test_new_status_values_present_in_model_enum():
    from app.models.platform.credit_application import PlatformCreditApplication

    enum_values = set(PlatformCreditApplication.__table__.c.status.type.enums)
    assert {"origination", "underwriting"}.issubset(enum_values)
    # the original values are retained (additive-only)
    assert {"started", "verifying", "under_review", "approved", "rejected"}.issubset(enum_values)
