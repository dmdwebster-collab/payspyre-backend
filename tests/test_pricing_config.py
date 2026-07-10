"""Typed PricingConfig schema + tolerant legacy loader + APR-per-frequency
validation (P0 WS-B). DB-free — pure schema/engine tests only."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.pricing_config import (
    ChargeTiming,
    FeeCalc,
    FeeConfig,
    FeeType,
    InterestConfig,
    PaymentFrequency,
    PricingConfig,
    PricingConfigError,
    origination_lump_fees_cents,
    parse_pricing_config,
    payments_in_term,
    quote_fees_cents,
)
from app.services import loan_quote


def _turnkey_demo_config(**overrides) -> dict:
    """The product Dave demoed in 07__WP_Settings_Part_1: 19.99% (9.99–23.99),
    admin $1/payment, NSF $45 on event, origination $25, late fee present but
    disabled (Canada policy)."""
    cfg = {
        "schema_version": 1,
        "interest": {
            "annual_rate_bps": 1999,
            "min_rate_bps": 999,
            "max_rate_bps": 2399,
            "rate_edit_roles": ["admin"],
        },
        "payment_frequencies": ["monthly", "bi_weekly"],
        "term_min_months": 12,
        "term_max_months": 48,
        "fees": [
            {"fee_type": "administration", "calc": "fixed_cents", "amount": 100,
             "charge_timing": "per_payment"},
            {"fee_type": "nsf", "calc": "fixed_cents", "amount": 4500,
             "charge_timing": "on_event", "add_on": True},
            {"fee_type": "origination", "calc": "fixed_cents", "amount": 2500,
             "charge_timing": "at_origination"},
            {"fee_type": "late", "calc": "fixed_cents", "amount": 0,
             "charge_timing": "on_event", "enabled": False},
        ],
    }
    cfg.update(overrides)
    return cfg


class TestSchemaValidation:
    def test_turnkey_demo_config_parses(self):
        cfg = PricingConfig.model_validate(_turnkey_demo_config())
        assert cfg.interest.annual_rate_bps == 1999
        assert cfg.payment_frequencies == [PaymentFrequency.MONTHLY, PaymentFrequency.BI_WEEKLY]
        assert len(cfg.fees) == 4

    def test_all_14_turnkey_fee_types_exist(self):
        assert {f.value for f in FeeType} == {
            "administration", "disbursement", "down_payment", "late",
            "late_unpaid_due", "nsf", "origination", "past_due_interest",
            "payment_holiday_interest", "payoff", "pre_approval",
            "pre_disbursement", "repayment", "sales_tax",
        }

    @pytest.mark.parametrize("late_type", ["late", "late_unpaid_due"])
    def test_enabled_late_fee_is_refused(self, late_type):
        # Canada policy: late fees stay DISABLED until counsel clears them.
        with pytest.raises(ValidationError, match="disabled in Canada"):
            FeeConfig(fee_type=late_type, calc="fixed_cents", amount=1000,
                      charge_timing="on_event", enabled=True)

    def test_disabled_late_fee_is_allowed(self):
        fee = FeeConfig(fee_type="late", calc="fixed_cents", amount=1000,
                        charge_timing="on_event", enabled=False)
        assert fee.enabled is False

    def test_interest_bounds_must_contain_default(self):
        with pytest.raises(ValidationError, match="min_rate_bps"):
            InterestConfig(annual_rate_bps=2500, min_rate_bps=999, max_rate_bps=2399)

    def test_interest_defaults_are_turnkey_demo_values(self):
        i = InterestConfig()
        assert (i.annual_rate_bps, i.min_rate_bps, i.max_rate_bps) == (1999, 999, 2399)
        assert i.rate_edit_roles == ["admin"]

    def test_negative_fee_amount_refused(self):
        with pytest.raises(ValidationError):
            FeeConfig(fee_type="nsf", calc="fixed_cents", amount=-1,
                      charge_timing="on_event")

    def test_duplicate_fee_definition_refused(self):
        cfg = _turnkey_demo_config()
        cfg["fees"].append(cfg["fees"][0])
        with pytest.raises(PricingConfigError, match="Duplicate fee"):
            parse_pricing_config(cfg)

    def test_amount_and_term_ordering(self):
        with pytest.raises(ValidationError, match="term_min_months"):
            PricingConfig(term_min_months=48, term_max_months=12)
        with pytest.raises(ValidationError, match="amount_min_cents"):
            PricingConfig(amount_min_cents=200, amount_max_cents=100)

    def test_unknown_key_on_typed_shape_refused(self):
        with pytest.raises(PricingConfigError, match="schema validation"):
            parse_pricing_config({"schema_version": 1, "intrest": {}})

    def test_frequencies_deduped(self):
        cfg = PricingConfig(payment_frequencies=["monthly", "monthly", "weekly"])
        assert cfg.payment_frequencies == [PaymentFrequency.MONTHLY, PaymentFrequency.WEEKLY]

    def test_json_round_trip_survives_jsonb(self):
        import json
        cfg = PricingConfig.model_validate(_turnkey_demo_config())
        dumped = json.loads(json.dumps(cfg.model_dump(mode="json", exclude_none=True)))
        again = parse_pricing_config(dumped)
        assert again == cfg


class TestFeeMath:
    def test_per_payment_fee_scales_with_payment_count(self):
        cfg = parse_pricing_config(_turnkey_demo_config())
        # 12 months: monthly = 12 payments, bi-weekly = 26 payments
        monthly = quote_fees_cents(cfg, 1_000_000, 12, "monthly")
        biweekly = quote_fees_cents(cfg, 1_000_000, 12, "bi_weekly")
        assert monthly == 2500 + 100 * 12    # origination + $1 x 12
        assert biweekly == 2500 + 100 * 26   # origination + $1 x 26 — Dave's APR-shift case

    def test_on_event_fees_excluded_from_cost_of_borrowing(self):
        # NSF $45 is contingent — it must never inflate the disclosed APR.
        cfg = parse_pricing_config(_turnkey_demo_config())
        no_nsf = _turnkey_demo_config()
        no_nsf["fees"] = [f for f in no_nsf["fees"] if f["fee_type"] != "nsf"]
        assert quote_fees_cents(cfg, 500_000, 24, "monthly") == quote_fees_cents(
            parse_pricing_config(no_nsf), 500_000, 24, "monthly"
        )

    def test_disabled_fee_excluded(self):
        cfg = _turnkey_demo_config()
        for f in cfg["fees"]:
            f["enabled"] = False
        # validator allows disabling anything; totals drop to zero
        assert quote_fees_cents(parse_pricing_config(cfg), 1_000_000, 12, "monthly") == 0

    def test_rate_bps_fee_scales_with_amount(self):
        cfg = PricingConfig(fees=[FeeConfig(
            fee_type="origination", calc="rate_bps", amount=250,  # 2.5%
            charge_timing="at_origination")])
        assert quote_fees_cents(cfg, 1_000_000, 24, "monthly") == 25_000
        assert quote_fees_cents(cfg, 2_000_000, 24, "monthly") == 50_000

    def test_per_frequency_override_wins(self):
        # Dave: "fees need to be adjustable per repayment period" — $1 monthly,
        # 50c weekly so the fee doesn't distort weekly APR.
        cfg = PricingConfig(fees=[FeeConfig(
            fee_type="administration", calc="fixed_cents", amount=100,
            charge_timing="per_payment",
            per_frequency_amounts={"weekly": 50})])
        assert quote_fees_cents(cfg, 1_000_000, 12, "monthly") == 100 * 12
        assert quote_fees_cents(cfg, 1_000_000, 12, "weekly") == 50 * 52

    def test_payments_in_term_agrees_with_quote_engine(self):
        for freq in PaymentFrequency:
            for term in (3, 6, 12, 17, 24, 48, 84):
                assert payments_in_term(term, freq) == loan_quote.num_payments(term, freq.value)

    def test_origination_lump_is_fixed_at_origination_only(self):
        cfg = parse_pricing_config(_turnkey_demo_config())
        assert origination_lump_fees_cents(cfg) == 2500  # not admin/pmt, not NSF


class TestLegacyLoader:
    def test_seed_022_shape_maps_fully(self):
        # The exact shape written by alembic 022_seed_dental_full_arch_v1 — the
        # origination_fee_pct here was silently DROPPED by the old quote reader.
        cfg = parse_pricing_config({
            "term_options": [24, 36, 48, 60],
            "apr_range": [7.99, 28.99],
            "origination_fee_pct": 0.025,
        })
        assert cfg.interest.annual_rate_bps == 799   # apr_range floor, as before
        assert cfg.interest.min_rate_bps == 799
        assert cfg.interest.max_rate_bps == 2899
        assert cfg.term_options == [24, 36, 48, 60]
        assert [f.value for f in cfg.payment_frequencies] == [
            "weekly", "bi_weekly", "semi_monthly", "monthly"]  # legacy = all four
        assert len(cfg.fees) == 1
        fee = cfg.fees[0]
        assert fee.fee_type is FeeType.ORIGINATION
        assert fee.calc is FeeCalc.RATE_BPS and fee.amount == 250
        assert fee.charge_timing is ChargeTiming.AT_ORIGINATION
        # THE FIX: the 2.5% origination fee now reaches the quote engine.
        assert quote_fees_cents(cfg, 1_500_000, 24, "monthly") == 37_500

    def test_legacy_fees_cents_lump_maps_to_fixed_origination(self):
        cfg = parse_pricing_config({"apr_bps": 1299, "fees_cents": 5000})
        assert quote_fees_cents(cfg, 1_000_000, 12, "monthly") == 5000
        assert origination_lump_fees_cents(cfg) == 5000  # old lump preserved exactly

    def test_empty_config_preserves_unconfigured_defaults(self):
        cfg = parse_pricing_config(None)
        assert cfg.interest is None  # readers fall back to the 12.99% default
        assert len(cfg.payment_frequencies) == 4
        assert cfg.fees == []

    def test_legacy_term_months_becomes_booking_default(self):
        cfg = parse_pricing_config({"term_months": 24})
        assert cfg.default_term_months == 24
        assert cfg.term_options == [24]

    def test_frequency_alias_biweekly(self):
        cfg = parse_pricing_config({"payment_frequencies": ["biweekly", "monthly"]})
        assert cfg.payment_frequencies == [PaymentFrequency.BI_WEEKLY, PaymentFrequency.MONTHLY]

    def test_non_dict_refused(self):
        with pytest.raises(PricingConfigError, match="must be an object"):
            parse_pricing_config(["not", "a", "dict"])


class TestProductTermsBackCompat:
    """product_terms() must keep its exact pre-schema behaviors (these mirror
    the DB-free assertions in tests/test_loan_quote.py, runnable here)."""

    def test_unconfigured_defaults(self):
        params = loan_quote.product_terms({})
        assert params["annual_rate_bps"] == 1299
        assert {f["value"] for f in params["frequencies"]} == set(loan_quote.FREQUENCIES)
        assert params["term_options"] == [12, 24, 36, 48, 60]

    def test_apr_range_floor(self):
        assert loan_quote.product_terms({"apr_range": [8.99, 24.99]})["annual_rate_bps"] == 899

    def test_term_range_from_options(self):
        params = loan_quote.product_terms({"term_options": [12, 24, 36, 60]})
        assert params["term_min"] == 12 and params["term_max"] == 60

    def test_term_range_explicit(self):
        params = loan_quote.product_terms({"term_min": 3, "term_max": 84})
        assert params["term_min"] == 3 and params["term_max"] == 84

    def test_typed_config_terms(self):
        params = loan_quote.product_terms(_turnkey_demo_config())
        assert params["term_min"] == 12 and params["term_max"] == 48
        assert params["annual_rate_bps"] == 1999
        assert [f["value"] for f in params["frequencies"]] == ["monthly", "bi_weekly"]


class TestAprPerFrequencyValidation:
    def _base(self):
        # 9.99% flat band, $5/payment admin fee — harmless monthly, criminal weekly
        # at the $1,000 minimum advance over 6 months.
        return {
            "schema_version": 1,
            "interest": {"annual_rate_bps": 999, "min_rate_bps": 999, "max_rate_bps": 999},
            "payment_frequencies": ["monthly"],
            "term_min_months": 6,
            "term_max_months": 12,
            "fees": [{"fee_type": "administration", "calc": "fixed_cents",
                      "amount": 500, "charge_timing": "per_payment"}],
        }

    def test_monthly_only_passes(self):
        cfg = loan_quote.validate_pricing_config(self._base(), 100_000, 1_000_000)
        assert cfg.payment_frequencies == [PaymentFrequency.MONTHLY]

    def test_enabling_weekly_trips_s347_at_boundary(self):
        # Same product + weekly checkbox: the $5/payment fee recurs 26x over a
        # 6-month term on a $1,000 floor — the APR blows through the 35% cap.
        # THIS is why APR must be validated for EVERY enabled frequency.
        bad = self._base()
        bad["payment_frequencies"] = ["monthly", "weekly"]
        with pytest.raises(PricingConfigError, match="s.347"):
            loan_quote.validate_pricing_config(bad, 100_000, 1_000_000)

    def test_rate_band_top_is_validated_not_just_default(self):
        cfg = self._base()
        cfg["fees"] = []
        cfg["interest"] = {"annual_rate_bps": 1999, "min_rate_bps": 999,
                           "max_rate_bps": 3600}  # top of band over the 35% cap
        with pytest.raises(PricingConfigError, match="s.347"):
            loan_quote.validate_pricing_config(cfg, 100_000, 1_000_000)

    def test_normalization_pins_legacy_default_rate(self):
        # A config with no rate info quotes/books at 12.99% today — validation
        # normalizes that to an explicit interest block so nothing shifts.
        cfg = loan_quote.validate_pricing_config({"term_options": [12, 24]}, 100_000, 1_000_000)
        assert cfg.interest.annual_rate_bps == 1299
        assert cfg.interest.min_rate_bps == cfg.interest.max_rate_bps == 1299

    def test_enabled_late_fee_blocked_at_validation(self):
        cfg = self._base()
        cfg["fees"] = [{"fee_type": "late", "calc": "fixed_cents", "amount": 2500,
                        "charge_timing": "on_event", "enabled": True}]
        with pytest.raises(PricingConfigError, match="disabled in Canada"):
            loan_quote.validate_pricing_config(cfg, 100_000, 1_000_000)

    def test_province_cap_hook_stub_returns_ok(self):
        assert loan_quote.province_cap_hook("BC", 1999) is True
        assert loan_quote.province_cap_hook(None, 3400) is True

    def test_worst_case_apr_still_catches_legacy_criminal_config(self):
        # Mirror of the compliance-controls test: small advance + heavy lump fee
        # + short term must surface a worst case over the cap (legacy shape).
        pricing = {"annual_rate_bps": 3000, "fees_cents": 80_000,
                   "term_min": 6, "term_max": 12}
        worst = loan_quote.product_worst_case_apr_bps(100_000, pricing)
        assert loan_quote.exceeds_criminal_rate(worst) is True
