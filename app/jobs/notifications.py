"""Notification processor job — ``python -m app.jobs.notifications``.

Drains the notification outbox: repeatedly runs ``NotificationProcessor`` until
there are no new trigger events left (or it stops making cursor progress, which
means it's stuck behind transient vendor failures that will retry on the next
scheduled run). The processor commits as it goes and is idempotent (dedupes per
source-event + channel), so this is safe to re-run any number of times.

Connects via ``DATABASE_URL``. Scheduling mirrors the delinquency job — DO App
Platform has no native cron, so this ships with a GitHub Actions cron
(``.github/workflows/notifications-cron.yml``); it can equally be run by any
external scheduler or by hand.
"""
from __future__ import annotations

from app.core.logging import get_logger
from app.db.base import SessionLocal
from app.services.notification_processor import NotificationProcessor

logger = get_logger(__name__)

# Per-batch scan cap and a hard ceiling on batches so a transient-failure
# standstill (cursor not advancing) can never spin forever within one run.
_BATCH = 500
_MAX_BATCHES = 100


def main() -> int:
    db = SessionLocal()
    try:
        processor = NotificationProcessor(db)
        sent = skipped = failed = scanned = 0
        last_cursor = -1
        for _ in range(_MAX_BATCHES):
            res = processor.run(limit=_BATCH)
            sent += res.sent
            skipped += res.skipped
            failed += res.failed
            scanned += res.scanned
            if res.scanned == 0:
                break  # fully drained
            if res.cursor_advanced_to <= last_cursor:
                break  # no forward progress → held by transient failures; retry next run
            last_cursor = res.cursor_advanced_to
        logger.info(
            "notification_job_complete",
            scanned=scanned, sent=sent, skipped=skipped, failed=failed,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 — log + non-zero exit; the scheduler retries
        db.rollback()
        logger.error("notification_job_failed", error=str(exc))
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
