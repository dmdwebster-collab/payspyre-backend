"""Repository for ``platform_notification_outbox`` — the durable retry queue (P7.4c).

Thin data-access layer used by two callers:
- ``RealNotificationDispatcher`` (enqueue on transient failure).
- ``scripts/process_notification_outbox.py`` (claim due rows, then mark
  sent / reschedule / fail).

The backoff math lives in ``app.services.notification_retry`` (pure, unit
tested); this module only persists the decisions.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.models.platform.notification_outbox import PlatformNotificationOutbox
from app.services.notification_retry import next_attempt_at


class NotificationOutboxRepo:
    def __init__(self, db: Session) -> None:
        self.db = db

    def enqueue(
        self,
        *,
        patient_id,
        application_id,
        contact_method: str,
        token: str,
        ttl_expires_at: datetime,
        attempts: int,
        next_attempt_at: datetime,
        last_error_class: str | None = None,
        last_error_redacted: str | None = None,
        notification_type: str = "magic_link",
    ) -> PlatformNotificationOutbox:
        """Insert a pending outbox row. Caller owns the commit boundary."""
        row = PlatformNotificationOutbox(
            patient_id=patient_id,
            application_id=application_id,
            notification_type=notification_type,
            contact_method=contact_method,
            token=token,
            ttl_expires_at=ttl_expires_at,
            status="pending",
            attempts=attempts,
            next_attempt_at=next_attempt_at,
            last_error_class=last_error_class,
            last_error_redacted=last_error_redacted,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def claim_due(self, *, now: datetime | None = None, limit: int = 50) -> list[PlatformNotificationOutbox]:
        """Return up to ``limit`` pending rows whose ``next_attempt_at`` is due.

        Uses ``SELECT ... FOR UPDATE SKIP LOCKED`` so concurrent workers don't
        process the same row. The caller must already be inside a transaction.
        """
        base = now or datetime.now(timezone.utc)
        return (
            self.db.query(PlatformNotificationOutbox)
            .filter(
                and_(
                    PlatformNotificationOutbox.status == "pending",
                    PlatformNotificationOutbox.next_attempt_at <= base,
                )
            )
            .order_by(PlatformNotificationOutbox.next_attempt_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
            .all()
        )

    def mark_sent(self, row: PlatformNotificationOutbox) -> None:
        row.status = "sent"
        self.db.add(row)
        self.db.flush()

    def reschedule(
        self,
        row: PlatformNotificationOutbox,
        *,
        delays: Sequence[int],
        error_class: str,
        error_redacted: str,
        now: datetime | None = None,
    ) -> None:
        """Bump the attempt count and push out ``next_attempt_at`` via backoff."""
        row.attempts = (row.attempts or 0) + 1
        row.last_error_class = error_class
        row.last_error_redacted = error_redacted
        row.next_attempt_at = next_attempt_at(row.attempts + 1, delays, now=now)
        self.db.add(row)
        self.db.flush()

    def mark_failed(
        self,
        row: PlatformNotificationOutbox,
        *,
        error_class: str,
        error_redacted: str,
    ) -> None:
        """Terminal: retries exhausted (or permanent error / expired token)."""
        row.attempts = (row.attempts or 0) + 1
        row.status = "failed"
        row.last_error_class = error_class
        row.last_error_redacted = error_redacted
        self.db.add(row)
        self.db.flush()
