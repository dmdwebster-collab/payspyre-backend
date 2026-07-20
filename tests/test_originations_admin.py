"""WS-E originations-admin domain logic — DB-FREE unit tests.

Covers the pure service layer behind the new admin endpoints:

  * assignment gating (assignee / unassigned / override paths),
  * admin field-edit validation + old→new change log + "version, don't
    overwrite" address/employment snapshots,
  * staff offer editing bounds (amount/term/frequency/rate band + the
    ``rate_edit_roles`` gate) from the typed PricingConfig,
  * pipeline waiting-time helper,
  * flag-based notification suppression (stubbed session; no DB),
  * the notification processor's per-run suppression memo,
  * migration 059 chain pin (down_revision = 054_hardship).

Deliberately touches NO database (shared remote test DB — fan-out rule).

Run JUST this file:
    source .venv/bin/activate && python -m pytest tests/test_originations_admin.py -q
"""
from __future__ import annotations

import importlib.util
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.schemas.pricing_config import (
    InterestConfig,
    PaymentFrequency,
    PricingConfig,
)
from app.services import flags as flags_service
from app.services import originations_admin as oa


# ---------------------------------------------------------------------------
# Assignment gating
# ---------------------------------------------------------------------------


class TestAssignmentGate:
    def test_assignee_passes_without_override(self):
        assert oa.check_assignment("user-1", "user-1", override_allowed=False) is False

    def test_unassigned_without_override_raises(self):
        with pytest.raises(oa.AssignmentRequired, match="unassigned"):
            oa.check_assignment(None, "user-1", override_allowed=False)

    def test_assigned_to_other_without_override_raises(self):
        with pytest.raises(oa.AssignmentRequired, match="another user"):
            oa.check_assignment("user-2", "user-1", override_allowed=False)

    def test_override_allows_and_is_flagged(self):
        assert oa.check_assignment("user-2", "user-1", override_allowed=True) is True
        assert oa.check_assignment(None, "user-1", override_allowed=True) is True

    def test_admin_role_is_implicit_override(self):
        assert oa.has_assignment_override({"admin"}, set()) is True

    def test_explicit_permission_grant(self):
        assert (
            oa.has_assignment_override({"staff"}, {oa.ASSIGNMENT_OVERRIDE_PERMISSION})
            is True
        )

    def test_plain_staff_has_no_override(self):
        assert oa.has_assignment_override({"staff"}, {("loans", "read")}) is False


# ---------------------------------------------------------------------------
# Admin field editing — validation, change log, versioning
# ---------------------------------------------------------------------------


