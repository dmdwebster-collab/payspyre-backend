"""PaySpyre P4 — Flow Engine (pure functional core).

``run_flow`` reads a credit application, its snapshotted credit-product config, a
patient profile and a set of adapters, and returns a :class:`FlowDecision`. It is
**pure**: it performs no DB writes and no network calls of its own — adapters do
all I/O (Hard Rule #2). State transitions and event emission belong to the P6
orchestration layer; this engine only *returns* ``next_state`` and a list of
``events_to_emit`` for the caller to persist (Hard Rule #3).

Decision logic (kickoff Pre-resolved Decision #2):

    score >= 680            -> approved
    600 <= score <= 679     -> manual_review
    score <  600            -> declined

The manual-review band defaults to ``{min: 600, max: 679}`` and is overridable
per-product via ``verification_matrix.bureau.manual_review_band`` (all thresholds
come from the snapshotted product config — Hard Rules #7 / #8 — never hardcoded
elsewhere).

NOTE ON SPEC §4: spec §4 describes a *stateful* engine that writes platform_events
and updates flow_state. This module deliberately implements only the pure subset
mandated by the kickoff (Hard Rules #2 / #3); stateful orchestration is P6.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Optional, TypeVar
from uuid import UUID

import structlog

from app.services.adapters.base import (
    BankAccountSummary,
    BureauResult,
    FlowAdapters,
    PatientProfile,
    VerificationResult,
)

logger = structlog.get_logger(__name__)

T = TypeVar("T")

# Engine defaults (kickoff Pre-resolved Decision #2). Overridable per-product via
# verification_matrix.bureau.manual_review_band.
DEFAULT_MANUAL_REVIEW_BAND: dict[str, int] = {"min": 600, "max": 679}

DEFAULT_TIMEOUT_SECONDS = 10.0

# decision -> application status (platform_application_status enum). The engine
# returns this; it does not set it (Hard Rule #3).
DECISION_TO_STATE: dict[str, str] = {
    "approved": "approved",
    "declined": "declined",
    "manual_review": "under_review",
}

# Stable decision_reasons codes.
REASON_QUEBEC = "quebec_coming_soon"
REASON_MANUAL_REVIEW_BAND = "manual_review_band"
REASON_BUREAU_BELOW_MINIMUM = "bureau_below_minimum"
REASON_ACTIVE_BANKRUPTCY = "active_bankruptcy"
REASON_FRAUD_SIGNAL_REVIEW = "fraud_signal_review"
REASON_IDENTITY_MANUAL_REVIEW = "identity_manual_review"  # P7.6 — Didit "In Review"


def _reason_failed(verification_type: str) -> str:
    return f"verification_failed:{verification_type}"


def _reason_unknown(verification_type: str) -> str:
    return f"verification_unknown:{verification_type}"


@dataclass(frozen=True)
class FlowDecision:
    """The value object returned by :func:`run_flow`."""

    decision: str  # "approved" | "declined" | "manual_review"
    decision_reasons: list[str]
    verifications_performed: list[dict[str, Any]]
    next_state: str
    events_to_emit: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "decision_reasons": self.decision_reasons,
            "verifications_performed": self.verifications_performed,
            "next_state": self.next_state,
            "events_to_emit": self.events_to_emit,
        }


@dataclass(frozen=True)
class ApplicantInput:
    """A single applicant's inputs, used to pass co-applicants to ``run_flow``."""

    application: Any
    product: Any
    patient: PatientProfile


@dataclass
class _ApplicantOutcome:
    decision: str
    effective_score: Optional[int]
    reasons: list[str]
    verifications: list[dict[str, Any]]
    events: list[dict[str, Any]]
    band: dict[str, int]


