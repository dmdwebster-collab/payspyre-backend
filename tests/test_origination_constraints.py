"""The credit product AS the calculation backbone of the New Application form.

Covers the two new endpoints and the shared validator behind them:

  * constraints resolve from the product — INCLUDING the ``policy_config``
    ``due_dates`` values that were stored-but-read-by-nothing until now;
  * an out-of-range amount / term / rate / frequency / first-payment-date is
    REFUSED with a FIELD-LEVEL error the form can attach to an input;
  * the quote's preliminary schedule is the existing amortization engine's
    schedule, row for row;
  * the APR is the Canadian Cost of Borrowing figure (SOR/2001-104 s.3-4) — it
    is NOT the nominal annual rate once a fee exists, and it matches a
    hand-computed worked example (``TestCanadianCostOfBorrowingAPR``, whose
    docstring carries the full derivation);
  * the single-provider / single-product auto-select flags;
  * server-side enforcement on the application-CREATE paths.

Most of this is DB-FREE (the resolver and validator are pure over a product
row); the pick list and the endpoints use the live test DB.
"""
import uuid
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.v1.api import api_router
from app.core.auth import get_current_user
from app.db.base import get_db
from app.models.loan import Vendor
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.patient import PlatformPatient
from app.schemas.pricing_config import (
    fee_rows_cents,
    parse_pricing_config,
    quote_fees_cents,
)
from app.schemas.product_policy_config import POLICY_SECTION_STATUS
from app.services import loan_quote, origination_constraints as oc
from app.services.loan_servicing import generate_amortization_schedule

_BASE = "/api/v1/admin/origination"
TODAY = date(2026, 3, 2)


# ---------------------------------------------------------------------------
# Product fixtures (DB-free stubs — the resolver only reads the row)
# ---------------------------------------------------------------------------


def _product(pricing=None, policy=None, **over):
    """A credit-product row stub with a fully typed pricing config."""
    base_pricing = {
        "schema_version": 1,
        "interest": {
            "annual_rate_bps": 1200,
            "min_rate_bps": 900,
            "max_rate_bps": 2400,
            "rate_edit_roles": ["admin"],
        },
        "payment_frequencies": ["monthly", "bi_weekly"],
        "term_min_months": 12,
        "term_max_months": 60,
        "default_term_months": 24,
        "fees": [],
    }
    fields = {
        "id": uuid.uuid4(),
        "code": "test_product_v1",
        "name": "Test Product v1",
        "version": 3,
        "status": "active",
        "currency": "CAD",
        "min_amount_cents": 500_000,
        "max_amount_cents": 5_000_000,
        "provinces": None,
        "pricing_config": pricing if pricing is not None else base_pricing,
        "policy_config": policy,
    }
    fields.update(over)
    return SimpleNamespace(**fields)


# ===========================================================================
# 1. Constraints resolve from the credit product
# ===========================================================================


class TestConstraintsResolution:
    def test_every_bound_and_default_comes_from_the_product(self):
        c = oc.resolve_constraints(_product(), as_of=TODAY)

        assert (c.amount.min_cents, c.amount.max_cents) == (500_000, 5_000_000)
        assert c.amount.default_cents == 500_000  # unset default -> the minimum
        assert (c.term.min_months, c.term.max_months) == (12, 60)
        assert c.term.default_months == 24  # pricing_config.default_term_months
        assert (c.rate.min_bps, c.rate.max_bps, c.rate.default_bps) == (900, 2400, 1200)
        assert c.rate.edit_roles == ["admin"]
        assert c.rate.editable is True
        assert [o.value for o in c.frequency.options] == ["monthly", "bi_weekly"]
        assert c.frequency.default == "monthly"  # unset -> Monthly when offered
        assert c.credit_product_version == 3

    def test_config_amount_bounds_can_only_NARROW_the_product_columns(self):
        p = _product()
        p.pricing_config = {**p.pricing_config, "amount_min_cents": 800_000,
                            "amount_max_cents": 9_000_000}
        c = oc.resolve_constraints(p, as_of=TODAY)
        assert c.amount.min_cents == 800_000        # narrowed
        assert c.amount.max_cents == 5_000_000      # the column still caps it

    def test_new_pricing_defaults_are_optional_and_behaviour_preserving(self):
        """The two fields ADDED to PricingConfig default to the pre-existing rule."""
        p = _product()
        p.pricing_config = {
            **p.pricing_config,
            "default_payment_frequency": "bi_weekly",
            "default_amount_cents": 1_250_000,
        }
        c = oc.resolve_constraints(p, as_of=TODAY)
        assert c.frequency.default == "bi_weekly"
        assert c.amount.default_cents == 1_250_000

    def test_default_amount_is_clamped_into_the_window(self):
        p = _product()
        p.pricing_config = {**p.pricing_config, "default_amount_cents": 9_999_999}
        assert oc.resolve_constraints(p, as_of=TODAY).amount.default_cents == 5_000_000

    def test_legacy_free_form_pricing_config_still_resolves(self):
        """The seeded product's pre-schema shape must not need a re-save."""
        p = _product(pricing={"apr_range": [7.99, 28.99],
                              "term_options": [24, 36, 48, 60],
                              "origination_fee_pct": 0.025})
        c = oc.resolve_constraints(p, as_of=TODAY)
        assert (c.rate.min_bps, c.rate.max_bps, c.rate.default_bps) == (799, 2899, 799)
        assert c.term.options == [24, 36, 48, 60]
        assert (c.term.min_months, c.term.max_months) == (24, 60)
        assert len(c.frequency.options) == 4  # legacy configs offered all four
        # ...in a synthesized order; Monthly is the default, not whatever the
        # enum happens to list first.
        assert c.frequency.default == "monthly"

    def test_first_offered_frequency_wins_when_monthly_is_not_offered(self):
        p = _product()
        p.pricing_config = {**p.pricing_config,
                            "payment_frequencies": ["bi_weekly", "weekly"]}
        assert oc.resolve_constraints(p, as_of=TODAY).frequency.default == "bi_weekly"