def _app_row(**overrides):
    """A SimpleNamespace standing in for the ORM application row."""
    base = dict(
        first_name="Ann",
        middle_name=None,
        last_name="Lee",
        date_of_birth=date(1990, 5, 1),
        marital_status="single",
        email="ann@example.com",
        residence_street="1 Main St",
        residence_unit=None,
        residence_city="Kelowna",
        residence_province="BC",
        residence_postal_code="V1V1V1",
        residential_status="rent",
        monthly_housing_payment_cents=150_000,
        employer_name="Acme Dental",
        job_title="Hygienist",
        income_type="employed_full_time",
        net_monthly_income_cents=450_000,
        pay_frequency="bi_weekly",
        hire_date=date(2020, 1, 15),
        number_of_dependents=0,
        branch=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestValidateChanges:
    def test_unknown_field_rejected(self):
        with pytest.raises(oa.InvalidEdit, match="not editable"):
            oa.validate_changes({"status": "approved"})

    def test_sin_is_never_editable(self):
        with pytest.raises(oa.InvalidEdit):
            oa.validate_changes({"sin": "123456789"})

    def test_empty_payload_rejected(self):
        with pytest.raises(oa.InvalidEdit, match="No changes"):
            oa.validate_changes({})

    def test_date_string_coerced(self):
        out = oa.validate_changes({"date_of_birth": "1991-02-03"})
        assert out["date_of_birth"] == date(1991, 2, 3)

    def test_bad_date_rejected(self):
        with pytest.raises(oa.InvalidEdit, match="ISO date"):
            oa.validate_changes({"date_of_birth": "yesterday"})

    def test_negative_int_rejected(self):
        with pytest.raises(oa.InvalidEdit, match=">= 0"):
            oa.validate_changes({"net_monthly_income_cents": -5})

    def test_bool_typed(self):
        with pytest.raises(oa.InvalidEdit, match="boolean"):
            oa.validate_changes({"ok_to_contact_at_work": "yes"})

    def test_enum_validated(self):
        with pytest.raises(oa.InvalidEdit, match="income_type"):
            oa.validate_changes({"income_type": "gig_economy"})
        assert oa.validate_changes({"income_type": "self_employed"}) == {
            "income_type": "self_employed"
        }

    def test_null_clears_and_strings_strip(self):
        out = oa.validate_changes({"middle_name": None, "first_name": "  Bob "})
        assert out == {"middle_name": None, "first_name": "Bob"}


class TestApplyAdminEdit:
    TODAY = date(2026, 7, 20)

    def test_change_log_old_new(self):
        row = _app_row()
        res = oa.apply_admin_edit(
            row, {"first_name": "Anne", "number_of_dependents": 2}, today=self.TODAY
        )
        assert res.changed
        assert res.change_log["first_name"] == {"old": "Ann", "new": "Anne"}
        assert res.change_log["number_of_dependents"] == {"old": 0, "new": 2}
        assert row.first_name == "Anne" and row.number_of_dependents == 2

    def test_noop_changes_dropped(self):
        row = _app_row()
        res = oa.apply_admin_edit(
            row, {"first_name": "Ann", "email": "ann@example.com"}, today=self.TODAY
        )
        assert not res.changed
        assert res.change_log == {}
        assert res.address_snapshot is None and res.employment_snapshot is None

    def test_dates_json_safe_in_change_log(self):
        row = _app_row()
        res = oa.apply_admin_edit(
            row, {"date_of_birth": date(1991, 2, 3)}, today=self.TODAY
        )
        assert res.change_log["date_of_birth"] == {"old": "1990-05-01", "new": "1991-02-03"}

    def test_address_edit_versions_prior_current(self):
        row = _app_row()
        res = oa.apply_admin_edit(
            row,
            {"residence_street": "99 New Rd", "residence_city": "Vernon"},
            today=self.TODAY,
        )
        snap = res.address_snapshot
        assert snap is not None
        # Prior (not new) values, versioned — never overwritten silently.
        assert snap["street"] == "1 Main St"
        assert snap["city"] == "Kelowna"
        assert snap["entry_source"] == "versioned_edit"
        assert snap["is_current"] is False
        assert snap["to_date"] == self.TODAY
        # The row itself now carries the new current values.
        assert row.residence_street == "99 New Rd"
        assert res.employment_snapshot is None

    def test_employment_edit_versions_prior_current(self):
        row = _app_row()
        res = oa.apply_admin_edit(
            row,
            {"employer_name": "New Smile Corp", "net_monthly_income_cents": 500_000},
            today=self.TODAY,
        )
        snap = res.employment_snapshot
        assert snap is not None
        assert snap["employer_name"] == "Acme Dental"
        assert snap["net_monthly_income_cents"] == 450_000
        assert snap["from_date"] == date(2020, 1, 15)  # hire_date -> from_date
        assert snap["entry_source"] == "versioned_edit"
        assert res.address_snapshot is None

    def test_personal_edit_creates_no_snapshots(self):
        row = _app_row()
        res = oa.apply_admin_edit(row, {"last_name": "Li"}, today=self.TODAY)
        assert res.changed
        assert res.address_snapshot is None and res.employment_snapshot is None


# ---------------------------------------------------------------------------
# Offer editing bounds (typed PricingConfig)
# ---------------------------------------------------------------------------


def _cfg(**overrides) -> PricingConfig:
    base = dict(
        interest=InterestConfig(
            annual_rate_bps=1999,
            min_rate_bps=999,
            max_rate_bps=2399,
            rate_edit_roles=["admin", "senior_underwriter"],
        ),
        payment_frequencies=[PaymentFrequency.MONTHLY, PaymentFrequency.BI_WEEKLY],
        amount_min_cents=50_000,
        amount_max_cents=2_000_000,
        term_min_months=6,
        term_max_months=60,
    )
    base.update(overrides)
    return PricingConfig(**base)


class TestValidateOffer:
    def _ok(self, **kw):
        args = dict(
            amount_cents=500_000,
            term_months=24,
            annual_rate_bps=None,
            frequency="monthly",
            actor_roles={"staff"},
            product_min_amount_cents=100_000,
            product_max_amount_cents=1_500_000,
        )
        args.update(kw)
        return oa.validate_offer(_cfg(), **args)

    def test_defaults_resolve(self):
        offer = self._ok()
        assert offer.annual_rate_bps == 1999  # product default
        assert offer.frequency == "monthly"

    def test_amount_below_product_min(self):
        with pytest.raises(oa.OfferOutOfBounds, match="below the product minimum"):
            self._ok(amount_cents=60_000)  # config allows 50k but product row says 100k

    def test_amount_above_config_max(self):
        # Config caps at 2M; the looser 5M product-row max must not override it
        # (the effective ceiling is the intersection = min).
        with pytest.raises(oa.OfferOutOfBounds, match="above the product maximum"):
            self._ok(amount_cents=2_100_000, product_max_amount_cents=5_000_000)

    def test_term_out_of_range(self):
        with pytest.raises(oa.OfferOutOfBounds, match="outside the product's range"):
            self._ok(term_months=72)

    def test_frequency_not_offered(self):
        with pytest.raises(oa.OfferOutOfBounds, match="frequency"):
            self._ok(frequency="weekly")

    def test_frequency_spelling_tolerant(self):
        assert self._ok(frequency="Bi-Weekly").frequency == "bi_weekly"

    def test_rate_out_of_band(self):
        with pytest.raises(oa.OfferOutOfBounds, match="band"):
            self._ok(annual_rate_bps=2500, actor_roles={"admin"})

    def test_custom_rate_requires_listed_role(self):
        with pytest.raises(oa.RateRoleNotPermitted):
            self._ok(annual_rate_bps=1500, actor_roles={"staff"})

    def test_custom_rate_allowed_for_listed_role(self):
        offer = self._ok(annual_rate_bps=1500, actor_roles={"staff", "senior_underwriter"})
        assert offer.annual_rate_bps == 1500

    def test_default_rate_needs_no_role(self):
        # Explicitly submitting the default is not a "custom" rate.
        assert self._ok(annual_rate_bps=1999, actor_roles={"staff"}).annual_rate_bps == 1999

    def test_nonpositive_rejected(self):
        with pytest.raises(oa.OfferOutOfBounds):
            self._ok(amount_cents=0)
        with pytest.raises(oa.OfferOutOfBounds):
            self._ok(term_months=0)


# ---------------------------------------------------------------------------
# Waiting time
# ---------------------------------------------------------------------------


class TestWaitingSeconds:
    NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)

    def test_from_status_updated_at(self):
        anchor = self.NOW - timedelta(hours=3)
        assert oa.waiting_seconds(anchor, self.NOW - timedelta(days=9), self.NOW) == 3 * 3600

    def test_falls_back_to_created_at(self):
        created = self.NOW - timedelta(minutes=30)
        assert oa.waiting_seconds(None, created, self.NOW) == 1800

    def test_clamped_at_zero(self):
        assert oa.waiting_seconds(self.NOW + timedelta(seconds=5), None, self.NOW) == 0

    def test_none_when_no_anchor(self):
        assert oa.waiting_seconds(None, None, self.NOW) is None


