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

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.v1.endpoints.admin_originations import require_assignment_or_override
from app.core.auth import (
    require_permission_or_admin,
    require_roles,
    user_has_permission,
    user_has_permission_or_admin,
    user_is_admin,
)
from app.core.config import settings as app_settings
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.event import PlatformEvent
from app.models.platform.loan import PlatformLoan, PlatformLoanPayment
from app.models.user import User
from app.services import decision_reasons, loan_ledger, loan_lifecycle, loan_servicing
from app.services.flow_orchestrator import (
    _TERMINAL_STATUSES,
    InvalidStateTransition,
    mark_cancelled,
)

logger = structlog.get_logger(__name__)

router = APIRouter()

# Actions that require a second approver (maker-checker). ``activate`` (books the
# loan at activation — activation-rework Wave 2) is scoped to an APPLICATION, not a
# loan (no loan exists yet); the other two target a loan.
_MAKER_CHECKER_ACTIONS = ("charge_off", "disburse", "activate")


def _actor_id(user) -> str:
    return str(getattr(user, "id", "") or "unknown")


def backdate_days(received_at: datetime, now: datetime) -> int:
    """How many days in the past ``received_at`` is (UTC date arithmetic;
    naive datetimes are treated as UTC). <= 0 means today/future. Pure —
    pinned by the WS-F settings-suite tests."""
    ra = received_at if received_at.tzinfo is not None else received_at.replace(tzinfo=timezone.utc)
    return (now.astimezone(timezone.utc).date() - ra.astimezone(timezone.utc).date()).days


def check_backdate_allowed(
    received_at: Optional[datetime], now: datetime, *, window_days: int,
    has_backdate_grant: bool,
) -> Optional[tuple[int, str]]:
    """WS-F transactions/backdate policy — pure. Returns None when allowed, or
    (http_status, detail).

    * today/future received_at → allowed for anyone (not a backdate);
    * past received_at → requires the transactions/backdate grant (or admin);
    * older than ``window_days`` → 422 for EVERYONE (admins included): postings
      that old need a data-migration path, not a servicing write.
    """
    if received_at is None:
        return None
    days_back = backdate_days(received_at, now)
    if days_back <= 0:
        return None
    if days_back > window_days:
        return (
            422,
            f"received_at is {days_back} days in the past — beyond the "
            f"{window_days}-day backdate window",
        )
    if not has_backdate_grant:
        return (
            403,
            "backdating a transaction requires the transactions/backdate permission",
        )
    return None


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
    # ``outcome`` is the internal credit-decision OUTCOME token (the scoring
    # engine's vocabulary, kept for the risk-score override machinery and the
    # stored decision JSON). ``declined`` here maps to the ``rejected`` STATUS.
    outcome: Literal["approved", "declined", "refer"]
    # For outcome="declined" these are REQUIRED and must be active REJECT codes
    # from the platform_decision_reasons directory (WS-E): staff rejections carry
    # only vetted, defensible reasons captured against the application.
    reason_codes: list[str] = []
    note: Optional[str] = None
    override: bool = False  # acknowledge overriding an existing automated decision

    @model_validator(mode="after")
    def _rejection_requires_reason_codes(self) -> "DecisionBody":
        # Schema-level enforcement (defense-in-depth alongside the endpoint's
        # directory-validation 422): a rejection can never even PARSE without at
        # least one reason code — a rejection must record its principal reasons.
        if self.outcome == "declined" and not self.reason_codes:
            raise ValueError(
                "a rejection requires at least one reason_code from the "
                "reject reason directory"
            )
        return self


