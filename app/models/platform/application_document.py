from uuid import uuid4

from sqlalchemy import Column, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base


class PlatformApplicationDocument(Base):
    """A KYC document uploaded against an application (manual-fallback path).

    The bytes live in object storage (DigitalOcean Spaces); this row holds only
    the object key + metadata. ``status`` is 'pending' when the presigned upload
    URL is issued and 'uploaded' once the client confirms the PUT succeeded.
    """

    __tablename__ = "platform_application_documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    application_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_credit_applications.id"),
        nullable=False,
        index=True,
    )
    doc_type = Column(String, nullable=False)        # id_front | id_back | selfie
    object_key = Column(String, nullable=False)
    content_type = Column(String, nullable=True)
    status = Column(String, nullable=False, server_default="pending")  # pending | uploaded
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    uploaded_at = Column(DateTime(timezone=True), nullable=True)
