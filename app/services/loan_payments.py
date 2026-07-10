"""Borrower-initiated loan payments — "Pay Now" (collection via Zumrails).

The disbursement side (push money to the borrower) lives in ``loan_lifecycle``.
This is the *collection* side (pull a repayment from the borrower):

    borrower taps "Pay Now"
        │  initiate_payment()                → Zumrails AddToAccount (debit)
        ▼                                       emits ``loan_payment_initiated``
    collection in flight (Zumrails txn id)
        │  on_collection_complete()          ← Zumrails "completed" webhook
        ▼                                       record_payment() applies the cash
    schedule updated, balance reduced, delinquency cured if caught up

DESIGN
------
* **Resolve-by-event, not by column.** A loan has many collections over its
  life, so there's no single ``collection_ref`` column. Each initiation emits a
  ``loan_payment_initiated`` event carrying the Zumrails txn id + loan id +
  amount; the webhook looks the loan up from that event. No schema change.
* **Idempotent settlement.** ``record_payment`` dedupes on
  ``(loan_id, external_ref=txn_id)`` (migration 033's unique index), so an
  at-least-once Zumrails webhook can never double-apply a payment.
* **Credentials sourced at the wiring layer** (reuses
  ``loan_lifecycle._build_zumrails_adapter``); the adapter never reads settings.
* **No business/pricing logic.** Allocation + balance math is owned by
  ``loan_servicing.record_payment``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.event import PlatformEvent
from app.models.platform.loan import PlatformLoan, PlatformLoanScheduleItem
from app.models.platform.patient import PlatformPatient
from app.services import loan_ledger, loan_servicing
from app.services.payments.zumrails_adapter import TransactionStatus

logger = get_logger(__name__)

INITIATED_EVENT = "loan_payment_initiated"
COMPLETED_EVENT = "loan_payment_completed"
FAILED_EVENT = "loan_payment_failed"

# Installment statuses that still owe money for the borrower's outstanding
# figure. ``suspended`` (staff schedule surgery) is INCLUDED here on purpose:
# suspension parks the automated jobs, it does not forgive the debt — a
# borrower may still choose to pay it.
_OPEN_ITEM_STATUSES = ("scheduled", "partial", "late", "suspended")

# Repayment modes a BORROWER may initiate (WS-F). ``special`` is staff-only.
BORROWER_MODES = ("regular", "add_on", "payoff")


class PaymentError(Exception):
    """Base for borrower-payment failures."""


class PaymentValidationError(PaymentError):
    """Bad request — inactive loan, bad amount, no funding profile (→ 4xx)."""


class PaymentProviderUnavailable(PaymentError):
    """Zumrails not configured / disabled (→ 503)."""


def outstanding_cents(db: Session, loan: PlatformLoan) -> int:
    """Total still owed across all unpaid/partial/late installments."""
    rows = (
        db.query(PlatformLoanScheduleItem)
        .filter(PlatformLoanScheduleItem.loan_id == loan.id)
        .filter(PlatformLoanScheduleItem.status.in_(_OPEN_ITEM_STATUSES))
        .all()
    )
    return sum(max(0, (i.total_cents or 0) - (i.paid_cents or 0)) for i in rows)


def _payer_id_for_loan(db: Session, loan: PlatformLoan) -> Optional[str]:
    """The borrower's Zumrails user id (same id used as the disbursement payee)."""
    application = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.id == loan.application_id)
        .first()
    )
    if application is None:
        return None
    patient = (
        db.query(PlatformPatient)
        .filter(PlatformPatient.id == application.patient_id)
        .first()
    )
    return getattr(patient, "zumrails_recipient_id", None)


def payment_options(db: Session, loan: PlatformLoan, *, as_of=None) -> dict:
    """The borrower's Pay-Now options (WS-F).

    ``regular`` is always offered (editable amount, capped at what's owed);
    ``payoff`` is offered with the SERVER-QUOTED amount (non-editable — the
    server computes, the client confirms); ``add_on`` is exposed ONLY when the
    add-on balance is positive (there is otherwise nothing for it to pay).
    """
    from datetime import date as _date

    as_of = as_of or _date.today()
    balances = loan_ledger.loan_balances(loan, as_of=as_of)
    modes = ["regular"]
    if balances.add_on_balance_cents > 0:
        modes.append("add_on")
    modes.append("payoff")
    return {
        "as_of": as_of.isoformat(),
        "modes": modes,
        "outstanding_cents": outstanding_cents(db, loan),
        "add_on_balance_cents": balances.add_on_balance_cents,
        "payoff_cents": balances.payoff_cents,
    }