@router.post("/applications/{application_id}/decision")
def decide(
    application_id: UUID,
    body: DecisionBody,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    """Record a staff decision. approve → books a loan; reject → captures reasons."""
    app = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.id == application_id)
        .first()
    )
    if app is None:
        raise HTTPException(status_code=404, detail="Application not found")
    # WS-E assignment gate: working a file's decision requires being its
    # assignee or an override (the admin role — implicitly allowed, keeping
    # this admin-only endpoint's existing behavior — or the explicit
    # applications/assignment_override permission). Override use is audited.
    assignment_override_used = require_assignment_or_override(app, user)
    if app.status in ("approved", "rejected") and not body.override:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Application already {app.status}; pass override=true to re-decide.",
        )

    # WS-F granular permissions: overriding an existing decision requires the
    # decisions/override grant (or admin). Router currently admits admins only,
    # so this is defense-in-depth for when the route widens to staff.
    if body.override and not (
        user_is_admin(user) or user_has_permission(user, "decisions", "override")
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="overriding an existing decision requires the decisions/override permission",
        )

    # Orphan-loan guard: overriding an APPROVED app away from "approved" would leave
    # its already-booked loan live (a rejected/referred application with a fundable
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

    # WS-E: a staff REJECTION is a credit decision that must carry at least one
    # vetted principal reason from the reject directory (a rejection records "the
    # principal reasons for the decision" — a rejection with no defensible coded
    # reason is not permitted).
    if body.outcome == "declined":
        if not body.reason_codes:
            raise HTTPException(
                status_code=422,
                detail="A rejection requires at least one reason_code from the "
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
    # Map the internal decision OUTCOME token onto the persisted STATUS:
    # approved->approved, declined->rejected (Dave's rename), refer->under_review.
    new_status = {
        "approved": "approved",
        "declined": "rejected",
        "refer": "under_review",
    }[body.outcome]
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
    # WS-E waiting-time anchor: stamp the transition like the orchestrator does
    # so the pipeline's time-in-status column resets on a staff decision too.
    app.status_updated_at = datetime.now(timezone.utc)
    # WS-I: a staff decision resolves any pending vendor reprocessing request,
    # so the admin queue's reprocessing lane only shows unhandled requests.
    app.vendor_reprocessing_requested = False

    # RISK-SCORE PERSISTENCE (migration 073). Records the UNDERWRITER half of the
    # dual decision against the score the underwriter actually saw (the current
    # row's score/checks/rules are carried forward verbatim). ⚠️ DECISION-PATH:
    # runs after the outcome is decided and written, feeds nothing back, and is
    # guarded so a recording failure can never fail a decision.
    try:
        from app.services import risk_scores

        risk_scores.record_underwriter_decision(
            db,
            app,
            outcome=body.outcome,
            actor=actor,
            reason_codes=body.reason_codes,
            note=body.note,
            decided_at=app.decision_at,
            is_override=bool(body.override),
        )
    except Exception as exc:  # noqa: BLE001 — decision integrity over recording
        logger.warning(
            "risk_score_underwriter_recording_failed",
            application_id=str(app.id),
            error=str(exc),
        )

    loan_id = None
    if body.outcome == "approved":
        if app_settings.ACTIVATION_BOOKS_LOAN:
            # Activation-rework (Wave 2, flag ON): a manual approval does NOT book
            # a loan. The file rests at ``approved``; offers are issued and the loan
            # is booked later at ACTIVATION (POST /applications/{id}/activate →
            # maker-checker → loan_lifecycle.activate_loan). loan_id stays None.
            pass
        else:
            # Flag OFF (default): unchanged. Delegate to the hardened lifecycle
            # entry point: idempotent (returns the existing loan if already
            # booked), race-safe, and emits the `loan_booked` money event — none
            # of which the inline create_loan_from_application call did.
            db.flush()
            try:
                loan = loan_lifecycle.book_loan(db, app)
                loan_id = str(loan.id)
            except Exception as exc:  # pricing/validation failure booking the loan
                db.rollback()
                raise HTTPException(status_code=409, detail=f"Could not book loan: {exc}")
    elif body.outcome == "declined":
        # SEAM (Dave 2026-07-22): a staff rejection NO LONGER auto-sends a
        # US-style "adverse-action notice" — that is a US regulatory concept, not
        # applicable in Canada. The principal reasons are still captured (on
        # ``app.decision`` above and via ``record_underwriter_decision``). What
        # notice (if any) a rejected Canadian applicant receives is an OPEN
        # COUNSEL/DAVE question; wire the replacement here once that is decided.
        pass

    _audit(
        db, event_type="admin_application_decision", actor=actor, application_id=app.id,
        payload={"outcome": body.outcome, "reasons": body.reason_codes, "note": body.note,
                 "override": body.override, "loan_id": loan_id,
                 "assignment_override_used": assignment_override_used},
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

    Distinct from a rejection: cancellation is not a credit decision (Turnkey
    parity, 02__WP_Underwriting.md §4) and carries its own CANCEL reason list.
    Requires an active CANCEL reason code from the directory; moves the application to the
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
    # WS-E assignment gate (see decide) — cancel is a decision action too.
    assignment_override_used = require_assignment_or_override(app, user)
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
            "assignment_override_used": assignment_override_used,
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
    """Assign an application to a staff user (default: self-assign). Audited.

    WS-E: plain staff may PICK UP work — self-assign an unassigned file or one
    already theirs. REASSIGNING (assigning someone else, or taking a file off
    another user) requires an override: the admin role or the explicit
    ``applications``/``assignment_override`` permission."""
    from app.services import originations_admin

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

    is_reassignment = (str(target_id) != actor) or (
        app.assigned_to_user_id is not None and str(app.assigned_to_user_id) != actor
    )
    if is_reassignment:
        user_roles = {ur.role.name for ur in getattr(user, "roles", [])}
        user_perms = {
            (rp.permission.resource, rp.permission.action)
            for ur in getattr(user, "roles", [])
            for rp in getattr(ur.role, "permissions", [])
        }
        if not originations_admin.has_assignment_override(user_roles, user_perms):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Reassigning requires the admin role or the "
                       "applications/assignment_override permission; staff may only "
                       "self-assign unassigned files.",
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
    """Clear an application's assignment (audited). Staff may drop their OWN
    assignment; clearing another user's requires an override (WS-E)."""
    from app.services import originations_admin

    app = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.id == application_id)
        .first()
    )
    if app is None:
        raise HTTPException(status_code=404, detail="Application not found")

    actor = _actor_id(user)
    if app.assigned_to_user_id is not None and str(app.assigned_to_user_id) != actor:
        user_roles = {ur.role.name for ur in getattr(user, "roles", [])}
        user_perms = {
            (rp.permission.resource, rp.permission.action)
            for ur in getattr(user, "roles", [])
            for rp in getattr(ur.role, "permissions", [])
        }
        if not originations_admin.has_assignment_override(user_roles, user_perms):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Clearing another user's assignment requires the admin role "
                       "or the applications/assignment_override permission.",
            )

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
    # WS-I reconciliation dimension: cash / check / eft / credit_card /
    # adjustment. Optional — when omitted it is derived from ``method``.
    payment_type: Optional[Literal["cash", "check", "eft", "credit_card", "adjustment"]] = None
    # WS-I permission-bounded BACKDATING: an ``effective_date`` earlier than
    # the received date requires the ``ledger.backdate`` permission (admin
    # implicitly allowed), is bounded to loan_ledger.BACKDATE_WINDOW_DAYS days
    # back, and makes ``note`` MANDATORY. The processing date stays the
    # received date, so the backdate is permanently visible in the dual-date
    # ledger row and the audit event.
    effective_date: Optional[date] = None
    # WS-F repayment modes (Turnkey "Submit repayment" — Dave's spec):
    #   regular — accrued interest → principal → fees (default)
    #   add_on  — pays the non-accruing add-on bucket only (≤ add-on balance)
    #   special — 100% principal (permission-gated: admin role only)
    #   payoff  — must equal the server-computed payoff for the effective date
    #             (GET /loans/{id}/payoff-quote first; server computes, client
    #             confirms) and closes the loan
    repayment_mode: Literal["regular", "add_on", "special", "payoff"] = "regular"


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
    """Manually post a payment/adjustment to a loan (audited). WS-F: the
    ``repayment_mode`` selects the Turnkey allocation semantics; mode-rule
    violations (amount caps, payoff exact-match) return 422 with the reason."""
    loan = _get_loan(db, loan_id)
    if body.amount_cents <= 0:
        raise HTTPException(status_code=422, detail="amount_cents must be positive")
    # WS-F granular permissions: a past-dated received_at is a BACKDATED posting —
    # requires the transactions/backdate grant (or admin) and is bounded by the
    # configured window even for admins.
    violation = check_backdate_allowed(
        body.received_at,
        datetime.now(timezone.utc),
        window_days=app_settings.TRANSACTION_BACKDATE_WINDOW_DAYS,
        has_backdate_grant=(
            user_is_admin(user) or user_has_permission(user, "transactions", "backdate")
        ),
    )
    if violation is not None:
        raise HTTPException(status_code=violation[0], detail=violation[1])
    # SPECIAL is permission-gated beyond the router's admin gate (Dave: 100%
    # principal, bypassing interest — a write-down lever). Explicit role check
    # so the gate survives if this endpoint is ever widened to staff.
    if body.repayment_mode == "special":
        user_roles = {role.role.name for role in getattr(user, "roles", [])}
        if "admin" not in user_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="special payments require the admin role",
            )
    # WS-I permissioned BACKDATING: an effective_date earlier than the received
    # date needs the ledger.backdate permission (admin implicitly allowed).
    # The bounded window (BACKDATE_WINDOW_DAYS) + mandatory comment are
    # enforced again inside record_payment (defense-in-depth); forward-dating
    # is always rejected there. Same-day effective_date is not a backdate.
    received_at = body.received_at or datetime.now(timezone.utc)
    if body.effective_date is not None and body.effective_date != received_at.date():
        if not user_has_permission_or_admin(user, "ledger", "backdate"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="backdating a transaction requires the ledger.backdate permission",
            )
        if not (body.note or "").strip():
            raise HTTPException(
                status_code=422,
                detail="a note (comment) is required for every backdated transaction",
            )
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
    try:
        payment = loan_servicing.record_payment(
            db, loan, body.amount_cents, received_at,
            body.method, body.external_ref,
            created_by=_actor_id(user), comment=body.note,
            repayment_mode=body.repayment_mode,
            effective_date=body.effective_date,
            payment_type=body.payment_type,
        )
    except ValueError as exc:
        # Mode-rule violation (add-on/special amount caps, payoff exact-match)
        # or a backdate-window violation. Nothing was committed; the message
        # carries the expected figure so the client can re-confirm (e.g. a
        # stale payoff quote).
        raise HTTPException(status_code=422, detail=str(exc))
    _audit(db, event_type="admin_loan_payment", actor=_actor_id(user),
           payload={"loan_id": str(loan_id), "amount_cents": body.amount_cents,
                    "method": body.method, "repayment_mode": body.repayment_mode,
                    "payment_type": body.payment_type,
                    "effective_date": (
                        body.effective_date.isoformat() if body.effective_date else None
                    ),
                    "backdated": bool(
                        body.effective_date
                        and body.effective_date != received_at.date()
                    ),
                    "note": body.note})
    db.commit()
    return {"payment_id": str(payment.id), "loan_status": loan.status,
            "principal_balance_cents": loan.principal_balance_cents,
            "repayment_mode": body.repayment_mode}


