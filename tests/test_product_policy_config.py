"""WS W2-APPCONFIG — product-policy config (DB-free).

Covers: typed-schema defaults that encode the engine's current behaviour, the
payoff engine-consistency guard (a config can't advertise a payoff rule the
actuals engine won't honour — 100% principal/NSF, 0% future interest), strict
validation, the descriptor for the quote path, and the fallible-safe
``policy_for_product`` accessor."""
import pytest

from app.schemas import product_policy_config as ppc
from app.services import product_policy


class TestDefaults:
    def test_defaults_encode_engine_behavior(self):
        cfg = ppc.ProductPolicyConfig()
        # Payoff: mandated grid.
        assert cfg.payoff.future_pct_for("interest") == 0
        assert cfg.payoff.future_pct_for("principal") == 100
        assert cfg.payoff.future_pct_for("nsf") == 100
        assert cfg.payoff.future_pct_for("administration") == 0
        assert cfg.payoff.future_pct_for("origination") == 0
        # Closure + recalc = current engine behaviour.
        assert cfg.repayment_modes.loan_closure is ppc.LoanClosure.ZERO_TOTAL_DEBT
        assert (
            cfg.repayment_modes.future_installments_recalc
            is ppc.FutureInstallmentRecalc.KEEP_INSTALLMENT_REDUCE_COUNT
        )

    def test_defaults_match_tl_tab_values(self):
        cfg = ppc.ProductPolicyConfig()
        assert cfg.grace_period.enabled is False
        assert cfg.due_dates.default_start_shift_days == 1
        assert cfg.due_dates.first_due_min_days == 1
        assert cfg.due_dates.first_due_max_days == 45
        assert cfg.due_dates.enable_date_rolling is False
        assert cfg.due_date_seasons.enabled is False
        assert cfg.disbursement.disbursement_type is ppc.DisbursementType.VIRTUAL
        assert cfg.approval.use_two_level_approval is False

    def test_default_repayment_modes_present(self):
        keys = {m.key for m in ppc.ProductPolicyConfig().repayment_modes.modes}
        assert {"automatic", "regular", "add_on", "special", "payoff"} <= keys

    def test_exactly_one_default_mode(self):
        modes = ppc.ProductPolicyConfig().repayment_modes.modes
        assert sum(1 for m in modes if m.is_default) == 1


class TestPayoffConsistencyGuard:
    def test_default_is_engine_consistent(self):
        # No exception.
        ppc.assert_payoff_consistent_with_engine(ppc.ProductPolicyConfig())

    def test_future_interest_must_be_zero(self):
        raw = {"payoff": {"rules": [{"category": "interest", "future_pct": 50}]}}
        with pytest.raises(ppc.ProductPolicyConfigError):
            ppc.parse_product_policy_config(raw)

    def test_principal_must_be_100(self):
        raw = {"payoff": {"rules": [{"category": "principal", "future_pct": 90}]}}
        with pytest.raises(ppc.ProductPolicyConfigError):
            ppc.parse_product_policy_config(raw)

    def test_nsf_must_be_100(self):
        raw = {"payoff": {"rules": [{"category": "nsf", "future_pct": 0}]}}
        with pytest.raises(ppc.ProductPolicyConfigError):
            ppc.parse_product_policy_config(raw)

    def test_unknown_category_is_allowed(self):
        # A category the engine doesn't fix is not guarded.
        raw = {"payoff": {"rules": [{"category": "sales_tax", "future_pct": 25}]}}
        cfg = ppc.parse_product_policy_config(raw)
        assert cfg.payoff.future_pct_for("sales_tax") == 25


class TestParsing:
    def test_none_and_empty_are_defaults(self):
        assert ppc.parse_product_policy_config(None) == ppc.ProductPolicyConfig()
        assert ppc.parse_product_policy_config({}) == ppc.ProductPolicyConfig()

    def test_roundtrip(self):
        cfg = ppc.ProductPolicyConfig()
        assert ppc.parse_product_policy_config(cfg.model_dump(mode="json")) == cfg

    def test_unknown_key_rejected(self):
        with pytest.raises(ppc.ProductPolicyConfigError):
            ppc.parse_product_policy_config({"grace_period": {"bogus": True}})

    def test_due_date_bounds_validated(self):
        raw = {"due_dates": {"first_due_min_days": 50, "first_due_max_days": 10}}
        with pytest.raises(ppc.ProductPolicyConfigError):
            ppc.parse_product_policy_config(raw)

    def test_seasons_enabled_requires_a_season(self):
        with pytest.raises(ppc.ProductPolicyConfigError):
            ppc.parse_product_policy_config({"due_date_seasons": {"enabled": True}})

    def test_non_dict_rejected(self):
        with pytest.raises(ppc.ProductPolicyConfigError):
            ppc.parse_product_policy_config("nope")


class TestDescriptor:
    def test_descriptor_shape(self):
        d = ppc.payoff_policy_descriptor(ppc.ProductPolicyConfig())
        assert d["future_pct"]["interest"] == 0
        assert d["future_pct"]["principal"] == 100
        assert d["closes_at_zero_total_debt"] is True
        assert d["use_payoff_grace_period"] is False


class TestAccessor:
    def test_none_product_is_default(self):
        assert product_policy.policy_for_product(None) is ppc.DEFAULT_PRODUCT_POLICY_CONFIG

    def test_null_policy_is_default(self):
        product = type("P", (), {"policy_config": None, "code": "X"})()
        assert product_policy.policy_for_product(product) is ppc.DEFAULT_PRODUCT_POLICY_CONFIG

    def test_configured_policy_parsed(self):
        cfg = ppc.ProductPolicyConfig()
        cfg.disbursement.multiple_disbursements = True
        product = type("P", (), {"policy_config": cfg.model_dump(mode="json"), "code": "X"})()
        got = product_policy.policy_for_product(product)
        assert got.disbursement.multiple_disbursements is True

    def test_invalid_stored_policy_degrades_to_default(self):
        bad = {"payoff": {"rules": [{"category": "interest", "future_pct": 99}]}}
        product = type("P", (), {"policy_config": bad, "code": "X"})()
        assert product_policy.policy_for_product(product) is ppc.DEFAULT_PRODUCT_POLICY_CONFIG

    def test_descriptor_none_for_unconfigured_product(self):
        product = type("P", (), {"policy_config": None, "code": "X"})()
        assert product_policy.payoff_descriptor_for_product(product) is None

    def test_descriptor_present_for_configured_product(self):
        cfg = ppc.ProductPolicyConfig()
        product = type("P", (), {"policy_config": cfg.model_dump(mode="json"), "code": "X"})()
        d = product_policy.payoff_descriptor_for_product(product)
        assert d is not None and d["future_pct"]["principal"] == 100
