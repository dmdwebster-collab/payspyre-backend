"""Clinic/vendor-side marketplace router (spec §6.3, vendor surface).

The spec's "vendor" actor maps onto this clinic API surface. Clinics ARE the
``vendors`` table (spec §13); ``get_current_clinic_user`` resolves the caller's
``vendor_id`` and every action is performed as that vendor. Vendors only ever
see de-identified, PII-free lead/listing projections (``vendor_view``) until the
patient selects them.

Service exception mapping:
  LookupError / None / not-found -> 404
  ValueError                     -> 400
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.clinic.v1.deps import ClinicPrincipal, get_current_clinic_user
from app.api.marketplace_schemas import VendorBillingEntry
from app.db.base import get_db
from app.services.marketplace import listings as listings_service

router = APIRouter(prefix="/marketplace", tags=["clinic-marketplace"])


@router.get("/leads")
def list_leads(
    treatment_category: Optional[str] = None,
    max_distance_km: Optional[int] = None,
    min_verification: Optional[str] = None,
    db: Session = Depends(get_db),
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
) -> list[dict]:
    """Browse live, de-identified leads (PII-free vendor_view projections)."""
    return listings_service.list_leads_for_vendor(
        db,
        treatment_category=treatment_category,
        max_distance_km=max_distance_km,
        min_verification=min_verification,
    )


@router.post("/leads/{listing_id}/express_interest", status_code=status.HTTP_201_CREATED)
def express_interest(
    listing_id: UUID,
    db: Session = Depends(get_db),
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
) -> dict:
    try:
        interest = listings_service.express_interest(
            db, listing_id=listing_id, vendor_id=principal.vendor_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return {
        "interest_id": str(interest.id),
        "listing_id": str(interest.listing_id),
        "expressed_at": interest.expressed_at.isoformat() if interest.expressed_at else None,
    }


@router.get("/listings/{listing_id}")
def get_listing(
    listing_id: UUID,
    db: Session = Depends(get_db),
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
) -> dict:
    try:
        view = listings_service.get_listing_for_vendor(
            db, listing_id=listing_id, vendor_id=principal.vendor_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if view is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No interest in this listing (or listing not visible)",
        )
    return view


@router.post("/listings/{listing_id}/book_appointment", status_code=status.HTTP_201_CREATED)
def book_appointment(
    listing_id: UUID,
    db: Session = Depends(get_db),
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
) -> dict:
    try:
        interest = listings_service.book_appointment(
            db, listing_id=listing_id, vendor_id=principal.vendor_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return {
        "interest_id": str(interest.id),
        "listing_id": str(interest.listing_id),
        "appointment_booked_at": (
            interest.appointment_booked_at.isoformat()
            if interest.appointment_booked_at
            else None
        ),
    }


@router.get("/billing/leads", response_model=list[VendorBillingEntry])
def billing_leads(
    db: Session = Depends(get_db),
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
):
    rows = listings_service.vendor_billing(db, vendor_id=principal.vendor_id)
    return [VendorBillingEntry.model_validate(r) for r in rows]
