"""Admin applications work-queue + account-360 (read-only, Phase 1).

Lender/admin portal M2. The whole-book application queue (no vendor scoping) with
filters, and a detail view carrying the decision context. Write actions
(decision/override/offers) are Phase 2.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
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
from app.services import document_storage

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
    # WS-E: underwriting queue assignment.
    assigned_to_user_id: Optional[UUID] = None
    assigned_at: Optional[datetime] = None
    created_at: datetime


class AdminApplicationDetail(BaseModel):
    """Queue-row metadata + the FULL canonical field set for the workspace."""

    id: UUID
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


@router.get("", response_model=list[AdminApplicationRow])
def list_applications(
    status_filter: Optional[str] = Query(None, alias="status"),
    vendor_id: Optional[UUID] = Query(None),
    assigned_to: Optional[UUID] = Query(
        None, description="Only applications assigned to this staff user (WS-E queue lane)."
    ),
    unassigned: bool = Query(
        False, description="Only unassigned applications (the pick-up queue). Ignored when assigned_to is set."
    ),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Whole-book application queue, newest first. Optional status / vendor /
    assignment filters."""
    q = db.query(PlatformCreditApplication).options(
        joinedload(PlatformCreditApplication.patient),
        joinedload(PlatformCreditApplication.credit_product),
    )
    if status_filter:
        q = q.filter(PlatformCreditApplication.status == status_filter)
    if vendor_id is not None:
        q = q.filter(PlatformCreditApplication.vendor_id == vendor_id)
    if assigned_to is not None:
        q = q.filter(PlatformCreditApplication.assigned_to_user_id == assigned_to)
    elif unassigned:
        q = q.filter(PlatformCreditApplication.assigned_to_user_id.is_(None))
    rows = (
        q.order_by(PlatformCreditApplication.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
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
            assigned_at=r.assigned_at,
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
