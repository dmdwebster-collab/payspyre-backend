"""Payment-delay notice scan — ``python -m app.jobs.payment_delay_scan``.

WS-F business calendar: emits a ``payment_delay_notice`` event for every open
installment whose due date within the next week lands on a non-business day
(Canadian statutory holiday or admin-configured closure); the notification
processor renders + sends them through the outbox lane (delivery inert until
SendGrid creds land — expected). Idempotent — safe to re-run daily.

Scheduling mirrors the dunning job (GitHub Actions cron); run it BEFORE /
alongside the notification processor. Connects via ``DATABASE_URL``.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.logging import get_logger
from app.db.base import SessionLocal
from app.services.business_calendar import queue_payment_delay_notices

logger = get_logger(__name__)


def main() -> int:
    as_of = datetime.now(timezone.utc).date()
    db = SessionLocal()
    try:
        result = queue_payment_delay_notices(db, as_of)
        db.commit()
        logger.info(
            "payment_delay_scan_complete",
            as_of=as_of.isoformat(),
            notices=result.notices_emitted,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 — log + non-zero exit; scheduler retries
        db.rollback()
        logger.error("payment_delay_scan_failed", as_of=as_of.isoformat(), error=str(exc))
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