class TestPreviouslyUnreadPolicyConfig:
    """``policy_config.due_dates`` had NO reader before this module.

    (See ``POLICY_SECTION_STATUS['due_dates']`` — flipped from
    ``pending_consumer`` to ``consumed`` by this workstream.)
    """

    POLICY = {
        "schema_version": 1,
        "due_dates": {
            "use_change_start_date": True,
            "default_start_shift_days": 5,
            "use_change_first_due_date": True,
            "first_due_min_days": 10,
            "first_due_max_days": 20,
        },
    }

    def test_due_dates_section_is_declared_consumed(self):
        assert POLICY_SECTION_STATUS["due_dates"]["status"] == "consumed"
        assert POLICY_SECTION_STATUS["repayment_modes"]["status"] == "consumed"
        assert POLICY_SECTION_STATUS["schedule_building"]["status"] == "consumed"

    def test_start_date_default_is_today_plus_the_configured_shift(self):
        c = oc.resolve_constraints(_product(policy=self.POLICY), as_of=TODAY)
        assert c.start_date.default == TODAY + timedelta(days=5)
        assert c.start_date.min == TODAY + timedelta(days=5)
        assert c.start_date.min_offset_days == 5

    def test_first_payment_window_is_relative_to_the_start_date(self):
        c = oc.resolve_constraints(_product(policy=self.POLICY), as_of=TODAY)
        start = TODAY + timedelta(days=5)
        assert c.first_payment_date.min == start + timedelta(days=10)
        assert c.first_payment_date.max == start + timedelta(days=20)
        assert c.first_payment_date.relative_to == "start_date"
        # Monthly would naturally land ~30 days out; the product's 20-day
        # ceiling pulls the DEFAULT back into the window.
        assert c.first_payment_date.default == start + timedelta(days=20)

    def test_shipped_defaults_preserve_todays_behaviour(self):
        """NULL policy_config -> shift 1 day, window 1..45 days (schema default)."""
        c = oc.resolve_constraints(_product(policy=None), as_of=TODAY)
        assert c.start_date.default == TODAY + timedelta(days=1)
        assert c.first_payment_date.min_offset_days == 1
        assert c.first_payment_date.max_offset_days == 45
        # Natural monthly date sits inside 1..45, so it IS the default.
        assert c.first_payment_date.default == date(2026, 4, 3)

    def test_schedule_building_and_repayment_modes_are_surfaced(self):
        c = oc.resolve_constraints(_product(), as_of=TODAY)
        assert c.schedule.loan_type == "daily_simple_interest"
        assert c.schedule.calculation_basis == "remaining_principal"
        assert c.repayment_modes.default == "regular"
        assert "payoff" in [m.key for m in c.repayment_modes.options]
        assert c.repayment_modes.future_installments_recalc == "keep_installment_reduce_count"

    def test_locked_date_switches_are_reported(self):
        policy = {"schema_version": 1, "due_dates": {
            "use_change_start_date": False, "use_change_first_due_date": False,
            "default_start_shift_days": 2, "first_due_min_days": 1,
            "first_due_max_days": 45}}
        c = oc.resolve_constraints(_product(policy=policy), as_of=TODAY)
        assert c.start_date.editable is False
        assert c.first_payment_date.editable is False


# ===========================================================================
# 2. Out-of-range entry is REFUSED, per field
# ===========================================================================


