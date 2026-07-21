"""DB-free unit tests for the per-province compliance engine (Workstream W2).

These exercise the pure evaluation core, the placeholder seed set, the
product-create wiring closure (via a monkeypatched rule loader), and the
loan_quote integration seam — no database, no live-DB fixtures.
"""
import pytest

from app.services import loan_quote
from app.services import province_compliance as pc
from app.services.province_compliance import (
    CANADA_PROVINCES,
    ProvinceEvaluation,
    RuleView,
    evaluate_apr,
    placeholder_seed_rows,
)
from app.schemas.pricing_config import PricingConfigError


def _rule(**over) -> RuleView:
    base = dict(
        province_code="ON",
        province_name="Ontario",
        enabled=True,
        apr_cap_bps=3000,
        high_cost_apr_threshold_bps=None,
        high_cost_license_held=False,
        license_required=False,
        quebec_language_required=False,
        counsel_confirmed=True,
    )
    base.update(over)
    return RuleView(**base)


# --------------------------------------------------------------------------- #
# Pure evaluation core
# --------------------------------------------------------------------------- #
class TestEvaluateApr:
    def test_under_cap_is_ok(self):
        result = evaluate_apr(_rule(apr_cap_bps=3000), 2500)
        assert isinstance(result, ProvinceEvaluation)
        assert result.ok is True
        assert result.block_reason is None

    def test_at_cap_blocks(self):
        result = evaluate_apr(_rule(apr_cap_bps=3000), 3000)
        assert result.ok is False
        assert "maximum for Ontario" in result.block_reason

    def test_above_cap_blocks(self):
        result = evaluate_apr(_rule(apr_cap_bps=3000), 3200)
        assert result.ok is False
        assert "32.00%" in result.block_reason

    def test_no_cap_configured_never_blocks_on_cap(self):
        # apr_cap None => the federal s.347 check elsewhere is the binding one.
        result = evaluate_apr(_rule(apr_cap_bps=None), 9999)
        assert result.ok is True

    def test_high_cost_threshold_without_license_blocks(self):
        result = evaluate_apr(
            _rule(apr_cap_bps=None, high_cost_apr_threshold_bps=3200,
                  high_cost_license_held=False),
            3300,
        )
        assert result.ok is False
        assert "high-cost-credit licensing threshold" in result.block_reason

    def test_high_cost_threshold_with_license_allows_but_warns(self):
        result = evaluate_apr(
            _rule(apr_cap_bps=None, high_cost_apr_threshold_bps=3200,
                  high_cost_license_held=True),
            3300,
        )
        assert result.ok is True
        assert any("high-cost credit" in w for w in result.warnings)

    def test_hard_cap_takes_precedence_over_high_cost(self):
        # apr above both: the hard-cap block wins (it is checked first).
        result = evaluate_apr(
            _rule(apr_cap_bps=3000, high_cost_apr_threshold_bps=2800,
                  high_cost_license_held=True),
            3100,
        )
        assert result.ok is False
        assert "maximum for" in result.block_reason

    def test_license_required_warning(self):
        result = evaluate_apr(_rule(license_required=True), 1000)
        assert result.ok is True
        assert any("alternative-lender licence" in w for w in result.warnings)

    def test_quebec_language_warning(self):
        result = evaluate_apr(
            _rule(province_code="QC", province_name="Quebec",
                  quebec_language_required=True),
            1000,
        )
        assert any("French-language" in w for w in result.warnings)

    def test_unconfirmed_rule_warns(self):
        result = evaluate_apr(_rule(counsel_confirmed=False), 1000)
        assert any("UNCONFIRMED" in w for w in result.warnings)

    def test_confirmed_rule_no_unconfirmed_warning(self):
        result = evaluate_apr(_rule(counsel_confirmed=True), 1000)
        assert not any("UNCONFIRMED" in w for w in result.warnings)


# --------------------------------------------------------------------------- #
# Placeholder seed set
# --------------------------------------------------------------------------- #
class TestPlaceholderSeed:
    def test_covers_all_thirteen_provinces(self):
        rows = placeholder_seed_rows()
        assert len(rows) == 13
        assert {r["province_code"] for r in rows} == set(CANADA_PROVINCES)

    def test_every_row_is_unconfirmed(self):
        assert all(r["counsel_confirmed"] is False for r in placeholder_seed_rows())

    def test_apr_cap_placeholder_is_federal_s347(self):
        # The only defensible non-invented number: the federal 35% ceiling.
        assert all(
            r["apr_cap_bps"] == loan_quote.CRIMINAL_RATE_CAP_BPS
            for r in placeholder_seed_rows()
        )

    def test_high_cost_threshold_left_unknown(self):
        # We do NOT invent per-province high-cost numbers.
        assert all(
            r["high_cost_apr_threshold_bps"] is None for r in placeholder_seed_rows()
        )

    def test_quebec_gate_preserved(self):
        qc = next(r for r in placeholder_seed_rows() if r["province_code"] == "QC")
        assert qc["enabled"] is False
        assert qc["quebec_language_required"] is True
        assert qc["language_requirement"] == "fr-CA"

    def test_saskatchewan_license_required(self):
        sk = next(r for r in placeholder_seed_rows() if r["province_code"] == "SK")
        assert sk["license_required"] is True

    def test_non_qc_provinces_enabled(self):
        for r in placeholder_seed_rows():
            if r["province_code"] != "QC":
                assert r["enabled"] is True