async def _call_with_timeout(coro: Awaitable[T], timeout_seconds: float) -> Optional[T]:
    """Await an adapter call under a timeout. On timeout, return None — the caller
    maps that to result="unknown" (NOT "failed"), per the kickoff timeout policy."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return None


def _matrix(product: Any) -> dict[str, Any]:
    matrix = getattr(product, "verification_matrix", None)
    return matrix if isinstance(matrix, dict) else {}


def _manual_review_band(bureau_cfg: dict[str, Any]) -> dict[str, int]:
    band = bureau_cfg.get("manual_review_band")
    if isinstance(band, dict) and "min" in band and "max" in band:
        return {"min": int(band["min"]), "max": int(band["max"])}
    return dict(DEFAULT_MANUAL_REVIEW_BAND)


def _verification_dict(
    verification_type: str, method: str, result: str, confidence: float
) -> dict[str, Any]:
    return {
        "type": verification_type,
        "method": method,
        "result": result,
        "confidence": round(float(confidence), 2),
    }


def _event(
    event_type: str,
    patient_id: Optional[UUID],
    application_id: Optional[UUID],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Build a platform_events-shaped dict for the orchestration layer to insert.

    Payloads carry only verification types/methods/results, scores, and counts —
    never email, name, DOB or other PII (Hard Rule #6)."""
    return {
        "event_type": event_type,
        "actor": "flow_engine",
        "patient_id": str(patient_id) if patient_id else None,
        "application_id": str(application_id) if application_id else None,
        "payload": payload,
    }


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _effective_identity_outcome(result: Optional[VerificationResult], min_confidence: float) -> str:
    """Apply the product's min_confidence threshold to a raw IDV result (Hard Rule
    #7 — threshold authority stays with the product config, not the adapter)."""
    if result is None or result.result == "unknown":
        return "unknown"
    if result.result == "failed":
        return "failed"
    if result.result == "manual_review":
        # P7.6 — vendor-asserted human review (Didit "In Review"). Distinct
        # from "unknown" (timeout/indeterminate); the confidence floor does
        # NOT apply because manual_review is not a graded outcome.
        return "manual_review"
    # result == "passed": enforce the product's confidence floor.
    if result.confidence >= min_confidence:
        return "passed"
    return "failed"


def _resolve_decision(
    outcome: _ApplicantOutcome,
    *,
    bankruptcy: bool,
    fraud_review: bool,
    has_unknown: bool,
    has_failed_required: bool,
    identity_manual_review: bool,
) -> str:
    """Resolve a single applicant's decision.

    Precedence: a *confirmed* hard disqualifier (bankruptcy, or a confirmed
    below-floor score) is final and declines — an unrelated timeout does not
    rescue it to manual_review. Otherwise any indeterminate/soft-review condition
    (unknown verification, failed required verification, fraud signal, identity
    manual-review — P7.6 — or a score in the manual-review band) yields
    manual_review. A clean above-band score approves."""
    score = outcome.effective_score
    band = outcome.band

    if bankruptcy:
        return "declined"
    if score is not None and score < band["min"]:
        if REASON_BUREAU_BELOW_MINIMUM not in outcome.reasons:
            outcome.reasons.append(REASON_BUREAU_BELOW_MINIMUM)
        return "declined"

    review = has_unknown or has_failed_required or fraud_review or identity_manual_review
    if score is not None and band["min"] <= score <= band["max"]:
        if REASON_MANUAL_REVIEW_BAND not in outcome.reasons:
            outcome.reasons.append(REASON_MANUAL_REVIEW_BAND)
        review = True

    if review:
        return "manual_review"
    return "approved"


