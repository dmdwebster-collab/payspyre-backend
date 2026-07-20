"""Append-only communications log — Dave's mandate #4 (WS-A comms hub).

One row per communication the borrower actually received (email / SMS /
in-app dashboard notification, recorded at send time with the FULL rendered
body) or per offline contact staff logged (phone call / in-person, mandatory
comment). "Legal evidence: every email/SMS/dashboard notification stored with
the exact message body, per account, forever — used in collections and court."

Append-only is enforced at the DATABASE level (migration 055 installs a
trigger blocking every UPDATE and DELETE, the ``platform_events`` pattern), so
there is deliberately no application code path that mutates or deletes a row.

Unlike ``platform_events`` (which stores only a recipient hash), the raw
recipient and body ARE the evidence here and are stored verbatim — with one
exception: bodies carrying credentials (magic-link sign-in codes) are stored
as ``<Hidden for privacy purposes>``, the on-camera Turnkey behavior.
"""
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base

# Closed sets, validated at the API/service layer (String-status convention).
CHANNEL_EMAIL = "email"
CHANNEL_SMS = "sms"
CHANNEL_DASHBOARD = "dashboard"
CHANNEL_OFFLINE = "offline"
CHANNELS = (CHANNEL_EMAIL, CHANNEL_SMS, CHANNEL_DASHBOARD, CHANNEL_OFFLINE)

DIRECTION_OUTBOUND = "outbound"
DIRECTION_INBOUND = "inbound"
DIRECTIONS = (DIRECTION_OUTBOUND, DIRECTION_INBOUND)


class PlatformCommunicationLog(Base):
    __tablename__ = "platform_communications_log"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    channel = Column(String, nullable=False)  # email | sms | dashboard | offline
    direction = Column(String, nullable=False, server_default=DIRECTION_OUTBOUND)
    # Raw recipient (email address / E.164 phone); NULL for dashboard/offline.
    recipient = Column(String, nullable=True)
    subject = Column(String, nullable=True)
    # FULL rendered message body — never truncated.
    body = Column(Text, nullable=False)
    # notification_render.NOTIFICATION_TYPES registry key; NULL for offline.
    notification_type = Column(String, nullable=True)
    # Plain FKs (no ondelete): evidence rows must never be cascade-deleted or
    # nulled out — a parent hard-delete is refused instead.
    patient_id = Column(
        UUID(as_uuid=True), ForeignKey("platform_patients.id"), nullable=True
    )
    application_id = Column(
        UUID(as_uuid=True), ForeignKey("platform_credit_applications.id"), nullable=True
    )
    loan_id = Column(UUID(as_uuid=True), ForeignKey("platform_loans.id"), nullable=True)
    # 'system' for automated sends, else the acting staff user's email/id.
    sent_by = Column(String, nullable=False, server_default="system")
    sent_by_user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    comment = Column(Text, nullable=True)  # mandatory for offline (API-enforced)
    # Offline-contact fields (TL "Log message": Date / Method / Purpose / Result).
    contact_method = Column(String, nullable=True)
    purpose = Column(String, nullable=True)
    result = Column(String, nullable=True)
    status = Column(String, nullable=False, server_default="recorded")
    vendor = Column(String, nullable=True)
    vendor_message_id = Column(String, nullable=True)
    # The platform_events row this communication corresponds to, when any.
    source_event_id = Column(BigInteger, nullable=True)
    occurred_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index(
            "ix_platform_communications_log_patient",
            "patient_id",
            text("occurred_at DESC"),
        ),
        Index(
            "ix_platform_communications_log_application",
            "application_id",
            text("occurred_at DESC"),
        ),
        Index(
            "ix_platform_communications_log_loan",
            "loan_id",
            text("occurred_at DESC"),
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<PlatformCommunicationLog(id={self.id}, channel={self.channel}, "
            f"notification_type={self.notification_type}, patient_id={self.patient_id})>"
        )
