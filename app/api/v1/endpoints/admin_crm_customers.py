"""Admin Customer CRM (WS-G — video 09 ``/tools/manageCustomers``).

The back-office CRM over all borrower (patient) profiles:

* customer list — search + status filter (active / blocked) + pagination;
* customer detail — the CROSS-LOAN view: identity + every application, loan and
  payment for the patient in one place;
* lock/block + unblock — a mandatory-reason audit surface
  (``customer_blocks`` service); blocked = no new originations, servicing
  unaffected;
* changelog — the per-customer timeline aggregating ``platform_events`` (which
  already carries profile-version supersede events, decision events, block
  events, ...).

Mounted at ``/api/v1/admin/crm/customers`` (see ``app/api/v1/api.py``). Reads
are admin/staff; the block/unblock writes are admin-only. SIN is NEVER exposed
(only ``sin_last3``); this is admin surface, but the same PII discipline as the
rest of the platform applies.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_roles
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.crm import PlatformCustomerBlock, PlatformCustomerBlockReason
from app.models.platform.event import PlatformEvent
from app.models.platform.loan import PlatformLoan, PlatformLoanPayment
from app.models.platform.patient import PlatformPatient
from app.services.customer_blocks import (
    CustomerBlockError,
    active_block,
    block_patient,
    unblock_patient,
)

router = APIRouter(dependencies=[Depends(require_roles("admin", "staff"))])

ADMIN_ONLY = Depends(require_roles("admin"))


# ---------------------------------------------------------------------------
# Pure helper
# ---------------------------------------------------------------------------


def patient_display_name(patient: PlatformPatient) -> str:
    parts = [
        getattr(patient, "legal_first_name", None),
        getattr(patient, "legal_last_name", None),
    ]
    return " ".join(p for p in parts if p).strip() or "Unknown"


def _actor_id(user) -> str:
    return str(getattr(user, "id", "") or getattr(user, "email", "") or "unknown")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CustomerListRow(BaseModel):
    patient_id: UUID
    full_name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    blocked: bool
    created_at: datetime


class CustomerListOut(BaseModel):
    rows: list[CustomerListRow]
    total: int


class BlockInfo(BaseModel):
    reason_code: str
    reason_text: str
    blocked_by: str
    blocked_at: datetime


class LoanSummary(BaseModel):
    loan_id: UUID
    application_id: Optional[UUID] = None
    status: str
    principal_cents: int
    principal_balance_cents: int
    disbursed_at: Optional[datetime] = None


class ApplicationSummary(BaseModel):
    application_id: UUID
    status: str
    vendor_id: Optional[UUID] = None
    requested_amount_cents: Optional[int] = None
    created_at: datetime


class PaymentSummary(BaseModel):
    loan_id: UUID
    amount_cents: int
    received_at: Optional[datetime] = None
    method: Optional[str] = None


class CustomerDetailOut(BaseModel):
    patient_id: UUID
    full_name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    sin_last3: Optional[str] = None
    blocked: bool
    active_block: Optional[BlockInfo] = None
    applications: list[ApplicationSummary]
    loans: list[LoanSummary]
    payments: list[PaymentSummary]


class BlockReasonOut(BaseModel):
    code: str
    label: str


class BlockBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason_code: str
    reason_text: str = Field(..., min_length=1)


class UnblockBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: Optional[str] = None


class ChangelogEntry(BaseModel):
    id: int
    occurred_at: datetime
    event_type: str
    actor: Optional[str] = None
    application_id: Optional[UUID] = None
    summary: dict


# ---------------------------------------------------------------------------
# Directory
# ---------------------------------------------------------------------------


@router.get("/block-reasons", response_model=list[BlockReasonOut])
def list_block_reasons(db: Session = Depends(get_db)):
    rows = (
        db.query(PlatformCustomerBlockReason)
        .filter(PlatformCustomerBlockReason.active.is_(True))
        .order_by(PlatformCustomerBlockReason.sort_order)
        .all()
    )
    return [BlockReasonOut(code=r.code, label=r.label) for r in rows]


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("", response_model=CustomerListOut)
def list_customers(
    q: Optional[str] = Query(None, description="Search name / email / phone"),
    status_filter: Optional[str] = Query(None, alias="status", description="active|blocked"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Paginated customer list (TL's 581-record grid).

    Excludes soft-deleted (PIPEDA) patients. ``status=blocked`` filters to
    patients with an ACTIVE block; ``status=active`` to those without.
    """
    # Subquery of patient_ids with an active block.
    blocked_ids_sq = (
        db.query(PlatformCustomerBlock.patient_id)
        .filter(PlatformCustomerBlock.unblocked_at.is_(None))
        .subquery()
    )

    base = db.query(PlatformPatient).filter(PlatformPatient.deleted_at.is_(None))
    if q:
        like = f"%{q.strip()}%"
        base = base.filter(
            or_(
                PlatformPatient.legal_first_name.ilike(like),
                PlatformPatient.legal_last_name.ilike(like),
                PlatformPatient.email.ilike(like),
                PlatformPatient.phone_e164.ilike(like),
            )
        )
    if status_filter == "blocked":
        base = base.filter(PlatformPatient.id.in_(db.query(blocked_ids_sq.c.patient_id)))
    elif status_filter == "active":
        base = base.filter(~PlatformPatient.id.in_(db.query(blocked_ids_sq.c.patient_id)))

    total = base.count()
    patients = (
        base.order_by(PlatformPatient.created_at.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )

    blocked_lookup = {
        pid
        for (pid,) in db.query(PlatformCustomerBlock.patient_id)
        .filter(
            PlatformCustomerBlock.unblocked_at.is_(None),
            PlatformCustomerBlock.patient_id.in_([p.id for p in patients] or [None]),
        )
        .all()
    }

    rows = [
        CustomerListRow(
            patient_id=p.id,
            full_name=patient_display_name(p),
            email=p.email,
            phone=p.phone_e164,
            blocked=p.id in blocked_lookup,
            created_at=p.created_at,
        )
        for p in patients
    ]
    return CustomerListOut(rows=rows, total=total)


# ---------------------------------------------------------------------------
# Detail (cross-loan)
# ---------------------------------------------------------------------------


def _get_patient_or_404(db: Session, patient_id: UUID) -> PlatformPatient:
    patient = (
        db.query(PlatformPatient)
        .filter(
            PlatformPatient.id == patient_id, PlatformPatient.deleted_at.is_(None)
        )
        .first()
    )
    if patient is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return patient


@router.get("/{patient_id}", response_model=CustomerDetailOut)
def customer_detail(patient_id: UUID, db: Session = Depends(get_db)):
    """Cross-loan customer view: identity + all applications / loans / payments."""
    patient = _get_patient_or_404(db, patient_id)

    apps = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.patient_id == patient_id)
        .order_by(PlatformCreditApplication.created_at.desc())
        .all()
    )
    loans = (
        db.query(PlatformLoan)
        .filter(PlatformLoan.patient_id == patient_id)
        .order_by(PlatformLoan.created_at.desc())
        .all()
    )
    loan_ids = [loan.id for loan in loans]
    payments = []
    if loan_ids:
        payments = (
            db.query(PlatformLoanPayment)
            .filter(PlatformLoanPayment.loan_id.in_(loan_ids))
            .order_by(PlatformLoanPayment.received_at.desc())
            .all()
        )

    ab = active_block(db, patient_id)
    return CustomerDetailOut(
        patient_id=patient.id,
        full_name=patient_display_name(patient),
        email=patient.email,
        phone=patient.phone_e164,
        sin_last3=patient.sin_last3,
        blocked=ab is not None,
        active_block=(
            BlockInfo(
                reason_code=ab.reason_code,
                reason_text=ab.reason_text,
                blocked_by=ab.blocked_by,
                blocked_at=ab.blocked_at,
            )
            if ab is not None
            else None
        ),
        applications=[
            ApplicationSummary(
                application_id=a.id,
                status=a.status,
                vendor_id=a.vendor_id,
                requested_amount_cents=a.requested_amount_cents,
                created_at=a.created_at,
            )
            for a in apps
        ],
        loans=[
            LoanSummary(
                loan_id=loan.id,
                application_id=loan.application_id,
                status=loan.status,
                principal_cents=loan.principal_cents,
                principal_balance_cents=loan.principal_balance_cents,
                disbursed_at=loan.disbursed_at,
            )
            for loan in loans
        ],
        payments=[
            PaymentSummary(
                loan_id=pmt.loan_id,
                amount_cents=pmt.amount_cents,
                received_at=pmt.received_at,
                method=pmt.method,
            )
            for pmt in payments
        ],
    )