async def _run_single_applicant(
    application: Any, product: Any, patient: PatientProfile, adapters: FlowAdapters,
    timeout_seconds: float,
) -> _ApplicantOutcome:
    reasons: list[str] = []
    verifications: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    matrix = _matrix(product)
    bureau_cfg = matrix.get("bureau", {}) if isinstance(matrix.get("bureau"), dict) else {}
    band = _manual_review_band(bureau_cfg)

    application_id = getattr(application, "id", None)
    patient_id = patient.patient_id

    # Hard Rule #9 — Quebec gate, before any adapter calls (conserves vendor calls).
    if (patient.province or "").strip().upper() == "QC":
        reasons.append(REASON_QUEBEC)
        events.append(_event("flow.quebec_blocked", patient_id, application_id, {"province": "QC"}))
        return _ApplicantOutcome("declined", None, reasons, verifications, events, band)

    has_unknown = False
    has_failed_required = False
    has_identity_manual_review = False  # P7.6 — vendor-asserted human review

    # --- Identity (KYC) ---------------------------------------------------
    identity_cfg = matrix.get("identity", {}) if isinstance(matrix.get("identity"), dict) else {}
    if identity_cfg.get("required"):
        min_confidence = float(identity_cfg.get("min_confidence", 0.0))
        methods = identity_cfg.get("methods") or ["id_doc_scan"]
        for method in methods:
            raw = await _call_with_timeout(adapters.verification.verify_identity(patient, method), timeout_seconds)
            effective = _effective_identity_outcome(raw, min_confidence)
            confidence = raw.confidence if raw is not None else 0.0
            verifications.append(_verification_dict("identity", method, effective, confidence))
            events.append(_event("flow.verification_performed", patient_id, application_id,
                                 {"type": "identity", "method": method, "result": effective}))
            if effective == "unknown":
                has_unknown = True
                reasons.append(_reason_unknown("identity"))
            elif effective == "failed":
                has_failed_required = True
                reasons.append(_reason_failed("identity"))
            elif effective == "manual_review":
                has_identity_manual_review = True
                if REASON_IDENTITY_MANUAL_REVIEW not in reasons:
                    reasons.append(REASON_IDENTITY_MANUAL_REVIEW)

    # --- Income (bank link) ----------------------------------------------
    income_cfg = matrix.get("income", {}) if isinstance(matrix.get("income"), dict) else {}
    if income_cfg.get("required"):
        methods = income_cfg.get("methods") or []
        require_bank = bool(income_cfg.get("require_bank_link"))
        if "bank_link" in methods:
            bank: Optional[BankAccountSummary] = await _call_with_timeout(
                adapters.bank.link_account(patient), timeout_seconds
            )
            result = "unknown" if bank is None else bank.result
            confidence = bank.confidence if bank is not None else 0.0
            verifications.append(_verification_dict("income", "bank_link", result, confidence))
            event_payload: dict[str, Any] = {"type": "income", "method": "bank_link", "result": result}
            if bank is not None:
                event_payload["nsf_count_90d"] = bank.nsf_count_90d
            events.append(_event("flow.verification_performed", patient_id, application_id, event_payload))
            # Only block when this product actually requires the bank link; otherwise
            # income can be satisfied by other methods (t4/noa, handled outside MVP).
            if require_bank:
                if result == "unknown":
                    has_unknown = True
                    reasons.append(_reason_unknown("income"))
                elif result == "failed":
                    has_failed_required = True
                    reasons.append(_reason_failed("income"))

    # --- Bureau (soft pull, then conditional hard pull) -------------------
    bankruptcy = False
    fraud_review = False
    effective_score: Optional[int] = None
    if "bureau" in matrix and bureau_cfg.get("soft_pull_required", True):
        soft: Optional[BureauResult] = await _call_with_timeout(adapters.bureau.soft_pull(patient), timeout_seconds)
        if soft is None or soft.result == "unknown":
            has_unknown = True
            verifications.append(_verification_dict("bureau", "soft_pull", "unknown", 0.0))
            events.append(_event("flow.verification_performed", patient_id, application_id,
                                 {"type": "bureau", "method": "soft_pull", "result": "unknown"}))
            reasons.append(_reason_unknown("bureau_soft"))
        else:
            effective_score = soft.score
            verifications.append(_verification_dict("bureau", "soft_pull", soft.result, soft.confidence))
            events.append(_event("flow.verification_performed", patient_id, application_id,
                                 {"type": "bureau", "method": "soft_pull", "result": soft.result, "score": soft.score}))
            if soft.bankruptcy:
                bankruptcy = True
            if soft.fraud_signals.get("identity_high_risk"):
                fraud_review = True

            # Soft-then-hard sequencing: only escalate to a hard pull when the soft
            # score is at least the decline floor AND the product requires it. A
            # sub-floor soft score is already a decline — don't burn a hard pull.
            if soft.score >= band["min"] and bureau_cfg.get("hard_pull_required"):
                hard: Optional[BureauResult] = await _call_with_timeout(adapters.bureau.hard_pull(patient), timeout_seconds)
                if hard is None or hard.result == "unknown":
                    has_unknown = True
                    verifications.append(_verification_dict("bureau", "hard_pull", "unknown", 0.0))
                    events.append(_event("flow.verification_performed", patient_id, application_id,
                                         {"type": "bureau", "method": "hard_pull", "result": "unknown"}))
                    reasons.append(_reason_unknown("bureau_hard"))
                else:
                    effective_score = hard.score
                    verifications.append(_verification_dict("bureau", "hard_pull", hard.result, hard.confidence))
                    events.append(_event("flow.verification_performed", patient_id, application_id,
                                         {"type": "bureau", "method": "hard_pull", "result": hard.result, "score": hard.score}))
                    if hard.bankruptcy:
                        bankruptcy = True
                    if hard.fraud_signals.get("identity_high_risk"):
                        fraud_review = True

    if bankruptcy and REASON_ACTIVE_BANKRUPTCY not in reasons:
        reasons.append(REASON_ACTIVE_BANKRUPTCY)
    if fraud_review and REASON_FRAUD_SIGNAL_REVIEW not in reasons:
        reasons.append(REASON_FRAUD_SIGNAL_REVIEW)

    outcome = _ApplicantOutcome("manual_review", effective_score, reasons, verifications, events, band)
    outcome.decision = _resolve_decision(
        outcome, bankruptcy=bankruptcy, fraud_review=fraud_review,
        has_unknown=has_unknown, has_failed_required=has_failed_required,
        identity_manual_review=has_identity_manual_review,
    )
    return outcome


