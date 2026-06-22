"""Lender/admin portal Phase 2 — write actions (the operating loop).

The audited operating loop on top of the existing engine:
  * application DECISION (approve → book loan; decline → adverse-action; refer)
  * loan PAYMENT (manual) + PAYOFF quote
  * MAKER-CHECKER for the irreversible/money actions (charge-off, disburse):
    a maker REQUESTS the action, a DIFFERENT admin APPROVES it, only then does it
    execute. Implemented purely on the append-only PlatformEvent log (no new
    table/migration): a request is an `admin_action_requested` event; "pending"
    = a request with no matching approved/rejected event referencing its id.

Every state change appends a PlatformEvent with the staff actor — the WORM log is
the compliance system-of-record. Decision/payment are admin-only writes;
charge-off/disburse are admin-only and gated by the second approver.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.auth import require_roles
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.event import PlatformEvent
from app.models.platform.loan import PlatformLoan, PlatformLoanPayment
from app.services import loan_servicing
from app.services.adverse_action import send_adverse_action_notice

router = APIRouter()

# Actions that require a second approver (maker-checker).
_MAKER_CHECKER_ACTIONS = ("charge_off", "disburse")


def _actor_id(user) -> str:
    return str(getattr(user, "id", "") or "unknown")


def _audit(db: Session, *, event_type: str, actor: str, payload: dict,
           application_id: Optional[UUID] = None) -> PlatformEvent:
    ev = PlatformEvent(
        event_type=event_type, actor=actor, application_id=application_id, payload=payload
    )
    db.add(ev)
    db.flush()
    return ev


# --- M2: application decision / override -----------------------------------


class DecisionBody(BaseModel):
    outcome: Literal["approved", "declined", "refer"]
    reason_codes: list[str] = []
    note: Optional[str] = None
    override: bool = False  # acknowledge overriding an existing automated decision


@router.post("/applications/{application_id}/decision")
def decide(
    application_id: UUID,
    body: DecisionBody,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    """Record a staff decision. approve → books a loan; decline → adverse action."""
    app = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.id == application_id)
        .first()
    )
    if app is None:
        raise HTTPException(status_code=404, detail="Application not found")
    if app.status in ("approved", "declined") and not body.override:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Application already {app.status}; pass override=true to re-decide.",
        )

    # Orphan-loan guard: overriding an APPROVED app away from "approved" would leave
    # its already-booked loan live (a declined/referred application with a fundable
    # loan). Block it — the loan must be charged-off/cancelled first. (Re-approving
    # is separately backstopped by the uq_platform_loans_application unique constraint.)
    if body.override and app.status == "approved" and body.outcome != "approved":
        existing_loan = (
            db.query(PlatformLoan.id)
            .filter(PlatformLoan.application_id == application_id)
            .first()
        )
        if existing_loan is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Application has a booked loan ({existing_loan[0]}); cancel or "
                    f"charge-off the loan before re-deciding."
                ),
            )

    actor = _actor_id(user)
    new_status = "under_review" if body.outcome == "refer" else body.outcome
    app.decision = {
        "outcome": body.outcome,
        "reasons": body.reason_codes,
        "note": body.note,
        "by": "admin",
        "override": body.override,
    }
    app.decision_by = actor
    app.decision_at = datetime.now(timezone.utc)
    app.status = new_status

    loan_id = None
    if body.outcome == "approved":
        db.flush()
        try:
            loan = loan_servicing.create_loan_from_application(db, app)
            loan_id = str(loan.id)
        except Exception as exc:  # e.g. a loan already booked for this application
            db.rollback()
            raise HTTPException(status_code=409, detail=f"Could not book loan: {exc}")
    elif body.outcome == "declined":
        send_adverse_action_notice(db, app, body.reason_codes)  # idempotent, never raises

    _audit(
        db, event_type="admin_application_decision", actor=actor, application_id=app.id,
        payload={"outcome": body.outcome, "reasons": body.reason_codes, "note": body.note,
                 "override": body.override, "loan_id": loan_id},
    )
    db.commit()
    return {"application_id": str(app.id), "status": app.status, "loan_id": loan_id}


# --- M3: servicing writes ---------------------------------------------------


class PaymentBody(BaseModel):
    amount_cents: int
    method: str = "manual"
    received_at: Optional[datetime] = None
    external_ref: Optional[str] = None
    note: Optional[str] = None


def _get_loan(db: Session, loan_id: UUID) -> PlatformLoan:
    loan = db.query(PlatformLoan).filter(PlatformLoan.id == loan_id).first()
    if loan is None:
        raise HTTPException(status_code=404, detail="Loan not found")
    return loan


@router.post("/loans/{loan_id}/payments")
def record_payment(
    loan_id: UUID,
    body: PaymentBody,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    """Manually post a payment/adjustment to a loan (audited)."""
    loan = _get_loan(db, loan_id)
    if body.amount_cents <= 0:
        raise HTTPException(status_code=422, detail="amount_cents must be positive")
    # Idempotency: when the caller supplies an external_ref, a retry / double-submit
    # must NOT post the payment (and reduce the balance) twice. Return the existing
    # receipt instead. Without an external_ref we can't dedupe, so the caller owns it.
    if body.external_ref:
        prior = (
            db.query(PlatformLoanPayment)
            .filter(
                PlatformLoanPayment.loan_id == loan_id,
                PlatformLoanPayment.external_ref == body.external_ref,
            )
            .first()
        )
        if prior is not None:
            return {"payment_id": str(prior.id), "loan_status": loan.status,
                    "principal_balance_cents": loan.principal_balance_cents,
                    "idempotent_replay": True}
    payment = loan_servicing.record_payment(
        db, loan, body.amount_cents, body.received_at or datetime.now(timezone.utc),
        body.method, body.external_ref,
    )
    _audit(db, event_type="admin_loan_payment", actor=_actor_id(user),
           payload={"loan_id": str(loan_id), "amount_cents": body.amount_cents,
                    "method": body.method, "note": body.note})
    db.commit()
    return {"payment_id": str(payment.id), "loan_status": loan.status,
            "principal_balance_cents": loan.principal_balance_cents}


@router.get("/loans/{loan_id}/payoff-quote")
def payoff_quote(
    loan_id: UUID,
    as_of: Optional[date] = None,
    db: Session = Depends(get_db),
    _user=Depends(require_roles("admin", "staff")),
):
    """Payoff quote (remaining principal + accrued interest) as of a date."""
    loan = _get_loan(db, loan_id)
    quote = loan_servicing.compute_payoff(db, loan, as_of or date.today())
    return {
        "as_of": quote.as_of.isoformat(),
        "principal_cents": quote.principal_cents,
        "accrued_interest_cents": quote.accrued_interest_cents,
        "payoff_cents": quote.payoff_cents,
    }


# --- M4/M5: maker-checker (charge-off, disburse) ---------------------------


class ActionRequestBody(BaseModel):
    reason_code: Optional[str] = None
    note: Optional[str] = None


def _request_action(db: Session, *, action: str, loan_id: UUID, user, body: ActionRequestBody) -> dict:
    _get_loan(db, loan_id)  # 404 if missing
    actor = _actor_id(user)
    ev = _audit(
        db, event_type="admin_action_requested", actor=actor,
        payload={"action": action, "loan_id": str(loan_id), "requested_by": actor,
                 "reason_code": body.reason_code, "note": body.note},
    )
    db.commit()
    return {"pending_action_id": ev.id, "action": action, "loan_id": str(loan_id),
            "status": "pending", "requested_by": actor}


@router.post("/loans/{loan_id}/charge-off")
def request_charge_off(loan_id: UUID, body: ActionRequestBody, db: Session = Depends(get_db),
                       user=Depends(require_roles("admin"))):
    """Request a charge-off — a SECOND admin must approve before it executes."""
    return _request_action(db, action="charge_off", loan_id=loan_id, user=user, body=body)


@router.post("/loans/{loan_id}/disburse")
def request_disburse(loan_id: UUID, body: ActionRequestBody, db: Session = Depends(get_db),
                     user=Depends(require_roles("admin"))):
    """Request disbursement — a SECOND admin must approve before it executes."""
    return _request_action(db, action="disburse", loan_id=loan_id, user=user, body=body)


class PendingAction(BaseModel):
    id: int
    action: str
    loan_id: str
    requested_by: str
    requested_at: datetime
    reason_code: Optional[str] = None
    note: Optional[str] = None


@router.get("/pending-actions", response_model=list[PendingAction])
def list_pending(db: Session = Depends(get_db), _user=Depends(require_roles("admin"))):
    """Maker-checker queue: requested actions with no approve/reject yet."""
    rows = db.execute(
        text(
            """
            SELECT e.id, e.occurred_at, e.payload
            FROM platform_events e
            WHERE e.event_type = 'admin_action_requested'
              AND NOT EXISTS (
                SELECT 1 FROM platform_events d
                WHERE d.event_type IN ('admin_action_approved', 'admin_action_rejected')
                  AND d.payload @> jsonb_build_object('request_event_id', e.id)
              )
            ORDER BY e.occurred_at DESC
            LIMIT 200
            """
        )
    ).all()
    out = []
    for ev_id, occurred_at, payload in rows:
        out.append(PendingAction(
            id=ev_id, action=payload["action"], loan_id=payload["loan_id"],
            requested_by=payload["requested_by"], requested_at=occurred_at,
            reason_code=payload.get("reason_code"), note=payload.get("note"),
        ))
    return out


def _load_pending(db: Session, request_event_id: int) -> dict:
    # FOR UPDATE locks the request-event row so two concurrent approvers serialize:
    # the first holds the lock through its commit, the second then re-reads and sees
    # the now-present terminal event → 409. Without this lock both could pass the
    # "already decided?" check under READ COMMITTED and double-execute (e.g. a double
    # disbursement). The unique index in migration 039 is the belt-and-suspenders.
    row = db.execute(
        text("SELECT payload FROM platform_events "
             "WHERE id = :id AND event_type = 'admin_action_requested' FOR UPDATE"),
        {"id": request_event_id},
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Pending action not found")
    decided = db.execute(
        text(
            """SELECT 1 FROM platform_events
               WHERE event_type IN ('admin_action_approved','admin_action_rejected')
                 AND payload @> jsonb_build_object('request_event_id', :id) LIMIT 1"""
        ),
        {"id": request_event_id},
    ).first()
    if decided is not None:
        raise HTTPException(status_code=409, detail="Pending action already decided")
    return row[0]


@router.post("/pending-actions/{request_event_id}/approve")
def approve_action(request_event_id: int, db: Session = Depends(get_db),
                   user=Depends(require_roles("admin"))):
    """Approve + EXECUTE a pending action. The approver must differ from the maker."""
    payload = _load_pending(db, request_event_id)
    actor = _actor_id(user)
    if actor == payload.get("requested_by"):
        raise HTTPException(status_code=403, detail="Maker-checker: the approver must be a different admin.")

    action = payload["action"]
    loan = _get_loan(db, UUID(payload["loan_id"]))
    if action == "charge_off":
        loan.status = "charged_off"
        result = {"loan_status": "charged_off"}
    elif action == "disburse":
        # Kick off funding. Actual Zumrails payout completes via the inbound
        # webhook (loan_lifecycle.on_disbursement_complete); here we move the
        # disbursement lifecycle forward and record the approved intent.
        loan.disbursement_status = "in_progress"
        result = {"disbursement_status": "in_progress"}
    else:
        raise HTTPException(status_code=422, detail=f"Unknown action '{action}'")

    _audit(db, event_type="admin_action_approved", actor=actor,
           payload={"request_event_id": request_event_id, "action": action,
                    "loan_id": payload["loan_id"], "approved_by": actor})
    db.commit()
    return {"request_event_id": request_event_id, "action": action, "executed": True, **result}


@router.post("/pending-actions/{request_event_id}/reject")
def reject_action(request_event_id: int, body: ActionRequestBody, db: Session = Depends(get_db),
                  user=Depends(require_roles("admin"))):
    """Reject a pending action (does not execute)."""
    payload = _load_pending(db, request_event_id)
    actor = _actor_id(user)
    _audit(db, event_type="admin_action_rejected", actor=actor,
           payload={"request_event_id": request_event_id, "action": payload["action"],
                    "loan_id": payload["loan_id"], "rejected_by": actor, "note": body.note})
    db.commit()
    return {"request_event_id": request_event_id, "status": "rejected"}
