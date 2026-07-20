"""Customer lock/block (WS-G — TL's "Lock user", video 09 f0007).

A block is an audit ROW (``platform_customer_blocks``), never a flag mutation
on the patient: who blocked, when, the directory reason code and the MANDATORY
free-text reason; unblocking closes the row (``unblocked_at``) rather than
deleting it, so the full lock/unlock history is queryable. A partial unique
index guarantees at most one ACTIVE block per patient.

Semantics (per the parity spec): **blocked = no NEW originations; servicing of
existing loans is unaffected.** Enforcement is a single helper —
``ensure_not_blocked`` — called from every origination entry point (clinic
vendor-intake; financing links funnel through the same patient row and land in
the same intake). Dunning, payments, and the borrower portal never consult
blocks.

Every block/unblock writes a ``platform_events`` row (§6 payload shape), so
the action shows up in the per-customer changelog automatically.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.platform.crm import PlatformCustomerBlock, PlatformCustomerBlockReason
from app.models.platform.event import PlatformEvent

BLOCKED_EVENT = "customer_blocked"
UNBLOCKED_EVENT = "customer_unblocked"


class CustomerBlockError(Exception):
    """Invalid block/unblock request (bad reason code, double block, ...)."""


class CustomerBlockedError(Exception):
    """Raised by ``ensure_not_blocked`` when a blocked patient starts an origination."""

    def __init__(self, patient_id) -> None:
        self.patient_id = patient_id
        super().__init__("This customer is blocked from new originations.")


def active_block(db: Session, patient_id) -> Optional[PlatformCustomerBlock]:
    """The patient's ACTIVE block row, or None."""
    return (
        db.query(PlatformCustomerBlock)
        .filter(
            PlatformCustomerBlock.patient_id == patient_id,
            PlatformCustomerBlock.unblocked_at.is_(None),
        )
        .first()
    )


def is_blocked(db: Session, patient_id) -> bool:
    return active_block(db, patient_id) is not None


def ensure_not_blocked(db: Session, patient_id) -> None:
    """Origination gate: raise ``CustomerBlockedError`` if the patient is blocked."""
    if patient_id is not None and is_blocked(db, patient_id):
        raise CustomerBlockedError(patient_id)


def _event(db: Session, *, event_type: str, patient_id, actor_id: str, after: dict) -> None:
    db.add(
        PlatformEvent(
            event_type=event_type,
            actor="admin",
            patient_id=patient_id,
            payload={
                "v": 1,
                "actor": {"type": "admin", "id": actor_id},
                "patient_id": str(patient_id),
                "before": {},
                "after": after,
            },
        )
    )


def block_patient(
    db: Session,
    *,
    patient_id,
    reason_code: str,
    reason_text: str,
    actor_id: str,
) -> PlatformCustomerBlock:
    """Create the ACTIVE block row + audit event. Caller owns the commit.

    ``reason_text`` is MANDATORY (Dave's lock dialog captures a block reason);
    ``reason_code`` must be an active directory code.
    """
    if not (reason_text or "").strip():
        raise CustomerBlockError("A block reason is required.")
    reason = (
        db.query(PlatformCustomerBlockReason)
        .filter(
            PlatformCustomerBlockReason.code == reason_code,
            PlatformCustomerBlockReason.active.is_(True),
        )
        .first()
    )
    if reason is None:
        raise CustomerBlockError(f"Unknown block reason code: {reason_code!r}")
    if is_blocked(db, patient_id):
        raise CustomerBlockError("Customer is already blocked.")

    row = PlatformCustomerBlock(
        patient_id=patient_id,
        reason_code=reason_code,
        reason_text=reason_text.strip(),
        blocked_by=actor_id,
    )
    db.add(row)
    _event(
        db,
        event_type=BLOCKED_EVENT,
        patient_id=patient_id,
        actor_id=actor_id,
        after={"blocked": True, "reason_code": reason_code, "reason_text": reason_text.strip()},
    )
    db.flush()
    return row


def unblock_patient(
    db: Session,
    *,
    patient_id,
    actor_id: str,
    note: Optional[str] = None,
) -> PlatformCustomerBlock:
    """Close the ACTIVE block row + audit event. Caller owns the commit."""
    row = active_block(db, patient_id)
    if row is None:
        raise CustomerBlockError("Customer is not blocked.")
    row.unblocked_at = datetime.now(timezone.utc)
    row.unblocked_by = actor_id
    row.unblock_note = note
    db.add(row)
    _event(
        db,
        event_type=UNBLOCKED_EVENT,
        patient_id=patient_id,
        actor_id=actor_id,
        after={"blocked": False, "note": note},
    )
    db.flush()
    return row
