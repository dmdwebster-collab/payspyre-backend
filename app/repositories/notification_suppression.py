"""Repository for the ``notification_suppression`` table — P7.4b.

Sub-ms ``is_suppressed`` lookup (indexed on ``recipient_redacted``) and an
idempotent ``upsert`` that uses the ``(contact_method, recipient_redacted)``
unique constraint to swallow duplicate vendor deliveries cleanly.

Reuses ``_hash_recipient`` from the real notification dispatcher so the
hash produced at suppression time exactly matches the hash the dispatcher
checks at send time — both must agree byte-for-byte or the gate misfires.
"""
from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.platform.notification_suppression import NotificationSuppression
from app.services.real_notification_dispatcher import _hash_recipient


class NotificationSuppressionRepo:
    def __init__(self, db: Session) -> None:
        self.db = db

    def is_suppressed(self, contact_method: str, recipient: str) -> bool:
        """Has this (channel, recipient) ever been suppressed? Sub-ms via the
        ``ix_notification_suppression_recipient`` index."""
        redacted = _hash_recipient(contact_method, recipient)
        return (
            self.db.query(NotificationSuppression.id)
            .filter(
                NotificationSuppression.contact_method == contact_method,
                NotificationSuppression.recipient_redacted == redacted,
            )
            .first()
            is not None
        )

    def is_suppressed_by_hash(self, contact_method: str, recipient_redacted: str) -> bool:
        """Variant used by the inbound webhook endpoints — they already have
        the redacted hash from the original ``notification_sent`` event."""
        return (
            self.db.query(NotificationSuppression.id)
            .filter(
                NotificationSuppression.contact_method == contact_method,
                NotificationSuppression.recipient_redacted == recipient_redacted,
            )
            .first()
            is not None
        )

    def upsert(
        self,
        *,
        contact_method: str,
        recipient_redacted: str,
        reason: str,
        vendor: str,
        vendor_event_id: str | None = None,
    ) -> None:
        """Idempotent insert. A duplicate (contact_method, recipient_redacted)
        triggers ``ON CONFLICT DO NOTHING`` so the first reason for a recipient
        sticks — second deliveries of the same vendor event are no-ops."""
        stmt = insert(NotificationSuppression).values(
            contact_method=contact_method,
            recipient_redacted=recipient_redacted,
            reason=reason,
            vendor=vendor,
            vendor_event_id=vendor_event_id,
        ).on_conflict_do_nothing(
            constraint="uq_notification_suppression_recipient",
        )
        self.db.execute(stmt)
        # Caller commits (the endpoint owns the unit-of-work boundary).
