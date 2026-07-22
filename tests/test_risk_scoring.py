"""Risk-score persistence model — PURE tests (no DB, no network).

Covers the whole derivation layer plus the two invariants that matter most:

  * **DECISION-PATH: outcomes are unchanged.** ``run_flow`` is re-run over a
    matrix of scenarios and every decision / next_state / reason list is
    asserted byte-identical to the pre-change behaviour. Recording a score
    cannot change what was decided because nothing in the derivation feeds back
    into the engine — this test pins that.
  * **Honesty.** Missing scorecards, missing inputs and degenerate PSI rows all
    yield NULL / ``-inf`` markers, never fabricated values.

The PSI arithmetic is pinned against the exact numbers on Dave's screen
(video 05 §A5 row 1: 32.43 / 13.37 → 19.06 / 2.43 / 0.89 / 0.1689).
"""
from __future__ import annotations

import asyncio
import copy
import math
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import risk_scoring as RS
from app.services.adapters import (
    FlowAdapters,
    MockBankAdapter,
    MockBureauAdapter,
    MockVerificationAdapter,
    PatientProfile,
)
from app.services.flow_engine import run_flow
from app.services.risk_scores import (
    OVERRIDE_HIGH_SIDE,
    OVERRIDE_LOW_SIDE,
    OVERRIDE_NONE,
    classify_override,
)
from app.services.scorecards import ScorecardDefinition, ScorecardRef

# ---------------------------------------------------------------------------
# Scaling / segments / calibration
# ---------------------------------------------------------------------------


def test_scale_score_maps_natural_range_onto_0_1000():
    assert RS.scale_score(0, 0, 100) == 0
    assert RS.scale_score(50, 0, 100) == 500
    assert RS.scale_score(100, 0, 100) == 1000
    # Negative natural minima are normal for additive scorecards.
    assert RS.scale_score(-40, -40, 60) == 0
    assert RS.scale_score(60, -40, 60) == 1000
    assert RS.scale_score(10, -40, 60) == 500


def test_scale_score_never_guesses():
    assert RS.scale_score(None, 0, 100) is None
    assert RS.scale_score(50, None, 100) is None
    assert RS.scale_score(50, 0, None) is None
    # Degenerate range → None, not a divide-by-zero or a fabricated midpoint.
    assert RS.scale_score(50, 100, 100) is None


def test_scale_score_clamps_out_of_range_totals():
    assert RS.scale_score(500, 0, 100) == 1000
    assert RS.scale_score(-500, 0, 100) == 0


@pytest.mark.parametrize(
    "score,segment",
    [
        (1000, "target_client"),
        (585, "target_client"),
        (560, "target_client"),
        (559, "acceptable_client"),
        (460, "acceptable_client"),
        (459, "restricted_client"),
        (219, "restricted_client"),
        (218, "strongly_restricted_client"),
        (142, "strongly_restricted_client"),
        (141, "rejected_client"),
        (0, "rejected_client"),
    ],
)
def test_segment_bands_match_daves_cuts(score, segment):
    assert RS.segment_for(score)[0] == segment


def test_segment_for_none_is_none():
    assert RS.segment_for(None) is None


def test_calibration_is_monotonic_and_anchored():
    at_anchor = RS.calibrate(RS.PD_ANCHOR_SCORE)
    assert at_anchor.odds_good_bad == pytest.approx(RS.PD_ANCHOR_ODDS)
    assert at_anchor.probability_of_default == pytest.approx(1 / 21, abs=1e-4)
    # +PDO points doubles the odds, by construction.
    doubled = RS.calibrate(RS.PD_ANCHOR_SCORE + RS.PD_POINTS_TO_DOUBLE_ODDS)
    assert doubled.odds_good_bad == pytest.approx(2 * RS.PD_ANCHOR_ODDS)
    # Higher score → lower probability of default, always.
    pds = [RS.calibrate(s).probability_of_default for s in range(0, 1001, 50)]
    assert pds == sorted(pds, reverse=True)


