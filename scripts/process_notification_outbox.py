"""Notification outbox worker — drains platform_notification_outbox (P7.4c).

Polls ``pending`` rows whose ``next_attempt_at`` is due, re-drives the magic-link
send via the same vendor senders the inline dispatcher uses, and:

- success            -> mark ``sent``.
- transient failure  -> reschedule with NOTIFICATION_RETRY_DELAYS backoff, until
                        NOTIFICATION_MAX_RETRIES is exhausted, then mark ``failed``.
- permanent failure  -> mark ``failed`` immediately (retry won't help).
- expired token      -> mark ``failed`` immediately (can't re-send a dead token).

A terminal failure emits a ``notification_retry_exhausted`` PlatformEvent — the
same audit shape as ``adverse_action_notice_failed`` (no PII) — so an undelivered
magic link is provable and actionable.

INERT by default: with settings.NOTIFICATION_OUTBOX_ENABLED off nothing is ever
enqueued, so a single pass is a harmless no-op. Run once (cron / one-shot) or with
--loop to poll forever at NOTIFICATION_QUEUE_PROCESSING_INTERVAL seconds.

Usage:
    python scripts/process_notification_outbox.py            # single pass
    python scripts/process_notification_outbox.py --loop     # poll forever
    python scripts/process_notification_outbox.py --limit 25 # cap rows per pass
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import SessionLocal
from app.models.platform.event import PlatformEvent
from app.repositories.notification_outbox import NotificationOutboxRepo
from app.services.notification_retry import is_exhausted, parse_retry_delays
from app.services.real_notification_dispatcher import (
    NotificationError,
    PermanentNotificationError,
    RealNotificationDispatcher,
    TransientNotificationError,
    _redact_pii,
)

logger = get_logger(__name__)

RETRY_EXHAUSTED_EVENT_TYPE = "notification_retry_exhausted"


def _record_exhausted_event(db, row, *, error_class: str, error_redacted: str) -> None:
    """Append a ``notification_retry_exhausted`` audit row (no PII). Mirrors the
    ``adverse_action_notice_failed`` pattern so undelivered notices are provable."""
    payload = {
        "v": 1,
        "actor": {"type": "system", "id": "system"},
        "application_id": str(row.application_id) if row.application_id else None,
        "patient_id": str(row.patient_id),
        "after": {
            "notification_type": row.notification_type,
            "channel": row.contact_method,
            "delivered": False,
            "attempts": row.attempts,
            "error_class": error_class,
            "error_message_redacted": error_redacted,
        },
    }
    db.add(
        PlatformEvent(
            event_type=RETRY_EXHAUSTED_EVENT_TYPE,
            actor="system",
            patient_id=row.patient_id,
            application_id=row.application_id,
            payload=payload,
        )
    )
    db.flush()


def _retry_one(db, dispatcher, row, *, delays, now: datetime) -> str:
    """Attempt a single outbox row. Returns the resulting status for logging."""
    repo = NotificationOutboxRepo(db)

    # A dead token can never be re-sent — fail terminally.
    ttl = row.ttl_expires_at
    if ttl is not None and ttl.tzinfo is None:
        ttl = ttl.replace(tzinfo=timezone.utc)
    if ttl is not None and ttl <= now:
        repo.mark_failed(row, error_class="ExpiredToken", error_redacted="magic-link token expired before retry")
        _record_exhausted_event(db, row, error_class="ExpiredToken", error_redacted="magic-link token expired before retry")
        return "failed (expired)"

    try:
        recipient = dispatcher._resolve_recipient(row.patient_id, row.contact_method)
        dispatcher._check_suppression(row.contact_method, recipient)
        sender = dispatcher._build_sender(row.contact_method)
        ttl_seconds = max(1, int((ttl - now).total_seconds())) if ttl else 900
        dispatcher._send(sender, row.contact_method, recipient, row.token, ttl_seconds)
    except PermanentNotificationError as exc:
        redacted = _redact_pii(str(exc))
        repo.mark_failed(row, error_class=type(exc).__name__, error_redacted=redacted)
        _record_exhausted_event(db, row, error_class=type(exc).__name__, error_redacted=redacted)
        return "failed (permanent)"
    except TransientNotificationError as exc:
        redacted = _redact_pii(str(exc))
        # +1 because this attempt counts toward the cap before we (maybe) bump it.
        if is_exhausted(row.attempts + 1, settings.NOTIFICATION_MAX_RETRIES):
            repo.mark_failed(row, error_class=type(exc).__name__, error_redacted=redacted)
            _record_exhausted_event(db, row, error_class=type(exc).__name__, error_redacted=redacted)
            return "failed (exhausted)"
        repo.reschedule(row, delays=delays, error_class=type(exc).__name__, error_redacted=redacted, now=now)
        return "rescheduled"
    except NotificationError as exc:
        # Unclassified NotificationError — treat conservatively as transient.
        redacted = _redact_pii(str(exc))
        if is_exhausted(row.attempts + 1, settings.NOTIFICATION_MAX_RETRIES):
            repo.mark_failed(row, error_class=type(exc).__name__, error_redacted=redacted)
            _record_exhausted_event(db, row, error_class=type(exc).__name__, error_redacted=redacted)
            return "failed (exhausted)"
        repo.reschedule(row, delays=delays, error_class=type(exc).__name__, error_redacted=redacted, now=now)
        return "rescheduled"

    repo.mark_sent(row)
    return "sent"


def process_once(*, limit: int = 50) -> int:
    """Process one batch of due rows. Returns the number of rows handled."""
    delays = parse_retry_delays(settings.NOTIFICATION_RETRY_DELAYS)
    db = SessionLocal()
    handled = 0
    try:
        now = datetime.now(timezone.utc)
        repo = NotificationOutboxRepo(db)
        rows = repo.claim_due(now=now, limit=limit)
        for row in rows:
            dispatcher = RealNotificationDispatcher(db)
            try:
                result = _retry_one(db, dispatcher, row, delays=delays, now=now)
                handled += 1
                logger.info(
                    "notification_outbox row processed",
                    outbox_id=row.id,
                    channel=row.contact_method,
                    attempts=row.attempts,
                    result=result,
                )
            except Exception:  # pragma: no cover - defensive per-row isolation
                logger.warning(
                    "notification_outbox row errored", outbox_id=row.id, exc_info=True
                )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return handled


def main() -> None:
    parser = argparse.ArgumentParser(description="Drain the notification retry outbox")
    parser.add_argument("--loop", action="store_true", help="poll forever")
    parser.add_argument("--limit", type=int, default=50, help="rows per pass")
    args = parser.parse_args()

    if not settings.NOTIFICATION_OUTBOX_ENABLED:
        logger.info("notification outbox disabled (NOTIFICATION_OUTBOX_ENABLED=false); nothing to do")

    if not args.loop:
        n = process_once(limit=args.limit)
        logger.info("notification outbox pass complete", handled=n)
        return

    interval = max(1, settings.NOTIFICATION_QUEUE_PROCESSING_INTERVAL)
    logger.info("notification outbox worker started", interval_seconds=interval)
    while True:
        try:
            process_once(limit=args.limit)
        except Exception:  # pragma: no cover - keep the loop alive
            logger.warning("notification outbox pass failed", exc_info=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
