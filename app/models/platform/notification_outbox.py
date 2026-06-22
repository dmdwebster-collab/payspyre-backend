"""``platform_notification_outbox`` model — durable notification retry queue (P7.4c).

A row is enqueued by ``RealNotificationDispatcher`` when a vendor send fails
*transiently* and ``settings.NOTIFICATION_OUTBOX_ENABLED`` is on. An out-of-band
worker (``scripts/process_notification_outbox.py``) polls ``status='pending'``
rows whose ``next_attempt_at`` is due, re-drives the send, and either marks them
``sent`` or reschedules with backoff — marking ``failed`` once retries are
exhausted (emitting a ``notification_retry_exhausted`` event, mirroring the
``adverse_action_notice_failed`` audit pattern).

PII boundaries (Hard Rule #6):
- The recipient is NOT stored here. The worker re-resolves phone/email from
  ``patient_id`` at retry time, exactly as the dispatcher does on the inline path.
- ``token`` is the plaintext magic-link code. It is short-lived (bounded by
  ``ttl_expires_at`` — the worker refuses to retry an expired item) and is the
  one piece of sensitive material the queue must hold to re-send. This is the
  deliberate tradeoff that keeps the queue feature-flagged OFF by default and
  flagged for human review before enablement (consider app-layer encryption of
  this column, or re-minting a fresh token per retry, before going live).
- ``last_error_redacted`` is run through the dispatcher's ``_redact_pii`` before
  it lands here.
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base


class PlatformNotificationOutbox(Base):
    __tablename__ = "platform_notification_outbox"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Routing — enough to re-drive a send via RealNotificationDispatcher.
    patient_id = Column(
        UUID(as_uuid=True), ForeignKey("platform_patients.id"), nullable=False
    )
    application_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_credit_applications.id"),
        nullable=True,
    )
    notification_type = Column(Text, nullable=False, server_default="magic_link")
    contact_method = Column(Text, nullable=False)            # "sms" | "email"
    token = Column(Text, nullable=False)                     # plaintext magic-link code (see PII note)
    ttl_expires_at = Column(DateTime(timezone=True), nullable=False)

    # Retry bookkeeping.
    status = Column(Text, nullable=False, server_default="pending")  # pending|sent|failed
    attempts = Column(Integer, nullable=False, server_default="1")   # incl. the inline send
    next_attempt_at = Column(DateTime(timezone=True), nullable=False)
    last_error_class = Column(Text, nullable=True)
    last_error_redacted = Column(Text, nullable=True)

    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:
        return (
            f"<PlatformNotificationOutbox(id={self.id}, status={self.status!r}, "
            f"channel={self.contact_method!r}, attempts={self.attempts})>"
        )