def initiate_payment(
    db: Session,
    loan: PlatformLoan,
    amount_cents: Optional[int],
    *,
    mode: str = "regular",
    payer_id: Optional[str] = None,
    zumrails=None,
) -> dict:
    """Start a borrower payment (Zumrails collection). Returns
    ``{"transaction_id", "status", "amount_cents", "repayment_mode"}``.

    WS-F modes (borrower-facing subset — ``special`` is staff-only):

      * ``regular`` — editable amount, capped at the schedule's outstanding.
      * ``add_on``  — only when the add-on balance is positive; capped at it.
      * ``payoff``  — SERVER-QUOTED amount (as of today). A client-supplied
        amount must match the quote exactly (stale quote → error carrying the
        current figure so the UI re-confirms); omitted → the quote is charged.

    Validates the loan is payable, fires the Zumrails debit, and emits
    ``loan_payment_initiated`` (carrying the mode) so the webhook can settle
    it. If Zumrails returns COMPLETED synchronously, the payment is recorded
    immediately. Raises ``PaymentValidationError`` / ``PaymentProviderUnavailable``.
    """
    from datetime import date as _date

    if loan.status not in ("active", "delinquent"):
        raise PaymentValidationError(
            f"loan is not in a payable state (status={loan.status})"
        )
    if mode not in BORROWER_MODES:
        raise PaymentValidationError(
            f"repayment mode {mode!r} is not available (choose one of {BORROWER_MODES})"
        )

    if mode == "payoff":
        balances = loan_ledger.loan_balances(loan, as_of=_date.today())
        payoff = balances.payoff_cents
        if payoff <= 0:
            raise PaymentValidationError("loan has no outstanding balance")
        # Non-editable: the server computes; a supplied amount only confirms.
        if amount_cents is not None and amount_cents != payoff:
            raise PaymentValidationError(
                f"payoff amount is server-computed: expected {payoff}, "
                f"got {amount_cents} (re-quote and confirm)"
            )
        amount_cents = payoff
    elif mode == "add_on":
        balances = loan_ledger.loan_balances(loan, as_of=_date.today())
        add_on = balances.add_on_balance_cents
        if add_on <= 0:
            raise PaymentValidationError("loan has no add-on balance to pay")
        if amount_cents is None or amount_cents <= 0:
            raise PaymentValidationError("amount_cents must be positive")
        if amount_cents > add_on:
            raise PaymentValidationError(
                f"amount_cents ({amount_cents}) exceeds the add-on balance ({add_on})"
            )
    else:  # regular
        if amount_cents is None or amount_cents <= 0:
            raise PaymentValidationError("amount_cents must be positive")
        owed = outstanding_cents(db, loan)
        if owed <= 0:
            raise PaymentValidationError("loan has no outstanding balance")
        if amount_cents > owed:
            raise PaymentValidationError(
                f"amount_cents ({amount_cents}) exceeds outstanding balance ({owed})"
            )

    if payer_id is None:
        payer_id = _payer_id_for_loan(db, loan)
    if not payer_id:
        raise PaymentValidationError("no funding profile on file for this borrower")

    if zumrails is None:
        from app.services.loan_lifecycle import _build_zumrails_adapter

        zumrails = _build_zumrails_adapter(db)
    if zumrails is None:
        raise PaymentProviderUnavailable("zumrails provider disabled or unconfigured")

    client_txn_id = f"pay-{loan.id}-{uuid4().hex[:12]}"
    result = zumrails.create_collection(
        payer_id=payer_id,
        amount_cents=amount_cents,
        client_transaction_id=client_txn_id,
        memo=f"Loan payment {loan.id}",
    )

    _emit(
        db,
        INITIATED_EVENT,
        loan=loan,
        payload_extra={
            "transaction_id": result.transaction_id,
            "client_transaction_id": client_txn_id,
            "amount_cents": amount_cents,
            "repayment_mode": mode,
            "status": result.status.value,
        },
    )
    db.commit()

    # Some rails ack terminal synchronously — settle right away if so.
    if result.status == TransactionStatus.COMPLETED:
        on_collection_complete(db, result.transaction_id)

    logger.info(
        "loan_payment_initiated",
        loan_id=str(loan.id),
        transaction_id=result.transaction_id,
        status=result.status.value,
        repayment_mode=mode,
    )
    return {
        "transaction_id": result.transaction_id,
        "status": result.status.value,
        "amount_cents": amount_cents,
        "repayment_mode": mode,
    }


