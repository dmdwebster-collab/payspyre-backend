"""Nightly dunning scan — ``python -m app.jobs.dunning``.

Emits payment reminder / overdue events for installments hitting a milestone
today (``dunning.run_dunning_scan``); the notification processor sends them.
Idempotent — safe to re-run. Connects via ``DATABASE_URL``.

Scheduling mirrors the delinquency job (GitHub Actions cron;
``.github/workflows/dunning-cron.yml``). Run it BEFORE / alongside the
notification processor so the events it emits get delivered promptly.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.logging import get_logger
from app.db.base import SessionLocal
from app.services.dunning import run_dunning_scan

logger = get_logger(__name__)


def main() -> int:
    as_of = datetime.now(timezone.utc).date()
    db = SessionLocal()
    try:
        result = run_dunning_scan(db, as_of)
        db.commit()
        logger.info(
            "dunning_complete",
            as_of=as_of.isoformat(),
            reminders=result.reminders_emitted,
            overdue=result.overdue_emitted,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 — log + non-zero exit; scheduler retries
        db.rollback()
        logger.error("dunning_failed", as_of=as_of.isoformat(), error=str(exc))
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