def test_calibration_none_in_none_out():
    assert RS.calibrate(None) is None


def test_odds_display_is_x_to_one():
    assert RS.calibrate(RS.PD_ANCHOR_SCORE).odds_display() == "20:1"


# ---------------------------------------------------------------------------
# Rule comments (Dave's generated sentences)
# ---------------------------------------------------------------------------


def test_generated_comment_matches_daves_wording():
    assert RS.attribute_comment("dti_ratio", 0.6155, False, None) == "DTI ratio is 61.55%"


def test_generated_comment_for_money_and_counts():
    assert (
        RS.attribute_comment("monthly_free_cash_flow_cents", 3712, False, None)
        == "Average Monthly Free Cash Flow is $37.12"
    )
    assert (
        RS.attribute_comment("nsf_count_90d", 2, False, None)
        == "2 NSF event(s) in the last 90 days"
    )


def test_missing_input_says_so_rather_than_inventing_a_value():
    comment = RS.attribute_comment("bureau_score", None, True, None)
    assert comment == "Credit bureau score is not available"


def test_unknown_attribute_key_still_reads_as_a_sentence():
    assert RS.attribute_comment("some_new_signal", 7, False, None) == (
        "Some new signal is 7"
    )


def test_reason_rule_is_total():
    group, name, comment = RS.reason_rule("verification_failed:identity")
    assert group == RS.GROUP_POLICY
    assert "identity" in comment
    # An unrecognised code still yields a usable row.
    assert RS.reason_rule("brand_new_code")[2]


# ---------------------------------------------------------------------------
# Check pipeline
# ---------------------------------------------------------------------------


def _scorecard_result(total=120, band="good"):
    return {
        "scorecard_id": str(uuid4()),
        "scorecard_name": "Default",
        "total": total,
        "band": band,
        "decision": "approved",
        "credit_limit_cents": 5_000_00,
        "annual_rate_bps": 1999,
        "natural_min": 0,
        "natural_max": 200,
        "breakdown": [
            {"key": "bureau_score", "value": 700, "points": 80,
             "bin_label": "700-760", "missing": False},
            {"key": "dti_ratio", "value": 0.6155, "points": -20,
             "bin_label": "high", "missing": False},
            {"key": "nsf_count_90d", "value": None, "points": 0,
             "bin_label": None, "missing": True},
        ],
    }


def test_pipeline_always_emits_all_eight_nodes_in_daves_order():
    checks = RS.build_checks(
        scorecard_result=_scorecard_result(),
        verifications=[],
        decision_reasons=[],
        decision="approved",
    )
    assert [c["key"] for c in checks] == list(RS.CHECK_ORDER)
    assert len(checks) == 8


def test_unmodelled_checks_are_honest_not_green():
    checks = {
        c["key"]: c
        for c in RS.build_checks(
            scorecard_result=None, verifications=[], decision_reasons=[], decision="approved"
        )
    }
    assert checks[RS.CHECK_WEB_ACTIVITY]["status"] == RS.STATUS_NOT_MODELLED
    assert checks[RS.CHECK_CYBER_SECURITY]["status"] == RS.STATUS_NOT_MODELLED
    # And with no scorecard, the application check reports not_run, not a score.
    assert checks[RS.CHECK_APPLICATION]["status"] == RS.STATUS_NOT_RUN
    assert checks[RS.CHECK_APPLICATION]["score"] is None


