"""Maintain the patient-level marketplace denorm fields: ``lead_state`` +
``verification_depth`` on ``platform_patients``.

These columns are denormalized snapshots the marketplace reads to TIER leads
(``lead_state``) and PRICE them (``verification_depth``). Nothing else advances
them, so without this they sit at their defaults (``unqualified`` / ``none``)
forever — every marketplace lead is mis-tiered and underpriced. This module
recomputes them from the patient's *passed* verifications (the §6.1 lead-state
ladder) and stamps the terminal state on a credit decision.

Lead-state ladder (spec §6.1):
    unqualified → pre_qualified : kyc_id passed
    pre_qualified → pre_approved : a bureau check passed (decision gate)
    pre_approved → approved : application approved
    any → declined : application declined

Verification depth (from the passed set):
    kyc_id                              → id_verified
    kyc_id + bank_link                  → id_bank_verified
    kyc_id + bank_link + bureau_*       → id_bank_cb_verified

All advances are FORWARD-ONLY (a lead never regresses), except the terminal
``approved`` / ``declined`` set explicitly at decision time.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.platform.patient import PlatformPatient
from app.models.platform.verification import PlatformVerification

_DEPTH_RANK = {
    "none": 0,
    "email_verified": 1,
    "phone_verified": 2,
    "id_verified": 3,
    "id_bank_verified": 4,
    "id_bank_cb_verified": 5,
}
# 'declined' is terminal and set explicitly — it is not part of the forward ladder.
_LEAD_RANK = {"unqualified": 0, "pre_qualified": 1, "pre_approved": 2, "approved": 3}
_TERMINAL = {"approved", "declined"}
_BUREAU = {"bureau_soft", "bureau_hard"}


def _passed_types(db: Session, patient_id: UUID) -> set[str]:
    rows = (
        db.query(PlatformVerification.verification_type)
        .filter(
            PlatformVerification.patient_id == patient_id,
            PlatformVerification.status == "passed",
        )
        .all()
    )
    return {r[0] for r in rows}


def derive_verification_depth(passed: set[str]) -> str:
    """Map the set of passed verification types to a verification_depth value."""
    if "kyc_id" not in passed:
        return "none"
    if "bank_link" not in passed:
        return "id_verified"
    if passed & _BUREAU:
        return "id_bank_cb_verified"
    return "id_bank_verified"


def refresh_from_verifications(db: Session, patient: PlatformPatient) -> None:
    """Advance ``verification_depth`` + ``lead_state`` from the patient's passed
    verifications. Forward-only; never regresses, never touches a terminal state."""
    passed = _passed_types(db, patient.id)

    new_depth = derive_verification_depth(passed)
    if _DEPTH_RANK[new_depth] > _DEPTH_RANK.get(patient.verification_depth or "none", 0):
        patient.verification_depth = new_depth

    if patient.lead_state in _TERMINAL:
        return
    target = patient.lead_state or "unqualified"
    if "kyc_id" in passed and _LEAD_RANK[target] < _LEAD_RANK["pre_qualified"]:
        target = "pre_qualified"
    if (passed & _BUREAU) and _LEAD_RANK[target] < _LEAD_RANK["pre_approved"]:
        target = "pre_approved"
    if target != patient.lead_state:
        patient.lead_state = target
        patient.lead_state_updated_at = datetime.now(timezone.utc)


def apply_decision(db: Session, patient: PlatformPatient, application_status: str) -> None:
    """Stamp the terminal ``lead_state`` on a credit decision and capture any
    verification depth gained during the deciding flow. No-op for non-terminal
    statuses (e.g. ``under_review``)."""
    refresh_from_verifications(db, patient)
    if application_status == "approved":
        patient.lead_state = "approved"
    elif application_status == "declined":
        patient.lead_state = "declined"
    else:
        return
    patient.lead_state_updated_at = datetime.now(timezone.utc)