def _errors(selection, product=None, policy=None):
    c = oc.resolve_constraints(product or _product(policy=policy), as_of=TODAY)
    return {e.field: e for e in oc.validate_selection(c, selection)}


class TestFieldLevelRejection:
    def test_amount_below_min_and_above_max(self):
        for amount in (499_999, 5_000_001):
            errs = _errors(oc.SelectionInput(amount_cents=amount))
            assert "amount_cents" in errs, amount
            assert errs["amount_cents"].code == "amount_out_of_range"
            assert (errs["amount_cents"].min, errs["amount_cents"].max) == (500_000, 5_000_000)

    def test_amount_at_the_boundaries_is_accepted(self):
        for amount in (500_000, 5_000_000):
            assert _errors(oc.SelectionInput(amount_cents=amount)) == {}

    def test_term_outside_the_band(self):
        errs = _errors(oc.SelectionInput(term_months=72))
        assert errs["term_months"].code == "term_out_of_range"
        assert (errs["term_months"].min, errs["term_months"].max) == (12, 60)

    def test_term_not_in_the_offered_option_list(self):
        p = _product(pricing={"apr_range": [7.99, 28.99],
                              "term_options": [24, 36, 48, 60]})
        errs = _errors(oc.SelectionInput(term_months=30), product=p)
        assert errs["term_months"].code == "term_not_offered"
        assert errs["term_months"].allowed == [24, 36, 48, 60]

    def test_rate_outside_the_product_band(self):
        errs = _errors(oc.SelectionInput(annual_rate_bps=2500))
        assert errs["annual_rate_bps"].code == "rate_out_of_band"
        assert (errs["annual_rate_bps"].min, errs["annual_rate_bps"].max) == (900, 2400)

    def test_frequency_not_offered(self):
        errs = _errors(oc.SelectionInput(frequency="weekly"))
        assert errs["frequency"].code == "frequency_not_offered"
        assert errs["frequency"].allowed == ["monthly", "bi_weekly"]
        # spelling tolerance still applies to an OFFERED frequency
        assert _errors(oc.SelectionInput(frequency="bi-weekly")) == {}

    def test_start_date_before_the_products_minimum_offset(self):
        errs = _errors(oc.SelectionInput(start_date=TODAY),
                       policy=TestPreviouslyUnreadPolicyConfig.POLICY)
        assert errs["start_date"].code == "start_date_too_early"
        assert errs["start_date"].min == TODAY + timedelta(days=5)

    def test_first_payment_date_outside_the_configured_window(self):
        policy = TestPreviouslyUnreadPolicyConfig.POLICY
        start = TODAY + timedelta(days=5)
        for bad in (start + timedelta(days=9), start + timedelta(days=21)):
            errs = _errors(
                oc.SelectionInput(start_date=start, first_payment_date=bad), policy=policy
            )
            assert errs["first_payment_date"].code == "first_payment_date_out_of_window"
            assert errs["first_payment_date"].min == start + timedelta(days=10)
            assert errs["first_payment_date"].max == start + timedelta(days=20)
        # inside the window: accepted
        assert _errors(
            oc.SelectionInput(start_date=start, first_payment_date=start + timedelta(days=15)),
            policy=policy,
        ) == {}

    def test_first_payment_window_follows_a_CUSTOM_start_date(self):
        policy = TestPreviouslyUnreadPolicyConfig.POLICY
        start = TODAY + timedelta(days=30)  # later than the default start
        ok = start + timedelta(days=12)
        assert _errors(
            oc.SelectionInput(start_date=start, first_payment_date=ok), policy=policy
        ) == {}

    def test_custom_first_payment_refused_when_the_product_forbids_it(self):
        policy = {"schema_version": 1, "due_dates": {"use_change_first_due_date": False}}
        errs = _errors(
            oc.SelectionInput(first_payment_date=TODAY + timedelta(days=20)), policy=policy
        )
        assert errs["first_payment_date"].code == "first_payment_date_not_editable"

    def test_every_breach_is_reported_at_once(self):
        errs = _errors(
            oc.SelectionInput(amount_cents=1, term_months=999, annual_rate_bps=9999,
                              frequency="daily")
        )
        assert set(errs) == {"amount_cents", "term_months", "annual_rate_bps", "frequency"}

    def test_unspecified_fields_are_skipped(self):
        assert oc.validate_selection(
            oc.resolve_constraints(_product(), as_of=TODAY), oc.SelectionInput()
        ) == []

    def test_violation_detail_is_machine_readable(self):
        with pytest.raises(oc.ConstraintViolation) as exc:
            oc.assert_selection_allowed(
                oc.resolve_constraints(_product(), as_of=TODAY),
                oc.SelectionInput(amount_cents=10),
            )
        detail = exc.value.as_detail()
        assert detail["errors"][0]["field"] == "amount_cents"
        assert detail["errors"][0]["code"] == "amount_out_of_range"