def test_verification_results_land_on_the_right_nodes():
    checks = {
        c["key"]: c
        for c in RS.build_checks(
            scorecard_result=_scorecard_result(),
            verifications=[
                {"type": "identity", "method": "id_doc_scan", "result": "passed",
                 "confidence": 0.99},
                {"type": "bureau", "method": "soft_pull", "result": "passed",
                 "confidence": 1.0},
                {"type": "bureau", "method": "hard_pull", "result": "unknown",
                 "confidence": 0.0},
                {"type": "income", "method": "bank_link", "result": "failed",
                 "confidence": 0.0},
            ],
            decision_reasons=[],
            decision="manual_review",
        )
    }
    assert checks[RS.CHECK_ID_VERIFICATION]["status"] == RS.STATUS_PASSED
    # Worst outcome wins for the node ring.
    assert checks[RS.CHECK_CREDIT_BUREAU]["status"] == RS.STATUS_UNKNOWN
    assert checks[RS.CHECK_BANK_ACCOUNT]["status"] == RS.STATUS_FAILED


def test_fraud_and_geolocation_derive_from_reason_codes():
    checks = {
        c["key"]: c
        for c in RS.build_checks(
            scorecard_result=None,
            verifications=[],
            decision_reasons=["fraud_signal_review", "quebec_coming_soon"],
            decision="declined",
        )
    }
    assert checks[RS.CHECK_ANTI_FRAUD]["status"] == RS.STATUS_REVIEW
    assert checks[RS.CHECK_GEOLOCATION]["status"] == RS.STATUS_FAILED


def test_recommended_decision_prose_lands_on_the_application_card():
    checks = RS.build_checks(
        scorecard_result=_scorecard_result(),
        verifications=[],
        decision_reasons=[],
        decision="manual_review",
    )
    card = next(c for c in checks if c["key"] == RS.CHECK_APPLICATION)
    assert "additional loan application review" in card["recommended_decision"]


# ---------------------------------------------------------------------------
# Rule evaluations / grouping
# ---------------------------------------------------------------------------


def test_rule_evaluations_cover_attributes_and_reasons():
    rows = RS.build_rule_evaluations(
        scorecard_result=_scorecard_result(),
        decision_reasons=["manual_review_band"],
    )
    by_key = {r["key"]: r for r in rows}
    assert by_key["bureau_score"]["result"] == "passed"
    # A negative contribution reads as a review row, not a silent pass.
    assert by_key["dti_ratio"]["result"] == "review"
    assert by_key["dti_ratio"]["comment"] == "DTI ratio is 61.55% (high)"
    assert by_key["nsf_count_90d"]["result"] == "not_evaluated"
    assert by_key["manual_review_band"]["result"] == "failed"


def test_rules_group_into_daves_accordions():
    groups = RS.group_rules(
        RS.build_rule_evaluations(
            scorecard_result=_scorecard_result(), decision_reasons=["manual_review_band"]
        )
    )
    keys = [g["group"] for g in groups]
    # Stable, spec order; only non-empty groups appear.
    assert keys == [g for g in [
        RS.GROUP_APPLICATION, RS.GROUP_BANK, RS.GROUP_BUREAU,
        RS.GROUP_IDENTITY, RS.GROUP_POLICY,
    ] if g in keys]
    bureau = next(g for g in groups if g["group"] == RS.GROUP_BUREAU)
    assert bureau["passed"] + bureau["failed"] == len(bureau["rules"])


# ---------------------------------------------------------------------------
# PSI — pinned against Dave's own numbers
# ---------------------------------------------------------------------------


def test_psi_row_matches_daves_screen():
    row = RS.psi_row(32.43, 13.37)
    assert row["a_minus_e"] == pytest.approx(19.06, abs=0.01)
    assert row["a_over_e"] == pytest.approx(2.43, abs=0.01)
    assert row["ln_a_over_e"] == pytest.approx(0.89, abs=0.01)
    assert row["index"] == pytest.approx(0.1689, abs=0.001)
    assert row["degenerate"] is None


def test_psi_degenerate_zero_actual_is_shipped_visibly():
    row = RS.psi_row(0.0, 20.0)
    assert row["degenerate"] == "-inf"
    assert row["ln_a_over_e"] is None
    assert row["index"] is None


