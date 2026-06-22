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

from app.core.auth import get_current_user, require_roles
from app.db.base import get_db
from app.models.loan import Vendor
from app.models.platform.credit_application import PlatformCreditApplication

router = APIRouter(dependencies=[Depends(require_roles("admin", "staff"))])


class AdminApplicationRow(BaseModel):
    id: UUID
    patient_name: str
    product_name: str
    vendor_name: str
    requested_amount_cents: int
    status: str
    decision_at: Optional[datetime] = None
    created_at: datetime


class AdminApplicationDetail(AdminApplicationRow):
    patient_contact: str
    vendor_id: Optional[UUID] = None
    applicant_role: str
    requested_amount_source: str
    decision: Optional[dict] = None
    decision_by: Optional[str] = None
    flow_state: dict
    self_reported: dict
    verification_count: int
    consent_count: int


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
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Whole-book application queue, newest first. Optional status / vendor filters."""
    q = db.query(PlatformCreditApplication).options(
        joinedload(PlatformCreditApplication.patient),
        joinedload(PlatformCreditApplication.credit_product),
    )
    if status_filter:
        q = q.filter(PlatformCreditApplication.status == status_filter)
    if vendor_id is not None:
        q = q.filter(PlatformCreditApplication.vendor_id == vendor_id)
    rows = q.order_by(PlatformCreditApplication.created_at.desc()).limit(limit).all()
    return [
        AdminApplicationRow(
            id=r.id,
            patient_name=_patient_name(r.patient),
            product_name=_product_name(r.credit_product),
            vendor_name=_vendor_name(db, r.vendor_id),
            requested_amount_cents=r.requested_amount_cents,
            status=r.status,
            decision_at=r.decision_at,
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
        requested_amount_source=app.requested_amount_source,
        applicant_role=app.applicant_role,
        status=app.status,
        decision=app.decision,
        decision_by=app.decision_by,
        decision_at=app.decision_at,
        flow_state=app.flow_state or {},
        self_reported=app.self_reported or {},
        verification_count=len(app.verifications or []),
        consent_count=len(app.consents or []),
        created_at=app.created_at,
    )
