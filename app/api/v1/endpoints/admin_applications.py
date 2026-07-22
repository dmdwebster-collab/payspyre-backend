"""Admin applications work-queue + account-360 (read-only, Phase 1).

Lender/admin portal M2 + WS-E pipeline depth: the whole-book application queue
with the full TL filter set (status / branch / product / assignee / vendor /
province / time-in-status) and sortable waiting-time + assignee columns, plus
a detail view carrying the decision context. Write actions live in
``admin_actions`` (decision) and ``admin_originations`` (edit/offer/flags).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func as sa_func
from sqlalchemy.orm import Session, joinedload

from app.api.platform_application_serialization import (
    CanonicalApplicationDetail,
    build_canonical_detail,
)
from app.core.auth import get_current_user, require_roles
from app.db.base import get_db
from app.models.loan import Vendor
from app.models.platform.application_document import PlatformApplicationDocument
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.user import User
from app.services import document_storage
from app.services.originations_admin import waiting_seconds

router = APIRouter(dependencies=[Depends(require_roles("admin", "staff"))])


class AdminApplicationRow(BaseModel):
    id: UUID
    patient_name: str
    product_name: str
    vendor_name: str
    requested_amount_cents: int
    status: str
    # Key personal/income summary so the queue is meaningful without opening detail.
    applicant_first_name: Optional[str] = None
    applicant_last_name: Optional[str] = None
    applicant_city: Optional[str] = None
    applicant_province: Optional[str] = None
    income_type: Optional[str] = None
    net_monthly_income_cents: Optional[int] = None
    decision_at: Optional[datetime] = None
    # WS-E: underwriting queue assignment (sortable assignee column).
    assigned_to_user_id: Optional[UUID] = None
    assigned_to_email: Optional[str] = None
    assigned_at: Optional[datetime] = None
    # WS-E pipeline columns: branch + time-in-status (sortable waiting time).
    branch: Optional[str] = None
    status_updated_at: Optional[datetime] = None
    time_in_status_seconds: Optional[int] = None
    # WS-I: the vendor asked for the deal back / another look (their one action).
    vendor_reprocessing_requested: bool = False
    created_at: datetime


class AdminApplicationDetail(BaseModel):
    """Queue-row metadata + the FULL canonical field set for the workspace."""

    id: UUID
    #: The borrower (``platform_patients.id``). Admin-only join key for every
    #: borrower-scoped tab; see ``ApplicationHeader.patient_id``.
    patient_id: UUID
    patient_name: str
    patient_contact: str
    product_name: str
    vendor_name: str
    vendor_id: Optional[UUID] = None
    requested_amount_cents: int
    requested_amount_source: str
    applicant_role: str
    status: str
    decision: Optional[dict] = None
    decision_by: Optional[str] = None
    decision_at: Optional[datetime] = None
    flow_state: dict
    self_reported: dict
    verification_count: int
    consent_count: int
    assigned_to_user_id: Optional[UUID] = None
    assigned_at: Optional[datetime] = None
    branch: Optional[str] = None
    status_updated_at: Optional[datetime] = None
    vendor_reprocessing_requested: bool = False
    created_at: datetime
    # The canonical Personal / ID / Residence / income / Financial field set.
    canonical: CanonicalApplicationDetail


def _patient_name(p) -> str:
    if p is None:
        return "Unknown patient"
    name = " ".join(x for x in [p.legal_first_name, p.legal_last_name] if x).strip()
    return name or "Unknown patient"


def _product_name(pr) -> str:
    return (pr.name if pr and pr.name else None) or "Financing"


def _vendor_name(db: Session, vendor_id) -> str:
    if vendor_id is None:
        return "—"
    v = db.query(Vendor.business_name).filter(Vendor.id == vendor_id).first()
    return v[0] if v else "—"


#: Expression anchoring "time in current status" — status_updated_at is
#: stamped by every audited transition; created_at is the pre-043 fallback.
_WAITING_ANCHOR = sa_func.coalesce(
    PlatformCreditApplication.status_updated_at, PlatformCreditApplication.created_at
)


@router.get("", response_model=list[AdminApplicationRow])
def list_applications(
    status_filter: Optional[str] = Query(None, alias="status"),
    vendor_id: Optional[UUID] = Query(None),
    product_id: Optional[UUID] = Query(
        None, description="Only applications for this credit product (TL pipeline filter)."
    ),
    branch: Optional[str] = Query(
        None, description="Only applications tagged with this branch label."
    ),
    province: Optional[str] = Query(
        None, description="Only applications whose residence province matches (e.g. 'BC')."
    ),
    assigned_to: Optional[UUID] = Query(
        None, description="Only applications assigned to this staff user (WS-E queue lane)."
    ),
    unassigned: bool = Query(
        False, description="Only unassigned applications (the pick-up queue). Ignored when assigned_to is set."
    ),
    reprocessing_requested: bool = Query(
        False,
        description="Only applications where the vendor requested reprocessing (WS-I lane).",
    ),
    min_waiting_hours: Optional[int] = Query(
        None, ge=0, description="Only applications in their current status at least this long."
    ),
    max_waiting_hours: Optional[int] = Query(
        None, ge=0, description="Only applications in their current status at most this long."
    ),
    sort: Literal["created_at", "waiting_time", "assignee"] = Query(
        "created_at",
        description="Sort column: created_at (default), waiting_time (time in "
        "current status), or assignee (assigned user's email).",
    ),
    order: Literal["asc", "desc"] = Query(
        "desc", description="Sort direction (waiting_time desc = longest-waiting first)."
    ),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Whole-book pipeline queue with the full TL filter set — status / branch /
    product / assignee / vendor / province / time-in-status — and sortable
    waiting-time + assignee columns (WS-E)."""
    q = db.query(PlatformCreditApplication).options(
        joinedload(PlatformCreditApplication.patient),
        joinedload(PlatformCreditApplication.credit_product),
    )
    if status_filter:
        q = q.filter(PlatformCreditApplication.status == status_filter)
    if vendor_id is not None:
        q = q.filter(PlatformCreditApplication.vendor_id == vendor_id)
    if product_id is not None:
        q = q.filter(PlatformCreditApplication.credit_product_id == product_id)
    if branch:
        q = q.filter(PlatformCreditApplication.branch == branch)
    if province:
        q = q.filter(
            sa_func.upper(PlatformCreditApplication.residence_province) == province.upper()
        )
    if assigned_to is not None:
        q = q.filter(PlatformCreditApplication.assigned_to_user_id == assigned_to)
    elif unassigned:
        q = q.filter(PlatformCreditApplication.assigned_to_user_id.is_(None))
    if reprocessing_requested:
        q = q.filter(PlatformCreditApplication.vendor_reprocessing_requested.is_(True))

    now = datetime.now(timezone.utc)
    if min_waiting_hours is not None:
        q = q.filter(_WAITING_ANCHOR <= now - timedelta(hours=min_waiting_hours))
    if max_waiting_hours is not None:
        q = q.filter(_WAITING_ANCHOR >= now - timedelta(hours=max_waiting_hours))

    if sort == "waiting_time":
        # Longest-waiting = oldest anchor, so desc(waiting) == asc(anchor).
        order_col = _WAITING_ANCHOR.asc() if order == "desc" else _WAITING_ANCHOR.desc()
        q = q.order_by(order_col, PlatformCreditApplication.created_at.desc())
    elif sort == "assignee":
        q = q.outerjoin(User, User.id == PlatformCreditApplication.assigned_to_user_id)
        email_col = sa_func.lower(User.email)
        order_col = email_col.asc().nullslast() if order == "asc" else email_col.desc().nullslast()
        q = q.order_by(order_col, PlatformCreditApplication.created_at.desc())
    else:
        order_col = (
            PlatformCreditApplication.created_at.asc()
            if order == "asc"
            else PlatformCreditApplication.created_at.desc()
        )
        q = q.order_by(order_col)

    rows = q.offset(offset).limit(limit).all()

    # Assignee display column, batch-resolved (no N+1).
    assignee_ids = {r.assigned_to_user_id for r in rows if r.assigned_to_user_id}
    emails: dict = {}
    if assignee_ids:
        emails = dict(
            db.query(User.id, User.email).filter(User.id.in_(assignee_ids)).all()
        )
    return [
        AdminApplicationRow(
            id=r.id,
            patient_name=_patient_name(r.patient),
            product_name=_product_name(r.credit_product),
            vendor_name=_vendor_name(db, r.vendor_id),
            requested_amount_cents=r.requested_amount_cents,
            status=r.status,
            applicant_first_name=r.first_name,
            applicant_last_name=r.last_name,
            applicant_city=r.residence_city,
            applicant_province=r.residence_province,
            income_type=str(r.income_type) if r.income_type is not None else None,
            net_monthly_income_cents=r.net_monthly_income_cents,
            decision_at=r.decision_at,
            assigned_to_user_id=r.assigned_to_user_id,
            assigned_to_email=emails.get(r.assigned_to_user_id),
            assigned_at=r.assigned_at,
            branch=r.branch,
            status_updated_at=r.status_updated_at,
            time_in_status_seconds=waiting_seconds(r.status_updated_at, r.created_at, now),
            vendor_reprocessing_requested=bool(r.vendor_reprocessing_requested),
            created_at=r.created_at,
        )
        for r in rows
    ]