# Staff payout calculator (WS-I): a quote may be FORWARD-DATED at most this
# many days (Turnkey parity — payoff quote for any date up to 30 days out).
PAYOFF_QUOTE_MAX_FORWARD_DAYS = 30


@router.get("/loans/{loan_id}/payoff-quote")
def payoff_quote(
    loan_id: UUID,
    as_of: Optional[date] = None,
    db: Session = Depends(get_db),
    _user=Depends(require_roles("admin", "staff")),
):
    """Staff payout calculator — payoff quote from the ACTUALS engine (WS-A):
    100% outstanding principal + daily simple interest accrued to ``as_of`` +
    fees due + add-on balance, and 0% future interest. The four buckets sum
    exactly to ``payoff_cents``.

    FORWARD-DATED (WS-I): ``as_of`` may be any date up to
    ``PAYOFF_QUOTE_MAX_FORWARD_DAYS`` (30) days in the future — the per-diem
    projection simply accrues interest to that date (422 beyond the window).
    Past dates remain allowed as a historical read.

    THE RULE (Turnkey parity, documented + pinned by tests): requesting a
    payoff quote is a PURE READ — it does NOT suspend, park, or alter any
    scheduled payment. Auto-collection and the amortization plan run
    unchanged unless/until the payoff payment is actually recorded
    (``repayment_mode='payoff'``). The response carries
    ``scheduled_payments_suspended: false`` to make that contract explicit.
    """
    today = date.today()
    as_of = as_of or today
    if (as_of - today).days > PAYOFF_QUOTE_MAX_FORWARD_DAYS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"as_of may be at most {PAYOFF_QUOTE_MAX_FORWARD_DAYS} days in "
                f"the future (got {as_of.isoformat()})"
            ),
        )
    loan = _get_loan(db, loan_id)
    quote = loan_servicing.compute_payoff(db, loan, as_of)

    # Itemized fee detail: the immutable fee-charge rows (NSF fees etc.), with
    # the aggregate fees-due/add-on balances they roll into. Fee PAYMENTS
    # reduce the aggregate buckets, so per-row "remaining" is not a ledger
    # concept — the charge rows + the two aggregates are the honest itemization.
    fee_rows = [
        {
            "reference": t.reference,
            "effective_date": t.effective_date.isoformat(),
            "fees_cents": t.fees_cents or 0,
            "add_on_cents": t.add_on_cents or 0,
            "comment": t.comment,
        }
        for t in loan_ledger.sorted_transactions(loan)
        if t.txn_type == "fee"
    ]

    # Per-diem transparency: the daily simple-interest accrual the projection
    # uses (annual rate on the CURRENT outstanding principal, actual/365).
    per_diem_cents = round(
        quote.principal_cents * (loan.annual_rate_bps / 10_000.0) / 365.0
    )

    return {
        "as_of": quote.as_of.isoformat(),
        "quote_date": today.isoformat(),
        "days_forward": max(0, (as_of - today).days),
        "principal_cents": quote.principal_cents,
        "accrued_interest_cents": quote.accrued_interest_cents,
        "fees_due_cents": quote.fees_due_cents,
        "add_on_balance_cents": quote.add_on_balance_cents,
        "payoff_cents": quote.payoff_cents,
        "per_diem_interest_cents": per_diem_cents,
        "fee_detail": fee_rows,
        # The contract: a payoff INQUIRY never suspends scheduled payments.
        "scheduled_payments_suspended": False,
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
                       user=Depends(require_permission_or_admin("loan", "write_off"))):
    """Request a charge-off (write-off) — a SECOND admin must approve before it executes.

    Dave, 2026-07-21: write-off must be permission-gated *specifically* rather
    than riding on the ``admin`` role. The gate is the granular ``loan/write_off``
    grant (WS-F granular perms + the per-user ``user_permissions`` table from
    #203), with ``admin`` implicitly allowed as everywhere else — so every user
    who could initiate a write-off before still can, and a non-admin can now be
    granted it without full admin.

    MAKER-CHECKER IS UNCHANGED: this only files a *request*. The second approver
    on ``POST /admin/pending-actions/{id}/approve`` is still ``admin``-only and
    is still the control that actually moves money — the permission decides only
    who may INITIATE.
    """
    return _request_action(db, action="charge_off", loan_id=loan_id, user=user, body=body)