# --------------------------------------------------------------------------- #
# Product-create wiring closure (monkeypatched loader — no DB)
# --------------------------------------------------------------------------- #
class _FakeRow:
    """Minimal stand-in for an ORM row, enough for RuleView.from_orm."""

    def __init__(self, **kw):
        code = kw.get("province_code", "ON")
        defaults = dict(
            province_code=code, province_name=CANADA_PROVINCES.get(code, code),
            enabled=True, apr_cap_bps=3000, high_cost_apr_threshold_bps=None,
            high_cost_license_held=False, license_required=False,
            quebec_language_required=False, counsel_confirmed=True,
        )
        defaults.update(kw)
        for k, v in defaults.items():
            setattr(self, k, v)


class TestPricingProvinceCheckClosure:
    def test_check_blocks_over_cap_province(self, monkeypatch):
        monkeypatch.setattr(
            pc, "list_rules",
            lambda db, enabled_only=False: [
                _FakeRow(province_code="ON", apr_cap_bps=3000),
                _FakeRow(province_code="BC", apr_cap_bps=2500),
            ],
        )
        check = pc.make_pricing_province_check(db=None)
        # 26% is fine in ON (cap 30%) but breaches BC (cap 25%).
        assert check("ON", 2600) is None
        assert check("BC", 2600) is not None
        assert "British Columbia" in check("BC", 2600)

    def test_unknown_or_disabled_province_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            pc, "list_rules",
            lambda db, enabled_only=False: [
                _FakeRow(province_code="AB", enabled=False, apr_cap_bps=1000),
            ],
        )
        check = pc.make_pricing_province_check(db=None)
        assert check("AB", 5000) is None  # disabled => no provincial cap applied
        assert check("XX", 5000) is None  # unknown code

    def test_resolve_effective_provinces_declared(self, monkeypatch):
        # Declared provinces are returned as-is (upper-cased).
        assert pc.resolve_effective_provinces(db=None, declared=["on", "bc"]) == ["ON", "BC"]

    def test_resolve_effective_provinces_default_is_all_enabled(self, monkeypatch):
        monkeypatch.setattr(
            pc, "list_rules",
            lambda db, enabled_only=False: [
                _FakeRow(province_code="ON"),
                _FakeRow(province_code="BC"),
            ],
        )
        assert pc.resolve_effective_provinces(db=None, declared=None) == ["ON", "BC"]


# --------------------------------------------------------------------------- #
# loan_quote integration seam (injected check — no DB)
# --------------------------------------------------------------------------- #
_PRICING = {
    "schema_version": 1,
    "interest": {
        "annual_rate_bps": 1999,
        "min_rate_bps": 1999,
        "max_rate_bps": 1999,
        "rate_edit_roles": ["admin"],
    },
    "payment_frequencies": ["monthly"],
    "term_min_months": 12,
    "term_max_months": 24,
}


class TestValidatePricingConfigIntegration:
    def test_passes_when_province_check_returns_none(self):
        cfg = loan_quote.validate_pricing_config(
            _PRICING, 500_000, 1_000_000,
            provinces=["ON"],
            province_check=lambda code, apr: None,
        )
        assert cfg is not None

    def test_blocks_when_province_check_returns_reason(self):
        def check(code, apr):
            return f"APR too high for {code}."

        with pytest.raises(PricingConfigError) as exc:
            loan_quote.validate_pricing_config(
                _PRICING, 500_000, 1_000_000,
                provinces=["BC"],
                province_check=check,
            )
        assert "APR too high for BC" in str(exc.value)

    def test_backward_compatible_without_province_check(self):
        # Legacy callers (no province_check, no provinces) still validate via
        # the s.347 gate and the always-ok province_cap_hook stub.
        cfg = loan_quote.validate_pricing_config(_PRICING, 500_000, 1_000_000)
        assert cfg is not None

    def test_federal_s347_still_enforced(self):
        # A config that reaches the criminal rate is blocked regardless of the
        # province engine — the existing guard must not be weakened.
        hot = dict(_PRICING)
        hot["interest"] = {
            "annual_rate_bps": 4000, "min_rate_bps": 4000, "max_rate_bps": 4000,
            "rate_edit_roles": ["admin"],
        }
        with pytest.raises(PricingConfigError) as exc:
            loan_quote.validate_pricing_config(
                hot, 500_000, 1_000_000,
                provinces=["ON"], province_check=lambda code, apr: None,
            )
        assert "s.347" in str(exc.value)
