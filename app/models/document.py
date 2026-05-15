from datetime import datetime
from uuid import uuid4

from sqlalchemy import Column, DateTime, Enum, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from app.db.base import Base


class Document(Base):
    __tablename__ = "documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    loan_application_id = Column(UUID(as_uuid=True), ForeignKey("loan_applications.id"), nullable=True)
    borrower_id = Column(UUID(as_uuid=True), ForeignKey("borrowers.id"), nullable=True)
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=True)
    document_type = Column(
        Enum(
            "id_front",
            "id_back",
            "selfie",
            "proof_of_income",
            "bank_statement",
            "tax_return",
            "utility_bill",
            "business_license",
            "incorporation_document",
            "shareholder_agreement",
            "other",
            name="document_type",
        ),
        nullable=False,
    )
    document_subtype = Column(String(100), nullable=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(
        Enum("uploading", "uploaded", "verified", "rejected", name="document_status"),
        nullable=False,
        default="uploading",
    )
    s3_object_key = Column(String(500), nullable=False, unique=True)
    s3_bucket = Column(String(255), nullable=False)
    s3_version_id = Column(String(255), nullable=True)
    file_name = Column(String(255), nullable=False)
    file_size_bytes = Column(Integer, nullable=True)
    file_content_type = Column(String(100), nullable=True)
    file_hash = Column(String(64), nullable=True)
    storage_class = Column(String(50), default="STANDARD")
    retention_period_days = Column(Integer, default=2555)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    uploaded_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    uploaded_at = Column(DateTime(timezone=True), nullable=True)
    verified_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    verification_notes = Column(Text, nullable=True)
    document_metadata = Column(JSONB, nullable=True)
    tags = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    versions = relationship("DocumentVersion", back_populates="document", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_documents_loan_app", "loan_application_id"),
        Index("idx_documents_borrower", "borrower_id"),
        Index("idx_documents_vendor", "vendor_id"),
        Index("idx_documents_type", "document_type"),
        Index("idx_documents_status", "status"),
        Index("idx_documents_s3_key", "s3_object_key"),
        Index("idx_documents_expires", "expires_at"),
    )


class DocumentVersion(Base):
    __tablename__ = "document_versions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    version_number = Column(Integer, nullable=False)
    s3_object_key = Column(String(500), nullable=False)
    s3_version_id = Column(String(255), nullable=True)
    file_name = Column(String(255), nullable=False)
    file_size_bytes = Column(Integer, nullable=True)
    file_content_type = Column(String(100), nullable=True)
    file_hash = Column(String(64), nullable=True)
    storage_class = Column(String(50), default="STANDARD")
    change_reason = Column(Text, nullable=True)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    document_metadata = Column(JSONB, nullable=True)

    document = relationship("Document", back_populates="versions")

    __table_args__ = (
        Index("idx_document_versions_doc", "document_id"),
        Index("idx_document_versions_version", "document_id", "version_number"),
    )