@router.post("/loans/{loan_id}/disburse")
def request_disburse(loan_id: UUID, body: ActionRequestBody, db: Session = Depends(get_db),
                     user=Depends(require_roles("admin"))):
    """Request disbursement — a SECOND admin must approve before it executes."""
    return _request_action(db, action="disburse", loan_id=loan_id, user=user, body=body)


@router.post("/applications/{application_id}/activate")
def request_activate(application_id: UUID, body: ActionRequestBody, db: Session = Depends(get_db),
                     user=Depends(require_roles("admin"))):
    """Request loan ACTIVATION (activation-rework Wave 2) — a SECOND admin approves.

    Scoped to the APPLICATION: no loan exists yet — the approval books it at
    activation via ``loan_lifecycle.activate_loan`` off the signed application
    agreement. Maker-checker is unchanged: this only files a request; the
    different-admin approver on ``/pending-actions/{id}/approve`` is what actually
    creates the loan. The signed-agreement precondition is enforced at approve
    time (and again inside ``activate_loan``)."""
    app = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.id == application_id)
        .first()
    )
    if app is None:
        raise HTTPException(status_code=404, detail="Application not found")
    actor = _actor_id(user)
    ev = _audit(
        db, event_type="admin_action_requested", actor=actor, application_id=application_id,
        payload={"action": "activate", "application_id": str(application_id),
                 "requested_by": actor, "reason_code": body.reason_code, "note": body.note},
    )
    db.commit()
    return {"pending_action_id": ev.id, "action": "activate",
            "application_id": str(application_id), "status": "pending", "requested_by": actor}


