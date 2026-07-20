"""Scheduled-transaction surgery (WS-F) — Dave: "a very, very important section".

Turnkey's Scheduled-transactions tab (03__WP_Servicing f0074–f0081) gives staff
three primitives on a live loan, WITHOUT ever altering the as-agreed
amortization plan (Dave's borrower-protection rule — the plan stays visible and
untouched; surgery layers on top of it):

  * SUSPEND an individual scheduled installment — the delinquency-aging and
    dunning jobs skip it (that is the whole point); the money is still owed.
  * UNSUSPEND — restore it (``partial`` if it carries cash, else ``scheduled``;
    the nightly aging pass re-derives ``late`` if it is overdue).
  * ADD a custom scheduled transaction — a one-off future payment instruction
    (date / amount / repayment mode) in ``platform_loan_custom_transactions``.
    Cancelling one flips its status (never a hard delete).

EVERY action requires a MANDATORY comment (Dave) and emits a ``platform_events``
row with the acting staff id — the event log IS the change history that
Turnkey's "Transaction change history" modal shows (f0081).

Designed as clean, reusable service functions: the hardship module (WS-J,
deferments / due-date changes) composes these primitives. Callers own commits
(functions add + flush only) so surgery can participate in larger transactions.

Money is integer cents. No PII in event payloads beyond the staff actor id.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.models.platform.event import PlatformEvent
from app.models.platform.loan import (
    PlatformLoan,
    PlatformLoanCustomTransaction,
    PlatformLoanScheduleItem,
)
from app.services.interest_engine import REPAYMENT_MODES

# Event types — the audit trail / change history (queried by schedule_changes).
ITEM_SUSPENDED_EVENT = "loan_schedule_item_suspended"
ITEM_UNSUSPENDED_EVENT = "loan_schedule_item_unsuspended"
CUSTOM_TXN_ADDED_EVENT = "loan_custom_transaction_added"
CUSTOM_TXN_CANCELLED_EVENT = "loan_custom_transaction_cancelled"

_CHANGE_EVENT_TYPES = (
    ITEM_SUSPENDED_EVENT,
    ITEM_UNSUSPENDED_EVENT,
    CUSTOM_TXN_ADDED_EVENT,
    CUSTOM_TXN_CANCELLED_EVENT,
)

# Statuses an installment may be suspended FROM. paid/waived carry nothing to
# park; suspended is already parked.
_SUSPENDABLE_STATUSES = ("scheduled", "partial", "late")


class ScheduleSurgeryError(ValueError):
    """A surgery rule violation (bad state / missing comment) — maps to 4xx."""


def _require_comment(comment: Optional[str]) -> str:
    """Dave's mandate: every surgery action carries a comment. Non-negotiable."""
    cleaned = (comment or "").strip()
    if not cleaned:
        raise ScheduleSurgeryError("a comment is required for every schedule change")
    return cleaned


def _emit(
    db: Session,
    event_type: str,
    loan: PlatformLoan,
    actor: str,
    payload_extra: dict,
) -> None:
    application_id = getattr(loan, "application_id", None)
    db.add(
        PlatformEvent(
            event_type=event_type,
            actor=actor,
            application_id=application_id,
            payload={
                "v": 1,
                "actor": {"type": "staff", "id": actor},
                "loan_id": str(loan.id),
                "application_id": str(application_id) if application_id else None,
                **payload_extra,
            },
        )
    )
    db.flush()


def _get_item(
    db: Session, loan: PlatformLoan, item_id: UUID
) -> PlatformLoanScheduleItem:
    """Fetch a schedule item and verify it belongs to ``loan`` (never surface
    another loan's installment through a mismatched URL)."""
    item = (
        db.query(PlatformLoanScheduleItem)
        .filter(
            PlatformLoanScheduleItem.id == item_id,
            PlatformLoanScheduleItem.loan_id == loan.id,
        )
        .first()
    )
    if item is None:
        raise ScheduleSurgeryError("schedule item not found on this loan")
    return item


def suspend_installment(
    db: Session,
    loan: PlatformLoan,
    item_id: UUID,
    *,
    comment: str,
    actor: str,
) -> PlatformLoanScheduleItem:
    """Suspend ONE scheduled installment (status → ``suspended``).

    The delinquency-aging and dunning jobs skip suspended items (pinned by
    tests). The money is STILL OWED — suspension parks the automation, it does
    not forgive the debt. Emits ``loan_schedule_item_suspended``.
    """
    cleaned = _require_comment(comment)
    item = _get_item(db, loan, item_id)
    if item.status not in _SUSPENDABLE_STATUSES:
        raise ScheduleSurgeryError(
            f"installment {item.installment_number} cannot be suspended from "
            f"status '{item.status}' (must be one of {_SUSPENDABLE_STATUSES})"
        )
    prior_status = item.status
    item.status = "suspended"
    _emit(
        db,
        ITEM_SUSPENDED_EVENT,
        loan,
        actor,
        {
            "schedule_item_id": str(item.id),
            "installment_number": item.installment_number,
            "due_date": item.due_date.isoformat(),
            "total_cents": item.total_cents,
            "paid_cents": item.paid_cents,
            "prior_status": prior_status,
            "comment": cleaned,
        },
    )
    return item


