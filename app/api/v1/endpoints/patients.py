from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_roles
from app.db.base import get_db
from app.models.user import User
from app.schemas.patient import (
    PatientQuickstartRequest,
    PatientFieldUpdateRequest,
    PatientProfileResponse,
    DiscrepancyResponse,
    PatientFieldHistoryResponse,
    MessageResponse,
)
from app.services.patient_profile import PatientProfileService

router = APIRouter()


@router.post(
    "/quickstart",
    response_model=PatientProfileResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create patient profile (quick-start)",
    description="Minimum viable profile: name + email required. All fields tagged as 'self_reported' source.",
)
async def create_patient_quickstart(
    data: PatientQuickstartRequest,
    db: Session = Depends(get_db),
):
    """
    Quick-start patient profile creation.

    Creates a patient record with self-reported identity data.
    - legal_first_name: required
    - legal_last_name: required
    - email: required
    - phone_e164: optional
    - dob: optional
    """
    service = PatientProfileService(db)
    try:
        result = service.create_patient_quickstart(data)
        profile_data = service.get_patient_profile(result.id)
        return _build_profile_response(profile_data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get(
    "/{patient_id}",
    response_model=PatientProfileResponse,
    summary="Get patient profile",
    description="Full patient profile with current source-tagged fields.",
)
async def get_patient_profile(
    patient_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Retrieve patient profile with all current source-tagged fields.
    """
    service = PatientProfileService(db)
    try:
        profile_data = service.get_patient_profile(patient_id)
        return _build_profile_response(profile_data)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.patch(
    "/{patient_id}/fields",
    response_model=MessageResponse,
    summary="Update patient field",
    description="Update a single patient field with source tagging. Detects discrepancies.",
)
async def update_patient_field(
    patient_id: UUID,
    data: PatientFieldUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Update a patient field with source tagging.

    - field_key: The field to update (e.g., 'legal_first_name', 'email', 'dob')
    - value: New value for the field
    - source: One of: self_reported, practice_prefill, id_doc, bureau
    - confidence: Optional 0.0-1.0 confidence score

    If a previous value exists from a different source and values differ,
    a discrepancy event is logged.
    """
    service = PatientProfileService(db)
    try:
        service.update_patient_field(
            patient_id=patient_id,
            data=data,
            actor=str(current_user.id) if current_user else "system",
        )
        return MessageResponse(message="Field updated successfully")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get(
    "/{patient_id}/discrepancies",
    response_model=DiscrepancyResponse,
    summary="List field discrepancies",
    description="Returns all fields with conflicting values from different sources.",
)
async def get_discrepancies(
    patient_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get all field discrepancies for a patient.

    Returns fields that have different values from different sources.
    Example: practice_prefill says 'John' but self_reported says 'Jon'.
    """
    service = PatientProfileService(db)
    try:
        discrepancies = service.detect_discrepancies(patient_id)
        return DiscrepancyResponse(
            patient_id=patient_id,
            discrepancies=discrepancies,
            total_count=len(discrepancies),
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get(
    "/{patient_id}/fields/{field_key}/history",
    response_model=PatientFieldHistoryResponse,
    summary="Get field history",
    description="Full immutable history of a field including superseded values.",
)
async def get_field_history(
    patient_id: UUID,
    field_key: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Get the complete history of a specific field.

    Returns all values ever set for this field, ordered newest first.
    Includes superseded values for audit trail.
    """
    service = PatientProfileService(db)
    try:
        history = service.get_field_history(patient_id, field_key)
        return PatientFieldHistoryResponse(field_key=field_key, history=history)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


def _build_profile_response(profile_data: dict) -> PatientProfileResponse:
    """Build PatientProfileResponse from service layer output."""
    patient = profile_data["patient"]
    fields = profile_data["fields"]

    return PatientProfileResponse(
        id=patient.id,
        legal_first_name=patient.legal_first_name,
        legal_last_name=patient.legal_last_name,
        dob=patient.dob,
        email=patient.email,
        phone_e164=patient.phone_e164,
        verification_depth=patient.verification_depth,
        lead_state=patient.lead_state,
        created_at=patient.created_at,
        updated_at=patient.updated_at,
        fields=fields,
    )