# ===========================================================================
# 3. The quote reuses the existing engines
# ===========================================================================


class TestQuoteUsesTheExistingEngines:
    def test_monthly_schedule_matches_generate_amortization_schedule(self):
        """Row for row against ``loan_servicing.generate_amortization_schedule``
        — the 30/360 engine that books the real loan. Same principal, same
        interest, same dates, so the preliminary schedule IS the final one."""
        p = _product()
        first = date(2026, 4, 1)
        q = oc.build_quote(
            p,
            oc.SelectionInput(amount_cents=2_000_000, term_months=36,
                              annual_rate_bps=1200, frequency="monthly",
                              start_date=date(2026, 3, 3), first_payment_date=first),
            as_of=TODAY,
        )
        engine = generate_amortization_schedule(2_000_000, 1200, 36, first)
        assert len(q.schedule) == len(engine) == 36
        for row, ref in zip(q.schedule, engine):
            assert row.number == ref.installment_number
            assert row.date == ref.due_date
            assert row.principal_cents == ref.principal_cents
            assert row.interest_cents == ref.interest_cents
            assert row.payment_cents == ref.total_cents  # no fees on this product

    def test_schedule_ties_out_exactly(self):
        p = _product()
        p.pricing_config = {**p.pricing_config, "fees": [
            {"fee_type": "origination", "calc": "fixed_cents", "amount": 25_00,
             "charge_timing": "at_origination"},
            {"fee_type": "administration", "calc": "fixed_cents", "amount": 1_00,
             "charge_timing": "per_payment"},
        ]}
        q = oc.build_quote(
            p, oc.SelectionInput(amount_cents=1_000_000, term_months=24,
                                 annual_rate_bps=1500, frequency="bi_weekly"),
            as_of=TODAY,
        )
        assert sum(r.principal_cents for r in q.schedule) == q.totals.principal_cents
        assert sum(r.interest_cents for r in q.schedule) == q.totals.interest_cents
        assert sum(r.fees_cents for r in q.schedule) == q.totals.fees_cents
        assert sum(r.payment_cents for r in q.schedule) == q.totals.total_cents
        assert q.totals.total_cents == (
            q.totals.principal_cents + q.totals.interest_cents + q.totals.fees_cents
        )
        assert q.schedule[-1].balance_cents == 0
        # Principal + Interest + Fees = Total (APR)
        assert q.disclosure_line.endswith(f"({q.apr_bps / 100:.2f}% APR)")

    def test_fee_rows_sum_to_the_one_fee_implementation(self):
        cfg = parse_pricing_config({
            "schema_version": 1,
            "fees": [
                {"fee_type": "origination", "calc": "rate_bps", "amount": 250,
                 "charge_timing": "at_origination"},
                {"fee_type": "administration", "calc": "fixed_cents", "amount": 100,
                 "charge_timing": "per_payment"},
                {"fee_type": "nsf", "calc": "fixed_cents", "amount": 4500,
                 "charge_timing": "on_event"},  # contingent -> excluded
            ],
        })
        for freq, term in (("monthly", 24), ("bi_weekly", 24), ("weekly", 12)):
            n = loan_quote.num_payments(term, freq)
            rows = fee_rows_cents(cfg, 1_000_000, freq, n)
            assert len(rows) == n
            assert sum(rows) == quote_fees_cents(cfg, 1_000_000, term, freq)
            assert rows[0] > rows[1]          # the at-origination lump lands first
            assert 4500 not in rows           # NSF never enters the cost of borrowing

    def test_approximate_payment_includes_the_per_payment_fee(self):
        p = _product()
        p.pricing_config = {**p.pricing_config, "fees": [
            {"fee_type": "administration", "calc": "fixed_cents", "amount": 100,
             "charge_timing": "per_payment"},
        ]}
        q = oc.build_quote(
            p, oc.SelectionInput(amount_cents=1_000_000, term_months=12,
                                 annual_rate_bps=1200, frequency="monthly"),
            as_of=TODAY,
        )
        assert q.approximate_payment_cents == q.installment_cents + 100
        assert q.approximate_payment_label == "Approximate Monthly Payment"

    def test_dates_step_per_frequency(self):
        p = _product()
        p.pricing_config = {**p.pricing_config,
                            "payment_frequencies": ["monthly", "bi_weekly", "weekly",
                                                    "semi_monthly"]}
        first = date(2026, 1, 31)
        for freq, expected_second in (
            ("monthly", date(2026, 2, 28)),      # month-end clamp, engine's rule
            ("bi_weekly", date(2026, 2, 14)),
            ("weekly", date(2026, 2, 7)),
            ("semi_monthly", date(2026, 2, 15)),  # +15 days for the odd half-month
        ):
            q = oc.build_quote(
                p, oc.SelectionInput(amount_cents=1_000_000, term_months=12,
                                     frequency=freq, start_date=date(2026, 1, 1),
                                     first_payment_date=first),
                as_of=date(2025, 12, 31),  # so 2026-01-01 is the earliest start
            )
            assert q.schedule[0].date == first, freq
            assert q.schedule[1].date == expected_second, freq

    def test_quote_with_no_inputs_uses_the_products_defaults(self):
        q = oc.build_quote(_product(), oc.SelectionInput(), as_of=TODAY)
        assert q.amount_cents == 500_000
        assert q.term_months == 24
        assert q.annual_rate_bps == 1200
        assert q.frequency == "monthly"
        assert q.start_date == TODAY + timedelta(days=1)

    def test_quote_refuses_an_out_of_range_input(self):
        with pytest.raises(oc.ConstraintViolation):
            oc.build_quote(_product(), oc.SelectionInput(amount_cents=99), as_of=TODAY)


