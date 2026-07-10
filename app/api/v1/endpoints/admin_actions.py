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
from app.models.user import User
from app.services import decision_reasons, loan_lifecycle, loan_servicing
from app.services.adverse_action import send_adverse_action_notice
from app.services.flow_orchestrator import (
    _TERMINAL_STATUSES,
    InvalidStateTransition,
    mark_cancelled,
)

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
    # For outcome="declined" these are REQUIRED and must be active REJECT codes
    # from the platform_decision_reasons directory (WS-E): staff declines carry
    # only vetted, defensible reasons, and the directory's borrower_facing_text
    # flows into the adverse-action notice.
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

    # WS-E: a staff DECLINE is a credit decision that must carry at least one
    # vetted principal reason from the reject directory (the adverse-action
    # notice states "the principal reasons for the decision" — a decline with no
    # defensible coded reason is not permitted).
    if body.outcome == "declined":
        if not body.reason_codes:
            raise HTTPException(
                status_code=422,
                detail="A decline requires at least one reason_code from the "
                       "reject reason directory (GET /admin/decision-reasons?kind=reject).",
            )
        unknown = decision_reasons.invalid_reject_codes(db, body.reason_codes)
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown or inactive reject reason code(s): {unknown}. "
                       f"Use active codes from GET /admin/decision-reasons?kind=reject.",
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
            # Delegate to the hardened lifecycle entry point: idempotent (returns
            # the existing loan if already booked), race-safe, and emits the
            # `loan_booked` money event — none of which the inline
            # create_loan_from_application call did.
            loan = loan_lifecycle.book_loan(db, app)
            loan_id = str(loan.id)
        except Exception as exc:  # pricing/validation failure booking the loan
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


# --- WS-E: cancel (non-credit closure) ---------------------------------------


class CancelBody(BaseModel):
    reason_code: str
    note: Optional[str] = None


@router.post("/applications/{application_id}/cancel")
def cancel_application(
    application_id: UUID,
    body: CancelBody,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    """Cancel an application — a NON-CREDIT administrative closure (WS-E).

    Distinct from a decline: no adverse-action notice is sent (cancellation is
    not a credit decision — Turnkey parity, 02__WP_Underwriting.md §4). Requires
    an active CANCEL reason code from the directory; moves the application to the
    existing ``withdrawn`` terminal state; audited via platform_events; the
    ``application_cancelled`` event also drives the applicant notification
    through the notification outbox processor.
    """
    app = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.id == application_id)
        .first()
    )
    if app is None:
        raise HTTPException(status_code=404, detail="Application not found")
    if app.status in _TERMINAL_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Application is already terminal ({app.status}); cannot cancel.",
        )

    reason = decision_reasons.get_active_reason(db, "cancel", body.reason_code)
    if reason is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown or inactive cancel reason code {body.reason_code!r}. "
                   f"Use active codes from GET /admin/decision-reasons?kind=cancel.",
        )

    actor = _actor_id(user)
    before_status = app.status
    try:
        mark_cancelled(app)  # status transition owned by flow_orchestrator
    except InvalidStateTransition as exc:  # belt — terminal check above
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    # One event: the WORM audit row AND the notification trigger (the outbox
    # processor plans an applicant email from it). borrower_facing_text is
    # snapshotted so the notice wording is exactly what the directory said at
    # cancel time, even if the directory is edited later. No PII in the payload.
    ev = PlatformEvent(
        event_type="application_cancelled",
        actor=actor,
        patient_id=app.patient_id,
        application_id=app.id,
        payload={
            "v": 1,
            "actor": {"type": "admin", "id": actor},
            "application_id": str(app.id),
            "patient_id": str(app.patient_id),
            "before": {"status": before_status},
            "after": {"status": app.status, "reason_code": body.reason_code},
            "reason_code": body.reason_code,
            "borrower_facing_text": reason.borrower_facing_text,
            "note": body.note,
        },
    )
    db.add(ev)
    db.commit()
    return {
        "application_id": str(app.id),
        "status": app.status,
        "reason_code": body.reason_code,
    }


