"""Collections work-surface endpoints (WS-C full parity) — mounted at
``/api/v1/admin/collections``.

Dave's collections modernization (04__WP_Collections), layered on the read
surfaces in ``admin_collections``:

  * BULK collector assignment (by vendor / borrower alpha / bucket) +
    single assign/unassign + per-collector queue ("assign to me
    one-at-a-time is just not viable"); junior/senior tier gating.
  * Action plans: settings-definable action-type directory (admin CRUD) +
    per-loan dated actions with mandatory comments and recorded outcomes.
  * Promise-to-pay: amount / date / mandatory comment / no-late-fee flag,
    evaluated against the ACTUAL payment ledger (open → kept | broken).
  * Header math: outstanding principal + interest due + fees due + add-on
    balance = total payout, straight from the actuals ledger service.
  * Insolvency portfolio: segregated queue (principal-then-DPD sort, per
    Dave), trustee-payment recording THROUGH the hardened record_payment
    money path, and the idempotent monthly maintenance-fee hook.

PERMISSIONS: the work surface is admin+staff (collectors are staff). The
settings directory and every insolvency MONEY write (trustee payment /
maintenance fee) are ADMIN-ONLY.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_roles
from app.db.base import get_db
from app.models.platform.collections_work import (
    PlatformCollectionAction,
    PlatformCollectionActionType,
    PlatformCollectorAssignment,
    PlatformPromiseToPay,
)
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.event import PlatformEvent
from app.models.platform.loan import PlatformLoan, PlatformLoanPayment
from app.models.platform.patient import PlatformPatient
from app.models.user import User
from app.services import collections_worksurface as work
from app.services.collections_worksurface import CollectionsError
from app.services.delinquency_buckets import ALL_BUCKETS, INSOLVENCY_STATUSES

router = APIRouter(dependencies=[Depends(require_roles("admin", "staff"))])
# Settings directory + insolvency money writes: admin only.
admin_router = APIRouter(dependencies=[Depends(require_roles("admin"))])


def _actor_id(user) -> str:
    return str(getattr(user, "id", "") or "unknown")


def _get_loan(db: Session, loan_id: UUID) -> PlatformLoan:
    loan = db.query(PlatformLoan).filter(PlatformLoan.id == loan_id).first()
    if loan is None:
        raise HTTPException(status_code=404, detail="Loan not found")
    return loan


def _patient_names(db: Session, loans: list[PlatformLoan]) -> dict:
    """loan_id → (patient_id, display name), resolving through the
    application (native loans) or the direct patient link (migrated)."""
    app_ids = [loan.application_id for loan in loans if loan.application_id]
    app_patient: dict = {}
    if app_ids:
        app_patient = dict(
            db.query(
                PlatformCreditApplication.id, PlatformCreditApplication.patient_id
            )
            .filter(PlatformCreditApplication.id.in_(app_ids))
            .all()
        )
    patient_ids = set()
    loan_patient: dict = {}
    for loan in loans:
        pid = app_patient.get(loan.application_id) or loan.patient_id
        loan_patient[loan.id] = pid
        if pid:
            patient_ids.add(pid)
    patients = {}
    if patient_ids:
        patients = {
            p.id: p
            for p in db.query(PlatformPatient)
            .filter(PlatformPatient.id.in_(patient_ids))
            .all()
        }
    out = {}
    for loan in loans:
        patient = patients.get(loan_patient[loan.id])
        name = (
            " ".join(
                x
                for x in [
                    getattr(patient, "legal_first_name", None),
                    getattr(patient, "legal_last_name", None),
                ]
                if x
            ).strip()
            if patient
            else ""
        )
        out[loan.id] = (loan_patient[loan.id], name or "—", patient)
    return out


# ===========================================================================
# 1. Collector assignment
# ===========================================================================


class AssignBody(BaseModel):
    # Defaults to the calling user — Turnkey's "Assign to me", minus the
    # one-at-a-time pain.
    collector_user_id: Optional[UUID] = None
    tier: Literal["junior", "senior"]
    reassign: bool = False


class BulkAssignBody(BaseModel):
    collector_user_id: UUID
    tier: Literal["junior", "senior"]
    # Selectors — at least one required; they AND together (Dave: "per vendor
    # or per vendor and alpha depending on how big that vendor is").
    vendor_id: Optional[UUID] = None
    alpha_from: Optional[str] = Field(None, description="Borrower last-name range start (A-Z)")
    alpha_to: Optional[str] = Field(None, description="Borrower last-name range end (A-Z)")
    buckets: Optional[list[str]] = Field(
        None, description="Month-end buckets, e.g. ['current_month_late','pot_30']"
    )
    reassign: bool = False
    limit: int = Field(500, ge=1, le=1000)


class AssignmentRow(BaseModel):
    id: UUID
    loan_id: UUID
    collector_user_id: UUID
    tier: str
    method: str
    active: bool


def _assignment_row(a: PlatformCollectorAssignment) -> AssignmentRow:
    return AssignmentRow(
        id=a.id,
        loan_id=a.loan_id,
        collector_user_id=a.collector_user_id,
        tier=str(a.tier),
        method=a.method,
        active=a.active,
    )


def _require_collector(db: Session, collector_user_id: UUID) -> User:
    collector = db.query(User).filter(User.id == collector_user_id).first()
    if collector is None or not collector.is_active:
        raise HTTPException(status_code=404, detail="Collector user not found or inactive")
    return collector


@router.post("/loans/{loan_id}/assign", response_model=AssignmentRow)
def assign_loan(
    loan_id: UUID,
    body: AssignBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Assign one delinquent loan to a collector (defaults to the caller —
    "assign to me"). Tier-gated: a junior cannot take deep-bucket accounts."""
    loan = _get_loan(db, loan_id)
    collector_id = body.collector_user_id or getattr(user, "id", None)
    if collector_id is None:
        raise HTTPException(status_code=422, detail="collector_user_id is required")
    _require_collector(db, collector_id)
    try:
        assignment = work.assign_loan(
            db,
            loan,
            collector_id,
            body.tier,
            method=work.ASSIGN_METHOD_MANUAL,
            actor_id=_actor_id(user),
            reassign=body.reassign,
        )
    except CollectionsError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    db.commit()
    return _assignment_row(assignment)