# ===========================================================================
# 4. APR — the Canadian Cost of Borrowing figure, NOT the nominal rate
# ===========================================================================


class TestCanadianCostOfBorrowingAPR:
    """WORKED EXAMPLE (hand-computable, SOR/2001-104 s.3(1)):

        Advance      P0 = $10,000.00   (1,000,000 cents)
        Contract rate     12.00%/yr nominal, monthly compounding periods
        Term              12 monthly instalments
        Fee               $300.00 origination, charged at origination
                          (non-contingent -> part of the cost of borrowing)

    Monthly rate r = 0.12/12 = 0.01. The annuity instalment is
        P0 * r / (1 - (1+r)^-12) = 1,000,000 * 0.01 / (1 - 1.01^-12)
                                 = 88,848.79 -> 88,849 cents.

    Amortizing at whole cents (interest = round(balance * 0.01), the final row
    absorbing the remaining principal) gives, in cents:

        total interest  I  = 66,186
        sum of the opening balances (= the principal outstanding at the end of
        each period BEFORE that period's payment, per s.3(2)(b))
                           = 6,618,534, over 12 periods

    Therefore, per s.3(1)  APR = C / (T x P) x 100 with
        C = I + fees = 66,186 + 30,000 = 96,186 cents
        P = 6,618,534 / 12          = 551,544.50 cents
        T = 12 months               = 1.0 year   (s.3(2)(c): a month is 1/12)

        APR = 96,186 / (1.0 x 551,544.50) = 0.1743939... = 17.44%

    i.e. **1744 bps against a 1200 bps contract rate** — the $300 fee adds 5.44
    percentage points of disclosed cost. This is exactly why the APR line on
    the form is not the annual interest rate.
    """

    AMOUNT = 1_000_000
    RATE_BPS = 1200
    FEE_CENTS = 30_000
    EXPECTED_APR_BPS = 1744

    def _fee_product(self):
        p = _product()
        p.pricing_config = {
            **p.pricing_config,
            "payment_frequencies": ["monthly"],
            "fees": [{"fee_type": "origination", "calc": "fixed_cents",
                      "amount": self.FEE_CENTS, "charge_timing": "at_origination"}],
        }
        return p

    def test_engine_matches_the_hand_computed_figure(self):
        assert loan_quote.compute_apr_bps(
            self.AMOUNT, self.RATE_BPS, 12, "monthly", self.FEE_CENTS
        ) == self.EXPECTED_APR_BPS

    def test_hand_computation_reproduced_from_first_principles(self):
        """Re-derive C, P and T here rather than trusting the engine's walk."""
        r = self.RATE_BPS / 10_000 / 12
        payment = round(self.AMOUNT * r / (1 - (1 + r) ** -12))
        balance, opening_sum, interest_total = self.AMOUNT, 0, 0
        for i in range(1, 13):
            opening_sum += balance
            interest = round(balance * r)
            principal = payment - interest if i < 12 else balance
            interest_total += interest
            balance -= principal
        assert (payment, interest_total, opening_sum, balance) == (
            88_849, 66_186, 6_618_534, 0
        )
        C = interest_total + self.FEE_CENTS
        P = opening_sum / 12
        T = 12 / 12
        assert (C, P, T) == (96_186, 551_544.5, 1.0)
        assert round(C / (T * P) * 10_000) == self.EXPECTED_APR_BPS

    def test_the_quote_discloses_that_apr(self):
        q = oc.build_quote(
            self._fee_product(),
            oc.SelectionInput(amount_cents=self.AMOUNT, term_months=12,
                              annual_rate_bps=self.RATE_BPS, frequency="monthly"),
            as_of=TODAY,
        )
        assert q.apr_bps == self.EXPECTED_APR_BPS
        assert q.apr_bps > q.annual_rate_bps  # APR != nominal rate
        assert q.totals.interest_cents == 66_186
        assert q.totals.fees_cents == self.FEE_CENTS
        assert q.cost_of_borrowing_cents == 96_186
        assert "17.44% APR" in q.disclosure_line
        assert "SOR/2001-104" in q.apr_basis

    def test_s4_no_fees_means_apr_IS_the_annual_interest_rate(self):
        """SOR/2001-104 s.4 — with interest as the only cost of borrowing the
        APR *is* the contract rate. Equality here is the regulation, not a bug."""
        q = oc.build_quote(
            _product(),
            oc.SelectionInput(amount_cents=self.AMOUNT, term_months=12,
                              annual_rate_bps=self.RATE_BPS, frequency="monthly"),
            as_of=TODAY,
        )
        assert q.totals.fees_cents == 0
        assert q.apr_bps == self.RATE_BPS

    def test_contingent_nsf_fee_does_not_inflate_the_apr(self):
        p = self._fee_product()
        p.pricing_config = {**p.pricing_config, "fees": [
            *p.pricing_config["fees"],
            {"fee_type": "nsf", "calc": "fixed_cents", "amount": 4500,
             "charge_timing": "on_event", "add_on": True},
        ]}
        q = oc.build_quote(
            p, oc.SelectionInput(amount_cents=self.AMOUNT, term_months=12,
                                 annual_rate_bps=self.RATE_BPS, frequency="monthly"),
            as_of=TODAY,
        )
        assert q.apr_bps == self.EXPECTED_APR_BPS
        assert q.totals.fees_cents == self.FEE_CENTS

    def test_per_payment_fee_costs_more_apr_at_a_higher_frequency(self):
        """Dave's point: a $1/payment fee distorts APR far more weekly than
        monthly, because it recurs more often over the same principal."""
        p = _product()
        p.pricing_config = {
            **p.pricing_config,
            "payment_frequencies": ["monthly", "bi_weekly", "weekly"],
            "fees": [{"fee_type": "administration", "calc": "fixed_cents",
                      "amount": 100, "charge_timing": "per_payment"}],
        }
        aprs = {
            f: oc.build_quote(
                p, oc.SelectionInput(amount_cents=1_000_000, term_months=24,
                                     annual_rate_bps=1200, frequency=f),
                as_of=TODAY,
            ).apr_bps
            for f in ("monthly", "bi_weekly", "weekly")
        }
        assert aprs["weekly"] > aprs["bi_weekly"] > aprs["monthly"] > 1200


