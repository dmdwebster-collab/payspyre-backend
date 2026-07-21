"""Pure unit tests for the WS-D 5-band verified-data scorecard (Dave mandate #3).

No DB, no network — the scoring core is pure. Covers: additive bin scoring,
natural min/max with NO clamping, band placement, missing-input handling, the
5-band definition validators, and verified-input assembly.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.scorecards import (
    BAND_NAMES,
    ScorecardDefinition,
    ScorecardRef,
    build_verified_inputs,
    score,
)


def _bands(
    *,
    excellent="approved",
    good="approved",
    average="manual_review",
    weak="manual_review",
    poor="declined",
):
    return [
        {"name": "excellent", "min_score": 40, "decision": excellent,
         "credit_limit_cents": 1_500_000, "annual_rate_bps": 999},
        {"name": "good", "min_score": 20, "decision": good,
         "credit_limit_cents": 1_000_000, "annual_rate_bps": 1499},
        {"name": "average", "min_score": 0, "decision": average,
         "credit_limit_cents": 500_000, "annual_rate_bps": 1999},
        {"name": "weak", "min_score": -20, "decision": weak,
         "credit_limit_cents": 250_000, "annual_rate_bps": 2499},
        {"name": "poor", "min_score": None, "decision": poor},
    ]


def _def(attributes=None, bands=None) -> ScorecardDefinition:
    attributes = attributes or [
        {
            "key": "bureau_score",
            "bins": [
                {"min_value": 720, "points": 30, "label": "prime"},
                {"min_value": 660, "max_value": 720, "points": 15},
                {"min_value": 600, "max_value": 660, "points": 0},
                {"max_value": 600, "points": -30, "label": "subprime"},
            ],
        },
        {
            "key": "nsf_count_90d",
            "bins": [
                {"max_value": 1, "points": 10},
                {"min_value": 1, "max_value": 3, "points": 0},
                {"min_value": 3, "points": -20},
            ],
        },
        {
            "key": "micro_lender_used",
            "bins": [
                {"max_value": 1, "points": 0},   # false → 0.0
                {"min_value": 1, "points": -25},  # true → 1.0
            ],
        },
    ]
    return ScorecardDefinition(attributes=attributes, bands=bands or _bands())


def _ref() -> ScorecardRef:
    return ScorecardRef(id="sc-1", name="Default", definition=_def())


# --- additive scoring + banding ------------------------------------------------


def test_additive_sum_and_band_excellent():
    result = score(_ref(), {"bureau_score": 760, "nsf_count_90d": 0, "micro_lender_used": False})
    assert result.total == 40  # 30 + 10 + 0
    assert result.band == "excellent"
    assert result.decision == "approved"
    assert result.credit_limit_cents == 1_500_000
    assert result.annual_rate_bps == 999


def test_micro_lender_penalty_drags_band_down():
    # Same bureau/nsf as excellent, but micro-lender usage subtracts 25.
    result = score(_ref(), {"bureau_score": 760, "nsf_count_90d": 0, "micro_lender_used": True})
    assert result.total == 15  # 30 + 10 - 25
    assert result.band == "average"  # 0 <= 15 < 20 (good floor)


def test_poor_floor_band_is_catch_all_decline():
    result = score(_ref(), {"bureau_score": 500, "nsf_count_90d": 5, "micro_lender_used": True})
    # -30 + -20 + -25 = -75 → below the 'weak' floor of -20 → poor
    assert result.total == -75
    assert result.band == "poor"
    assert result.decision == "declined"


def test_no_clamping_natural_min_max():
    # Natural range = Σ per-attribute extremes; the model must NOT clamp to it.
    definition = _def()
    nat_min, nat_max = definition.natural_range()
    assert nat_min == -75  # -30 + -20 + -25
    assert nat_max == 40   # 30 + 10 + max(micro bins {0, -25}) = 0
    result = score(_ref(), {"bureau_score": 900, "nsf_count_90d": 0, "micro_lender_used": False})
    assert result.total == 40
    assert result.natural_min == nat_min
    assert result.natural_max == nat_max


def test_missing_input_uses_missing_points_and_flags():
    result = score(_ref(), {"bureau_score": 760})  # nsf + micro missing
    # 30 (bureau) + 0 (nsf missing) + 0 (micro missing) = 30
    assert result.total == 30
    missing = {b.key for b in result.breakdown if b.missing}
    assert missing == {"nsf_count_90d", "micro_lender_used"}


def test_missing_points_can_be_negative():
    attrs = [
        {"key": "bureau_score", "missing_points": -50,
         "bins": [{"min_value": 600, "points": 20}, {"max_value": 600, "points": -20}]},
    ]
    definition = ScorecardDefinition(attributes=attrs, bands=_bands())
    ref = ScorecardRef(id="x", name="x", definition=definition)
    result = score(ref, {})  # bureau_score absent → -50
    assert result.total == -50
    assert result.band == "poor"


def test_half_open_bins_no_double_count_at_boundary():
    # value exactly 660 must land in the [660,720) bin (points 15), not [600,660).
    result = score(_ref(), {"bureau_score": 660, "nsf_count_90d": 0, "micro_lender_used": False})
    assert result.total == 25  # 15 + 10


def test_value_outside_all_bins_is_neutral_missing():
    attrs = [{"key": "bureau_score", "missing_points": 0,
              "bins": [{"min_value": 600, "max_value": 700, "points": 10}]}]
    definition = ScorecardDefinition(attributes=attrs, bands=_bands())
    ref = ScorecardRef(id="x", name="x", definition=definition)
    result = score(ref, {"bureau_score": 800})  # above the only bin
    assert result.total == 0
    assert result.breakdown[0].missing is True


# --- definition validators -----------------------------------------------------


def test_bands_must_be_exactly_five_named_in_order():
    with pytest.raises((ValidationError, ValueError)):
        ScorecardDefinition(
            attributes=[{"key": "x", "bins": [{"points": 1}]}],
            bands=_bands()[:4],  # only 4
        )


def test_band_names_must_match_canonical():
    bad = _bands()
    bad[0]["name"] = "amazing"
    with pytest.raises((ValidationError, ValueError)):
        ScorecardDefinition(attributes=[{"key": "x", "bins": [{"points": 1}]}], bands=bad)
    assert BAND_NAMES[0] == "excellent"


def test_floor_band_cannot_approve():
    bad = _bands(poor="approved")
    with pytest.raises((ValidationError, ValueError)):
        ScorecardDefinition(attributes=[{"key": "x", "bins": [{"points": 1}]}], bands=bad)


def test_thresholds_must_be_strictly_descending():
    bad = _bands()
    bad[1]["min_score"] = 40  # good == excellent
    with pytest.raises((ValidationError, ValueError)):
        ScorecardDefinition(attributes=[{"key": "x", "bins": [{"points": 1}]}], bands=bad)


def test_overlapping_bins_rejected():
    with pytest.raises((ValidationError, ValueError)):
        ScorecardDefinition(
            attributes=[{"key": "x", "bins": [
                {"min_value": 0, "max_value": 100, "points": 1},
                {"min_value": 50, "max_value": 150, "points": 2},
            ]}],
            bands=_bands(),
        )


def test_bin_min_must_be_below_max():
    with pytest.raises((ValidationError, ValueError)):
        ScorecardDefinition(
            attributes=[{"key": "x", "bins": [{"min_value": 100, "max_value": 50, "points": 1}]}],
            bands=_bands(),
        )


# --- verified-input assembly ---------------------------------------------------


def test_build_verified_inputs_prefers_hard_pull():
    stored = {
        "bureau_soft": {"credit_score": 640},
        "bureau_hard": {"credit_score": 700},
        "bank_link": {"monthly_income_cents": 400_000, "nsf_count_90d": 2,
                      "account_age_months": 30, "avg_balance_cents": 120_000},
        "kyc_id": {"confidence": 0.93},
    }
    inputs = build_verified_inputs(stored)
    assert inputs["bureau_score"] == 700  # hard wins
    assert inputs["monthly_income_cents"] == 400_000
    assert inputs["nsf_count_90d"] == 2
    assert inputs["identity_confidence"] == 0.93


def test_build_verified_inputs_pulls_statement_analysis_risk():
    stored = {
        "bank_link": {
            "monthly_income_cents": 300_000,
            "statement_analysis": {
                "risk_attributes": {
                    "micro_lender_used": True,
                    "micro_lender_txn_count_90d": 4,
                    "monthly_expense_cents": 250_000,
                    "monthly_free_cash_flow_cents": 50_000,
                }
            },
        }
    }
    inputs = build_verified_inputs(stored)
    assert inputs["micro_lender_used"] is True
    assert inputs["micro_lender_txn_count_90d"] == 4
    assert inputs["monthly_free_cash_flow_cents"] == 50_000


def test_build_verified_inputs_tolerates_missing_and_garbage():
    assert build_verified_inputs({}) == {}
    inputs = build_verified_inputs({"bureau_soft": {"credit_score": "not-a-number"}})
    assert "bureau_score" not in inputs
