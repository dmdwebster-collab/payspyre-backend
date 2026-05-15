from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class DocumentUploadRequest(BaseModel):
    document_type: str = Field(..., pattern="^(id_front|id_back|selfie|proof_of_income|bank_statement|tax_return|utility_bill|business_license|incorporation_document|shareholder_agreement|other)$")
    document_subtype: str | None = None
    title: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    file_name: str = Field(..., min_length=1, max_length=255)
    file_content_type: str | None = None
    file_size_bytes: int | None = Field(None, gt=0)
    metadata: dict | None = None
    tags: list[str] | None = None
    loan_application_id: UUID | None = None
    borrower_id: UUID | None = None
    vendor_id: UUID | None = None


class DocumentUploadResponse(BaseModel):
    upload_url: str
    upload_fields: dict
    document_id: UUID
    object_key: str
    expires_in: int


class DocumentConfirmUpload(BaseModel):
    document_id: UUID
    file_size_bytes: int
    file_hash: str | None = None


class DocumentResponse(BaseModel):
    id: UUID
    loan_application_id: UUID | None
    borrower_id: UUID | None
    vendor_id: UUID | None
    document_type: str
    document_subtype: str | None
    title: str
    description: str | None
    status: str
    s3_object_key: str
    s3_bucket: str
    s3_version_id: str | None
    file_name: str
    file_size_bytes: int | None
    file_content_type: str | None
    file_hash: str | None
    storage_class: str
    retention_period_days: int
    expires_at: datetime | None
    uploaded_by: UUID | None
    uploaded_at: datetime | None
    verified_by: UUID | None
    verified_at: datetime | None
    verification_notes: str | None
    metadata: dict | None
    tags: list[str] | None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class DocumentDownloadRequest(BaseModel):
    document_id: UUID
    expires_in: int = Field(default=3600, ge=60, le=86400)


class DocumentDownloadResponse(BaseModel):
    download_url: str
    expires_in: int
    document: DocumentResponse


class DocumentListResponse(BaseModel):
    documents: list[DocumentResponse]
    total: int
    page: int
    page_size: int


class DocumentVerificationRequest(BaseModel):
    document_id: UUID
    status: str = Field(..., pattern="^(verified|rejected)$")
    notes: str | None = None


class DocumentVerificationResponse(BaseModel):
    document_id: UUID
    status: str
    verified_at: datetime
    verified_by: UUID


class DocumentVersionResponse(BaseModel):
    id: UUID
    document_id: UUID
    version_number: int
    s3_object_key: str
    s3_version_id: str | None
    file_name: str
    file_size_bytes: int | None
    file_content_type: str | None
    file_hash: str | None
    storage_class: str
    change_reason: str | None
    created_by: UUID | None
    created_at: datetime
    metadata: dict | None

    class Config:
        from_attributes = True


class DocumentMetadataResponse(BaseModel):
    document_id: UUID
    content_type: str | None
    content_length: int | None
    last_modified: datetime | None
    metadata: dict
    etag: str | None


class DocumentDeleteResponse(BaseModel):
    document_id: UUID
    deleted_at: datetime
    success: bool