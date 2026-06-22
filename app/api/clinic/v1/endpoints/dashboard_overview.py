"""Clinic-facing dashboard overview — the single "everything at a glance" read.

GET /clinic/v1/dashboard/overview — one round-trip for the landing screen.

This endpoint COMPOSES the per-domain block helpers built alongside it:
``applications_block`` (dashboard_applications), ``loan_book_block`` +
``payments_block`` (dashboard_loanbook), and ``marketplace_block``
(dashboard_marketplace). It owns no queries of its own beyond the vendor row;
each block helper is independently scoped to ``vendor_id`` (see spec §3.1).

SCOPING: clinics ARE the ``vendors`` table (spec §13). ``get_current_clinic_user``
resolves the caller's ``vendor_id``; every block helper filters on it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.clinic.v1.deps import ClinicPrincipal, get_current_clinic_user
from app.api.clinic.v1.endpoints.dashboard_applications import applications_block
from app.api.clinic.v1.endpoints.dashboard_loanbook import loan_book_block, payments_block
from app.api.clinic.v1.endpoints.dashboard_marketplace import marketplace_block
from app.db.base import get_db
from app.models.loan import Vendor

router = APIRouter(tags=["clinic-dashboard"])

DEFAULT_WINDOW_DAYS = 30


# --- response schema (mirrors VendorOverview, spec §3.1) -------------------


class OverviewVendor(BaseModel):
    id: str
    business_name: str
    status: str


class VendorOverview(BaseModel):
    vendor: OverviewVendor
    window: dict
    applications: dict
    loan_book: dict
    payments: dict
    marketplace: dict


# --- route -----------------------------------------------------------------


@router.get("/dashboard/overview", response_model=VendorOverview)
def dashboard_overview(
    frm: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None, alias="to"),
    db: Session = Depends(get_db),
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
):
    """One-shot overview for the dashboard landing screen.

    Window defaults to the last 30 days; ``from``/``to`` override it. Each
    domain block is computed by its own scoped helper so the numbers always
    agree with the dedicated per-domain endpoints.
    """
    now = datetime.now(timezone.utc)
    to = to or now
    frm = frm or (to - timedelta(days=DEFAULT_WINDOW_DAYS))

    vendor = db.query(Vendor).filter(Vendor.id == principal.vendor_id).first()
    if vendor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Vendor not found"
        )

    return VendorOverview(
        vendor=OverviewVendor(
            id=str(vendor.id),
            business_name=vendor.business_name,
            status=vendor.status,
        ),
        window={"from": frm, "to": to},
        applications=applications_block(db, principal.vendor_id, frm, to),
        loan_book=loan_book_block(db, principal.vendor_id),
        payments=payments_block(db, principal.vendor_id, frm, to),
        marketplace=marketplace_block(db, principal.vendor_id, frm, to),
    )
