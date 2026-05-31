"""Pure unit tests for the P7.6 identity manual_review path through ``run_flow``.

Closes the cross-verification orchestrator hand-off P7.5 deliberately left
unfinished: an identity verification that lands at ``manual_review`` (Didit
"In Review" → ``VerificationResult(result="manual_review", ...)``) must pull
the application to ``under_review`` across all bureau outcomes, instead of
being misclassified as ``passed`` via the (pre-P7.6) fall-through branch in
``_effective_identity_outcome``.

Pattern matches ``tests/test_flow_engine.py``: the engine is pure (Hard Rule
#2), so adapters are mocks/stubs and inputs are plain value objects. No DB,
no network. Helpers (`make_product`, `make_application`, `make_patient`,
`make_adapters`, `DENTAL_MATRIX`) are imported from the existing engine test
file to stay aligned with its fixtures.
"""
from __future__ import annotations

from typing import Optional

import pytest

from app.services.adapters import (
    BankAccountSummary,
    BureauResult,
    FlowAdapters,
    MockBankAdapter,
    MockBureauAdapter,
    MockVerificationAdapter,
)
from app.services.adapters.base import (
    BureauAdapter,
    PatientProfile,
    VerificationAdapter,
    VerificationResult,
)
from app.services.flow_engine import (
    REASON_IDENTITY_MANUAL_REVIEW,
    FlowDecision,
    run_flow,
)
from app.services.verifications.replay_adapters import ReplayVerificationAdapter

# Reuse the existing engine test helpers + matrix.
from tests.test_flow_engine import (  # noqa: E402
    DENTAL_MATRIX,
    ApplicantInput,
    make_adapters,
    make_application,
    make_patient,
    make_product,
)


# ---------------------------------------------------------------------------
# Scripted adapters — return preset results without touching the mock's
# deterministic seed logic, so the (effective, score) input space is exact.
# ---------------------------------------------------------------------------


class ScriptedVerification(VerificationAdapter):
    """Returns a preset ``VerificationResult`` on every call."""
    def __init__(self, *, result: str, confidence: float = 0.95) -> None:
        self._result = result
        self._confidence = confidence

    async def verify_identity(self, patient: PatientProfile, method: str) -> VerificationResult:
        return VerificationResult(
            verification_type="identity",
            method=method,
            result=self._result,            # type: ignore[arg-type]
            confidence=self._confidence,
            vendor="scripted",
        )


class TimeoutVerification(VerificationAdapter):
    """Sleeps long enough to trip the engine's per-adapter timeout."""
    async def verify_identity(self, patient: PatientProfile, method: str) -> VerificationResult:
        import asyncio
        await asyncio.sleep(10.0)
        # Unreachable under the engine's 10s default; the engine's
        # _call_with_timeout returns None which it maps to "unknown".
        raise AssertionError("should have timed out")


class TimeoutBureau(BureauAdapter):
    """Sleeps long enough for both pulls to time out."""
    async def soft_pull(self, patient: PatientProfile) -> BureauResult:
        import asyncio
        await asyncio.sleep(10.0)
        raise AssertionError("should have timed out")

    async def hard_pull(self, patient: PatientProfile) -> BureauResult:
        import asyncio
        await asyncio.sleep(10.0)
        raise AssertionError("should have timed out")


# ---------------------------------------------------------------------------
# Identity manual_review × bureau outcome matrix
# ---------------------------------------------------------------------------


async def test_identity_manual_review_with_clean_bureau():
    """720 score + identity manual_review → application-level manual_review
    with identity_manual_review listed in the reasons."""
    adapters = make_adapters(
        verification=ScriptedVerification(result="manual_review", confidence=0.95),
        bureau=MockBureauAdapter(forced_score=720, forced_bankruptcy=False, forced_fraud_high_risk=False),
    )
    decision = await run_flow(make_application(), make_product(), make_patient(), adapters)
    assert isinstance(decision, FlowDecision)
    assert decision.decision == "manual_review"
    assert decision.next_state == "under_review"
    assert REASON_IDENTITY_MANUAL_REVIEW in decision.decision_reasons
    # Should NOT have the bureau-band reason (clean 720 is above the band).
    assert "manual_review_band" not in decision.decision_reasons
    # The identity verification rows record the effective outcome.
    identity_results = {v["result"] for v in decision.verifications_performed if v["type"] == "identity"}
    assert identity_results == {"manual_review"}


async def test_identity_manual_review_with_band_bureau():
    """Mid-band 650 + identity manual_review → both reasons present."""
    adapters = make_adapters(
        verification=ScriptedVerification(result="manual_review"),
        bureau=MockBureauAdapter(forced_score=650, forced_bankruptcy=False, forced_fraud_high_risk=False),
    )
    decision = await run_flow(make_application(), make_product(), make_patient(), adapters)
    assert decision.decision == "manual_review"
    assert REASON_IDENTITY_MANUAL_REVIEW in decision.decision_reasons
    assert "manual_review_band" in decision.decision_reasons


async def test_identity_manual_review_with_below_floor_bureau():
    """Hard disqualifier wins: a confirmed sub-floor score declines regardless
    of identity. ``identity_manual_review`` MAY still appear in the reasons
    because it's pushed before resolution; ``bureau_below_minimum`` MUST."""
    adapters = make_adapters(
        verification=ScriptedVerification(result="manual_review"),
        bureau=MockBureauAdapter(forced_score=550, forced_bankruptcy=False, forced_fraud_high_risk=False),
    )
    decision = await run_flow(make_application(), make_product(), make_patient(), adapters)
    assert decision.decision == "declined"
    assert "bureau_below_minimum" in decision.decision_reasons


