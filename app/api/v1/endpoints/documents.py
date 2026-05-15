from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import get_db
from app.models.document import Document, DocumentVersion
from app.schemas.document import (
    DocumentConfirmUpload,
    DocumentDeleteResponse,
    DocumentDownloadRequest,
    DocumentDownloadResponse,
    DocumentListResponse,
    DocumentMetadataResponse,
    DocumentResponse,
    DocumentUploadRequest,
    DocumentUploadResponse,
    DocumentVerificationRequest,
    DocumentVerificationResponse,
    DocumentVersionResponse,
)
from app.services.storage import storage_service

router = APIRouter()


@router.post("/documents/upload/initiate", response_model=DocumentUploadResponse)
async def initiate_document_upload(
    data: DocumentUploadRequest,
    db: Session = Depends(get_db),
):
    entity_id = None
    if data.loan_application_id:
        entity_type = "loan_applications"
        entity_id = str(data.loan_application_id)
    elif data.borrower_id:
        entity_type = "borrowers"
        entity_id = str(data.borrower_id)
    elif data.vendor_id:
        entity_type = "vendors"
        entity_id = str(data.vendor_id)
    else:
        raise HTTPException(
            status_code=400,
            detail="Must provide either loan_application_id, borrower_id, or vendor_id",
        )

    presigned_data = storage_service.generate_presigned_upload_url(
        entity_type=entity_type,
        entity_id=entity_id,
        document_type=data.document_type,
        filename=data.file_name,
        content_type=data.file_content_type,
        max_file_size_mb=10,
    )

    document = Document(
        id=uuid4(),
        loan_application_id=data.loan_application_id,
        borrower_id=data.borrower_id,
        vendor_id=data.vendor_id,
        document_type=data.document_type,
        document_subtype=data.document_subtype,
        title=data.title,
        description=data.description,
        status="uploading",
        s3_object_key=presigned_data["object_key"],
        s3_bucket=storage_service._bucket_name,
        file_name=data.file_name,
        file_content_type=data.file_content_type,
        file_size_bytes=data.file_size_bytes,
        document_metadata=data.metadata,
        tags=data.tags if data.tags else None,
        expires_at=datetime.now(timezone.utc) + timedelta(days=2555),
    )

    db.add(document)
    db.commit()
    db.refresh(document)

    return DocumentUploadResponse(
        upload_url=presigned_data["url"],
        upload_fields=presigned_data["fields"],
        document_id=document.id,
        object_key=presigned_data["object_key"],
        expires_in=presigned_data["expires_in"],
    )


@router.post("/documents/upload/confirm", response_model=DocumentResponse)
async def confirm_document_upload(
    data: DocumentConfirmUpload,
    db: Session = Depends(get_db),
):
    document = db.query(Document).filter(Document.id == data.document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    if not storage_service.verify_object_exists(document.s3_object_key):
        raise HTTPException(status_code=400, detail="File not found in storage")

    s3_metadata = storage_service.get_object_metadata(document.s3_object_key)

    document.status = "uploaded"
    document.file_size_bytes = data.file_size_bytes
    document.file_hash = data.file_hash
    document.file_content_type = s3_metadata.get("content_type") or document.file_content_type
    document.uploaded_at = datetime.now(timezone.utc)
    document.file_hash = s3_metadata.get("etag", "").strip('"')

    db.commit()
    db.refresh(document)

    return document


@router.get("/documents/{document_id}", response_model=DocumentResponse)
async def get_document(document_id: UUID, db: Session = Depends(get_db)):
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    return document


@router.get("/documents", response_model=DocumentListResponse)
async def list_documents(
    loan_application_id: UUID | None = Query(None),
    borrower_id: UUID | None = Query(None),
    vendor_id: UUID | None = Query(None),
    document_type: str | None = Query(None),
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    query = db.query(Document)

    if loan_application_id:
        query = query.filter(Document.loan_application_id == loan_application_id)
    if borrower_id:
        query = query.filter(Document.borrower_id == borrower_id)
    if vendor_id:
        query = query.filter(Document.vendor_id == vendor_id)
    if document_type:
        query = query.filter(Document.document_type == document_type)
    if status:
        query = query.filter(Document.status == status)

    total = query.count()
    documents = query.order_by(Document.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()

    return DocumentListResponse(
        documents=documents,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/documents/download", response_model=DocumentDownloadResponse)
async def generate_download_url(
    data: DocumentDownloadRequest,
    db: Session = Depends(get_db),
):
    document = db.query(Document).filter(Document.id == data.document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    if document.status != "uploaded" and document.status != "verified":
        raise HTTPException(status_code=400, detail="Document not ready for download")

    download_url = storage_service.generate_presigned_download_url(
        object_key=document.s3_object_key,
        expires_in=data.expires_in,
    )

    return DocumentDownloadResponse(
        download_url=download_url,
        expires_in=data.expires_in,
        document=document,
    )


@router.get("/documents/{document_id}/metadata", response_model=DocumentMetadataResponse)
async def get_document_metadata(document_id: UUID, db: Session = Depends(get_db)):
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    s3_metadata = storage_service.get_object_metadata(document.s3_object_key)

    return DocumentMetadataResponse(
        document_id=document.id,
        content_type=s3_metadata.get("content_type"),
        content_length=s3_metadata.get("content_length"),
        last_modified=s3_metadata.get("last_modified"),
        metadata=s3_metadata.get("metadata", {}),
        etag=s3_metadata.get("etag"),
    )


@router.post("/documents/verify", response_model=DocumentVerificationResponse)
async def verify_document(
    data: DocumentVerificationRequest,
    db: Session = Depends(get_db),
):
    document = db.query(Document).filter(Document.id == data.document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    if document.status not in ["uploaded", "rejected"]:
        raise HTTPException(status_code=400, detail="Document cannot be verified in current state")

    document.status = data.status
    document.verified_at = datetime.now(timezone.utc)
    document.verification_notes = data.notes

    db.commit()
    db.refresh(document)

    return DocumentVerificationResponse(
        document_id=document.id,
        status=document.status,
        verified_at=document.verified_at,
        verified_by=document.verified_by,
    )


@router.get("/documents/{document_id}/versions", response_model=list[DocumentVersionResponse])
async def list_document_versions(document_id: UUID, db: Session = Depends(get_db)):
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    versions = db.query(DocumentVersion).filter(DocumentVersion.document_id == document_id).order_by(DocumentVersion.version_number.desc()).all()

    return versions


@router.delete("/documents/{document_id}", response_model=DocumentDeleteResponse)
async def delete_document(document_id: UUID, db: Session = Depends(get_db)):
    document = db.query(Document).filter(Document.id == document_id).first()
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")

    if document.status == "verified":
        raise HTTPException(status_code=400, detail="Cannot delete verified documents")

    storage_service.delete_object(document.s3_object_key)

    for version in document.versions:
        storage_service.delete_object(version.s3_object_key)

    db.delete(document)
    db.commit()

    return DocumentDeleteResponse(
        document_id=document.id,
        deleted_at=datetime.now(timezone.utc),
        success=True,
    )