"""Borrower profile (WS-J items 2 + 3) — versioned edits + write-only ID images.

Versioned edits (Dave, video 11): "the new address would become current, and
the old address would become former. Same with employer information." Edits go
through ``PatientProfileService._write_field`` semantics — a NEW current
``platform_patient_fields`` row is written and the prior one is superseded
(``is_current=False`` + supersession link), NEVER overwritten. Current AND
former values are exposed in reads (3-year history per Dave — rows are
retained indefinitely; presentation-side windows come later with retention
policy work).

ID images (Dave): "restrict this to upload new ID and not whatever the
existing one is." The borrower can UPLOAD ID front/back (self + co-borrower)
via presigned PUT and list METADATA — but no borrower endpoint returns an
object key, a download URL, or the bytes. Reads are staff-only (audited admin
endpoint). Enforced at the endpoint layer: the read route simply does not
exist on the borrower surface.

Profile edits are SENSITIVE → gated by ``require_step_up`` (additive: no-op
for patients without 2FA).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.applicant.v1.deps import (
    ApplicantClaims,
    get_current_applicant,
    require_step_up,
)
from app.core.logging import get_logger
from app.db.base import get_db
from app.models.platform.borrower_portal import (
    ID_DOCUMENT_BUCKETS,
    PlatformPatientIdDocument,
)
from app.models.platform.event import PlatformEvent
from app.models.platform.patient import PlatformPatient
from app.models.platform.patient_field import PlatformPatientField
from app.schemas.patient import PatientFieldUpdateRequest
from app.services import document_storage
from app.services.patient_profile import PatientProfileService

logger = get_logger(__name__)
router = APIRouter(prefix="/profile", tags=["borrower-profile"])

# The borrower-editable profile fields (video 11 §1.3: email, phones, address,
# employment; full name is display-only). Address + employment are structured
# JSON values under ONE field key each so a change versions as one unit.
EDITABLE_FIELD_KEYS = (
    "email",
    "main_phone",
    "alternative_phone",
    "address",
    "employment",
)

_ALLOWED_CONTENT = ("image/jpeg", "image/png", "image/heic", "image/webp", "application/pdf")


# --- schemas ----------------------------------------------------------------


class AddressIn(BaseModel):
    street: str = Field(..., min_length=1, max_length=200)
    unit: Optional[str] = Field(None, max_length=50)
    city: str = Field(..., min_length=1, max_length=100)
    province: str = Field(..., min_length=2, max_length=50)
    postal_code: str = Field(..., min_length=3, max_length=10)


class EmploymentIn(BaseModel):
    status: str = Field(..., min_length=1, max_length=50)  # e.g. "Employed"
    employer_name: Optional[str] = Field(None, max_length=200)


class ProfileUpdateBody(BaseModel):
    email: Optional[str] = Field(None, min_length=3, max_length=320)
    main_phone: Optional[str] = Field(None, max_length=20)
    alternative_phone: Optional[str] = Field(None, max_length=20)
    address: Optional[AddressIn] = None
    employment: Optional[EmploymentIn] = None


class FieldVersion(BaseModel):
    field_key: str
    value: Any
    is_current: bool
    effective_from: datetime
    superseded_at: Optional[datetime] = None


class ProfileResponse(BaseModel):
    patient_id: UUID
    full_name: Optional[str] = None  # display-only, never borrower-editable
    current: dict[str, Any]
    updated_keys: list[str] = []


class ProfileHistoryResponse(BaseModel):
    versions: list[FieldVersion]


class IdDocUploadBody(BaseModel):
    bucket: str
    content_type: str
    filename: Optional[str] = Field(None, max_length=255)
    size_bytes: Optional[int] = Field(None, ge=0)


class IdDocUploadResponse(BaseModel):
    document_id: UUID
    upload_url: str


class IdDocConfirmResponse(BaseModel):
    document_id: UUID
    status: str
    is_current: bool


class IdDocMeta(BaseModel):
    """Metadata ONLY — deliberately no object key and no URL of any kind."""

    document_id: UUID
    bucket: str
    filename: Optional[str] = None
    size_bytes: Optional[int] = None
    status: str
    is_current: bool
    uploaded_at: Optional[datetime] = None


# --- helpers ----------------------------------------------------------------


def _get_patient(db: Session, patient_id: UUID) -> PlatformPatient:
    patient = db.query(PlatformPatient).filter(PlatformPatient.id == patient_id).first()
    if patient is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    return patient


def _current_profile_fields(db: Session, patient_id: UUID) -> dict[str, Any]:
    rows = (
        db.query(PlatformPatientField)
        .filter(
            PlatformPatientField.patient_id == patient_id,
            PlatformPatientField.field_key.in_(EDITABLE_FIELD_KEYS),
            PlatformPatientField.is_current.is_(True),
            PlatformPatientField.source == "self_reported",
        )
        .order_by(PlatformPatientField.verified_at.desc())
        .all()
    )
    current: dict[str, Any] = {}
    for row in rows:
        current.setdefault(row.field_key, row.field_value)
    return current


# --- profile routes ---------------------------------------------------------


@router.get("", response_model=ProfileResponse)
def get_profile(
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
):
    patient = _get_patient(db, claims.patient_id)
    name_parts = [p for p in (patient.legal_first_name, patient.legal_last_name) if p]
    current = _current_profile_fields(db, claims.patient_id)
    # Denormalized fallbacks so a patient with no versioned rows yet still sees
    # their contact details.
    current.setdefault("email", patient.email)
    current.setdefault("main_phone", patient.phone_e164)
    return ProfileResponse(
        patient_id=patient.id,
        full_name=" ".join(name_parts) or None,
        current=current,
    )


@router.put("", response_model=ProfileResponse)
def update_profile(
    body: ProfileUpdateBody,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(require_step_up),
):
    """Versioned profile edit (SENSITIVE — step-up gated when 2FA is active).

    Every provided field writes a NEW current ``platform_patient_fields``
    version; the prior current becomes former (supersession chain). Nothing is
    overwritten in place. Denormalized ``platform_patients`` contact columns
    are refreshed for fast lookups (the versioned rows are the history)."""
    patient = _get_patient(db, claims.patient_id)
    service = PatientProfileService(db)

    updates: dict[str, Any] = {}
    if body.email is not None:
        updates["email"] = body.email.strip().lower()
    if body.main_phone is not None:
        updates["main_phone"] = body.main_phone.strip()
    if body.alternative_phone is not None:
        updates["alternative_phone"] = body.alternative_phone.strip()
    if body.address is not None:
        updates["address"] = body.address.model_dump()
    if body.employment is not None:
        updates["employment"] = body.employment.model_dump()

    if not updates:
        raise HTTPException(status_code=422, detail="No profile fields provided")

    for key, value in updates.items():
        service.update_patient_field(
            claims.patient_id,
            PatientFieldUpdateRequest(field_key=key, value=value, source="self_reported"),
            actor="patient",
        )

    # Refresh the denormalized contact columns.
    if "email" in updates:
        patient.email = updates["email"]
    if "main_phone" in updates:
        patient.phone_e164 = updates["main_phone"]
    db.add(
        PlatformEvent(
            event_type="borrower_profile_updated",
            actor="patient",
            patient_id=claims.patient_id,
            payload={
                "v": 1,
                "actor": {"type": "patient", "id": str(claims.patient_id)},
                "patient_id": str(claims.patient_id),
                # Keys only — never PII values — in the WORM event log.
                "updated_keys": sorted(updates.keys()),
            },
        )
    )
    db.commit()
    logger.info(
        "borrower_profile_updated",
        patient_id=str(claims.patient_id),
        keys=sorted(updates.keys()),
    )

    name_parts = [p for p in (patient.legal_first_name, patient.legal_last_name) if p]
    return ProfileResponse(
        patient_id=patient.id,
        full_name=" ".join(name_parts) or None,
        current=_current_profile_fields(db, claims.patient_id),
        updated_keys=sorted(updates.keys()),
    )


@router.get("/history", response_model=ProfileHistoryResponse)
def get_profile_history(
    field_key: Optional[str] = None,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
):
    """Current + FORMER versions of the borrower's editable fields (newest
    first). Dave's current/former model: prior addresses and employers stay on
    file — this surfaces them."""
    if field_key is not None and field_key not in EDITABLE_FIELD_KEYS:
        raise HTTPException(
            status_code=422, detail=f"field_key must be one of {list(EDITABLE_FIELD_KEYS)}"
        )
    q = (
        db.query(PlatformPatientField)
        .filter(
            PlatformPatientField.patient_id == claims.patient_id,
            PlatformPatientField.field_key.in_(
                (field_key,) if field_key else EDITABLE_FIELD_KEYS
            ),
        )
        .order_by(PlatformPatientField.verified_at.desc())
    )
    return ProfileHistoryResponse(
        versions=[
            FieldVersion(
                field_key=row.field_key,
                value=row.field_value,
                is_current=row.is_current,
                effective_from=row.verified_at,
                superseded_at=row.superseded_at,
            )
            for row in q.all()
        ]
    )


# --- ID documents (WRITE-ONLY from the portal) ------------------------------


@router.post("/id-documents/upload-url", response_model=IdDocUploadResponse)
def request_id_upload_url(
    body: IdDocUploadBody,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
):
    """Presigned PUT for a new ID image (borrower or co-borrower, front/back).

    503 until object storage is configured. The response carries the upload
    URL only — there is NO corresponding borrower download route, by design."""
    if body.bucket not in ID_DOCUMENT_BUCKETS:
        raise HTTPException(
            status_code=422, detail=f"bucket must be one of {list(ID_DOCUMENT_BUCKETS)}"
        )
    if body.content_type not in _ALLOWED_CONTENT:
        raise HTTPException(status_code=422, detail="Unsupported content type")
    if not document_storage.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Document upload is not available yet.",
        )
    _get_patient(db, claims.patient_id)

    document_id = uuid4()
    object_key = f"patients/{claims.patient_id}/id/{body.bucket}/{document_id}"
    url = document_storage.presigned_put_url(object_key, body.content_type)
    db.add(
        PlatformPatientIdDocument(
            id=document_id,
            patient_id=claims.patient_id,
            bucket=body.bucket,
            object_key=object_key,
            content_type=body.content_type,
            filename=body.filename,
            size_bytes=body.size_bytes,
            status="pending",
            is_current=False,  # becomes current on confirm
        )
    )
    db.commit()
    return IdDocUploadResponse(document_id=document_id, upload_url=url)


@router.post("/id-documents/{document_id}/confirm", response_model=IdDocConfirmResponse)
def confirm_id_upload(
    document_id: UUID,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
):
    """Confirm the PUT succeeded: the new upload becomes the CURRENT document
    of its bucket; the previous current becomes FORMER (retained, never
    deleted — Dave's ongoing new+former ID file)."""
    doc = (
        db.query(PlatformPatientIdDocument)
        .filter(
            PlatformPatientIdDocument.id == document_id,
            PlatformPatientIdDocument.patient_id == claims.patient_id,
        )
        .first()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")

    now = datetime.now(timezone.utc)
    previous_current = (
        db.query(PlatformPatientIdDocument)
        .filter(
            PlatformPatientIdDocument.patient_id == claims.patient_id,
            PlatformPatientIdDocument.bucket == doc.bucket,
            PlatformPatientIdDocument.is_current.is_(True),
            PlatformPatientIdDocument.id != doc.id,
        )
        .all()
    )
    for prev in previous_current:
        prev.is_current = False
        prev.superseded_at = now
        prev.superseded_by_id = doc.id

    doc.status = "uploaded"
    doc.uploaded_at = now
    doc.is_current = True
    db.commit()
    return IdDocConfirmResponse(document_id=doc.id, status=doc.status, is_current=True)


@router.get("/id-documents", response_model=list[IdDocMeta])
def list_id_documents(
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
):
    """Metadata list (filename/size/status/current) — NEVER a download URL or
    object key. The borrower surface has no read route for the bytes."""
    docs = (
        db.query(PlatformPatientIdDocument)
        .filter(PlatformPatientIdDocument.patient_id == claims.patient_id)
        .order_by(PlatformPatientIdDocument.created_at.desc())
        .all()
    )
    return [
        IdDocMeta(
            document_id=d.id,
            bucket=d.bucket,
            filename=d.filename,
            size_bytes=d.size_bytes,
            status=d.status,
            is_current=d.is_current,
            uploaded_at=d.uploaded_at,
        )
        for d in docs
    ]
