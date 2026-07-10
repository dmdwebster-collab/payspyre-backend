"""DB-free tests for the P0 schema pack (WS-C).

Covers Dave's structural data-model mandates:

  1. SIN optional end-to-end — no request schema, column set or intake path
     requires a SIN; an application can be completed (manual or finalize)
     without one. (SIN redaction rules themselves are covered by the existing
     SIN suites; here we assert *optionality*.)
  2. Income-type intake policy — EI and Student are not acceptable income
     types on NEW intake (config-driven allowlist/blocklist); storage stays
     permissive.
  3. 3-year address + employment history — entry schemas, coverage validation
     (3 years or since age of majority, overlaps allowed, >=1 current entry),
     and the version-not-overwrite snapshot builders.
  4. Co-borrower separate-file linking — linkage metadata surfaced on the
     canonical detail projection.

All pure/unit — no DB. The DB-backed integration path is covered by the full
CI suite against the migrated Postgres schema (migration 046).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.api.applicant.v1.endpoints import finalize as finalize_module
from app.api.applicant.v1.schemas import (
    AddressHistoryEntryInput,
    EmploymentHistoryEntryInput,
    FinalizeApplicationBody,
    ManualApplicationBody,
    SecondaryIncomeInput,
    SinCollectBody,
)
from app.api.platform_application_serialization import build_canonical_detail
from app.core.application_history import (
    required_window_start,
    snapshot_prior_address,
    snapshot_prior_employment,
    validate_history_entries,
)
from app.core.intake_policy import (
    check_canonical_income_type,
    check_free_text_income_type,
    normalize_income_label,
)

TODAY = date(2026, 7, 10)


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

_MANUAL_BASE = {
    "first_name": "Jordan",
    "last_name": "Public",
    "date_of_birth": "1990-04-15",
    "email": "jordan@example.com",
    "marital_status": "single",
    "citizenship": "Canadian",
    "street": "123 Test St",
    "city": "Toronto",
    "province": "ON",
    "postal_code": "M5V 2T6",
    "residential_status": "rent",
    "id_verification_type": "drivers_license",
    "main_phone": "+14165550123",
    "income_type": "Employed",
    "net_monthly_income_cents": 600000,
    "pay_frequency": "bi_weekly",
    "employer_name": "Acme Dental",
    "job_title": "Hygienist",
    "other_monthly_expenses_cents": 90000,
}


def _emp(**over):
    base = dict(
        employer_name="Acme Dental",
        employment_type="full_time",
        from_date=date.today() - timedelta(days=4 * 365),
    )
    base.update(over)
    return EmploymentHistoryEntryInput(**base)


# ---------------------------------------------------------------------------
# 1. SIN is optional end-to-end
# ---------------------------------------------------------------------------


def test_manual_application_completes_without_sin():
    """The full manual application (v1.00 field set) validates with NO SIN."""
    body = ManualApplicationBody(**_MANUAL_BASE)
    assert body.social_insurance_number is None
    dumped = body.model_dump(mode="json")
    assert dumped["social_insurance_number"] is None


def test_finalize_body_completes_without_sin():
    """A fully-populated finalize payload has no SIN field at all."""
    body = FinalizeApplicationBody(
        first_name="Jordan",
        last_name="Public",
        income_type="employed_full_time",
        net_monthly_income_cents=600000,
    )
    fields = set(FinalizeApplicationBody.model_fields.keys())
    assert "sin" not in fields
    assert "social_insurance_number" not in fields
    assert body.model_dump(exclude_unset=True)["first_name"] == "Jordan"


_SIN_TOKEN = re.compile(r"(^|_)sin($|_)")  # matches sin/sin_*/*_sin, not "housing"


def test_finalize_columns_contain_no_sin_column():
    assert not any(_SIN_TOKEN.search(col) for col in finalize_module._FINALIZE_COLUMNS)


def test_sin_collection_supports_explicit_decline():
    """The dedicated /sin path accepts a decline — SIN can be refused outright."""
    body = SinCollectBody(declined=True)
    assert body.declined is True and body.sin is None


def test_history_tables_carry_no_sin_columns():
    from app.models.platform.application_history import (
        PlatformApplicationAddressHistory,
        PlatformApplicationEmploymentHistory,
    )

    for model in (PlatformApplicationAddressHistory, PlatformApplicationEmploymentHistory):
        cols = set(model.__table__.columns.keys())
        assert not any(_SIN_TOKEN.search(c) for c in cols)


def test_history_entry_schemas_declare_no_sin_fields():
    for schema in (AddressHistoryEntryInput, EmploymentHistoryEntryInput):
        assert not any(_SIN_TOKEN.search(f) for f in schema.model_fields)
    # An extra "sin" key is dropped, never stored, on the entry schemas.
    entry = AddressHistoryEntryInput.model_validate(
        {"street": "x", "from_date": "2020-01-01", "sin": "046454286"}
    )
    assert "sin" not in entry.model_dump()
    assert "046454286" not in str(entry.model_dump())


# ---------------------------------------------------------------------------
# 2. Income-type intake policy (Dave: EI + Student removed for new intake)
# ---------------------------------------------------------------------------


def test_canonical_policy_rejects_employment_insurance_and_student():
    assert check_canonical_income_type("employment_insurance") is not None
    assert check_canonical_income_type("student") is not None


@pytest.mark.parametrize(
    "ok",
    [
        "employed_full_time", "employed_part_time", "employed_seasonal",
        "self_employed", "retirement_pension", "disability", "other", None, "",
    ],
)
def test_canonical_policy_accepts_valid_types(ok):
    assert check_canonical_income_type(ok) is None


@pytest.mark.parametrize("label", ["EI", "ei", "Student", "student", "Employment Insurance", "Employment Insurance / Disability"])
def test_free_text_policy_rejects_removed_labels(label):
    assert check_free_text_income_type(label) is not None


@pytest.mark.parametrize("label", ["Employed", "Self-employed", "Retirement/Pension", "Disability", "Other"])
def test_free_text_policy_accepts_valid_labels(label):
    assert check_free_text_income_type(label) is None


def test_normalize_income_label():
    assert normalize_income_label("Employment Insurance / Disability") == (
        "employment_insurance_disability"
    )
    assert normalize_income_label("  EI ") == "ei"


def test_finalize_body_rejects_ei_income_type():
    with pytest.raises(ValidationError, match="not an accepted income type"):
        FinalizeApplicationBody(income_type="employment_insurance")


def test_secondary_income_rejects_ei_income_type():
    with pytest.raises(ValidationError, match="not an accepted income type"):
        SecondaryIncomeInput(income_type="employment_insurance")


def test_employment_history_entry_rejects_ei_income_type():
    with pytest.raises(ValidationError, match="not an accepted income type"):
        _emp(income_type="employment_insurance")


@pytest.mark.parametrize("label", ["EI", "Student", "Employment Insurance"])
def test_manual_body_rejects_removed_income_labels(label):
    with pytest.raises(ValidationError, match="not an accepted income type"):
        ManualApplicationBody(**{**_MANUAL_BASE, "income_type": label})


def test_manual_body_accepts_employed_label():
    body = ManualApplicationBody(**_MANUAL_BASE)  # income_type == "Employed"
    assert body.income_type == "Employed"


def test_storage_stays_permissive_for_legacy_ei_rows():
    """The DB enum retains 'employment_insurance' (legacy rows round-trip)."""
    from app.models.platform.credit_application import PlatformCreditApplication

    enum_values = set(PlatformCreditApplication.__table__.c.income_type.type.enums)
    assert "employment_insurance" in enum_values  # storage permissive
    assert "student" not in enum_values  # never a storable value
    # ...while intake policy rejects it (validation-layer enforcement only).
    assert check_canonical_income_type("employment_insurance") is not None


# ---------------------------------------------------------------------------
# 3a. History entry schemas
# ---------------------------------------------------------------------------


def test_address_entry_range_validation():
    with pytest.raises(ValidationError, match="on or after from_date"):
        AddressHistoryEntryInput(
            street="1 Main", from_date=date(2025, 5, 1), to_date=date(2025, 4, 1)
        )


def test_employment_entry_requires_employment_type():
    with pytest.raises(ValidationError):
        EmploymentHistoryEntryInput(employer_name="Acme", from_date=date(2024, 1, 1))


def test_employment_entry_rejects_unknown_employment_type():
    with pytest.raises(ValidationError):
        _emp(employment_type="full time employee")


@pytest.mark.parametrize(
    "et", ["full_time", "part_time", "seasonal", "self_employed", "retired", "unemployed", "other"]
)
def test_employment_entry_accepts_daves_employment_types(et):
    assert _emp(employment_type=et).employment_type == et


def test_employment_type_enum_matches_model():
    from app.models.platform.application_history import (
        EMPLOYMENT_TYPES,
        PlatformApplicationEmploymentHistory,
    )

    enum_values = set(
        PlatformApplicationEmploymentHistory.__table__.c.employment_type.type.enums
    )
    assert enum_values == set(EMPLOYMENT_TYPES) == {
        "full_time", "part_time", "seasonal", "self_employed",
        "retired", "unemployed", "other",
    }


# ---------------------------------------------------------------------------
# 3b. History coverage validation (3 years / since age of majority)
# ---------------------------------------------------------------------------

_KW = dict(years_required=3, age_of_majority_years=18, gap_tolerance_days=31)


def _entry(from_date, to_date=None, current=False):
    return SimpleNamespace(from_date=from_date, to_date=to_date, is_current=current)


def test_coverage_single_current_entry_spanning_three_years_passes():
    entries = [_entry(TODAY - timedelta(days=4 * 365), None, current=True)]
    assert validate_history_entries(entries, today=TODAY, date_of_birth=date(1990, 4, 15), **_KW) == []


def test_coverage_two_entries_with_overlap_passes():
    entries = [
        _entry(TODAY - timedelta(days=5 * 365), TODAY - timedelta(days=300)),
        _entry(TODAY - timedelta(days=400), None, current=True),  # overlaps prior
    ]
    assert validate_history_entries(entries, today=TODAY, date_of_birth=date(1990, 4, 15), **_KW) == []


def test_coverage_requires_a_current_entry():
    entries = [_entry(TODAY - timedelta(days=4 * 365), TODAY)]
    errors = validate_history_entries(entries, today=TODAY, date_of_birth=None, **_KW)
    assert any("is_current" in e for e in errors)


def test_coverage_fails_when_history_starts_too_late():
    entries = [_entry(TODAY - timedelta(days=365), None, current=True)]
    errors = validate_history_entries(entries, today=TODAY, date_of_birth=date(1990, 4, 15), **_KW)
    assert any("reach back" in e for e in errors)


def test_coverage_fails_on_large_gap():
    entries = [
        _entry(TODAY - timedelta(days=4 * 365), TODAY - timedelta(days=2 * 365)),
        _entry(TODAY - timedelta(days=365), None, current=True),  # ~1yr gap
    ]
    errors = validate_history_entries(entries, today=TODAY, date_of_birth=None, **_KW)
    assert errors  # gap > tolerance


def test_coverage_allows_small_gap_within_tolerance():
    entries = [
        _entry(TODAY - timedelta(days=4 * 365), TODAY - timedelta(days=400)),
        _entry(TODAY - timedelta(days=380), None, current=True),  # 20-day gap
    ]
    assert validate_history_entries(entries, today=TODAY, date_of_birth=None, **_KW) == []


def test_coverage_shortens_to_age_of_majority():
    """A 19-year-old cannot have 3 years of adult history — only since majority."""
    dob = TODAY.replace(year=TODAY.year - 19)  # turned 18 one year ago
    entries = [_entry(TODAY - timedelta(days=360), None, current=True)]
    assert validate_history_entries(entries, today=TODAY, date_of_birth=dob, **_KW) == []


def test_coverage_rejects_empty_list():
    errors = validate_history_entries([], today=TODAY, date_of_birth=None, **_KW)
    assert any("at least one entry" in e for e in errors)


def test_coverage_rejects_future_from_date():
    entries = [_entry(TODAY + timedelta(days=1), None, current=True)]
    errors = validate_history_entries(entries, today=TODAY, date_of_birth=None, **_KW)
    assert any("future" in e for e in errors)


def test_coverage_rejects_inverted_range():
    entries = [
        _entry(TODAY - timedelta(days=100), TODAY - timedelta(days=200)),
        _entry(TODAY - timedelta(days=4 * 365), None, current=True),
    ]
    errors = validate_history_entries(entries, today=TODAY, date_of_birth=None, **_KW)
    assert any("on or after from_date" in e for e in errors)


def test_required_window_start_prefers_recent_majority():
    dob = date(2008, 1, 1)  # majority (18) on 2026-01-01 — after TODAY-3y
    start = required_window_start(
        today=TODAY, date_of_birth=dob, years_required=3, age_of_majority_years=18
    )
    assert start == date(2026, 1, 1)


def test_manual_body_validates_history_coverage_with_own_dob():
    good = {
        **_MANUAL_BASE,
        "address_history": [
            {
                "street": "123 Test St",
                "from_date": (date.today() - timedelta(days=4 * 365)).isoformat(),
                "is_current": True,
            }
        ],
    }
    assert ManualApplicationBody(**good).address_history[0].is_current is True

    bad = {
        **_MANUAL_BASE,
        "address_history": [
            {
                "street": "123 Test St",
                "from_date": (date.today() - timedelta(days=100)).isoformat(),
                "is_current": True,
            }
        ],
    }
    with pytest.raises(ValidationError, match="reach back"):
        ManualApplicationBody(**bad)


# ---------------------------------------------------------------------------
# 3c. Version-not-overwrite snapshots (current entry edits)
# ---------------------------------------------------------------------------


def _prior_address():
    return {
        "residence_street": "123 Old St",
        "residence_unit": None,
        "residence_city": "Toronto",
        "residence_province": "ON",
        "residence_postal_code": "M5V 2T6",
        "residential_status": "rent",
        "monthly_housing_payment_cents": 180000,
    }


def test_address_edit_produces_versioned_snapshot():
    snap = snapshot_prior_address(
        _prior_address(), {"residence_street": "456 New Ave"}, today=TODAY
    )
    assert snap is not None
    assert snap["street"] == "123 Old St"
    assert snap["city"] == "Toronto"
    assert snap["is_current"] is False
    assert snap["entry_source"] == "versioned_edit"
    assert snap["to_date"] == TODAY
    assert snap["from_date"] is None


def test_address_snapshot_skipped_when_nothing_changed():
    prior = _prior_address()
    # same value re-sent -> no version churn
    assert snapshot_prior_address(prior, {"residence_street": "123 Old St"}, today=TODAY) is None
    # unrelated field sent -> no snapshot
    assert snapshot_prior_address(prior, {"first_name": "Jordan"}, today=TODAY) is None
    # nothing sent -> no snapshot
    assert snapshot_prior_address(prior, {}, today=TODAY) is None


def test_address_snapshot_skipped_when_prior_was_empty():
    prior = {k: None for k in _prior_address()}
    assert snapshot_prior_address(prior, {"residence_street": "456 New Ave"}, today=TODAY) is None


def test_employment_edit_versions_with_hire_date_as_from_date():
    prior = {
        "employer_name": "Acme Dental",
        "job_title": "Hygienist",
        "income_type": "employed_full_time",
        "net_monthly_income_cents": 600000,
        "pay_frequency": "biweekly",
        "hire_date": date(2022, 5, 1),
    }
    snap = snapshot_prior_employment(prior, {"employer_name": "New Corp"}, today=TODAY)
    assert snap is not None
    assert snap["employer_name"] == "Acme Dental"
    assert snap["from_date"] == date(2022, 5, 1)  # hire_date becomes from_date
    assert snap["to_date"] == TODAY
    assert snap["is_current"] is False
    assert snap["entry_source"] == "versioned_edit"


def test_employment_snapshot_skipped_without_prior_employer():
    prior = {"employer_name": None, "job_title": None, "income_type": None,
             "net_monthly_income_cents": None, "pay_frequency": None, "hire_date": None}
    assert snapshot_prior_employment(prior, {"employer_name": "New Corp"}, today=TODAY) is None


# ---------------------------------------------------------------------------
# 4. Serialization: history sections + co-borrower separate-file linkage
# ---------------------------------------------------------------------------


def _fake_history_row(**over):
    base = dict(
        id=uuid4(),
        street="123 Old St", unit=None, city="Toronto", province="ON",
        postal_code="M5V 2T6", residential_status="rent",
        monthly_housing_payment_cents=180000,
        from_date=date(2020, 1, 1), to_date=date(2024, 1, 1),
        is_current=False, entry_source="applicant",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _fake_emp_row(**over):
    base = dict(
        id=uuid4(),
        employer_name="Acme Dental", job_title="Hygienist",
        employment_type="full_time", income_type="employed_full_time",
        net_monthly_income_cents=600000, pay_frequency="biweekly",
        from_date=date(2022, 5, 1), to_date=None,
        is_current=True, entry_source="applicant",
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
        secondary_incomes=[],
        patient_id=uuid4(),
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_detail_includes_history_sections_sorted_current_first():
    current = _fake_history_row(
        street="456 New Ave", from_date=date(2024, 1, 1), to_date=None, is_current=True
    )
    old = _fake_history_row()
    app = _fake_application(
        address_history=[old, current],
        employment_history=[_fake_emp_row()],
    )
    detail = build_canonical_detail(app, None)
    assert [a.street for a in detail.address_history] == ["456 New Ave", "123 Old St"]
    assert detail.address_history[0].is_current is True
    assert detail.employment_history[0].employment_type == "full_time"
    assert detail.employment_history[0].income_type == "employed_full_time"


def test_detail_history_defaults_empty_for_legacy_objects():
    detail = build_canonical_detail(_fake_application(), None)
    assert detail.address_history == []
    assert detail.employment_history == []


def test_detail_co_borrower_section_on_a_co_borrower_file():
    primary_id = uuid4()
    app = _fake_application(
        applicant_role="co_applicant",
        co_applicant_of_application_id=primary_id,
        relationship_to_primary="spouse",
    )
    detail = build_canonical_detail(app, None)
    assert detail.co_borrower.applicant_role == "co_applicant"
    assert detail.co_borrower.co_applicant_of_application_id == primary_id
    assert detail.co_borrower.relationship_to_primary == "spouse"
    assert detail.co_borrower.linked_application_ids == []


def test_detail_co_borrower_section_on_a_primary_file_lists_linked_files():
    linked = [uuid4(), uuid4()]
    detail = build_canonical_detail(_fake_application(), None, linked_application_ids=linked)
    assert detail.co_borrower.applicant_role == "primary"
    assert detail.co_borrower.co_applicant_of_application_id is None
    assert detail.co_borrower.linked_application_ids == linked


def test_finalize_body_accepts_history_and_relationship_fields():
    body = FinalizeApplicationBody(
        relationship_to_primary="spouse",
        address_history=[
            AddressHistoryEntryInput(
                street="123 Test St",
                from_date=date.today() - timedelta(days=4 * 365),
                is_current=True,
            )
        ],
        employment_history=[
            EmploymentHistoryEntryInput(
                employer_name="Acme Dental",
                employment_type="full_time",
                from_date=date.today() - timedelta(days=4 * 365),
                is_current=True,
            )
        ],
    )
    provided = body.model_dump(exclude_unset=True)
    assert provided["relationship_to_primary"] == "spouse"
    assert len(provided["address_history"]) == 1
    assert len(provided["employment_history"]) == 1


# ---------------------------------------------------------------------------
# 5. Migration 046 shape (additive-only, correct chaining)
# ---------------------------------------------------------------------------


def test_migration_046_chains_from_043_and_is_additive():
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[1]
        / "alembic" / "versions" / "046_application_history.py"
    )
    spec = importlib.util.spec_from_file_location("mig046", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.revision == "046_application_history"
    assert mod.down_revision == "043_canonical_credit_application"

    import inspect

    upgrade_src = inspect.getsource(mod.upgrade)
    # additive-only: the upgrade never drops or renames anything
    assert "drop_" not in upgrade_src
    assert "rename" not in upgrade_src
    assert "alter_column" not in upgrade_src