# ---------------------------------------------------------------------------
# Lock / block
# ---------------------------------------------------------------------------


@router.post("/{patient_id}/block", response_model=BlockInfo, dependencies=[ADMIN_ONLY])
def block_customer(
    patient_id: UUID,
    body: BlockBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Lock/block a customer (mandatory reason). Blocked = no new originations."""
    _get_patient_or_404(db, patient_id)
    try:
        row = block_patient(
            db,
            patient_id=patient_id,
            reason_code=body.reason_code,
            reason_text=body.reason_text,
            actor_id=_actor_id(user),
        )
    except CustomerBlockError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    db.commit()
    return BlockInfo(
        reason_code=row.reason_code,
        reason_text=row.reason_text,
        blocked_by=row.blocked_by,
        blocked_at=row.blocked_at,
    )


@router.post("/{patient_id}/unblock", dependencies=[ADMIN_ONLY])
def unblock_customer(
    patient_id: UUID,
    body: UnblockBody | None = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Unlock a customer. Idempotency: unblocking an unblocked customer is 409."""
    _get_patient_or_404(db, patient_id)
    try:
        unblock_patient(
            db,
            patient_id=patient_id,
            actor_id=_actor_id(user),
            note=body.note if body is not None else None,
        )
    except CustomerBlockError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    db.commit()
    return {"patient_id": str(patient_id), "blocked": False}


# ---------------------------------------------------------------------------
# Changelog
# ---------------------------------------------------------------------------


@router.get("/{patient_id}/changelog", response_model=list[ChangelogEntry])
def customer_changelog(
    patient_id: UUID,
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """Per-customer timeline aggregating ``platform_events`` for the patient.

    This surfaces profile-version supersedes (``patient_field_*``), decisions,
    block/unblock, and every other event the platform already writes against
    the patient — the changelog TL's customer-detail tab shows, sourced from
    our append-only event log rather than a bespoke table.
    """
    _get_patient_or_404(db, patient_id)
    rows = (
        db.query(PlatformEvent)
        .filter(PlatformEvent.patient_id == patient_id)
        .order_by(PlatformEvent.occurred_at.desc(), PlatformEvent.id.desc())
        .limit(limit)
        .all()
    )
    entries: list[ChangelogEntry] = []
    for ev in rows:
        payload = ev.payload if isinstance(ev.payload, dict) else {}
        summary = payload.get("after") if isinstance(payload.get("after"), dict) else {}
        entries.append(
            ChangelogEntry(
                id=ev.id,
                occurred_at=ev.occurred_at,
                event_type=ev.event_type,
                actor=ev.actor,
                application_id=ev.application_id,
                summary=summary or {},
            )
        )
    return entries