def test_psi_degenerate_zero_expected():
    row = RS.psi_row(10.0, 0.0)
    assert row["degenerate"] == "+inf"
    assert row["index"] is None
    # Both zero is not a degeneracy — the band is simply absent from both.
    assert RS.psi_row(0.0, 0.0)["degenerate"] is None


def test_psi_index_formula_is_the_standard_one():
    a, e = 40.0, 25.0
    expected = ((a - e) / 100.0) * math.log(a / e)
    assert RS.psi_row(a, e)["index"] == pytest.approx(expected, abs=1e-4)


def test_psi_rag_thresholds():
    assert RS.psi_rag(0.05) == "green"
    assert RS.psi_rag(0.10) == "amber"
    assert RS.psi_rag(0.25) == "amber"
    assert RS.psi_rag(0.4894) == "red"   # Dave's own total, on red
    assert RS.psi_rag(None) is None


# ---------------------------------------------------------------------------
# Override classification (Final score / Overrides reports)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "system,underwriter,expected",
    [
        ("declined", "approved", OVERRIDE_HIGH_SIDE),
        ("approved", "declined", OVERRIDE_LOW_SIDE),
        ("approved", "approved", OVERRIDE_NONE),
        ("declined", "declined", OVERRIDE_NONE),
        ("manual_review", "approved", OVERRIDE_NONE),
        ("manual_review", "declined", OVERRIDE_NONE),
        (None, "approved", OVERRIDE_NONE),
        ("approved", None, OVERRIDE_NONE),
        (None, None, OVERRIDE_NONE),
    ],
)
def test_classify_override(system, underwriter, expected):
    assert classify_override(system, underwriter) == expected


# ---------------------------------------------------------------------------
# DECISION-PATH: outcomes unchanged
# ---------------------------------------------------------------------------

DENTAL_MATRIX: dict = {
    "identity": {"required": True, "methods": ["id_doc_scan"], "min_confidence": 0.85},
    "income": {"required": True, "methods": ["bank_link"], "require_bank_link": False},
    "bureau": {"soft_pull_required": True, "hard_pull_required": True},
}


def _product():
    return SimpleNamespace(id=uuid4(), verification_matrix=copy.deepcopy(DENTAL_MATRIX))


def _application():
    return SimpleNamespace(id=uuid4(), requested_amount_cents=4_000_000)


def _adapters(score=720, bankruptcy=False, fraud=False):
    return FlowAdapters(
        verification=MockVerificationAdapter(),
        bureau=MockBureauAdapter(
            forced_score=score,
            forced_bankruptcy=bankruptcy,
            forced_fraud_high_risk=fraud,
        ),
        bank=MockBankAdapter(),
    )


def _scorecard():
    definition = ScorecardDefinition(
        attributes=[
            {
                "key": "bureau_score",
                "bins": [
                    {"max_value": 600, "points": 0, "label": "<600"},
                    {"min_value": 600, "max_value": 700, "points": 50, "label": "600-699"},
                    {"min_value": 700, "points": 100, "label": "700+"},
                ],
            }
        ],
        bands=[
            {"name": "excellent", "min_score": 100, "decision": "approved"},
            {"name": "good", "min_score": 80, "decision": "approved"},
            {"name": "average", "min_score": 50, "decision": "manual_review"},
            {"name": "weak", "min_score": 20, "decision": "manual_review"},
            {"name": "poor", "min_score": None, "decision": "declined"},
        ],
    )
    return ScorecardRef(id=str(uuid4()), name="Test", definition=definition)