# ===========================================================================
# 5. Vendor -> provider -> product pick list (DB)
# ===========================================================================


def _admin():
    return SimpleNamespace(
        id=uuid.uuid4(), roles=[SimpleNamespace(role=SimpleNamespace(name="admin"))]
    )


@pytest.fixture
def client(db_session: Session):
    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = _admin
    yield TestClient(app)
    app.dependency_overrides.clear()


def _seed_product(db: Session) -> PlatformCreditProduct:
    p = (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == "dental_full_arch_v1")
        .one()
    )
    return p


def _vendor(db: Session, province="BC") -> Vendor:
    v = Vendor(
        business_name=f"Clinic {uuid.uuid4().hex[:6]}",
        business_type="corporation",
        contact_name="Dr Who",
        email=f"v-{uuid.uuid4().hex[:8]}@example.com",
        phone="2505551234",
        address_line1="1 Main St",
        city="Kelowna",
        province=province,
        postal_code="V1Y1A1",
        status="active",
    )
    db.add(v)
    db.commit()
    db.refresh(v)
    return v


def _application(db: Session, vendor, product, provider_name):
    patient = PlatformPatient(
        email=f"p-{uuid.uuid4().hex[:8]}@example.com",
        legal_first_name="Sam",
        legal_last_name="Ito",
    )
    db.add(patient)
    db.commit()
    row = PlatformCreditApplication(
        patient_id=patient.id,
        credit_product_id=product.id,
        credit_product_version=1,
        requested_amount_cents=2_000_000,
        requested_amount_source="clinic",
        vendor_id=vendor.id,
        provider_name=provider_name,
        status="started",
    )
    db.add(row)
    db.commit()
    return row


