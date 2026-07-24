"""Admin loan book + servicing detail. M3 + WS-F.

Whole-book loan list (no vendor scoping) with status/DPD, a detail view
reusing the existing loan-servicing status builder, the immutable ledger view
(WS-A), and scheduled-transaction SURGERY (WS-F): suspend/unsuspend an
individual installment, add/cancel a custom scheduled transaction — every
action comment-mandatory and audited to ``platform_events``.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_roles, user_has_permission_or_admin
from app.db.base import get_db
from app.models.loan import Vendor
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.event import PlatformEvent
from app.models.platform.loan import (
    PlatformLoan,
    PlatformLoanCustomTransaction,
    PlatformLoanDelinquencySnapshot,
    PlatformLoanPayment,
    PlatformLoanScheduleItem,
    PlatformLoanStatement,
)
from app.models.platform.patient import PlatformPatient
from app.services import auto_collection
from app.services import delinquency_buckets
from app.services import loan_ledger as ledger_service
from app.services import schedule_surgery
from app.services import servicing_status as servicing_status_service
from app.services.loan_servicing import get_loan_status

router = APIRouter(dependencies=[Depends(require_roles("admin", "staff"))])


class AdminLoanRow(BaseModel):
    loan_id: UUID
    application_id: Optional[UUID] = None
    patient_name: str
    vendor_name: str
    principal_cents: int
    principal_balance_cents: int
    status: str
    disbursed_at: Optional[datetime] = None
    days_past_due: int
    # WS-H: last month-end snapshot bucket + insolvency classification.
    current_bucket: str = "current"
    insolvency_status: Optional[str] = None


def _patient_name(p) -> str:
    if p is None:
        return "—"
    return " ".join(x for x in [p.legal_first_name, p.legal_last_name] if x).strip() or "—"


def _days_past_due(db: Session, loan_id: UUID) -> int:
    row = (
        db.query(PlatformLoanScheduleItem.due_date)
        .filter(
            PlatformLoanScheduleItem.loan_id == loan_id,
            PlatformLoanScheduleItem.status.notin_(("paid", "waived")),
        )
        .order_by(PlatformLoanScheduleItem.installment_number.asc())
        .first()
    )
    if row is None or row[0] is None:
        return 0
    return max(0, (date.today() - row[0]).days)


@router.get("", response_model=list[AdminLoanRow])
def list_loans(
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Whole-book loan list, newest first. Optional status filter."""
    q = (
        db.query(PlatformLoan, PlatformPatient, Vendor.business_name)
        .outerjoin(
            PlatformCreditApplication,
            PlatformLoan.application_id == PlatformCreditApplication.id,
        )
        .outerjoin(PlatformPatient, PlatformCreditApplication.patient_id == PlatformPatient.id)
        .outerjoin(Vendor, PlatformCreditApplication.vendor_id == Vendor.id)
    )
    if status_filter:
        q = q.filter(PlatformLoan.status == status_filter)
    rows = q.order_by(PlatformLoan.created_at.desc()).limit(limit).all()
    return [
        AdminLoanRow(
            loan_id=loan.id,
            application_id=loan.application_id,
            patient_name=_patient_name(patient),
            vendor_name=vendor_name or "—",
            principal_cents=loan.principal_cents,
            principal_balance_cents=loan.principal_balance_cents,
            status=loan.status,
            disbursed_at=loan.disbursed_at,
            days_past_due=_days_past_due(db, loan.id),
            current_bucket=loan.current_bucket or "current",
            insolvency_status=loan.insolvency_status,
        )
        for loan, patient, vendor_name in rows
    ]