@router.get("/{application_id}", response_model=AdminApplicationDetail)
def get_application(
    application_id: UUID,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Full application detail + decision context (read-only)."""
    app = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.id == application_id)
        .options(
            joinedload(PlatformCreditApplication.patient),
            joinedload(PlatformCreditApplication.credit_product),
            joinedload(PlatformCreditApplication.verifications),
            joinedload(PlatformCreditApplication.consents),
            joinedload(PlatformCreditApplication.secondary_incomes),
        )
        .first()
    )
    if app is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")
    p = app.patient
    contact = ((p.email or p.phone_e164) if p else "") or ""
    return AdminApplicationDetail(
        id=app.id,
        patient_id=app.patient_id,
        patient_name=_patient_name(p),
        patient_contact=contact,
        product_name=_product_name(app.credit_product),
        vendor_name=_vendor_name(db, app.vendor_id),
        vendor_id=app.vendor_id,
        requested_amount_cents=app.requested_amount_cents,
        requested_amount_source=str(app.requested_amount_source),
        applicant_role=str(app.applicant_role),
        status=str(app.status),
        decision=app.decision,
        decision_by=app.decision_by,
        decision_at=app.decision_at,
        flow_state=app.flow_state or {},
        self_reported=app.self_reported or {},
        verification_count=len(app.verifications or []),
        consent_count=len(app.consents or []),
        assigned_to_user_id=app.assigned_to_user_id,
        assigned_at=app.assigned_at,
        branch=app.branch,
        status_updated_at=app.status_updated_at,
        vendor_reprocessing_requested=bool(app.vendor_reprocessing_requested),
        created_at=app.created_at,
        canonical=build_canonical_detail(app, p),
    )


class AdminDocumentRow(BaseModel):
    document_id: UUID
    doc_type: str
    status: str
    download_url: Optional[str] = None  # short-TTL presigned GET; None if not uploaded/unconfigured


@router.get("/{application_id}/documents", response_model=list[AdminDocumentRow])
def list_application_documents(
    application_id: UUID,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """KYC documents for an application, each with a short-TTL signed download URL
    (issued only for uploaded docs when storage is configured). RBAC-gated by the
    router; every call is an admin action over sensitive data."""
    docs = (
        db.query(PlatformApplicationDocument)
        .filter(PlatformApplicationDocument.application_id == application_id)
        .order_by(PlatformApplicationDocument.created_at)
        .all()
    )
    configured = document_storage.is_configured()
    rows: list[AdminDocumentRow] = []
    for d in docs:
        url = None
        if configured and d.status == "uploaded":
            try:
                url = document_storage.presigned_get_url(d.object_key)
            except document_storage.StorageNotConfigured:
                url = None
        rows.append(
            AdminDocumentRow(document_id=d.id, doc_type=d.doc_type, status=d.status, download_url=url)
        )
    return rows
