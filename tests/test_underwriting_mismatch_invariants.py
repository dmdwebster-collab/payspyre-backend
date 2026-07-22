"""WS-E invariant: NO verification-mismatch path auto-declines.

Dave (02__WP_Underwriting.md §5): "If anything doesn't match in the verification
process, it needs human intervention. It cannot be ignored. It cannot be
subverted. It shouldn't also be rejected. We need to have human oversight."

These pure tests sweep every soft/indeterminate verification condition the
engine can produce — failed identity, unknown identity (timeout), vendor
manual-review identity, failed/unknown bank link, unknown bureau pulls, fraud
signals, manual-review-band scores — with an otherwise-clean applicant, and
assert each routes to ``manual_review`` (status ``under_review``), NEVER
``declined``. The only permitted auto-declines are the confirmed hard
disqualifiers: Quebec (province gate), a confirmed below-floor score, and the
bankruptcy policy (active, or recently discharged without vendor override).

No DB, no network (engine is pure; adapters are mocks/stubs).
"""
from __future__ import annotations

import copy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.adapters import (
    FlowAdapters,
    MockBankAdapter,
    MockBureauAdapter,
    MockVerificationAdapter,
    PatientProfile,
)
from app.services.adapters.base import BureauAdapter, BureauResult
from app.services.flow_engine import ApplicantInput, run_flow

# Requires all three verification lanes so every mismatch lane is exercised.
MATRIX: dict = {
    "identity": {"required": True, "methods": ["id_doc_scan"], "min_confidence": 0.85},
    "income": {"required": True, "methods": ["bank_link"], "require_bank_link": True},
    "bureau": {"soft_pull_required": True, "hard_pull_required": True},
}

CLEAN_SCORE = 720  # above the default manual-review band (600–659)
BAND_SCORE = 640   # inside the band


def make_inputs(matrix=None):
    application = SimpleNamespace(id=uuid4(), requested_amount_cents=1_000_000)
    product = SimpleNamespace(id=uuid4(), verification_matrix=copy.deepcopy(matrix or MATRIX))
    patient = PatientProfile(patient_id=uuid4(), province="BC", email="inv@example.com")
    return application, product, patient


def clean_adapters(**overrides) -> FlowAdapters:
    """All-green adapters; override any lane to inject one mismatch."""
    return FlowAdapters(
        verification=overrides.get(
            "verification",
            MockVerificationAdapter(forced_result="passed", forced_confidence=0.99),
        ),
        bureau=overrides.get(
            "bureau",
            MockBureauAdapter(forced_score=CLEAN_SCORE, forced_bankruptcy=False,
                              forced_fraud_high_risk=False, forced_result="passed"),
        ),
        bank=overrides.get("bank", MockBankAdapter(forced_result="passed")),
    )


class HardPullUnknownBureau(BureauAdapter):
    """Soft pull clean; hard pull times out (unknown) — the soft-then-hard
    sequencing mismatch case."""

    async def soft_pull(self, patient):
        return BureauResult(pull_type="soft", score=CLEAN_SCORE, result="passed")

    async def hard_pull(self, patient):
        return BureauResult(pull_type="hard", score=0, result="unknown")


async def test_baseline_all_green_approves():
    """Control: with no mismatch the same setup approves — so any manual_review
    below is attributable to the injected condition."""
    application, product, patient = make_inputs()
    decision = await run_flow(application, product, patient, clean_adapters())
    assert decision.decision == "approved"


MISMATCH_CASES = {
    # identity lane
    "identity_failed": {"verification": MockVerificationAdapter(forced_result="failed")},
    "identity_unknown_timeout": {"verification": MockVerificationAdapter(forced_result="unknown")},
    "identity_vendor_manual_review": {
        "verification": MockVerificationAdapter(forced_result="manual_review")
    },
    "identity_low_confidence": {
        # passes the vendor but under the product's min_confidence → treated failed
        "verification": MockVerificationAdapter(forced_result="passed", forced_confidence=0.10)
    },
    # income / bank lane
    "bank_link_failed": {"bank": MockBankAdapter(forced_result="failed")},
    "bank_link_unknown_timeout": {"bank": MockBankAdapter(forced_result="unknown")},
    # bureau lane (indeterminate — NOT a confirmed low score)
    "bureau_soft_unknown": {
        "bureau": MockBureauAdapter(forced_result="unknown", forced_score=CLEAN_SCORE)
    },
    "bureau_hard_unknown": {"bureau": HardPullUnknownBureau()},
    # fraud signal → review, never auto-reject
    "fraud_signal": {
        "bureau": MockBureauAdapter(forced_score=CLEAN_SCORE, forced_bankruptcy=False,
                                    forced_fraud_high_risk=True)
    },
    # score inside the manual-review band
    "score_in_review_band": {
        "bureau": MockBureauAdapter(forced_score=BAND_SCORE, forced_bankruptcy=False,
                                    forced_fraud_high_risk=False)
    },
}


@pytest.mark.parametrize("case", sorted(MISMATCH_CASES))
async def test_no_mismatch_path_auto_declines(case):
    """THE INVARIANT: every soft/indeterminate verification condition routes to
    manual_review — never declined, never approved-through."""
    application, product, patient = make_inputs()
    decision = await run_flow(
        application, product, patient, clean_adapters(**MISMATCH_CASES[case])
    )
    assert decision.decision != "declined", (
        f"{case}: a verification mismatch AUTO-DECLINED — violates the "
        f"human-intervention invariant (reasons={decision.decision_reasons})"
    )
    assert decision.decision != "approved", (
        f"{case}: a verification mismatch was IGNORED (auto-approved) — "
        f"violates the human-intervention invariant"
    )
    assert decision.decision == "manual_review"
    assert decision.next_state == "under_review"


@pytest.mark.parametrize("case", sorted(MISMATCH_CASES))
async def test_mismatch_plus_confirmed_hard_decline_still_declines(case):
    """Precedence guard: a mismatch must not RESCUE a confirmed below-floor
    score into review — the confirmed hard disqualifier still declines. (The
    invariant protects mismatches from auto-decline; it never weakens confirmed
    decline protections.)"""
    if case in ("bureau_soft_unknown", "bureau_hard_unknown", "fraud_signal",
                "score_in_review_band"):
        pytest.skip("bureau lane is the injected condition here; no confirmed score to pair")
    application, product, patient = make_inputs()
    low_bureau = MockBureauAdapter(forced_score=520, forced_bankruptcy=False,
                                   forced_fraud_high_risk=False)
    decision = await run_flow(
        application, product, patient,
        clean_adapters(bureau=low_bureau, **MISMATCH_CASES[case]),
    )
    assert decision.decision == "declined"


async def test_group_with_one_mismatched_co_applicant_goes_to_review():
    """Co-applicant path: one applicant's mismatch pulls the GROUP to
    manual_review — never declined, never approved over an unresolved member."""
    application, product, patient = make_inputs()

    co_app, co_product, _ = make_inputs()
    co_patient = PatientProfile(patient_id=uuid4(), province="BC", email="co@example.com")

    # Adapters are shared across the group run; identity mismatch affects both —
    # which is fine: the invariant is about the GROUP decision.
    decision = await run_flow(
        application, product, patient,
        clean_adapters(verification=MockVerificationAdapter(forced_result="failed")),
        co_applicants=[ApplicantInput(application=co_app, product=co_product, patient=co_patient)],
    )
    assert decision.decision == "manual_review"
    assert decision.next_state == "under_review"