def _delinquency_block(
    loan: PlatformLoan,
    today: date,
    servicing: Optional["servicing_status_service.ServicingStatus"] = None,
) -> dict:
    """WS-H: live delinquency view for one loan (no extra queries — walks the
    loaded schedule).

    * ``current_bucket`` — the last MONTH-END snapshot assignment (buckets
      move only at month-end).
    * ``effective_bucket`` — what collections should treat the loan as right
      now: insolvency/written_off override immediately, and a FULL catch-up
      (no past-due unpaid installment) cures to ``current`` immediately.
    * ``days_past_due`` — the LIVE DPD. BASIS: Dave's Account-Due-As-Of
      (``servicing_status``) when available, which advances the account per
      the product's ``amount_to_move_pct`` (default 1.0 = strict full
      installment, closely preserving the prior oldest-installment DPD). Falls
      back to the oldest-unpaid-installment DPD only when no schedule anchors
      the account (pre-disbursement).
    * ``amount_past_due_cents`` — LIVE (how far behind contract, unchanged).
    """
    items = sorted(loan.schedule or [], key=lambda s: s.installment_number)
    not_owed = delinquency_buckets.NOT_OWED_STATUSES
    past_due = [
        s
        for s in items
        if s.status not in not_owed and s.paid_cents < s.total_cents and s.due_date < today
    ]
    if servicing is not None and servicing.days_past_due is not None:
        dpd = servicing.days_past_due
    else:
        dpd = max(((today - s.due_date).days for s in past_due), default=0)
    amount_past_due = sum(s.total_cents - s.paid_cents for s in past_due)
    return {
        "current_bucket": loan.current_bucket,
        "effective_bucket": delinquency_buckets.effective_bucket(
            loan.status, loan.insolvency_status, loan.current_bucket, bool(past_due)
        ),
        "days_past_due": dpd,
        "amount_past_due_cents": amount_past_due,
        "insolvency_status": loan.insolvency_status,
        "insolvency_marked_at": (
            loan.insolvency_marked_at.isoformat() if loan.insolvency_marked_at else None
        ),
        "insolvency_marked_by": loan.insolvency_marked_by,
    }