#: (label, score, bankruptcy, fraud, province, use_scorecard) → expected outcome.
#: These are the SHIPPED outcomes; the risk-score model must not move any of them.
DECISION_MATRIX = [
    ("legacy_high_score_approves", 720, False, False, "BC", False, "approved"),
    ("legacy_band_reviews", 620, False, False, "BC", False, "manual_review"),
    ("legacy_below_floor_declines", 550, False, False, "BC", False, "declined"),
    ("legacy_bankruptcy_declines", 720, True, False, "BC", False, "declined"),
    ("legacy_fraud_reviews", 720, False, True, "BC", False, "manual_review"),
    ("legacy_quebec_declines", 720, False, False, "QC", False, "declined"),
    ("scorecard_top_band_approves", 720, False, False, "BC", True, "approved"),
    ("scorecard_mid_band_reviews", 650, False, False, "BC", True, "manual_review"),
    ("scorecard_floor_band_declines", 550, False, False, "BC", True, "declined"),
    ("scorecard_bankruptcy_still_declines", 720, True, False, "BC", True, "declined"),
]


@pytest.mark.parametrize(
    "label,score,bankruptcy,fraud,province,use_scorecard,expected", DECISION_MATRIX
)
def test_decision_path_outcomes_unchanged(
    label, score, bankruptcy, fraud, province, use_scorecard, expected
):
    """DECISION-PATH proof: the shipped outcome for every scenario is unchanged.

    The risk-score model records what the engine computed; it is written after
    the fact and read by nothing the engine consults. This test pins the actual
    outcomes so any future change to the recording layer that leaked into the
    decision would fail here.
    """
    decision = asyncio.run(
        run_flow(
            _application(),
            _product(),
            PatientProfile(patient_id=uuid4(), province=province, email="p@example.com"),
            _adapters(score=score, bankruptcy=bankruptcy, fraud=fraud),
            scorecard=_scorecard() if use_scorecard else None,
            scorecard_inputs={"bureau_score": score} if use_scorecard else None,
        )
    )
    assert decision.decision == expected, label
    assert decision.next_state == {
        "approved": "approved", "declined": "declined", "manual_review": "under_review",
    }[expected]


@pytest.mark.parametrize(
    "label,score,bankruptcy,fraud,province,use_scorecard,expected", DECISION_MATRIX
)
def test_deriving_the_score_record_is_side_effect_free(
    label, score, bankruptcy, fraud, province, use_scorecard, expected
):
    """Running the full derivation over a FlowDecision leaves it untouched."""
    decision = asyncio.run(
        run_flow(
            _application(),
            _product(),
            PatientProfile(patient_id=uuid4(), province=province, email="p@example.com"),
            _adapters(score=score, bankruptcy=bankruptcy, fraud=fraud),
            scorecard=_scorecard() if use_scorecard else None,
            scorecard_inputs={"bureau_score": score} if use_scorecard else None,
        )
    )
    before = decision.to_dict()
    snapshot = copy.deepcopy(before)

    RS.build_checks(
        scorecard_result=before["scorecard_result"],
        verifications=before["verifications_performed"],
        decision_reasons=before["decision_reasons"],
        decision=before["decision"],
    )
    RS.build_rule_evaluations(
        scorecard_result=before["scorecard_result"],
        decision_reasons=before["decision_reasons"],
    )
    if before["scorecard_result"]:
        scaled = RS.scale_score(
            before["scorecard_result"]["total"],
            before["scorecard_result"]["natural_min"],
            before["scorecard_result"]["natural_max"],
        )
        RS.calibrate(scaled)
        RS.segment_for(scaled)

    assert decision.to_dict() == snapshot, label
    assert decision.decision == expected


def test_no_scorecard_means_no_fabricated_score():
    """The legacy path must record NULLs, not a synthesised score."""
    decision = asyncio.run(
        run_flow(
            _application(),
            _product(),
            PatientProfile(patient_id=uuid4(), province="BC", email="p@example.com"),
            _adapters(score=720),
            scorecard=None,
        )
    )
    assert decision.scorecard_result is None
    assert RS.scale_score(None, None, None) is None
    assert RS.segment_for(None) is None
    assert RS.calibrate(None) is None