class TestPickList:
    def test_single_product_is_flagged_and_auto_selected(self, db_session):
        picks = oc.product_pick_list(db_session)
        assert picks.single_product is True
        assert len(picks.products) == 1
        assert picks.default_credit_product_id == _seed_product(db_session).id
        assert picks.products[0].is_default is True

    def test_two_products_clears_the_auto_select_flag(self, db_session):
        seed = _seed_product(db_session)
        db_session.add(
            PlatformCreditProduct(
                code=f"second_{uuid.uuid4().hex[:6]}",
                name="Second Product",
                vertical="dental",
                status="active",
                min_amount_cents=100_000,
                max_amount_cents=900_000,
                currency="CAD",
                verification_matrix=seed.verification_matrix,
                decision_ruleset="x.yaml",
                pricing_config={"schema_version": 1, "fees": []},
                funding_source="payspyre_capital",
            )
        )
        db_session.commit()
        picks = oc.product_pick_list(db_session)
        assert picks.single_product is False
        assert len(picks.products) == 2
        # No usage history and no single candidate -> nothing is pre-selected.
        assert picks.default_credit_product_id is None

    def test_single_provider_is_flagged_and_auto_selected(self, db_session):
        vendor = _vendor(db_session)
        product = _seed_product(db_session)
        _application(db_session, vendor, product, "Dr. Singh")
        _application(db_session, vendor, product, "Dr. Singh")

        picks = oc.product_pick_list(db_session, vendor_id=vendor.id)
        assert picks.single_provider is True
        assert picks.provider == "Dr. Singh"
        assert picks.providers[0].application_count == 2
        assert picks.vendor_name == vendor.business_name

    def test_multiple_providers_are_listed_without_auto_select(self, db_session):
        vendor = _vendor(db_session)
        product = _seed_product(db_session)
        _application(db_session, vendor, product, "Dr. Singh")
        _application(db_session, vendor, product, "Dr. Tremblay")

        picks = oc.product_pick_list(db_session, vendor_id=vendor.id)
        assert picks.single_provider is False
        assert [p.name for p in picks.providers] == ["Dr. Singh", "Dr. Tremblay"]
        assert picks.provider is None

    def test_province_restricted_product_is_hidden_from_an_out_of_province_vendor(
        self, db_session
    ):
        product = _seed_product(db_session)
        product.provinces = ["ON"]
        db_session.commit()
        vendor = _vendor(db_session, province="BC")
        assert oc.product_pick_list(db_session, vendor_id=vendor.id).products == []
        # Fail-open when the vendor's free-text province is not a 2-letter code.
        loose = _vendor(db_session, province="British Columbia")
        assert len(oc.product_pick_list(db_session, vendor_id=loose.id).products) == 1


# ===========================================================================
# 6. The endpoints
# ===========================================================================


class TestConstraintsEndpoint:
    def test_one_call_returns_selectors_and_bounds(self, client, db_session):
        product = _seed_product(db_session)
        r = client.get(f"{_BASE}/constraints", params={"as_of": TODAY.isoformat()})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["selection"]["single_product"] is True
        assert body["selection"]["default_credit_product_id"] == str(product.id)
        c = body["constraints"]
        assert c["credit_product_id"] == str(product.id)
        assert c["amount"] == {
            "min_cents": 1_500_000, "max_cents": 8_000_000,
            "default_cents": 1_500_000, "currency": "CAD",
        }
        assert c["term"]["options"] == [24, 36, 48, 60]
        assert c["rate"]["default_bps"] == 799
        assert c["frequency"]["default"] == "monthly"
        assert c["start_date"]["default"] == (TODAY + timedelta(days=1)).isoformat()
        assert c["first_payment_date"]["max_offset_days"] == 45
        assert c["sources"]["start_date"].endswith("due_dates.default_start_shift_days")

    def test_vendor_scoping_returns_the_provider_list(self, client, db_session):
        vendor = _vendor(db_session)
        _application(db_session, vendor, _seed_product(db_session), "Dr. Singh")
        r = client.get(f"{_BASE}/constraints", params={"vendor_id": str(vendor.id)})
        assert r.status_code == 200, r.text
        assert r.json()["selection"]["providers"][0]["name"] == "Dr. Singh"

    def test_unknown_product_is_404(self, client, db_session):
        r = client.get(
            f"{_BASE}/constraints", params={"credit_product_id": str(uuid.uuid4())}
        )
        assert r.status_code == 404