def _find_initiation(db: Session, transaction_id: str) -> Optional[dict]:
    """Return the initiating event's payload for a Zumrails collection txn, or None."""
    row = db.execute(
        text(
            """
            SELECT payload FROM platform_events
            WHERE event_type = :etype AND payload->>'transaction_id' = :txn
            ORDER BY id DESC LIMIT 1
            """
        ),
        {"etype": INITIATED_EVENT, "txn": transaction_id},
    ).first()
    return row[0] if row else None


def on_collection_complete(db: Session, transaction_id: str) -> bool:
    """Settle a completed collection: record the payment (idempotent) + emit
    ``loan_payment_completed``. Returns False if the txn isn't one of ours
    (orphaned). Safe to call from an at-least-once webhook.

    The initiation event's ``repayment_mode`` (WS-F) is applied at settlement.
    PAYOFF DRIFT: a payoff quoted at initiation can differ from the payoff as
    of the settlement date (per-diem interest accrued in between). The payoff
    allocator demands an exact match, so when the settled cash no longer
    equals the payoff, the payment falls back to REGULAR allocation (interest
    → principal → fees; money is never lost) and the completed event flags
    ``payoff_mismatch`` with both figures for staff true-up. If the cash still
    clears all four buckets, record_payment's zero-debt rule closes the loan
    regardless of mode.
    """
    init = _find_initiation(db, transaction_id)
    if init is None:
        return False
    loan = (
        db.query(PlatformLoan)
        .filter(PlatformLoan.id == init["loan_id"])
        .first()
    )
    if loan is None:
        return False

    amount_cents = int(init["amount_cents"])
    mode = init.get("repayment_mode") or "regular"
    received_at = datetime.now(timezone.utc)
    payoff_mismatch = None
    if mode == "payoff":
        balances = loan_ledger.loan_balances(loan, as_of=received_at.date())
        if balances.payoff_cents != amount_cents:
            payoff_mismatch = {
                "quoted_payoff_cents": amount_cents,
                "payoff_cents_at_settlement": balances.payoff_cents,
            }
            mode = "regular"

    loan_servicing.record_payment(
        db,
        loan,
        amount_cents=amount_cents,
        received_at=received_at,
        method="zumrails_collection",
        external_ref=transaction_id,  # (loan_id, external_ref) unique → replay-safe
        repayment_mode=mode,
    )
    extra = {
        "transaction_id": transaction_id,
        "amount_cents": amount_cents,
        "repayment_mode": mode,
    }
    if payoff_mismatch:
        extra["payoff_mismatch"] = payoff_mismatch
    _emit(db, COMPLETED_EVENT, loan=loan, payload_extra=extra)
    db.commit()
    logger.info("loan_payment_completed", loan_id=str(loan.id), transaction_id=transaction_id)
    return True


def on_collection_failed(db: Session, transaction_id: str) -> bool:
    """Record a failed collection as ``loan_payment_failed``. No balance change.
    Returns False if the txn isn't one of ours."""
    init = _find_initiation(db, transaction_id)
    if init is None:
        return False
    loan = db.query(PlatformLoan).filter(PlatformLoan.id == init["loan_id"]).first()
    if loan is None:
        return False
    _emit(
        db,
        FAILED_EVENT,
        loan=loan,
        payload_extra={"transaction_id": transaction_id, "amount_cents": int(init["amount_cents"])},
    )
    db.commit()
    logger.warning("loan_payment_failed", loan_id=str(loan.id), transaction_id=transaction_id)
    return True


def _emit(db: Session, event_type: str, *, loan: PlatformLoan, payload_extra: dict) -> None:
    application_id = loan.application_id
    patient_id = None
    if application_id is not None:
        app_row = (
            db.query(PlatformCreditApplication)
            .filter(PlatformCreditApplication.id == application_id)
            .first()
        )
        patient_id = app_row.patient_id if app_row else None
    payload = {
        "v": 1,
        "actor": {"type": "patient", "id": str(patient_id) if patient_id else "system"},
        "application_id": str(application_id) if application_id else None,
        "patient_id": str(patient_id) if patient_id else None,
        "loan_id": str(loan.id),
        **payload_extra,
    }
    db.add(
        PlatformEvent(
            event_type=event_type,
            actor="patient",
            patient_id=patient_id,
            application_id=application_id,
            payload=payload,
        )
    )
    db.flush()
