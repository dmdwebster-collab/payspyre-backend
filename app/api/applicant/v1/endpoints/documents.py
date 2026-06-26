"""KYC document upload endpoints (manual-application fallback).

The browser uploads the file DIRECTLY to object storage via a short-TTL presigned
PUT URL issued here — the bytes never pass through the API. We persist only a
``PlatformApplicationDocument`` (object key + status). INERT until SPACES_* is
configured: ``request_upload_url`` returns 503 when storage is unconfigured.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.applicant.v1.deps import (
    ApplicantClaims,
    get_current_applicant,
    require_app_scope,
)
from app.core.logging import get_logger
from app.db.base import get_db
from app.models.platform.application_document import PlatformApplicationDocument
from app.models.platform.credit_application import PlatformCreditApplication
from app.services import document_storage

logger = get_logger(__name__)
router = APIRouter(prefix="/applications", tags=["applicant-documents"])

# Photo/scan content types we accept for an ID or selfie upload.
_ALLOWED_CONTENT = ("image/jpeg", "image/png", "image/heic", "image/webp", "application/pdf")


class UploadUrlRequest(BaseModel):
    doc_type: str
    content_type: str


class UploadUrlResponse(BaseModel):
    document_id: UUID
    upload_url: str
    object_key: str


class ConfirmResponse(BaseModel):
    document_id: UUID
    status: str


class DocumentSummary(BaseModel):
    document_id: UUID
    doc_type: str
    status: str


def _require_application(db: Session, application_id: UUID) -> PlatformCreditApplication:
    application = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.id == application_id)
        .first()
    )
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")
    return application


@router.post("/{application_id}/documents/upload-url", response_model=UploadUrlResponse)
def request_upload_url(
    application_id: UUID,
    body: UploadUrlRequest,
    claims: ApplicantClaims = Depends(get_current_applicant),
    db: Session = Depends(get_db),
):
    """Issue a short-TTL presigned URL the client PUTs the document to, and record
    a pending document row. 503 until document storage is configured."""
    require_app_scope(application_id, claims)
    if body.doc_type not in document_storage.DOCUMENT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"doc_type must be one of {list(document_storage.DOCUMENT_TYPES)}",
        )
    if body.content_type not in _ALLOWED_CONTENT:
        raise HTTPException(status_code=422, detail="Unsupported content type")
    if not document_storage.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Document upload is not available yet.",
        )
    _require_application(db, application_id)

    document_id = uuid4()
    object_key = document_storage.build_object_key(application_id, body.doc_type, document_id)
    url = document_storage.presigned_put_url(object_key, body.content_type)
    db.add(
        PlatformApplicationDocument(
            id=document_id, application_id=application_id, doc_type=body.doc_type,
            object_key=object_key, content_type=body.content_type, status="pending",
        )
    )
    db.commit()
    return UploadUrlResponse(document_id=document_id, upload_url=url, object_key=object_key)


@router.post(
    "/{application_id}/documents/{document_id}/confirm", response_model=ConfirmResponse
)
def confirm_upload(
    application_id: UUID,
    document_id: UUID,
    claims: ApplicantClaims = Depends(get_current_applicant),
    db: Session = Depends(get_db),
):
    """Mark a document uploaded once the client's PUT to storage has succeeded."""
    require_app_scope(application_id, claims)
    doc = (
        db.query(PlatformApplicationDocument)
        .filter(
            PlatformApplicationDocument.id == document_id,
            PlatformApplicationDocument.application_id == application_id,
        )
        .first()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    doc.status = "uploaded"
    doc.uploaded_at = datetime.now(timezone.utc)
    db.commit()
    return ConfirmResponse(document_id=doc.id, status=doc.status)


@router.get("/{application_id}/documents", response_model=list[DocumentSummary])
def list_documents(
    application_id: UUID,
    claims: ApplicantClaims = Depends(get_current_applicant),
    db: Session = Depends(get_db),
):
    """The applicant's own document list (status only — no download URLs here)."""
    require_app_scope(application_id, claims)
    docs = (
        db.query(PlatformApplicationDocument)
        .filter(PlatformApplicationDocument.application_id == application_id)
        .order_by(PlatformApplicationDocument.created_at)
        .all()
    )
    return [
        DocumentSummary(document_id=d.id, doc_type=d.doc_type, status=d.status) for d in docs
    ]
