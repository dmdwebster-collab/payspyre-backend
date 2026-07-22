"""WS-F decision-rules directory — DB-free tests.

Covers: registry completeness + video-08 seed values, DB-edit merging via a
fake session, edit validation (outcome lock, param typing, band ordering), and
the DECISION-PATH overlay contract (identity when untouched — the behaviour-
preservation guarantee)."""
from types import SimpleNamespace

from app.services import decision_rules as dr


class FakeDB:
    """Minimal stand-in for Session.get(PlatformDecisionRule, key)."""

    def __init__(self, rows=None):
        self.rows = rows or {}

    def get(self, model, key):
        return self.rows.get(key)


def _row(**kw):
    return SimpleNamespace(
        params=kw.get("params", {}),
        outcome=kw.get("outcome"),
        enabled=kw.get("enabled"),
    )


class TestRegistry:
    def test_directory_size_and_groups(self):
        assert len(dr.DECISION_RULE_REGISTRY) >= 50
        groups = {s.group for s in dr.DECISION_RULE_REGISTRY.values()}
        assert groups == set(dr.GROUPS)

    def test_video_08_seed_values(self):
        reg = dr.DECISION_RULE_REGISTRY
        assert reg["dti"].params == {"max_percent": 50}
        assert reg["pti"].params == {"max_percent": 20}
        assert reg["net_income"].params == {"min_amount": 1500}
        assert reg["minimal_age"].params == {"min_age": 19}
        assert reg["active_loans"].params == {"max_active_loans": 2}
        assert reg["delinquency_check"].params == {"minor_dpd": 14, "major_dpd": 30}
        # P0/T4: seeded from Dave's Settings screen (second cut 660), was 680.
        assert reg["bureau_score"].params == {"decline_below": 600, "approve_at": 660}
        assert reg["suspicious_age"].params == {"max_age": 100}
        assert reg["nsf_fees_90d"].params == {"max_count": 0}

    def test_tl_disabled_rules_ship_disabled(self):
        for key in ("child_support_income_90d", "avg_monthly_expenditure",
                    "avg_monthly_government_income", "avg_monthly_non_employer_income"):
            assert dr.DECISION_RULE_REGISTRY[key].enabled is False

    def test_wired_rules_are_outcome_locked(self):
        assert set(dr.WIRED_RULE_KEYS) == {"bureau_score", "bankruptcy_check"}
        for key in dr.WIRED_RULE_KEYS:
            assert dr.DECISION_RULE_REGISTRY[key].outcome_locked

    def test_default_outcomes_are_valid(self):
        for spec in dr.DECISION_RULE_REGISTRY.values():
            assert spec.outcome in dr.ALLOWED_OUTCOMES


class TestEffectiveRules:
    def test_no_rows_yields_registry_defaults(self):
        rules = dr.get_effective_rules(FakeDB())
        assert len(rules) == len(dr.DECISION_RULE_REGISTRY)
        assert all(not r.customized for r in rules)
        dti = next(r for r in rules if r.key == "dti")
        assert dti.params == {"max_percent": 50}
        assert dti.enabled and dti.outcome == "manual_review"

    def test_row_edit_merges_partially(self):
        db = FakeDB({"dti": _row(params={"max_percent": 45}, enabled=False)})
        rule = dr.get_effective_rule(db, "dti")
        assert rule.customized
        assert rule.params == {"max_percent": 45}
        assert rule.enabled is False
        assert rule.outcome == "manual_review"  # inherited

    def test_unknown_param_keys_in_row_ignored(self):
        db = FakeDB({"dti": _row(params={"bogus": 1, "max_percent": 40})})
        assert dr.get_effective_rule(db, "dti").params == {"max_percent": 40}

    def test_locked_outcome_ignores_row_value(self):
        db = FakeDB({"bureau_score": _row(outcome="approve")})
        assert dr.get_effective_rule(db, "bureau_score").outcome == "manual_review"


class TestValidateEdit:
    def test_valid_edit(self):
        assert dr.validate_edit("dti", params={"max_percent": 45}) is None
        assert dr.validate_edit("dti", outcome="decline") is None

    def test_unknown_rule_and_param(self):
        assert dr.validate_edit("nope") is not None
        assert dr.validate_edit("dti", params={"nope": 1}) is not None

    def test_param_typing(self):
        assert dr.validate_edit("dti", params={"max_percent": "fifty"}) is not None
        assert dr.validate_edit("dti", params={"max_percent": True}) is not None
        assert dr.validate_edit("dti", params={"max_percent": -1}) is not None

    def test_bad_outcome(self):
        assert dr.validate_edit("dti", outcome="explode") is not None

    def test_wired_outcome_locked(self):
        assert dr.validate_edit("bureau_score", outcome="decline") is not None
        assert dr.validate_edit("bureau_score", outcome="manual_review") is None

    def test_band_ordering_guard(self):
        assert dr.validate_edit("bureau_score", params={"approve_at": 550}) is not None
        assert dr.validate_edit(
            "bureau_score", params={"decline_below": 620, "approve_at": 700}
        ) is None


class TestOverlay:
    """DECISION-PATH contract: identity unless a wired rule was edited."""

    def _product(self):
        return SimpleNamespace(
            id="prod-1",
            version=3,
            verification_matrix={
                "bureau": {"manual_review_band": {"min": 600, "max": 679}},
                "identity": {"required": True},
            },
        )

    def test_no_edits_returns_same_object(self):
        product = self._product()
        assert dr.apply_overlay(FakeDB(), product) is product

    def test_unwired_edits_return_same_object(self):
        product = self._product()
        db = FakeDB({"dti": _row(params={"max_percent": 10})})
        assert dr.apply_overlay(db, product) is product

    def test_bureau_score_edit_overrides_band(self):
        product = self._product()
        db = FakeDB({"bureau_score": _row(params={"decline_below": 580, "approve_at": 660})})
        out = dr.apply_overlay(db, product)
        assert out is not product
        assert out.verification_matrix["bureau"]["manual_review_band"] == {
            "min": 580, "max": 659,
        }
        # Untouched sections carried over; snapshot never mutated.
        assert out.verification_matrix["identity"] == {"required": True}
        assert product.verification_matrix["bureau"]["manual_review_band"] == {
            "min": 600, "max": 679,
        }

    def test_disabled_wired_edit_removes_overlay(self):
        product = self._product()
        db = FakeDB({
            "bureau_score": _row(params={"decline_below": 580, "approve_at": 660},
                                 enabled=False)
        })
        assert dr.apply_overlay(db, product) is product

    def test_bankruptcy_edit_overrides_min_years(self):
        product = self._product()
        db = FakeDB({"bankruptcy_check": _row(params={"min_years_discharged": 3})})
        out = dr.apply_overlay(db, product)
        assert out.verification_matrix["bureau"]["bankruptcy_discharge_min_years"] == 3

    def test_directory_failure_falls_back_to_snapshot(self):
        class ExplodingDB:
            def get(self, model, key):
                raise RuntimeError("boom")

        product = self._product()
        assert dr.apply_overlay(ExplodingDB(), product) is product