@router.get("/{loan_id}")
def get_loan(
    loan_id: UUID,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Full servicing detail — reuses loan_servicing.get_loan_status (schedule,
    payments, balances) plus the WS-H delinquency-bucket block (month-end
    bucket, live DPD, insolvency, snapshot history) and the auto-charge switch
    state (WS-G). 404 if the loan doesn't exist."""
    detail = get_loan_status(db, loan_id)
    if detail is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Loan not found")

    loan = db.query(PlatformLoan).filter(PlatformLoan.id == loan_id).first()
    # Borrower join key for the servicing workspace's borrower-scoped tabs.
    # Added on the ADMIN detail only (not in loan_servicing.get_loan_status,
    # which archive.py also feeds) so no other surface's payload changes.
    detail["patient_id"] = str(loan.patient_id) if loan.patient_id else None

    # Dave's Account-Due-As-Of / Amount-to-Move servicing status (additive
    # fields; drives the live-DPD basis in the delinquency block below).
    today = date.today()
    servicing = servicing_status_service.build_servicing_status(db, loan, today)
    if servicing is not None:
        detail["servicing_status"] = servicing.to_api_fields()

    block = _delinquency_block(loan, today, servicing)
    snapshots = (
        db.query(PlatformLoanDelinquencySnapshot)
        .filter(PlatformLoanDelinquencySnapshot.loan_id == loan_id)
        .order_by(PlatformLoanDelinquencySnapshot.snapshot_month.desc())
        .limit(12)
        .all()
    )
    block["snapshots"] = [
        {
            "snapshot_month": s.snapshot_month.isoformat(),
            "bucket": s.bucket,
            "days_past_due": s.days_past_due,
            "amount_past_due_cents": s.amount_past_due_cents,
            "outstanding_principal_cents": s.outstanding_principal_cents,
            "bureau_reportable": s.bureau_reportable,
            "snapshotted_at": s.snapshotted_at.isoformat() if s.snapshotted_at else None,
        }
        for s in snapshots
    ]
    detail["delinquency"] = block
    detail["auto_charge"] = {
        # Raw per-loan switch: True / False / None (None = inherit platform default).
        "enabled": loan.auto_charge_enabled,
        # What the engine actually does for this loan (per-loan switch resolved
        # against the platform default; the AUTO_COLLECTION_ENABLED feature
        # flag still gates the engine globally).
        "effective_enabled": auto_collection.effective_auto_charge(loan),
        "disabled_reason": loan.auto_charge_disabled_reason,
    }
    return detail


class InsolvencyBody(BaseModel):
    """Mark (or clear) a loan's segregated insolvency classification.

    ``status='none'`` clears — admin-only. A comment is MANDATORY either way
    (the determination is a human judgement; the why must be on the record)."""

    status: Literal["consumer_proposal", "bankruptcy", "credit_counseling", "none"]
    comment: str = Field(min_length=3, max_length=2000)


@router.post("/{loan_id}/insolvency")
def set_loan_insolvency(
    loan_id: UUID,
    body: InsolvencyBody,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin", "staff")),
):
    """Manually mark a loan insolvent (consumer proposal / bankruptcy / credit
    counseling) or clear the marking (WS-H).

    Insolvency OVERRIDES bucket derivation (the segregated insolvency
    portfolio, per Dave) — marking flips ``current_bucket`` immediately;
    clearing re-derives from the live schedule. Staff may mark; CLEARING
    requires admin (undoing a legal-status determination is the more dangerous
    direction). Fully audited via ``platform_events``.
    """
    loan = db.query(PlatformLoan).filter(PlatformLoan.id == loan_id).first()
    if loan is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Loan not found")

    if body.status == "none":
        roles = {r.role.name for r in user.roles}
        if "admin" not in roles:
            raise HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail="Clearing an insolvency marking requires the admin role.",
            )

    try:
        delinquency_buckets.set_insolvency(
            db, loan, body.status, body.comment, str(getattr(user, "id", "") or "unknown")
        )
    except ValueError as exc:
        raise HTTPException(status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    db.commit()
    return {
        "loan_id": str(loan.id),
        "insolvency_status": loan.insolvency_status,
        "current_bucket": loan.current_bucket,
    }



# --- WS-G: Disable / re-enable auto-charges (Turnkey Servicing parity) -------


class AutoChargeDisableBody(BaseModel):
    # Mandatory: why auto-charges are being stopped (dead account, blanket
    # stop-payment, borrower request, …) — audited verbatim.
    reason: str


@router.post("/{loan_id}/auto-charge/disable")
def disable_auto_charge(
    loan_id: UUID,
    body: AutoChargeDisableBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Disable scheduled auto-charges for a loan (the "Disable Auto-Charges"
    kill switch). Reason is REQUIRED and the change is audited via
    platform_events. Idempotent — disabling an already-disabled loan just
    refreshes the reason."""
    loan = db.query(PlatformLoan).filter(PlatformLoan.id == loan_id).first()
    if loan is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Loan not found")
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A reason is required to disable auto-charges",
        )
    before = loan.auto_charge_enabled
    loan.auto_charge_enabled = False
    loan.auto_charge_disabled_reason = reason[:500]
    actor = str(getattr(user, "id", "") or "unknown")
    db.add(
        PlatformEvent(
            event_type=auto_collection.AUTO_CHARGE_DISABLED_EVENT,
            actor=actor,
            application_id=loan.application_id,
            payload={
                "v": 1,
                "actor": {"type": "staff", "id": actor},
                "loan_id": str(loan.id),
                "application_id": str(loan.application_id) if loan.application_id else None,
                "before": {"auto_charge_enabled": before},
                "after": {"auto_charge_enabled": False},
                "reason": reason[:500],
                "disabled_by": f"staff:{actor}",
            },
        )
    )
    db.commit()
    return {
        "loan_id": str(loan.id),
        "auto_charge_enabled": False,
        "disabled_reason": loan.auto_charge_disabled_reason,
    }


@router.post("/{loan_id}/auto-charge/enable")
def enable_auto_charge(
    loan_id: UUID,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Re-enable scheduled auto-charges (e.g. after a new default bank account
    is on file). Clears the disabled reason; audited. Idempotent."""
    loan = db.query(PlatformLoan).filter(PlatformLoan.id == loan_id).first()
    if loan is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Loan not found")
    before = loan.auto_charge_enabled
    previous_reason = loan.auto_charge_disabled_reason
    loan.auto_charge_enabled = True
    loan.auto_charge_disabled_reason = None
    actor = str(getattr(user, "id", "") or "unknown")
    db.add(
        PlatformEvent(
            event_type=auto_collection.AUTO_CHARGE_ENABLED_EVENT,
            actor=actor,
            application_id=loan.application_id,
            payload={
                "v": 1,
                "actor": {"type": "staff", "id": actor},
                "loan_id": str(loan.id),
                "application_id": str(loan.application_id) if loan.application_id else None,
                "before": {
                    "auto_charge_enabled": before,
                    "disabled_reason": previous_reason,
                },
                "after": {"auto_charge_enabled": True},
                "enabled_by": f"staff:{actor}",
            },
        )
    )
    db.commit()
    return {"loan_id": str(loan.id), "auto_charge_enabled": True, "disabled_reason": None}


def _require_loan(db: Session, loan_id: UUID) -> None:
    if db.query(PlatformLoan.id).filter(PlatformLoan.id == loan_id).first() is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Loan not found")


class ScheduleRow(BaseModel):
    # Schedule-item UUID — the handle the surgery (suspend/unsuspend) and
    # hardship-deferment endpoints key on. The ORM rows carry it; the response
    # model previously dropped it, leaving the cockpit unable to act per-item.
    id: UUID
    installment_number: int
    due_date: date
    principal_cents: int
    interest_cents: int
    total_cents: int
    paid_cents: int
    status: str


class PaymentRow(BaseModel):
    amount_cents: int
    received_at: datetime
    method: str
    external_ref: Optional[str] = None


class StatementRow(BaseModel):
    period_start: date
    period_end: date
    opening_balance_cents: int
    principal_paid_cents: int
    interest_paid_cents: int
    closing_balance_cents: int


@router.get("/{loan_id}/schedule", response_model=list[ScheduleRow])
def loan_schedule(loan_id: UUID, db: Session = Depends(get_db), _user=Depends(get_current_user)):
    """The full amortization schedule — the calculation engine made visible.
    Reflects surgery: a staff-suspended installment shows status ``suspended``.
    Custom transactions live at ``/schedule/custom``; the change history at
    ``/schedule/changes``."""
    _require_loan(db, loan_id)
    return (
        db.query(PlatformLoanScheduleItem)
        .filter(PlatformLoanScheduleItem.loan_id == loan_id)
        .order_by(PlatformLoanScheduleItem.installment_number)
        .all()
    )


@router.get("/{loan_id}/payments", response_model=list[PaymentRow])
def loan_payments(loan_id: UUID, db: Session = Depends(get_db), _user=Depends(get_current_user)):
    """The payment ledger (cash receipts), newest first."""
    _require_loan(db, loan_id)
    return (
        db.query(PlatformLoanPayment)
        .filter(PlatformLoanPayment.loan_id == loan_id)
        .order_by(PlatformLoanPayment.received_at.desc())
        .all()
    )


@router.get("/{loan_id}/ledger")
def loan_ledger_view(
    loan_id: UUID,
    as_of: Optional[date] = None,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """The immutable money ledger (WS-A): every transaction with its allocation
    split, dual dates (effective vs processing), reference number, and running
    category balances — plus the actuals-engine balance view at ``as_of``
    (outstanding principal + interest due + fees due + add-on == payoff)."""
    loan = db.query(PlatformLoan).filter(PlatformLoan.id == loan_id).first()
    if loan is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Loan not found")
    return ledger_service.ledger_view(loan, as_of or date.today())


@router.get("/{loan_id}/statements", response_model=list[StatementRow])
def loan_statements(loan_id: UUID, db: Session = Depends(get_db), _user=Depends(get_current_user)):
    """Billing statements (opening/closing balance + principal/interest split)."""
    _require_loan(db, loan_id)
    return (
        db.query(PlatformLoanStatement)
        .filter(PlatformLoanStatement.loan_id == loan_id)
        .order_by(PlatformLoanStatement.period_start.desc())
        .all()
    )


# ---------------------------------------------------------------------------
# Scheduled-transaction surgery (WS-F) — Dave: "a very, very important section".
#
# Staff can suspend/unsuspend an individual installment and add/cancel custom
# scheduled transactions. Comment is MANDATORY on every action; every change
# emits a platform_event (the change history below). The amortization plan is
# never altered — surgery layers on top of it. Router-level gate:
# require_roles("admin", "staff").
# ---------------------------------------------------------------------------


def _get_loan_for_surgery(db: Session, loan_id: UUID) -> PlatformLoan:
    loan = db.query(PlatformLoan).filter(PlatformLoan.id == loan_id).first()
    if loan is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Loan not found")
    return loan


def _actor_id(user) -> str:
    return str(getattr(user, "id", "") or "unknown")


class SurgeryCommentBody(BaseModel):
    """Every surgery action carries a mandatory comment (Dave)."""

    comment: str = Field(..., min_length=1, max_length=2000)


class CustomTransactionBody(SurgeryCommentBody):
    scheduled_date: date
    amount_cents: int = Field(..., gt=0)
    repayment_mode: Literal["regular", "add_on", "special", "payoff"] = "regular"


class CustomTransactionRow(BaseModel):
    id: UUID
    scheduled_date: date
    amount_cents: int
    repayment_mode: str
    status: str
    comment: str
    created_by: str
    created_at: datetime
    cancelled_by: Optional[str] = None
    cancelled_at: Optional[datetime] = None


def _item_response(item: PlatformLoanScheduleItem) -> dict:
    return {
        "schedule_item_id": str(item.id),
        "installment_number": item.installment_number,
        "due_date": item.due_date.isoformat(),
        "status": item.status,
        "paid_cents": item.paid_cents,
        "total_cents": item.total_cents,
    }


@router.post("/{loan_id}/schedule/{item_id}/suspend")
def suspend_schedule_item(
    loan_id: UUID,
    item_id: UUID,
    body: SurgeryCommentBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Suspend ONE scheduled installment: delinquency-aging and dunning skip it
    (the point of suspending); the money is still owed. Audited."""
    loan = _get_loan_for_surgery(db, loan_id)
    try:
        item = schedule_surgery.suspend_installment(
            db, loan, item_id, comment=body.comment, actor=_actor_id(user)
        )
    except schedule_surgery.ScheduleSurgeryError as exc:
        raise HTTPException(status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    db.commit()
    return _item_response(item)


@router.post("/{loan_id}/schedule/{item_id}/unsuspend")
def unsuspend_schedule_item(
    loan_id: UUID,
    item_id: UUID,
    body: SurgeryCommentBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Lift a suspension (restores ``partial``/``scheduled``; the nightly aging
    pass re-derives ``late`` if overdue). Audited."""
    loan = _get_loan_for_surgery(db, loan_id)
    try:
        item = schedule_surgery.unsuspend_installment(
            db, loan, item_id, comment=body.comment, actor=_actor_id(user)
        )
    except schedule_surgery.ScheduleSurgeryError as exc:
        raise HTTPException(status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    db.commit()
    return _item_response(item)


@router.post("/{loan_id}/schedule/custom", response_model=CustomTransactionRow)
def add_custom_transaction(
    loan_id: UUID,
    body: CustomTransactionBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Add a one-off custom scheduled transaction (date / amount / repayment
    mode) layered on top of the plan — the amortization schedule is untouched.
    Audited.

    WS-I: a PAST ``scheduled_date`` is a permission-bounded backdate — it
    requires the ``ledger.backdate`` permission (admin implicitly allowed) and
    is bounded to loan_ledger.BACKDATE_WINDOW_DAYS days back inside the
    service; the audit event carries ``backdated: true``."""
    loan = _get_loan_for_surgery(db, loan_id)
    allow_backdate = False
    if body.scheduled_date < date.today():
        if not user_has_permission_or_admin(user, "ledger", "backdate"):
            raise HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail=(
                    "backdating a custom transaction requires the "
                    "ledger.backdate permission"
                ),
            )
        allow_backdate = True
    try:
        row = schedule_surgery.add_custom_transaction(
            db,
            loan,
            scheduled_date=body.scheduled_date,
            amount_cents=body.amount_cents,
            repayment_mode=body.repayment_mode,
            comment=body.comment,
            actor=_actor_id(user),
            allow_backdate=allow_backdate,
        )
    except schedule_surgery.ScheduleSurgeryError as exc:
        raise HTTPException(status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    db.commit()
    db.refresh(row)
    return row


@router.post("/{loan_id}/schedule/custom/{custom_id}/cancel", response_model=CustomTransactionRow)
def cancel_custom_transaction(
    loan_id: UUID,
    custom_id: UUID,
    body: SurgeryCommentBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Cancel a still-scheduled custom transaction (status flip — never a hard
    delete; history stays auditable). Audited."""
    loan = _get_loan_for_surgery(db, loan_id)
    try:
        row = schedule_surgery.cancel_custom_transaction(
            db, loan, custom_id, comment=body.comment, actor=_actor_id(user)
        )
    except schedule_surgery.ScheduleSurgeryError as exc:
        raise HTTPException(status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    db.commit()
    db.refresh(row)
    return row


@router.get("/{loan_id}/schedule/custom", response_model=list[CustomTransactionRow])
def list_custom_transactions(
    loan_id: UUID,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """The loan's custom scheduled transactions (all statuses, newest date first)."""
    _require_loan(db, loan_id)
    return (
        db.query(PlatformLoanCustomTransaction)
        .filter(PlatformLoanCustomTransaction.loan_id == loan_id)
        .order_by(PlatformLoanCustomTransaction.scheduled_date.desc())
        .all()
    )


@router.get("/{loan_id}/schedule/changes")
def schedule_change_history(
    loan_id: UUID,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Full schedule-surgery change history (suspend / unsuspend / custom add /
    cancel), newest first — read straight off the platform_events audit log.
    Together with GET /schedule (whose rows now surface ``suspended``) and
    GET /schedule/custom, this is the Scheduled-transactions tab's data."""
    loan = _get_loan_for_surgery(db, loan_id)
    return {"loan_id": str(loan_id), "changes": schedule_surgery.schedule_changes(db, loan)}