async def test_identity_manual_review_with_bankruptcy():
    """Bankruptcy is a hard disqualifier; identity manual_review doesn't rescue."""
    adapters = make_adapters(
        verification=ScriptedVerification(result="manual_review"),
        bureau=MockBureauAdapter(forced_score=720, forced_bankruptcy=True, forced_fraud_high_risk=False),
    )
    decision = await run_flow(make_application(), make_product(), make_patient(), adapters)
    assert decision.decision == "declined"
    assert "active_bankruptcy" in decision.decision_reasons


async def test_identity_manual_review_with_bureau_unknown():
    """Identity manual_review + bureau timeout → manual_review with both reasons."""
    adapters = make_adapters(
        verification=ScriptedVerification(result="manual_review"),
        bureau=TimeoutBureau(),
    )
    # Use a tiny timeout so the suite stays fast.
    decision = await run_flow(
        make_application(), make_product(), make_patient(), adapters,
        timeout_seconds=0.05,
    )
    assert decision.decision == "manual_review"
    assert REASON_IDENTITY_MANUAL_REVIEW in decision.decision_reasons
    assert "verification_unknown:bureau_soft" in decision.decision_reasons


# ---------------------------------------------------------------------------
# Regression guards — non-manual_review paths unchanged
# ---------------------------------------------------------------------------


async def test_identity_passed_clean_bureau_still_approves():
    """Pre-P7.6 happy path: identity passed + clean bureau → approved.
    Regression guard so the new branch doesn't accidentally divert it."""
    decision = await run_flow(make_application(), make_product(), make_patient(), make_adapters())
    assert decision.decision == "approved"
    assert REASON_IDENTITY_MANUAL_REVIEW not in decision.decision_reasons


async def test_identity_failed_still_manual_review():
    """Identity failed → manual_review via has_failed_required (existing).
    Identity_manual_review reason should NOT be in the list — they're
    independent signals (kickoff §"NOT blockers" #2)."""
    adapters = make_adapters(
        verification=ScriptedVerification(result="failed"),
        bureau=MockBureauAdapter(forced_score=720, forced_bankruptcy=False, forced_fraud_high_risk=False),
    )
    decision = await run_flow(make_application(), make_product(), make_patient(), adapters)
    assert decision.decision == "manual_review"
    assert "verification_failed:identity" in decision.decision_reasons
    assert REASON_IDENTITY_MANUAL_REVIEW not in decision.decision_reasons


async def test_identity_unknown_still_manual_review():
    """Identity unknown (adapter timeout) → manual_review via has_unknown.
    Identity_manual_review reason should NOT be in the list."""
    adapters = make_adapters(
        verification=TimeoutVerification(),
        bureau=MockBureauAdapter(forced_score=720, forced_bankruptcy=False, forced_fraud_high_risk=False),
    )
    decision = await run_flow(
        make_application(), make_product(), make_patient(), adapters,
        timeout_seconds=0.05,
    )
    assert decision.decision == "manual_review"
    assert "verification_unknown:identity" in decision.decision_reasons
    assert REASON_IDENTITY_MANUAL_REVIEW not in decision.decision_reasons


# ---------------------------------------------------------------------------
# Group combination — average strategy pulls the group to manual_review
# ---------------------------------------------------------------------------


async def test_group_average_identity_manual_review_pulls_group():
    """Primary identity manual_review + clean co-applicant identity passed,
    both clean scores → group manual_review via the existing per-applicant
    rule in ``_combine_average``."""
    adapters = make_adapters(
        # Primary always returns manual_review; co also calls verify_identity,
        # so this single adapter will return manual_review for both. The point
        # of the test is the GROUP-level rule that catches per-applicant
        # manual_review — see ``_combine_average``. The primary's
        # identity_manual_review reason is enough to pull the group.
        verification=ScriptedVerification(result="manual_review"),
        bureau=MockBureauAdapter(forced_score=720, forced_bankruptcy=False, forced_fraud_high_risk=False),
    )
    co = ApplicantInput(application=make_application(), product=make_product(), patient=make_patient(email="co@x.test"))
    decision = await run_flow(
        make_application(), make_product(), make_patient(email="primary@x.test"),
        adapters, co_applicants=[co],
    )
    assert decision.decision == "manual_review"
    assert REASON_IDENTITY_MANUAL_REVIEW in decision.decision_reasons
    group_events = [e for e in decision.events_to_emit if e["event_type"] == "flow.group_decision_evaluated"]
    assert len(group_events) == 1
    assert group_events[0]["payload"]["applicant_count"] == 2


# ---------------------------------------------------------------------------
# Replay adapter sanity — accepts the widened Literal
# ---------------------------------------------------------------------------


async def test_replay_verification_adapter_accepts_manual_review():
    """The orchestrator's ``ReplayVerificationAdapter`` reconstructs a
    ``VerificationResult`` from the stored ``verification_completed`` event
    payload. With the P7.6 widening of ``VerificationOutcome``, a stored
    ``"result": "manual_review"`` (P7.5 webhook landing) must construct
    cleanly instead of raising — this is the type-system regression guard."""
    stored = {
        "kyc_id": {
            "result": "manual_review",
            "method": "document",
            "confidence": 0.95,
            "vendor": "didit",
            "vendor_session_ref": "didit-sess-abc",
        }
    }
    adapter = ReplayVerificationAdapter(stored)
    result = await adapter.verify_identity(
        PatientProfile(patient_id=__import__("uuid").uuid4()),
        method="id_doc_scan",
    )
    assert result.result == "manual_review"
    assert result.confidence == 0.95
    assert result.vendor == "didit"