class TestQuoteEndpoint:
    def test_live_quote_returns_the_disclosure_line_and_schedule(self, client, db_session):
        product = _seed_product(db_session)
        r = client.post(
            f"{_BASE}/quote",
            json={
                "credit_product_id": str(product.id),
                "amount_cents": 2_000_000,
                "term_months": 24,
                "frequency": "monthly",
                "as_of": TODAY.isoformat(),
            },
        )
        assert r.status_code == 200, r.text
        q = r.json()
        assert q["num_payments"] == 24
        assert q["approximate_payment_label"] == "Approximate Monthly Payment"
        assert set(q["schedule"][0]) == {
            "number", "date", "payment_cents", "principal_cents",
            "interest_cents", "fees_cents", "balance_cents",
        }
        # 2.5% origination fee on the seeded product -> APR above the 7.99% rate
        assert q["totals"]["fees_cents"] == 50_000
        assert q["apr_bps"] > q["annual_rate_bps"] == 799
        assert q["schedule"][0]["fees_cents"] == 50_000
        assert q["schedule"][1]["fees_cents"] == 0
        assert q["schedule"][-1]["balance_cents"] == 0

    def test_out_of_range_amount_is_a_field_level_422(self, client, db_session):
        product = _seed_product(db_session)
        r = client.post(
            f"{_BASE}/quote",
            json={"credit_product_id": str(product.id), "amount_cents": 100},
        )
        assert r.status_code == 422, r.text
        errors = r.json()["detail"]["errors"]
        assert errors[0]["field"] == "amount_cents"
        assert errors[0]["code"] == "amount_out_of_range"
        assert errors[0]["min"] == 1_500_000

    def test_out_of_window_first_payment_date_is_rejected(self, client, db_session):
        product = _seed_product(db_session)
        r = client.post(
            f"{_BASE}/quote",
            json={
                "credit_product_id": str(product.id),
                "amount_cents": 2_000_000,
                "term_months": 24,
                "as_of": TODAY.isoformat(),
                "start_date": (TODAY + timedelta(days=1)).isoformat(),
                "first_payment_date": (TODAY + timedelta(days=200)).isoformat(),
            },
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["errors"][0]["field"] == "first_payment_date"

    def test_quote_needs_only_the_product(self, client, db_session):
        product = _seed_product(db_session)
        r = client.post(f"{_BASE}/quote", json={"credit_product_id": str(product.id)})
        assert r.status_code == 200, r.text
        assert r.json()["amount_cents"] == 1_500_000  # the product's default


# ===========================================================================
# 7. Server-side enforcement on the CREATE path
# ===========================================================================


class TestCreatePathEnforcement:
    """The rules cannot be bypassed by posting straight to the API."""

    def _profile(self, db: Session):
        from app.services import customer_profile as profiles

        return profiles.create_borrower(
            db,
            values={
                "personal": {"first_name": "Rae", "last_name": "Kim",
                             "date_of_birth": "1990-01-01", "citizenship": "canadian"},
                "contact": {"email": f"rae-{uuid.uuid4().hex[:8]}@example.ca",
                            "main_phone": f"25055{uuid.uuid4().int % 100000:05d}"},
            },
            actor="staff-1",
            source="staff",
        )

    def test_out_of_range_amount_is_refused_with_a_field_error(self, client, db_session):
        profile = self._profile(db_session)
        product = _seed_product(db_session)
        r = client.post(
            f"/api/v1/admin/customer-profiles/{profile.id}/applications",
            json={
                "credit_product_id": str(product.id),
                "requested_amount_cents": 100,  # below the product's 1,500,000 min
            },
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["errors"][0]["field"] == "amount_cents"

    def test_out_of_band_rate_is_refused(self, client, db_session):
        profile = self._profile(db_session)
        product = _seed_product(db_session)
        r = client.post(
            f"/api/v1/admin/customer-profiles/{profile.id}/applications",
            json={
                "credit_product_id": str(product.id),
                "requested_amount_cents": 2_000_000,
                "requested_annual_rate_bps": 4_000,  # above the 28.99% ceiling
            },
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["errors"][0]["field"] == "annual_rate_bps"

    def test_first_payment_date_outside_the_window_is_refused(self, client, db_session):
        profile = self._profile(db_session)
        product = _seed_product(db_session)
        r = client.post(
            f"/api/v1/admin/customer-profiles/{profile.id}/applications",
            json={
                "credit_product_id": str(product.id),
                "requested_amount_cents": 2_000_000,
                "requested_term_months": 24,
                "start_date": (date.today() + timedelta(days=1)).isoformat(),
                "use_custom_first_due_date": True,
                "first_due_date": (date.today() + timedelta(days=200)).isoformat(),
            },
        )
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["errors"][0]["field"] == "first_payment_date"

    def test_a_compliant_application_still_creates(self, client, db_session):
        profile = self._profile(db_session)
        product = _seed_product(db_session)
        r = client.post(
            f"/api/v1/admin/customer-profiles/{profile.id}/applications",
            json={
                "credit_product_id": str(product.id),
                "requested_amount_cents": 2_000_000,
                "requested_term_months": 36,
                "requested_annual_rate_bps": 1_200,
                "start_date": (date.today() + timedelta(days=1)).isoformat(),
                "use_custom_first_due_date": True,
                "first_due_date": (date.today() + timedelta(days=31)).isoformat(),
            },
        )
        assert r.status_code == 201, r.text
        assert r.json()["requested_amount_cents"] == 2_000_000