def _finalize(outcome: _ApplicantOutcome, patient_id: Optional[UUID], application_id: Optional[UUID]) -> FlowDecision:
    decision = outcome.decision
    reasons = _dedupe(outcome.reasons)
    events = list(outcome.events)
    events.append(_event("flow.decision_evaluated", patient_id, application_id, {
        "decision": decision,
        "decision_reasons": reasons,
        "effective_score": outcome.effective_score,
        "next_state": DECISION_TO_STATE[decision],
    }))
    logger.info("flow_decision", application_id=str(application_id) if application_id else None,
                decision=decision, decision_reasons=reasons, effective_score=outcome.effective_score)
    return FlowDecision(
        decision=decision,
        decision_reasons=reasons,
        verifications_performed=outcome.verifications,
        next_state=DECISION_TO_STATE[decision],
        events_to_emit=events,
    )


def _combine_average(outcomes: list[_ApplicantOutcome], patient_id: Optional[UUID],
                     application_id: Optional[UUID]) -> FlowDecision:
    """Combine an application group via the ``average`` strategy (kickoff Decision
    #1, spec §4.5). Hard declines pass through; otherwise the average score is
    re-banded and any applicant needing review pulls the group to manual_review."""
    band = outcomes[0].band
    all_reasons: list[str] = []
    all_verifications: list[dict[str, Any]] = []
    all_events: list[dict[str, Any]] = []
    for o in outcomes:
        all_reasons.extend(o.reasons)
        all_verifications.extend(o.verifications)
        all_events.extend(o.events)

    # Hard declines pass through (spec §4.5 rule 5).
    if any(o.decision == "declined" for o in outcomes):
        group_decision = "declined"
        average_score: Optional[int] = None
    else:
        scores = [o.effective_score for o in outcomes if o.effective_score is not None]
        average_score = round(sum(scores) / len(scores)) if scores else None
        group_decision = "approved"
        if average_score is not None and band["min"] <= average_score <= band["max"]:
            group_decision = "manual_review"
            if REASON_MANUAL_REVIEW_BAND not in all_reasons:
                all_reasons.append(REASON_MANUAL_REVIEW_BAND)
        elif average_score is not None and average_score < band["min"]:
            group_decision = "declined"
            if REASON_BUREAU_BELOW_MINIMUM not in all_reasons:
                all_reasons.append(REASON_BUREAU_BELOW_MINIMUM)
        # Any applicant individually needing review pulls the group to review
        # (can't approve a group where one applicant is unresolved).
        if group_decision == "approved" and any(o.decision == "manual_review" for o in outcomes):
            group_decision = "manual_review"

    reasons = _dedupe(all_reasons)
    all_events.append(_event("flow.group_decision_evaluated", patient_id, application_id, {
        "decision": group_decision,
        "decision_reasons": reasons,
        "combination_strategy": "average",
        "applicant_count": len(outcomes),
        "average_score": average_score,
        "next_state": DECISION_TO_STATE[group_decision],
    }))
    logger.info("flow_group_decision", application_id=str(application_id) if application_id else None,
                decision=group_decision, applicant_count=len(outcomes), average_score=average_score)
    return FlowDecision(
        decision=group_decision,
        decision_reasons=reasons,
        verifications_performed=all_verifications,
        next_state=DECISION_TO_STATE[group_decision],
        events_to_emit=all_events,
    )


async def run_flow(
    application: Any,
    product: Any,
    patient: PatientProfile,
    adapters: FlowAdapters,
    *,
    co_applicants: Optional[list[ApplicantInput]] = None,
    combination_strategy: str = "average",
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> FlowDecision:
    """Run the verification + decision flow for an application and return a
    :class:`FlowDecision`. Pure: no DB writes, no network calls outside adapters.

    For a single applicant, ``combination_strategy`` is ignored. When
    ``co_applicants`` are supplied, only ``"average"`` is implemented for MVP; any
    other strategy raises ``NotImplementedError`` (kickoff Pre-resolved Decision #1).
    """
    primary = await _run_single_applicant(application, product, patient, adapters, timeout_seconds)

    if not co_applicants:
        return _finalize(primary, patient.patient_id, getattr(application, "id", None))

    if combination_strategy != "average":
        raise NotImplementedError(
            f"combination_strategy={combination_strategy} not implemented; MVP supports only 'average'"
        )

    outcomes = [primary]
    for co in co_applicants:
        outcomes.append(
            await _run_single_applicant(co.application, co.product, co.patient, adapters, timeout_seconds)
        )
    return _combine_average(outcomes, patient.patient_id, getattr(application, "id", None))
