"""Pure flow-engine tests for the WS-D scorecard wiring.

No DB, no network. Proves:
  * with scorecard=None the engine is the LEGACY 680/600 path (unchanged);
  * a scorecard REPLACES the banding step;
  * a band DECLINE is FINAL only when data is complete — an unknown/failed
    verification demotes it to manual_review (score only verified data);
  * hard disqualifiers (bankruptcy) keep precedence over the band;
  * co-applicant groups ignore the scorecard (v1 descope) and stay on averaging.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace
from uuid import uuid4

from app.services.adapters import (
    FlowAdapters,
    MockBankAdapter,
    MockBureauAdapter,
    MockVerificationAdapter,
    PatientProfile,
)
from app.services.adapters.base import BureauAdapter
from app.services.flow_engine import ApplicantInput, run_flow
from app.services.scorecards import ScorecardDefinition, ScorecardRef

DENTAL_MATRIX: dict = {
    "identity": {"required": True, "methods": ["id_doc_scan"], "min_confidence": 0.85},
    "income": {"required": True, "methods": ["bank_link"], "require_bank_link": False},
    "bureau": {"soft_pull_required": True, "hard_pull_required": True},
}


def make_product(matrix=None):
    return SimpleNamespace(
        id=uuid4(),
        verification_matrix=copy.deepcopy(DENTAL_MATRIX) if matrix is None else matrix,
    )


def make_application():
    return SimpleNamespace(id=uuid4(), requested_amount_cents=4_000_000)


def make_patient(province="BC"):
    return PatientProfile(patient_id=uuid4(), province=province, email="p@example.com")


def make_adapters(*, bureau=None, bank=None, verification=None):
    return FlowAdapters(
        verification=verification or MockVerificationAdapter(),
        bureau=bureau or MockBureauAdapter(forced_score=720, forced_bankruptcy=False,
                                           forced_fraud_high_risk=False),
        bank=bank or MockBankAdapter(),
    )


def _scorecard(*, decline_below=0):
    """A minimal scorecard keyed to bureau_score; excellent→approved,
    average→manual_review, poor→declined."""
    definition = ScorecardDefinition(
        attributes=[{
            "key": "bureau_score",
            "bins": [
                {"min_value": 700, "points": 40},
                {"min_value": 640, "max_value": 700, "points": 10},
                {"max_value": 640, "points": -40},
            ],
        }],
        bands=[
            {"name": "excellent", "min_score": 40, "decision": "approved",
             "credit_limit_cents": 1_000_000, "annual_rate_bps": 999},
            {"name": "good", "min_score": 20, "decision": "approved",
             "credit_limit_cents": 800_000, "annual_rate_bps": 1299},
            {"name": "average", "min_score": 0, "decision": "manual_review",
             "credit_limit_cents": 400_000, "annual_rate_bps": 1999},
            {"name": "weak", "min_score": -39, "decision": "manual_review",
             "credit_limit_cents": 200_000, "annual_rate_bps": 2499},
            {"name": "poor", "min_score": None, "decision": "declined"},
        ],
    )
    return ScorecardRef(id="sc", name="Test", definition=definition)


async def test_no_scorecard_is_legacy_path():
    # score 720 with no scorecard → legacy approve (above 679 band), no scorecard_result
    decision = await run_flow(make_application(), make_product(), make_patient(),
                              make_adapters())
    assert decision.decision == "approved"
    assert decision.scorecard_result is None


async def test_scorecard_replaces_banding_approved():
    inputs = {"bureau_score": 760}
    decision = await run_flow(make_application(), make_product(), make_patient(),
                              make_adapters(), scorecard=_scorecard(),
                              scorecard_inputs=inputs)
    assert decision.decision == "approved"
    assert decision.scorecard_result is not None
    assert decision.scorecard_result["band"] == "excellent"
    assert decision.scorecard_result["credit_limit_cents"] == 1_000_000


async def test_scorecard_band_manual_review():
    inputs = {"bureau_score": 660}  # +10 → average band → manual_review
    decision = await run_flow(make_application(), make_product(), make_patient(),
                              make_adapters(), scorecard=_scorecard(),
                              scorecard_inputs=inputs)
    assert decision.decision == "manual_review"
    assert decision.scorecard_result["band"] == "average"
    assert "scorecard_band_review" in decision.decision_reasons


async def test_scorecard_band_decline_is_final_when_data_complete():
    inputs = {"bureau_score": 500}  # -40 → poor → declined
    decision = await run_flow(make_application(), make_product(), make_patient(),
                              make_adapters(), scorecard=_scorecard(),
                              scorecard_inputs=inputs)
    assert decision.decision == "declined"
    assert decision.scorecard_result["band"] == "poor"
    assert "scorecard_band_fail" in decision.decision_reasons


class _UnknownBureau(BureauAdapter):
    """Soft pull times out → unknown (incomplete verification)."""

    async def soft_pull(self, patient):
        return None  # engine maps None → unknown

    async def hard_pull(self, patient):
        return None


async def test_band_decline_demotes_to_review_when_verification_unknown():
    # bureau unknown → data NOT fully verified. Even a poor-band score must NOT
    # auto-decline; it routes to a human (Dave's mandate).
    inputs = {"bureau_score": 500}
    decision = await run_flow(make_application(), make_product(), make_patient(),
                              make_adapters(bureau=_UnknownBureau()),
                              scorecard=_scorecard(), scorecard_inputs=inputs)
    assert decision.decision == "manual_review"
    assert "scorecard_band_fail" in decision.decision_reasons  # reason recorded


async def test_bankruptcy_hard_decline_overrides_band_approve():
    # Confirmed active bankruptcy → declined regardless of a strong score band.
    bureau = MockBureauAdapter(forced_score=760, forced_bankruptcy=True,
                               forced_fraud_high_risk=False)
    inputs = {"bureau_score": 760}
    decision = await run_flow(make_application(), make_product(), make_patient(),
                              make_adapters(bureau=bureau), scorecard=_scorecard(),
                              scorecard_inputs=inputs)
    assert decision.decision == "declined"


async def test_group_ignores_scorecard_v1_descope():
    primary_app, primary_prod, primary_pat = make_application(), make_product(), make_patient()
    co = ApplicantInput(application=make_application(), product=make_product(),
                        patient=make_patient())
    decision = await run_flow(primary_app, primary_prod, primary_pat, make_adapters(),
                              co_applicants=[co], combination_strategy="average",
                              scorecard=_scorecard(), scorecard_inputs={"bureau_score": 760})
    # Group path taken (averaging) — scorecard not applied.
    assert decision.scorecard_result is None
    assert decision.decision in ("approved", "manual_review", "declined")
