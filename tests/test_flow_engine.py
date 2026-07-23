"""Pure unit tests for the P4 flow engine.

No database, no network. The engine is pure (Hard Rule #2), so its tests are pure:
adapters are mocks/stubs and inputs are plain value objects. This file is the
verbatim merge-gate evidence for P4 (run with
``python -m pytest tests/test_flow_engine.py -v``).

Each Hard Rule that the engine is responsible for has at least one test below; see
the mapping in the PR description.
"""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.adapters import (
    BankAccountSummary,
    BureauResult,
    FlowAdapters,
    MockBankAdapter,
    MockBureauAdapter,
    MockVerificationAdapter,
    PatientProfile,
)
from app.services.adapters.base import BankAdapter, BureauAdapter
from app.services.adapters.didit import DiditSessionVerificationAdapter
from app.services.flow_engine import ApplicantInput, FlowDecision, run_flow

# Matches the seeded dental_full_arch_v1 verification_matrix (migration 022).
DENTAL_MATRIX: dict = {
    "identity": {"required": True, "methods": ["email_otp", "id_doc_scan"], "min_confidence": 0.85},
    "income": {
        "required": True,
        "methods": ["bank_link", "t4", "noa"],
        "require_bank_link": False,
        "min_stated_income_cents": 5_000_000,
    },
    "bureau": {"soft_pull_required": True, "hard_pull_required": True, "min_score": 660, "max_score_age_days": 90},
    "affordability": {"max_dti": 0.43, "min_payment_to_income_ratio": 0.20, "max_loan_to_income_ratio": 3.0},
}


def make_product(matrix: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        verification_matrix=copy.deepcopy(DENTAL_MATRIX) if matrix is None else matrix,
    )


def make_application() -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), requested_amount_cents=4_000_000)


def make_patient(*, province: str = "BC", email: str = "patient@example.com") -> PatientProfile:
    return PatientProfile(patient_id=uuid4(), province=province, email=email)


def make_adapters(*, verification=None, bureau=None, bank=None) -> FlowAdapters:
    return FlowAdapters(
        verification=verification or MockVerificationAdapter(),
        bureau=bureau or MockBureauAdapter(forced_score=720, forced_bankruptcy=False, forced_fraud_high_risk=False),
        bank=bank or MockBankAdapter(),
    )


# --- Required kickoff cases ----------------------------------------------------


async def test_happy_path_all_green_approved():
    decision = await run_flow(make_application(), make_product(), make_patient(), make_adapters())
    assert isinstance(decision, FlowDecision)
    assert decision.decision == "approved"
    assert decision.next_state == "approved"
    assert decision.decision_reasons == []
    # identity (x2 methods) + income bank_link + bureau soft + bureau hard
    methods = {(v["type"], v["method"]) for v in decision.verifications_performed}
    assert ("bureau", "soft_pull") in methods
    assert ("bureau", "hard_pull") in methods
    assert all(v["result"] == "passed" for v in decision.verifications_performed)


async def test_low_score_declined():
    adapters = make_adapters(
        bureau=MockBureauAdapter(forced_score=560, forced_bankruptcy=False, forced_fraud_high_risk=False)
    )
    decision = await run_flow(make_application(), make_product(), make_patient(), adapters)
    assert decision.decision == "declined"  # decision OUTCOME token unchanged
    assert decision.next_state == "rejected"  # STATUS renamed declined->rejected
    assert "bureau_below_minimum" in decision.decision_reasons
    # Soft score below floor must NOT trigger a hard pull (conserve bureau calls).
    methods = {(v["type"], v["method"]) for v in decision.verifications_performed}
    assert ("bureau", "hard_pull") not in methods


async def test_mid_band_manual_review():
    adapters = make_adapters(
        bureau=MockBureauAdapter(forced_score=640, forced_bankruptcy=False, forced_fraud_high_risk=False)
    )
    decision = await run_flow(make_application(), make_product(), make_patient(), adapters)
    assert decision.decision == "manual_review"
    assert decision.next_state == "under_review"
    assert "manual_review_band" in decision.decision_reasons


async def test_co_applicant_average_manual_review():
    """Primary 700 + co-applicant 600 -> average 650 -> manual_review band.

    (Was 700/640 -> 670; 670 now AUTO-APPROVES under the corrected 660 cut —
    P0/T4. The averaging behaviour under test is unchanged.)"""
    scores = {"primary@x.test": 700, "co@x.test": 600}
    adapters = make_adapters(bureau=ScriptedBureau(scores))

    primary_patient = make_patient(email="primary@x.test")
    co_patient = make_patient(email="co@x.test")
    co = ApplicantInput(application=make_application(), product=make_product(), patient=co_patient)

    decision = await run_flow(make_application(), make_product(), primary_patient, adapters, co_applicants=[co])
    assert decision.decision == "manual_review"
    assert "manual_review_band" in decision.decision_reasons
    group_events = [e for e in decision.events_to_emit if e["event_type"] == "flow.group_decision_evaluated"]
    assert len(group_events) == 1
    assert group_events[0]["payload"]["average_score"] == 650
    assert group_events[0]["payload"]["applicant_count"] == 2


