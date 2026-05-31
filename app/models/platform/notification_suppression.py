"""``notification_suppression`` model — P7.4b.

Populated by the Twilio + Resend inbound status-callback endpoints when a
delivery indicates the recipient is unreachable / opted-out / complaining.
Consulted at send time by ``RealNotificationDispatcher`` before any vendor
HTTP call, gated by ``settings.USE_SUPPRESSION_CHECK``.

The recipient is stored only as the same ``recipient_redacted`` SHA-256 hash
the dispatcher uses in the ``notification_sent`` event payload — Hard Rule #6
forbids raw PII in queryable tables.
"""
from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, Text, UniqueConstraint, func

from app.db.base import Base


class NotificationSuppression(Base):
    __tablename__ = "notification_suppression"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    contact_method = Column(Text, nullable=False)        # "sms" | "email"
    recipient_redacted = Column(Text, nullable=False)    # sha256(canonical)
    reason = Column(Text, nullable=False)                # "opt_out" | "hard_bounce" | "invalid_recipient" | "spam_complaint"
    vendor = Column(Text, nullable=False)                # "twilio" | "resend"
    vendor_event_id = Column(Text, nullable=True)        # source nonce, for traceback
    suppressed_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "contact_method", "recipient_redacted",
            name="uq_notification_suppression_recipient",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<NotificationSuppression(channel={self.contact_method!r}, "
            f"reason={self.reason!r}, vendor={self.vendor!r})>"
        )
