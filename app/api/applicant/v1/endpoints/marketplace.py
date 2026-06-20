"""Applicant-side marketplace router (spec §6.3, patient surface).

The spec's "patient" actor maps onto this applicant API surface. Every endpoint
is authenticated with the patient JWT (``get_current_applicant``) and scoped to
``claims.patient_id`` — a patient can only act on their OWN listings. Owner
scoping is enforced in the service layer (PermissionError -> 403 here).

Service exception mapping:
  PermissionError          -> 403
  LookupError / not-found  -> 404
  ValueError               -> 400
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.applicant.v1.deps import ApplicantClaims, get_current_applicant
from app.api.marketplace_schemas import (
    CreateListingRequest,
    InterestedClinic,
    ListingSummary,
    SelectClinicRequest,
)
from app.db.base import get_db
from app.services.marketplace import listings as listings_service

router = APIRouter(prefix="/marketplace", tags=["applicant-marketplace"])


@router.post("/listings", response_model=ListingSummary, status_code=status.HTTP_201_CREATED)
def create_listing(
    body: CreateListingRequest,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
):
    try:
        listing = listings_service.create_listing(
            db,
            patient_id=claims.patient_id,
            treatment_categories=body.treatment_categories,
            treatment_urgency=body.treatment_urgency,
            estimated_budget_cents=body.estimated_budget_cents,
            location_postal_code=body.location_postal_code,
            max_travel_km=body.max_travel_km,
            consent_acknowledged=body.consent_acknowledged,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return ListingSummary.model_validate(listing)


@router.get(
    "/listings/{listing_id}/interested_clinics",
    response_model=list[InterestedClinic],
)
def interested_clinics(
    listing_id: UUID,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
):
    try:
        rows = listings_service.interested_clinics(
            db, listing_id=listing_id, patient_id=claims.patient_id
        )
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return [InterestedClinic.model_validate(r) for r in rows]


@router.post("/listings/{listing_id}/select_clinic", response_model=ListingSummary)
def select_clinic(
    listing_id: UUID,
    body: SelectClinicRequest,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
):
    try:
        listing = listings_service.select_clinic(
            db,
            listing_id=listing_id,
            patient_id=claims.patient_id,
            vendor_id=body.vendor_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return ListingSummary.model_validate(listing)


@router.post("/listings/{listing_id}/pause", response_model=ListingSummary)
def pause_listing(
    listing_id: UUID,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
):
    try:
        listing = listings_service.pause_listing(
            db, listing_id=listing_id, patient_id=claims.patient_id
        )
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return ListingSummary.model_validate(listing)
