"""DB-free tests for the admin applications detail + applicant review/finalize API.

These exercise the pure serialization projection, the finalize request schema, and
the finalize routing/status logic using lightweight stand-in objects — no DB. The
DB-backed integration path is covered by the full CI suite against the migrated
Postgres schema.

Focus areas (mapping to the hard CI constraints):
  * SIN is NEVER exposed beyond ``sin_last3`` and never accepted on the finalize body.
  * Finalize routes status ONLY through the orchestrator's ``mark_*`` transitions.
  * The full canonical field set round-trips through the serializer.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.api.applicant.v1.schemas import (
    FinalizeApplicationBody,
    SecondaryIncomeInput,
)
from app.api.platform_application_serialization import (
    CanonicalApplicationDetail,
    build_canonical_detail,
)
from app.api.applicant.v1.endpoints import finalize as finalize_module


# --- fixtures (lightweight stand-ins, no DB) -------------------------------


def _fake_secondary(**over):
    base = dict(
        id=uuid4(),
        income_type="self_employed",
        net_monthly_income_cents=120000,
        pay_frequency="monthly",
        next_pay_date=date(2026, 8, 1),
        employer_name="Side Gig Ltd",
        job_title="Consultant",
        hire_date=date(2024, 1, 1),
        work_phone="+15550001111",
        work_phone_ext="12",
        description="freelance",
        created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    base.update(over)
    return SimpleNamespace(**base)


def _fake_application(**over):
    base = dict(
        id=uuid4(),
        status="started",
        created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        status_updated_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        requested_amount_cents=500000,
        requested_amount_source="patient",
        applicant_role="primary",
        # personal
        first_name="Jordan", middle_name="Lee", last_name="Public",
        date_of_birth=date(1990, 4, 15), marital_status="single",
        number_of_dependents=0, citizenship="canadian_citizen",
        education="bachelors", main_phone="+14165550123",
        alternative_phone=None, email="jordan@example.com",
        # id
        id_type="drivers_licence", id_number="D1234", id_province_of_issue="ON",
        id_expiry=date(2030, 1, 1),
        # residence
        residence_street="123 Test St", residence_unit="4B",
        residence_city="Toronto", residence_province="ON",
        residence_postal_code="M5V2T6", time_at_address_years=3,
        time_at_address_months=2, residential_status="rent",
        monthly_housing_payment_cents=180000,
        # primary income
        income_type="employed_full_time", net_monthly_income_cents=600000,
        next_pay_date=date(2026, 7, 15), pay_frequency="biweekly",
        employer_name="Acme Dental", hire_date=date(2022, 5, 1),
        job_title="Hygienist", work_phone="+14165551000",
        work_phone_ext="7", ok_to_contact_at_work=True,
        # financial
        number_of_credit_accounts=4, car_ownership="financing",
        monthly_car_payment_cents=45000, non_discretionary_expenses_cents=90000,
        secondary_incomes=[_fake_secondary()],
        flow_state={},
        patient_id=uuid4(),
    )
    base.update(over)
    return SimpleNamespace(**base)


def _fake_patient(**over):
    base = dict(
        sin_last3="321",
        sin_collected_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        sin_declined=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


# --- serialization projection ----------------------------------------------


def test_build_canonical_detail_round_trips_full_field_set():
    app = _fake_application()
    detail = build_canonical_detail(app, _fake_patient())
    assert isinstance(detail, CanonicalApplicationDetail)
    # sections populated
    assert detail.personal.first_name == "Jordan"
    assert detail.personal.email == "jordan@example.com"
    assert detail.residence.residence_city == "Toronto"
    assert detail.primary_income.income_type == "employed_full_time"
    assert detail.primary_income.net_monthly_income_cents == 600000
    assert detail.financial.car_ownership == "financing"
    assert len(detail.secondary_incomes) == 1
    assert detail.secondary_incomes[0].income_type == "self_employed"


def test_canonical_detail_exposes_only_sin_last3():
    app = _fake_application()
    detail = build_canonical_detail(app, _fake_patient(sin_last3="789"))
    assert detail.identification.sin_last3 == "789"
    assert detail.identification.sin_collected is True
    assert detail.identification.sin_declined is False
    # The detail model must carry NO field that could hold a raw/encrypted SIN.
    dumped = detail.model_dump()
    flat = str(dumped)
    assert "sin_encrypted" not in flat
    keys = set(detail.identification.model_dump().keys())
    assert "sin" not in keys
    assert "sin_number" not in keys
    assert "social_insurance_number" not in keys


def test_canonical_detail_handles_missing_patient():
    detail = build_canonical_detail(_fake_application(), None)
    assert detail.identification.sin_last3 is None
    assert detail.identification.sin_collected is False
    assert detail.identification.sin_declined is False


def test_canonical_detail_handles_empty_secondary_incomes():
    detail = build_canonical_detail(_fake_application(secondary_incomes=[]), _fake_patient())
    assert detail.secondary_incomes == []


# --- finalize request schema ------------------------------------------------


def test_finalize_body_all_fields_optional():
    # A bare finalize (no field corrections) is valid.
    body = FinalizeApplicationBody()
    assert body.model_dump(exclude_unset=True) == {}


def test_finalize_body_has_no_sin_field():
    # SIN must never be accepted on the finalize body (dedicated /sin path only).
    fields = set(FinalizeApplicationBody.model_fields.keys())
    assert "sin" not in fields
    assert "social_insurance_number" not in fields
    assert "sin_encrypted" not in fields


def test_finalize_body_exclude_unset_only_reports_sent_fields():
    body = FinalizeApplicationBody(first_name="Casey", net_monthly_income_cents=700000)
    provided = body.model_dump(exclude_unset=True)
    assert provided == {"first_name": "Casey", "net_monthly_income_cents": 700000}


def test_finalize_columns_cover_every_writable_canonical_column():
    """Every column the finalize body can set must be listed in _FINALIZE_COLUMNS
    (minus the child-row collections, handled separately) — a drift guard."""
    body_fields = set(FinalizeApplicationBody.model_fields.keys()) - set(
        finalize_module._COLLECTION_FIELDS
    )
    assert body_fields == set(finalize_module._FINALIZE_COLUMNS)


def test_secondary_income_input_all_optional():
    line = SecondaryIncomeInput()
    assert line.model_dump(exclude_unset=True) == {}


# --- finalize routing logic (status via orchestrator only) ------------------


def _apply_finalize_routing(app, use_real_adapters: bool):
    """Mirror the routing branch in finalize_application without a request/DB:
    exercises the SAME orchestrator transition functions the endpoint uses."""
    from app.services.flow_orchestrator import mark_manual_review, mark_verification

    if use_real_adapters:
        mark_verification(app)
        return "verification"
    mark_manual_review(app)
    return "manual_review"


@pytest.mark.parametrize(
    "use_real,expected_status,expected_route",
    [
        (False, "under_review", "manual_review"),  # test/mock → MANUAL underwriting
        (True, "verifying", "verification"),        # real adapters → verification
    ],
)
def test_finalize_routing_uses_orchestrator_transitions(use_real, expected_status, expected_route):
    app = _fake_application(status="started")
    route = _apply_finalize_routing(app, use_real)
    assert app.status == expected_status
    assert route == expected_route


def test_finalizable_statuses_exclude_terminal():
    terminal = {"approved", "rejected", "withdrawn", "expired"}
    assert terminal.isdisjoint(set(finalize_module._FINALIZABLE))


def test_finalize_module_does_not_assign_status_directly():
    """The finalize module must never contain a raw ``application.status =`` write —
    the money-path guardrail enforces this repo-wide; assert it here too."""
    import inspect

    src = inspect.getsource(finalize_module)
    assert "application.status =" not in src
    assert "mark_manual_review" in src
    assert "mark_verification" in src