# ---------------------------------------------------------------------------
# Flag-based notification suppression (stubbed session — no DB)
# ---------------------------------------------------------------------------


class _StubResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _StubSession:
    def __init__(self, row=None):
        self.row = row
        self.calls: list[dict] = []

    def execute(self, stmt, params=None):
        self.calls.append(params or {})
        return _StubResult(self.row)


class TestNotificationSuppressed:
    def test_no_subject_short_circuits(self):
        db = _StubSession(row=(1,))
        assert flags_service.notification_suppressed(db) is False
        assert db.calls == []  # never touches the session

    def test_suppressed_when_row_found(self):
        db = _StubSession(row=(1,))
        assert flags_service.notification_suppressed(db, patient_id="p-1") is True
        assert db.calls[0] == {"patient_id": "p-1", "loan_id": None}

    def test_not_suppressed_when_no_row(self):
        db = _StubSession(row=None)
        assert (
            flags_service.notification_suppressed(db, patient_id="p-1", loan_id="l-1")
            is False
        )
        assert db.calls[0] == {"patient_id": "p-1", "loan_id": "l-1"}


class TestProcessorSuppressionMemo:
    def test_memoized_per_subject_pair(self, monkeypatch):
        from app.services.notification_processor import NotificationProcessor

        proc = NotificationProcessor(db=Mock(), dispatcher=object())
        calls = []

        def fake_check(db, *, patient_id=None, loan_id=None):
            calls.append((patient_id, loan_id))
            return True

        monkeypatch.setattr(
            "app.services.notification_processor.flags_service.notification_suppressed",
            fake_check,
        )
        assert proc._flag_suppressed("p-1", "l-1") is True
        assert proc._flag_suppressed("p-1", "l-1") is True  # memo hit
        assert proc._flag_suppressed("p-1", None) is True  # different key
        assert len(calls) == 2

    def test_skip_reason_constant_mentions_flag(self):
        assert "flag" in flags_service.SUPPRESSION_SKIP_REASON


# ---------------------------------------------------------------------------
# Migration chain pin
# ---------------------------------------------------------------------------


def test_migration_059_chain_pin():
    """059_originations_admin chains onto 054_hardship (the single head at
    branch time). The merge train re-chains this on collision — this pin makes
    the re-chain a conscious, test-updating change."""
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "059_originations_admin.py"
    )
    spec = importlib.util.spec_from_file_location("migration_059", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "059_originations_admin"
    assert mod.down_revision == "058_underwriting_depth"


# ---------------------------------------------------------------------------
# Editable-field whitelist stays aligned with the ORM model
# ---------------------------------------------------------------------------


def test_editable_fields_exist_on_model():
    from app.models.platform.credit_application import PlatformCreditApplication

    for field_name in oa.EDITABLE_FIELDS:
        assert hasattr(PlatformCreditApplication, field_name), (
            f"EDITABLE_FIELDS lists {field_name!r} which is not a column on "
            f"PlatformCreditApplication"
        )