class PendingAction(BaseModel):
    id: int
    action: str
    # A pending action targets EITHER a loan (charge_off / disburse) or an
    # application (activate). Exactly one is populated per row.
    loan_id: Optional[str] = None
    application_id: Optional[str] = None
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
            id=ev_id, action=payload["action"],
            loan_id=payload.get("loan_id"), application_id=payload.get("application_id"),
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

    # Validate + precondition-check BEFORE recording the approval, so a rejected
    # precondition can't leave an orphan approval event.
    if action not in _MAKER_CHECKER_ACTIONS:
        raise HTTPException(status_code=422, detail=f"Unknown action '{action}'")

    application = None
    loan = None
    if action == "activate":
        # Activation-rework Wave 2: the pending action targets the APPLICATION —
        # the approval books the loan at activation. Precondition: the application
        # agreement is signed (checked again inside activate_loan).
        application = (
            db.query(PlatformCreditApplication)
            .filter(PlatformCreditApplication.id == UUID(payload["application_id"]))
            .first()
        )
        if application is None:
            raise HTTPException(status_code=404, detail="Application not found")
        if application.agreement_status != "signed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Application agreement is not signed (agreement_status="
                       f"{application.agreement_status!r}); cannot activate.",
            )
    else:
        loan = _get_loan(db, UUID(payload["loan_id"]))
        if action == "disburse":
            # The admin "release disbursement" action funds an already-SIGNED loan
            # that hasn't started disbursing — it does not bypass e-sign. (Audit
            # H1/L4.)
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
    approval_payload = {"request_event_id": request_event_id, "action": action,
                        "approved_by": actor}
    if action == "activate":
        approval_payload["application_id"] = payload["application_id"]
    else:
        approval_payload["loan_id"] = payload["loan_id"]
    _audit(db, event_type="admin_action_approved", actor=actor, payload=approval_payload)
    db.flush()

    try:
        if action == "charge_off":
            loan = loan_lifecycle.charge_off_loan(
                db, loan, reason_code=payload.get("reason_code"), actor=actor)
            result = {"loan_status": loan.status}
        elif action == "disburse":
            loan = loan_lifecycle.initiate_disbursement(db, loan)
            result = {"disbursement_status": loan.disbursement_status,
                      "disbursement_ref": loan.disbursement_ref}
        else:  # activate — books + activates the loan at activation
            loan = loan_lifecycle.activate_loan(db, application, actor=actor)
            result = {"loan_id": str(loan.id), "loan_status": loan.status,
                      "booked_at_activation": loan.booked_at_activation}
    except ValueError as exc:  # business-rule rejection (e.g. unsigned/paid loan)
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