def unsuspend_installment(
    db: Session,
    loan: PlatformLoan,
    item_id: UUID,
    *,
    comment: str,
    actor: str,
) -> PlatformLoanScheduleItem:
    """Lift a suspension: status → ``partial`` if the item carries cash, else
    ``scheduled``. If overdue, the nightly aging pass re-derives ``late`` (and
    the loan's delinquency) — deliberately NOT done here so the status flow has
    exactly one owner. Emits ``loan_schedule_item_unsuspended``.
    """
    cleaned = _require_comment(comment)
    item = _get_item(db, loan, item_id)
    if item.status != "suspended":
        raise ScheduleSurgeryError(
            f"installment {item.installment_number} is not suspended "
            f"(status '{item.status}')"
        )
    restored = "partial" if (item.paid_cents or 0) > 0 else "scheduled"
    item.status = restored
    _emit(
        db,
        ITEM_UNSUSPENDED_EVENT,
        loan,
        actor,
        {
            "schedule_item_id": str(item.id),
            "installment_number": item.installment_number,
            "due_date": item.due_date.isoformat(),
            "restored_status": restored,
            "comment": cleaned,
        },
    )
    return item


def add_custom_transaction(
    db: Session,
    loan: PlatformLoan,
    *,
    scheduled_date: date,
    amount_cents: int,
    repayment_mode: str = "regular",
    comment: str,
    actor: str,
) -> PlatformLoanCustomTransaction:
    """Add a one-off custom scheduled transaction (Turnkey "Add transaction").

    The amortization plan is untouched — this is a NEW instruction layered on
    top (e.g. pull a missed June 26 installment on July 10). Executes later via
    auto-collection (WS-G) or a staff manual payment; until then it is a
    ``scheduled`` row in ``platform_loan_custom_transactions``.
    Emits ``loan_custom_transaction_added``.
    """
    cleaned = _require_comment(comment)
    if amount_cents <= 0:
        raise ScheduleSurgeryError("amount_cents must be positive")
    if repayment_mode not in REPAYMENT_MODES:
        raise ScheduleSurgeryError(
            f"unknown repayment mode {repayment_mode!r} "
            f"(expected one of {REPAYMENT_MODES})"
        )
    row = PlatformLoanCustomTransaction(
        loan_id=loan.id,
        scheduled_date=scheduled_date,
        amount_cents=amount_cents,
        repayment_mode=repayment_mode,
        status="scheduled",
        comment=cleaned,
        created_by=actor,
    )
    db.add(row)
    db.flush()
    _emit(
        db,
        CUSTOM_TXN_ADDED_EVENT,
        loan,
        actor,
        {
            "custom_transaction_id": str(row.id),
            "scheduled_date": scheduled_date.isoformat(),
            "amount_cents": amount_cents,
            "repayment_mode": repayment_mode,
            "comment": cleaned,
        },
    )
    return row


def cancel_custom_transaction(
    db: Session,
    loan: PlatformLoan,
    custom_id: UUID,
    *,
    comment: str,
    actor: str,
) -> PlatformLoanCustomTransaction:
    """Cancel a still-scheduled custom transaction (status → ``cancelled``).

    Never a hard delete — the row and its full history stay auditable. A
    processed transaction cannot be cancelled (the money already moved; a
    correction is a ledger reversal). Emits ``loan_custom_transaction_cancelled``.
    """
    cleaned = _require_comment(comment)
    row = (
        db.query(PlatformLoanCustomTransaction)
        .filter(
            PlatformLoanCustomTransaction.id == custom_id,
            PlatformLoanCustomTransaction.loan_id == loan.id,
        )
        .first()
    )
    if row is None:
        raise ScheduleSurgeryError("custom transaction not found on this loan")
    if row.status != "scheduled":
        raise ScheduleSurgeryError(
            f"custom transaction is '{row.status}' — only a scheduled one can "
            f"be cancelled"
        )
    row.status = "cancelled"
    row.cancelled_by = actor
    row.cancelled_at = datetime.now(timezone.utc)
    _emit(
        db,
        CUSTOM_TXN_CANCELLED_EVENT,
        loan,
        actor,
        {
            "custom_transaction_id": str(row.id),
            "scheduled_date": row.scheduled_date.isoformat(),
            "amount_cents": row.amount_cents,
            "repayment_mode": row.repayment_mode,
            "comment": cleaned,
        },
    )
    return row


def schedule_changes(db: Session, loan: PlatformLoan, *, limit: int = 200) -> list[dict]:
    """The loan's schedule-surgery change history, newest first (Turnkey's
    "Transaction change history" modal) — read straight off the event log, the
    single source of audit truth."""
    stmt = text(
        """
        SELECT id, occurred_at, event_type, actor, payload
        FROM platform_events
        WHERE event_type IN :etypes
          AND payload->>'loan_id' = :loan_id
        ORDER BY id DESC
        LIMIT :limit
        """
    ).bindparams(bindparam("etypes", expanding=True))
    rows = db.execute(
        stmt,
        {
            "etypes": list(_CHANGE_EVENT_TYPES),
            "loan_id": str(loan.id),
            "limit": limit,
        },
    ).all()
    return [
        {
            "event_id": event_id,
            "occurred_at": occurred_at.isoformat() if occurred_at else None,
            "event_type": event_type,
            "actor": actor,
            "detail": payload,
        }
        for event_id, occurred_at, event_type, actor, payload in rows
    ]