# --- WS-E: assignment (underwriting queue) -----------------------------------


class AssignBody(BaseModel):
    user_id: Optional[UUID] = None  # omit to self-assign


@router.post("/applications/{application_id}/assign")
def assign_application(
    application_id: UUID,
    body: AssignBody,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin", "staff")),
):
    """Assign an application to a staff user (default: self-assign). Audited."""
    app = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.id == application_id)
        .first()
    )
    if app is None:
        raise HTTPException(status_code=404, detail="Application not found")

    actor = _actor_id(user)
    if body.user_id is not None:
        target_id = body.user_id
    else:
        try:
            target_id = UUID(actor)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="Cannot self-assign without a resolvable user id; pass user_id.",
            )
    target = (
        db.query(User)
        .filter(User.id == target_id, User.is_active.is_(True))
        .first()
    )
    if target is None:
        raise HTTPException(status_code=422, detail="Assignee user not found or inactive.")

    previous = str(app.assigned_to_user_id) if app.assigned_to_user_id else None
    app.assigned_to_user_id = target_id
    app.assigned_at = datetime.now(timezone.utc)
    _audit(
        db, event_type="admin_application_assigned", actor=actor, application_id=app.id,
        payload={"assigned_to": str(target_id), "previously_assigned_to": previous},
    )
    db.commit()
    return {
        "application_id": str(app.id),
        "assigned_to_user_id": str(target_id),
        "assigned_at": app.assigned_at.isoformat(),
    }


@router.post("/applications/{application_id}/unassign")
def unassign_application(
    application_id: UUID,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin", "staff")),
):
    """Clear an application's assignment (audited)."""
    app = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.id == application_id)
        .first()
    )
    if app is None:
        raise HTTPException(status_code=404, detail="Application not found")

    previous = str(app.assigned_to_user_id) if app.assigned_to_user_id else None
    app.assigned_to_user_id = None
    app.assigned_at = None
    _audit(
        db, event_type="admin_application_unassigned", actor=_actor_id(user),
        application_id=app.id, payload={"previously_assigned_to": previous},
    )
    db.commit()
    return {"application_id": str(app.id), "assigned_to_user_id": None}


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

    # Validate + precondition-check BEFORE recording the approval, so a rejected
    # precondition can't leave an orphan approval event.
    if action not in ("charge_off", "disburse"):
        raise HTTPException(status_code=422, detail=f"Unknown action '{action}'")
    if action == "disburse":
        # The admin "release disbursement" action funds an already-SIGNED loan that
        # hasn't started disbursing — it does not bypass e-sign. (Audit H1/L4.)
        if loan.agreement_status != "signed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Loan agreement is not signed (agreement_status="
                       f"{loan.agreement_status!r}); cannot disburse.",
            )
        if loan.disbursement_status != "not_started":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Disbursement already {loan.disbursement_status!r}.",
            )

    # Record the approval FIRST (within the request-row lock), THEN execute via the
    # hardened lifecycle entry point. The entry point commits — which commits this
    # flushed approval event too, so a concurrent approver (blocked on the lock)
    # re-reads, sees the terminal event, and 409s. Migration 039's unique index
    # backstops the approval event itself. No state column is written inline here.
    _audit(db, event_type="admin_action_approved", actor=actor,
           payload={"request_event_id": request_event_id, "action": action,
                    "loan_id": payload["loan_id"], "approved_by": actor})
    db.flush()

    try:
        if action == "charge_off":
            loan = loan_lifecycle.charge_off_loan(
                db, loan, reason_code=payload.get("reason_code"), actor=actor)
            result = {"loan_status": loan.status}
        else:  # disburse
            loan = loan_lifecycle.initiate_disbursement(db, loan)
            result = {"disbursement_status": loan.disbursement_status,
                      "disbursement_ref": loan.disbursement_ref}
    except ValueError as exc:  # business-rule rejection (e.g. charging off a paid loan)
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    db.commit()  # belt — the entry point already committed; a no-op flushes nothing new
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