async def test_co_applicant_hard_decline_passthrough():
    """If either applicant hard-declines, the group declines regardless of average."""
    scores = {"primary@x.test": 720, "co@x.test": 540}
    adapters = make_adapters(bureau=ScriptedBureau(scores))
    co = ApplicantInput(
        application=make_application(), product=make_product(), patient=make_patient(email="co@x.test")
    )
    decision = await run_flow(
        make_application(), make_product(), make_patient(email="primary@x.test"), adapters, co_applicants=[co]
    )
    assert decision.decision == "declined"


async def test_engine_adapter_agnostic():
    """Hard Rule #4 — swap only the verification adapter (Mock <-> Didit) via config;
    the engine produces a decision either way with no engine changes."""
    bureau = MockBureauAdapter(forced_score=720, forced_bankruptcy=False, forced_fraud_high_risk=False)

    mock_adapters = FlowAdapters(MockVerificationAdapter(), bureau, MockBankAdapter())
    mock_decision = await run_flow(make_application(), make_product(), make_patient(), mock_adapters)

    # Stub the session-oriented Didit HTTP layer by injecting a fake client — keeps
    # the test offline with zero new dependencies; no real Didit credentials in CI.
    from unittest.mock import AsyncMock

    fake_client = SimpleNamespace(
        create_verification_session=AsyncMock(
            return_value=SimpleNamespace(kyc_session_id=uuid4(), verification_url="https://x", expires_at="z")
        )
    )
    didit_adapters = FlowAdapters(DiditSessionVerificationAdapter(client=fake_client), bureau, MockBankAdapter())
    didit_decision = await run_flow(make_application(), make_product(), make_patient(), didit_adapters)

    assert isinstance(mock_decision, FlowDecision)
    assert isinstance(didit_decision, FlowDecision)
    assert mock_decision.decision in {"approved", "declined", "manual_review"}
    assert didit_decision.decision in {"approved", "declined", "manual_review"}
    # Mock identity passes -> approved; Didit session is initiated-only -> identity
    # "unknown" -> manual_review. Both are valid decisions from the same engine.
    assert mock_decision.decision == "approved"
    assert didit_decision.decision == "manual_review"
    assert fake_client.create_verification_session.await_count >= 1


# --- Hard-rule-specific coverage ----------------------------------------------


async def test_quebec_immediate_decline_no_adapter_calls():
    """Hard Rule #9 — QC declines immediately, before any adapter call."""
    # Bureau would otherwise approve (720); Quebec must still decline.
    decision = await run_flow(make_application(), make_product(), make_patient(province="QC"), make_adapters())
    assert decision.decision == "declined"
    assert decision.decision_reasons == ["quebec_coming_soon"]
    # No verifications were performed -> proves no adapter was called.
    assert decision.verifications_performed == []


async def test_manual_review_band_product_override():
    """Hard Rule #7 — band override comes from verification_matrix.bureau.manual_review_band."""
    matrix = copy.deepcopy(DENTAL_MATRIX)
    matrix["bureau"]["manual_review_band"] = {"min": 650, "max": 700}
    adapters = make_adapters(
        bureau=MockBureauAdapter(forced_score=695, forced_bankruptcy=False, forced_fraud_high_risk=False)
    )
    # 695 would be APPROVED under the default 660 floor, but the override band
    # (650-700) puts it in manual_review.
    decision = await run_flow(make_application(), make_product(matrix), make_patient(), adapters)
    assert decision.decision == "manual_review"
    assert "manual_review_band" in decision.decision_reasons


async def test_engine_reads_snapshotted_matrix_only():
    """Hard Rule #8 — the engine reads the matrix from the passed product snapshot,
    never re-fetching. An empty matrix yields no verifications and no bureau calls."""
    decision = await run_flow(make_application(), make_product({}), make_patient(), make_adapters())
    assert decision.verifications_performed == []
    assert decision.decision == "approved"


async def test_required_identity_below_min_confidence_is_manual_review():
    """A 'passed' IDV below the product's min_confidence is downgraded to failed
    (threshold authority is the product, Hard Rule #7) -> manual_review."""
    adapters = make_adapters(verification=MockVerificationAdapter(forced_confidence=0.50))
    decision = await run_flow(make_application(), make_product(), make_patient(), adapters)
    assert decision.decision == "manual_review"
    assert "verification_failed:identity" in decision.decision_reasons


async def test_adapter_timeout_yields_unknown_manual_review():
    """Timeout -> result 'unknown' (not 'failed') -> manual_review, regardless of
    the score the call would have returned."""
    adapters = make_adapters(bureau=SlowBureau())
    decision = await run_flow(
        make_application(), make_product(), make_patient(), adapters, timeout_seconds=0.01
    )
    assert decision.decision == "manual_review"
    assert "verification_unknown:bureau_soft" in decision.decision_reasons