class UnassignBody(BaseModel):
    comment: Optional[str] = None


@router.post("/loans/{loan_id}/unassign", response_model=AssignmentRow)
def unassign_loan(
    loan_id: UUID,
    body: UnassignBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    loan = _get_loan(db, loan_id)
    try:
        assignment = work.unassign_loan(
            db, loan, actor_id=_actor_id(user), comment=body.comment
        )
    except CollectionsError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    db.commit()
    return _assignment_row(assignment)


class BulkAssignResult(BaseModel):
    assigned: list[AssignmentRow]
    skipped: list[dict]
    matched: int


@router.post("/assignments/bulk", response_model=BulkAssignResult)
def bulk_assign(
    body: BulkAssignBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """BULK collector assignment — Dave's replacement for "go in each
    individual account and assign it to a user … just not viable".

    Selects the delinquent worklist (past-due loans, plus the insolvency
    portfolio for seniors), narrows by the given selectors (vendor /
    borrower-last-name alpha range / month-end bucket — they AND together),
    and assigns every match to the collector. Already-assigned loans are
    skipped unless ``reassign``; tier violations are skipped with the reason
    (never a partial failure)."""
    if body.vendor_id is None and body.alpha_from is None and body.buckets is None:
        raise HTTPException(
            status_code=422,
            detail="At least one selector (vendor_id, alpha range, buckets) is required",
        )
    if (body.alpha_from is None) != (body.alpha_to is None):
        raise HTTPException(
            status_code=422, detail="alpha_from and alpha_to must be given together"
        )
    if body.buckets:
        unknown = [b for b in body.buckets if b not in ALL_BUCKETS]
        if unknown:
            raise HTTPException(
                status_code=422, detail=f"Unknown buckets: {', '.join(unknown)}"
            )
    _require_collector(db, body.collector_user_id)

    today = date.today()
    method = (
        work.ASSIGN_METHOD_BULK_VENDOR
        if body.vendor_id is not None
        else work.ASSIGN_METHOD_BULK_ALPHA
        if body.alpha_from is not None
        else work.ASSIGN_METHOD_BULK_BUCKET
    )

    query = db.query(PlatformLoan).filter(
        PlatformLoan.status.in_(("active", "delinquent", "charged_off"))
    )
    if body.vendor_id is not None:
        query = query.join(
            PlatformCreditApplication,
            PlatformLoan.application_id == PlatformCreditApplication.id,
        ).filter(PlatformCreditApplication.vendor_id == body.vendor_id)
    loans = query.all()

    names = _patient_names(db, loans)
    assigned: list[AssignmentRow] = []
    skipped: list[dict] = []
    matched = 0
    for loan in loans:
        bucket = work.display_bucket(loan, today)
        in_worklist = (
            work.loan_has_past_due_unpaid(loan, today)
            or bucket in ("insolvency", "written_off")
        )
        if not in_worklist:
            continue
        if body.buckets and bucket not in body.buckets:
            continue
        if body.alpha_from is not None:
            patient = names[loan.id][2]
            last_name = getattr(patient, "legal_last_name", None) if patient else None
            try:
                if not work.alpha_matches(last_name, body.alpha_from, body.alpha_to):
                    continue
            except CollectionsError as exc:
                raise HTTPException(status_code=422, detail=str(exc))
        matched += 1
        if matched > body.limit:
            skipped.append({"loan_id": str(loan.id), "reason": "over limit"})
            continue
        try:
            assignment = work.assign_loan(
                db,
                loan,
                body.collector_user_id,
                body.tier,
                method=method,
                actor_id=_actor_id(user),
                reassign=body.reassign,
                today=today,
            )
            assigned.append(_assignment_row(assignment))
        except CollectionsError as exc:
            skipped.append({"loan_id": str(loan.id), "reason": str(exc)})
    db.commit()
    return BulkAssignResult(assigned=assigned, skipped=skipped, matched=matched)


class MyQueueRow(BaseModel):
    loan_id: UUID
    patient_name: str
    principal_balance_cents: int
    days_past_due: int
    bucket: str
    tier: str
    assigned_at: Optional[datetime] = None
    insolvency_status: Optional[str] = None
    # Promise-to-pay surfacing (Dave: promises show in the queues).
    open_promise_date: Optional[date] = None
    open_promise_amount_cents: Optional[int] = None
    broken_promise_count: int = 0


@router.get("/queue/my", response_model=list[MyQueueRow])
def my_queue(
    collector_user_id: Optional[UUID] = Query(
        None, description="Defaults to the calling user"
    ),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """The per-collector queue (Dave: "limit the display of accounts by
    user"), sorted by outstanding principal then DPD, both descending —
    with promise-to-pay surfacing."""
    collector_id = collector_user_id or getattr(user, "id", None)
    assignments = (
        db.query(PlatformCollectorAssignment)
        .filter(
            PlatformCollectorAssignment.collector_user_id == collector_id,
            PlatformCollectorAssignment.active.is_(True),
        )
        .all()
    )
    if not assignments:
        return []
    loan_ids = [a.loan_id for a in assignments]
    loans = {
        loan.id: loan
        for loan in db.query(PlatformLoan)
        .filter(PlatformLoan.id.in_(loan_ids))
        .all()
    }
    promises = (
        db.query(PlatformPromiseToPay)
        .filter(PlatformPromiseToPay.loan_id.in_(loan_ids))
        .all()
    )
    open_by_loan: dict = {}
    broken_by_loan: dict = {}
    for promise in promises:
        if promise.status == work.PTP_OPEN:
            current = open_by_loan.get(promise.loan_id)
            if current is None or promise.promised_date < current.promised_date:
                open_by_loan[promise.loan_id] = promise
        elif promise.status == work.PTP_BROKEN:
            broken_by_loan[promise.loan_id] = broken_by_loan.get(promise.loan_id, 0) + 1

    names = _patient_names(db, list(loans.values()))
    today = date.today()
    rows: list[MyQueueRow] = []
    for assignment in assignments:
        loan = loans.get(assignment.loan_id)
        if loan is None:
            continue
        open_promise = open_by_loan.get(loan.id)
        rows.append(
            MyQueueRow(
                loan_id=loan.id,
                patient_name=names[loan.id][1],
                principal_balance_cents=loan.principal_balance_cents,
                days_past_due=work.loan_days_past_due(loan, today),
                bucket=work.display_bucket(loan, today),
                tier=str(assignment.tier),
                assigned_at=assignment.assigned_at,
                insolvency_status=loan.insolvency_status,
                open_promise_date=open_promise.promised_date if open_promise else None,
                open_promise_amount_cents=(
                    open_promise.amount_cents if open_promise else None
                ),
                broken_promise_count=broken_by_loan.get(loan.id, 0),
            )
        )
    rows.sort(
        key=lambda r: work.insolvency_sort_key(
            r.principal_balance_cents, r.days_past_due
        )
    )
    return rows


# ===========================================================================
# 2. Action plans
# ===========================================================================


class ActionTypeRow(BaseModel):
    id: UUID
    code: str
    label: str
    description: Optional[str] = None
    active: bool
    sort_order: int


def _action_type_row(t: PlatformCollectionActionType) -> ActionTypeRow:
    return ActionTypeRow(
        id=t.id,
        code=t.code,
        label=t.label,
        description=t.description,
        active=t.active,
        sort_order=t.sort_order,
    )


class ActionTypeCreateBody(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=200)
    description: Optional[str] = None
    sort_order: int = 0


class ActionTypePatchBody(BaseModel):
    label: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = None
    sort_order: Optional[int] = None
    active: Optional[bool] = None  # false = soft-deactivate


@router.get("/action-types", response_model=list[ActionTypeRow])
def list_action_types(
    include_inactive: bool = Query(True),
    db: Session = Depends(get_db),
):
    """The settings-defined action-type directory, ordered for pickers.
    Readable by staff (they pick from it); writes are admin-only."""
    q = db.query(PlatformCollectionActionType)
    if not include_inactive:
        q = q.filter(PlatformCollectionActionType.active.is_(True))
    rows = q.order_by(
        PlatformCollectionActionType.sort_order,
        PlatformCollectionActionType.code,
    ).all()
    return [_action_type_row(t) for t in rows]


@admin_router.post(
    "/action-types", response_model=ActionTypeRow, status_code=status.HTTP_201_CREATED
)
def create_action_type(
    body: ActionTypeCreateBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Add an action type to the settings directory (Dave: "those action
    types need to be in a settings area that we can define and add and
    remove to"). ``code`` is immutable; removal is soft-deactivation."""
    code = body.code.strip().lower().replace(" ", "_")
    row = PlatformCollectionActionType(
        code=code,
        label=body.label.strip(),
        description=(body.description or "").strip() or None,
        sort_order=body.sort_order,
        active=True,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An action type with code {code!r} already exists.",
        )
    db.add(
        PlatformEvent(
            event_type="collection_action_type_created",
            actor=_actor_id(user),
            payload={"action_type_id": str(row.id), "code": code},
        )
    )
    db.commit()
    return _action_type_row(row)


@admin_router.patch("/action-types/{type_id}", response_model=ActionTypeRow)
def update_action_type(
    type_id: UUID,
    body: ActionTypePatchBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    row = (
        db.query(PlatformCollectionActionType)
        .filter(PlatformCollectionActionType.id == type_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Action type not found")
    before = {"label": row.label, "active": row.active, "sort_order": row.sort_order}
    if body.label is not None:
        row.label = body.label.strip()
    if body.description is not None:
        row.description = body.description.strip() or None
    if body.sort_order is not None:
        row.sort_order = body.sort_order
    if body.active is not None:
        row.active = body.active
    db.add(
        PlatformEvent(
            event_type="collection_action_type_updated",
            actor=_actor_id(user),
            payload={
                "action_type_id": str(row.id),
                "code": row.code,
                "before": before,
                "after": {
                    "label": row.label,
                    "active": row.active,
                    "sort_order": row.sort_order,
                },
            },
        )
    )
    db.commit()
    return _action_type_row(row)


class ActionRow(BaseModel):
    id: UUID
    loan_id: UUID
    action_type_id: UUID
    action_type_code: Optional[str] = None
    action_type_label: Optional[str] = None
    due_date: date
    assignee_user_id: Optional[UUID] = None
    status: str
    comment: str
    outcome: Optional[str] = None
    created_by: str
    completed_by: Optional[str] = None
    completed_at: Optional[datetime] = None
    cancelled_by: Optional[str] = None
    cancelled_at: Optional[datetime] = None
    created_at: Optional[datetime] = None


def _action_row(a: PlatformCollectionAction) -> ActionRow:
    action_type = a.action_type
    return ActionRow(
        id=a.id,
        loan_id=a.loan_id,
        action_type_id=a.action_type_id,
        action_type_code=getattr(action_type, "code", None),
        action_type_label=getattr(action_type, "label", None),
        due_date=a.due_date,
        assignee_user_id=a.assignee_user_id,
        status=str(a.status),
        comment=a.comment,
        outcome=a.outcome,
        created_by=a.created_by,
        completed_by=a.completed_by,
        completed_at=a.completed_at,
        cancelled_by=a.cancelled_by,
        cancelled_at=a.cancelled_at,
        created_at=a.created_at,
    )


class ActionCreateBody(BaseModel):
    action_type_id: UUID
    due_date: date
    assignee_user_id: Optional[UUID] = None
    comment: str = Field(min_length=1)


@router.get("/loans/{loan_id}/actions", response_model=list[ActionRow])
def list_actions(
    loan_id: UUID,
    db: Session = Depends(get_db),
):
    """The loan's FULL action-plan history (nothing is ever deleted)."""
    _get_loan(db, loan_id)
    rows = (
        db.query(PlatformCollectionAction)
        .filter(PlatformCollectionAction.loan_id == loan_id)
        .order_by(
            PlatformCollectionAction.due_date.asc(),
            PlatformCollectionAction.created_at.asc(),
        )
        .all()
    )
    return [_action_row(a) for a in rows]


@router.post(
    "/loans/{loan_id}/actions",
    response_model=ActionRow,
    status_code=status.HTTP_201_CREATED,
)
def create_action(
    loan_id: UUID,
    body: ActionCreateBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Plan an action on a loan (Turnkey "New action" dialog; comment
    MANDATORY per Dave — "internal messages … reviewed by back-end staff")."""
    loan = _get_loan(db, loan_id)
    if not body.comment.strip():
        raise HTTPException(status_code=422, detail="A comment is mandatory")
    action_type = (
        db.query(PlatformCollectionActionType)
        .filter(PlatformCollectionActionType.id == body.action_type_id)
        .first()
    )
    if action_type is None:
        raise HTTPException(status_code=422, detail="Unknown action type")
    if not action_type.active:
        raise HTTPException(status_code=422, detail="Action type is deactivated")
    if body.assignee_user_id is not None:
        _require_collector(db, body.assignee_user_id)
    action = PlatformCollectionAction(
        loan_id=loan.id,
        action_type_id=action_type.id,
        due_date=body.due_date,
        assignee_user_id=body.assignee_user_id,
        status="planned",
        comment=body.comment.strip(),
        created_by=_actor_id(user),
    )
    db.add(action)
    db.flush()
    db.add(
        PlatformEvent(
            event_type=work.ACTION_CREATED_EVENT,
            actor=_actor_id(user),
            application_id=loan.application_id,
            payload={
                "loan_id": str(loan.id),
                "action_id": str(action.id),
                "action_type": action_type.code,
                "due_date": body.due_date.isoformat(),
            },
        )
    )
    db.commit()
    return _action_row(action)


class ActionCompleteBody(BaseModel):
    outcome: str = Field(min_length=1)
    comment: Optional[str] = None


class ActionCancelBody(BaseModel):
    comment: str = Field(min_length=1)


def _get_action(db: Session, action_id: UUID) -> PlatformCollectionAction:
    action = (
        db.query(PlatformCollectionAction)
        .filter(PlatformCollectionAction.id == action_id)
        .first()
    )
    if action is None:
        raise HTTPException(status_code=404, detail="Action not found")
    return action


@router.post("/actions/{action_id}/complete", response_model=ActionRow)
def complete_action(
    action_id: UUID,
    body: ActionCompleteBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Record the outcome of a planned action (outcome mandatory)."""
    action = _get_action(db, action_id)
    if action.status != "planned":
        raise HTTPException(
            status_code=422, detail=f"Action is {action.status}, not planned"
        )
    action.status = "completed"
    action.outcome = body.outcome.strip()
    if body.comment and body.comment.strip():
        action.comment = f"{action.comment}\n[completion] {body.comment.strip()}"
    action.completed_by = _actor_id(user)
    action.completed_at = datetime.now(timezone.utc)
    db.add(
        PlatformEvent(
            event_type=work.ACTION_COMPLETED_EVENT,
            actor=_actor_id(user),
            payload={"action_id": str(action.id), "outcome": action.outcome},
        )
    )
    db.commit()
    return _action_row(action)


@router.post("/actions/{action_id}/cancel", response_model=ActionRow)
def cancel_action(
    action_id: UUID,
    body: ActionCancelBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    action = _get_action(db, action_id)
    if action.status != "planned":
        raise HTTPException(
            status_code=422, detail=f"Action is {action.status}, not planned"
        )
    action.status = "cancelled"
    action.comment = f"{action.comment}\n[cancelled] {body.comment.strip()}"
    action.cancelled_by = _actor_id(user)
    action.cancelled_at = datetime.now(timezone.utc)
    db.add(
        PlatformEvent(
            event_type=work.ACTION_CANCELLED_EVENT,
            actor=_actor_id(user),
            payload={"action_id": str(action.id)},
        )
    )
    db.commit()
    return _action_row(action)


# ===========================================================================
# 3. Promise to pay
# ===========================================================================


class PromiseRow(BaseModel):
    id: UUID
    loan_id: UUID
    amount_cents: int
    promised_date: date
    comment: str
    no_late_fees: bool
    status: str
    covered_cents: Optional[int] = None
    evaluated_at: Optional[datetime] = None
    created_by: str
    created_at: Optional[datetime] = None


def _promise_row(p: PlatformPromiseToPay) -> PromiseRow:
    return PromiseRow(
        id=p.id,
        loan_id=p.loan_id,
        amount_cents=p.amount_cents,
        promised_date=p.promised_date,
        comment=p.comment,
        no_late_fees=p.no_late_fees,
        status=str(p.status),
        covered_cents=p.covered_cents,
        evaluated_at=p.evaluated_at,
        created_by=p.created_by,
        created_at=p.created_at,
    )


class PromiseCreateBody(BaseModel):
    amount_cents: int = Field(gt=0)
    promised_date: date
    comment: str = Field(min_length=1)
    no_late_fees: bool = False


@router.get("/loans/{loan_id}/promises", response_model=list[PromiseRow])
def list_promises(
    loan_id: UUID,
    db: Session = Depends(get_db),
):
    """The loan's promises, with open ones re-evaluated against ACTUAL
    ledger payments on read (kept / broken derivation is persisted)."""
    loan = _get_loan(db, loan_id)
    promises = (
        db.query(PlatformPromiseToPay)
        .filter(PlatformPromiseToPay.loan_id == loan_id)
        .order_by(PlatformPromiseToPay.created_at.desc())
        .all()
    )
    changed = work.refresh_promise_statuses(db, loan, promises)
    if changed:
        db.commit()
    return [_promise_row(p) for p in promises]


@router.post(
    "/loans/{loan_id}/promises",
    response_model=PromiseRow,
    status_code=status.HTTP_201_CREATED,
)
def create_promise(
    loan_id: UUID,
    body: PromiseCreateBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Record a promise to pay (amount / date / MANDATORY comment / optional
    no-late-fee flag — Turnkey dialog parity). One OPEN promise per loan."""
    loan = _get_loan(db, loan_id)
    if not body.comment.strip():
        raise HTTPException(status_code=422, detail="A comment is mandatory")
    if body.promised_date < date.today():
        raise HTTPException(
            status_code=422, detail="promised_date cannot be in the past"
        )
    existing_open = (
        db.query(PlatformPromiseToPay)
        .filter(
            PlatformPromiseToPay.loan_id == loan_id,
            PlatformPromiseToPay.status == work.PTP_OPEN,
        )
        .first()
    )
    if existing_open is not None:
        raise HTTPException(
            status_code=409,
            detail="Loan already has an open promise to pay "
            "(cancel it or wait for evaluation)",
        )
    promise = PlatformPromiseToPay(
        loan_id=loan.id,
        amount_cents=body.amount_cents,
        promised_date=body.promised_date,
        comment=body.comment.strip(),
        no_late_fees=body.no_late_fees,
        status=work.PTP_OPEN,
        created_by=_actor_id(user),
    )
    db.add(promise)
    db.flush()
    db.add(
        PlatformEvent(
            event_type=work.PTP_CREATED_EVENT,
            actor=_actor_id(user),
            application_id=loan.application_id,
            payload={
                "loan_id": str(loan.id),
                "promise_id": str(promise.id),
                "amount_cents": body.amount_cents,
                "promised_date": body.promised_date.isoformat(),
                "no_late_fees": body.no_late_fees,
            },
        )
    )
    db.commit()
    return _promise_row(promise)


class PromiseCancelBody(BaseModel):
    comment: str = Field(min_length=1)


@router.post("/promises/{promise_id}/cancel", response_model=PromiseRow)
def cancel_promise(
    promise_id: UUID,
    body: PromiseCancelBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    promise = (
        db.query(PlatformPromiseToPay)
        .filter(PlatformPromiseToPay.id == promise_id)
        .first()
    )
    if promise is None:
        raise HTTPException(status_code=404, detail="Promise not found")
    if promise.status != work.PTP_OPEN:
        raise HTTPException(
            status_code=422, detail=f"Promise is {promise.status}, not open"
        )
    promise.status = work.PTP_CANCELLED
    promise.cancelled_by = _actor_id(user)
    promise.cancelled_at = datetime.now(timezone.utc)
    promise.comment = f"{promise.comment}\n[cancelled] {body.comment.strip()}"
    db.add(
        PlatformEvent(
            event_type=work.PTP_CANCELLED_EVENT,
            actor=_actor_id(user),
            payload={"promise_id": str(promise.id)},
        )
    )
    db.commit()
    return _promise_row(promise)


# ===========================================================================
# 4. Header math
# ===========================================================================


@router.get("/loans/{loan_id}/header")
def loan_header(
    loan_id: UUID,
    as_of: Optional[date] = None,
    db: Session = Depends(get_db),
):
    """Dave's header identity from the actuals ledger: outstanding principal
    + interest due + fees due + add-on balance = total payout."""
    loan = _get_loan(db, loan_id)
    return work.header_view(loan, as_of or date.today(), today=date.today())


# ===========================================================================
# 5. Insolvency portfolio
# ===========================================================================


class InsolvencyQueueRow(BaseModel):
    loan_id: UUID
    patient_name: str
    insolvency_status: str
    insolvency_marked_at: Optional[datetime] = None
    principal_balance_cents: int
    days_past_due: int
    loan_status: str
    assigned_collector_user_id: Optional[UUID] = None


@router.get("/insolvency", response_model=list[InsolvencyQueueRow])
def insolvency_queue(
    insolvency_status: Optional[
        Literal["consumer_proposal", "bankruptcy", "credit_counseling"]
    ] = Query(None),
    limit: int = Query(200, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """Dave's SEGREGATED insolvency portfolio queue — consumer proposal /
    bankruptcy / credit counseling, "separated out of those other collection
    queues", written off the main book but still maintained. Sorted by
    outstanding principal then DPD, both descending (per Dave)."""
    statuses = (insolvency_status,) if insolvency_status else INSOLVENCY_STATUSES
    loans = (
        db.query(PlatformLoan)
        .filter(PlatformLoan.insolvency_status.in_(statuses))
        .all()
    )
    names = _patient_names(db, loans)
    assignments = {}
    if loans:
        assignments = {
            a.loan_id: a
            for a in db.query(PlatformCollectorAssignment)
            .filter(
                PlatformCollectorAssignment.loan_id.in_([loan.id for loan in loans]),
                PlatformCollectorAssignment.active.is_(True),
            )
            .all()
        }
    today = date.today()
    rows = [
        InsolvencyQueueRow(
            loan_id=loan.id,
            patient_name=names[loan.id][1],
            insolvency_status=str(loan.insolvency_status),
            insolvency_marked_at=loan.insolvency_marked_at,
            principal_balance_cents=loan.principal_balance_cents,
            days_past_due=work.loan_days_past_due(loan, today),
            loan_status=str(loan.status),
            assigned_collector_user_id=(
                assignments[loan.id].collector_user_id
                if loan.id in assignments
                else None
            ),
        )
        for loan in loans
    ]
    rows.sort(
        key=lambda r: work.insolvency_sort_key(
            r.principal_balance_cents, r.days_past_due
        )
    )
    return rows[:limit]


class TrusteePaymentBody(BaseModel):
    amount_cents: int = Field(gt=0)
    received_at: Optional[datetime] = None
    external_ref: Optional[str] = None
    comment: Optional[str] = None


@admin_router.post("/insolvency/{loan_id}/trustee-payments")
def record_trustee_payment(
    loan_id: UUID,
    body: TrusteePaymentBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Record a payment received from an insolvency trustee. ADMIN-ONLY money
    write, delegated to the hardened ``record_payment`` path
    (``method='trustee'``). Idempotent on ``external_ref``."""
    loan = _get_loan(db, loan_id)
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
            return {
                "payment_id": str(prior.id),
                "loan_status": loan.status,
                "principal_balance_cents": loan.principal_balance_cents,
                "idempotent_replay": True,
            }
    try:
        payment = work.record_trustee_payment(
            db,
            loan,
            body.amount_cents,
            received_at=body.received_at,
            external_ref=body.external_ref,
            comment=body.comment,
            actor_id=_actor_id(user),
        )
    except (CollectionsError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    db.commit()
    return {
        "payment_id": str(payment.id),
        "loan_status": loan.status,
        "principal_balance_cents": loan.principal_balance_cents,
    }


class MaintenanceFeeBody(BaseModel):
    amount_cents: int = Field(gt=0)
    fee_month: Optional[date] = Field(
        None, description="Any date in the month to charge (defaults to this month)"
    )


@admin_router.post("/insolvency/{loan_id}/maintenance-fees")
def charge_maintenance_fee(
    loan_id: UUID,
    body: MaintenanceFeeBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Charge the monthly insolvency maintenance fee for ONE loan. ADMIN-ONLY.
    Idempotent per (loan, month) — an already-charged month returns 409 and
    charges nothing. The money lands as an immutable add-on ledger fee row."""
    loan = _get_loan(db, loan_id)
    try:
        txn = work.apply_maintenance_fee(
            db,
            loan,
            body.amount_cents,
            fee_month=body.fee_month,
            actor_id=_actor_id(user),
        )
    except CollectionsError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if txn is None:
        raise HTTPException(
            status_code=409,
            detail="Maintenance fee already charged for this loan and month",
        )
    db.commit()
    return {
        "transaction_id": str(txn.id),
        "reference": txn.reference,
        "amount_cents": txn.amount_cents,
        "effective_date": txn.effective_date.isoformat(),
    }


class MaintenanceRunBody(BaseModel):
    amount_cents: int = Field(gt=0)
    fee_month: Optional[date] = None


@admin_router.post("/insolvency/maintenance-fees/run")
def run_maintenance_fees(
    body: MaintenanceRunBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """The monthly maintenance-fee HOOK across the whole insolvency portfolio
    (idempotent — re-runs skip already-charged loans). ADMIN-ONLY; this is
    what the parked monthly cron will call."""
    return work.run_monthly_maintenance_fees(
        db, body.amount_cents, fee_month=body.fee_month, actor_id=_actor_id(user)
    )
