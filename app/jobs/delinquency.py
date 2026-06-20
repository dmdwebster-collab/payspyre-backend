"""Nightly delinquency-aging job — ``python -m app.jobs.delinquency``.

Flips overdue, not-fully-paid loan installments to ``late`` and their loans to
``delinquent`` (``loan_servicing.run_delinquency_aging``). Idempotent — safe to
re-run any number of times. Connects via ``DATABASE_URL``.

Scheduling: DO App Platform has no native cron, so this ships with a GitHub
Actions cron (``.github/workflows/delinquency-cron.yml``). It can equally be run
by a DO Function, an external cron service, or by hand.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.logging import get_logger
from app.db.base import SessionLocal
from app.services.loan_servicing import run_delinquency_aging

logger = get_logger(__name__)


def main() -> int:
    as_of = datetime.now(timezone.utc).date()
    db = SessionLocal()
    try:
        result = run_delinquency_aging(db, as_of)
        db.commit()
        logger.info("delinquency_aging_complete", as_of=as_of.isoformat(), result=str(result))
        return 0
    except Exception as exc:  # noqa: BLE001 — log + non-zero exit; the scheduler retries
        db.rollback()
        logger.error("delinquency_aging_failed", as_of=as_of.isoformat(), error=str(exc))
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