async def test_non_average_strategy_raises():
    """Pre-resolved Decision #1 — only 'average' is implemented for MVP."""
    co = ApplicantInput(application=make_application(), product=make_product(), patient=make_patient(email="co@x.test"))
    with pytest.raises(NotImplementedError, match="MVP supports only 'average'"):
        await run_flow(
            make_application(), make_product(), make_patient(), make_adapters(),
            co_applicants=[co], combination_strategy="strongest_standalone_override",
        )


async def test_single_applicant_ignores_combination_strategy():
    """Empty co-applicant case: combination_strategy is ignored entirely."""
    decision = await run_flow(
        make_application(), make_product(), make_patient(), make_adapters(),
        combination_strategy="strongest_standalone_override",
    )
    assert decision.decision == "approved"


async def test_mock_bureau_deterministic_with_safescan_signals():
    """Hard Rule #10 — deterministic by email + SafeScan-style synthetic signals."""
    adapter = MockBureauAdapter()
    patient = make_patient(email="determinism@x.test")
    first = await adapter.soft_pull(patient)
    second = await adapter.soft_pull(patient)
    assert first.score == second.score
    assert first.fraud_signals == second.fraud_signals
    assert "safescan_score" in first.fraud_signals
    assert "identity_high_risk" in first.fraud_signals


async def test_mock_bank_deterministic_flinks_shape():
    """Hard Rule #11 — deterministic by email + Flinks-shaped income/NSF."""
    adapter = MockBankAdapter()
    patient = make_patient(email="bankcheck@x.test")
    first = await adapter.link_account(patient)
    second = await adapter.link_account(patient)
    assert first == second
    assert first.monthly_income_after_tax_cents > 0
    assert first.nsf_count_90d >= 0
    assert first.account_age_months >= 0


async def test_no_pii_in_emitted_events():
    """Hard Rule #6 — no PII (email, names) in any emitted event payload."""
    patient = PatientProfile(
        patient_id=uuid4(),
        province="BC",
        email="sentinel-pii@x.test",
        legal_first_name="ZZSENTINELFIRST",
        legal_last_name="ZZSENTINELLAST",
    )
    decision = await run_flow(make_application(), make_product(), patient, make_adapters())
    blob = json.dumps(decision.events_to_emit)
    assert "sentinel-pii@x.test" not in blob
    assert "ZZSENTINELFIRST" not in blob
    assert "ZZSENTINELLAST" not in blob
    # And not in the surfaced verifications either.
    assert "sentinel-pii@x.test" not in json.dumps(decision.verifications_performed)


async def test_soft_then_hard_pull_sequencing_runs_hard_when_eligible():
    """Score >= floor AND hard_pull_required -> a hard pull is performed."""
    adapters = make_adapters(
        bureau=MockBureauAdapter(forced_score=700, forced_bankruptcy=False, forced_fraud_high_risk=False)
    )
    decision = await run_flow(make_application(), make_product(), make_patient(), adapters)
    methods = {(v["type"], v["method"]) for v in decision.verifications_performed}
    assert ("bureau", "soft_pull") in methods
    assert ("bureau", "hard_pull") in methods


async def test_bankruptcy_hard_declines_even_with_good_score():
    """A confirmed bankruptcy is a hard decline even when the score is in range."""
    adapters = make_adapters(
        bureau=MockBureauAdapter(forced_score=720, forced_bankruptcy=True, forced_fraud_high_risk=False)
    )
    decision = await run_flow(make_application(), make_product(), make_patient(), adapters)
    assert decision.decision == "declined"
    assert "active_bankruptcy" in decision.decision_reasons


# --- Test-only scripted adapters ----------------------------------------------


class ScriptedBureau(BureauAdapter):
    """Returns a fixed score per patient email — lets co-applicant tests control
    each applicant's score precisely."""

    def __init__(self, scores_by_email: dict[str, int]) -> None:
        self._scores = scores_by_email

    def _result(self, patient: PatientProfile, pull_type: str) -> BureauResult:
        return BureauResult(
            pull_type=pull_type,  # type: ignore[arg-type]
            score=self._scores[patient.email or ""],
            result="passed",
            bankruptcy=False,
            fraud_signals={"safescan_score": 100, "identity_high_risk": False},
        )

    async def soft_pull(self, patient: PatientProfile) -> BureauResult:
        return self._result(patient, "soft")

    async def hard_pull(self, patient: PatientProfile) -> BureauResult:
        return self._result(patient, "hard")


class SlowBureau(BureauAdapter):
    """Soft pull that exceeds the engine timeout, to exercise the unknown path."""

    async def soft_pull(self, patient: PatientProfile) -> BureauResult:
        await asyncio.sleep(1.0)
        return BureauResult("soft", 720, "passed")

    async def hard_pull(self, patient: PatientProfile) -> BureauResult:
        return BureauResult("hard", 720, "passed")
