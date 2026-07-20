"""Application message threads — the in-app Slack replacement.

MSO vendors (clinics) and PaySpyre admins talk back and forth about a specific
loan application. Each thread is anchored to one
``platform_credit_applications`` row; the borrower/applicant is NOT a
participant (internal ops channel only).

Two tables:

* :class:`PlatformApplicationMessage` — one posted message. ``sender_kind`` is
  ``'vendor'`` (a clinic staffer) or ``'admin'`` (a PaySpyre operator); stored
  as text (a closed set validated at the API layer, matching the String-status
  convention of ``platform_application_documents``).
* :class:`PlatformApplicationMessageRead` — a per-(application, user) last-read
  watermark. "Unread" for a user = messages from the OTHER side created after
  that user's watermark, so the badge/unread-count queries stay cheap.
"""
from uuid import uuid4

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base

# sender_kind values (closed set; validated at the API/schema layer).
SENDER_KIND_VENDOR = "vendor"
SENDER_KIND_ADMIN = "admin"


class PlatformApplicationMessage(Base):
    __tablename__ = "platform_application_messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    application_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_credit_applications.id", ondelete="CASCADE"),
        nullable=False,
    )
    sender_kind = Column(String, nullable=False)  # 'vendor' | 'admin'
    sender_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index(
            "ix_platform_application_messages_application_id_created_at",
            "application_id",
            "created_at",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<PlatformApplicationMessage(id={self.id}, "
            f"application_id={self.application_id}, sender_kind={self.sender_kind})>"
        )


class PlatformApplicationMessageRead(Base):
    """Per-(application, user) last-read watermark (a read receipt, updated in
    place — not an append-only event). Unread = other-side messages newer than
    ``last_read_at``."""

    __tablename__ = "platform_application_message_reads"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    application_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_credit_applications.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    last_read_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "application_id", "user_id", name="uq_app_message_read_app_user"
        ),
    )
