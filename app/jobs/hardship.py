"""Nightly hardship maintenance — ``python -m app.jobs.hardship``.

Runs ``hardship.run_maintenance`` (WS-J):

* ``awaiting_signature`` requests past their signature window (config default
  30 days) → ``expired`` — the borrower never signed, so NOTHING was ever
  applied to the schedule.
* ``active`` deferments whose end-of-contract custom transactions are all in
  the past → ``completed`` (v1: the applied changes persist; the full
  temporary-AoT auto snap-back is the P1 follow-up).

Idempotent — safe to re-run any number of times. Connects via ``DATABASE_URL``.

Scheduling mirrors the delinquency/dunning jobs (GitHub Actions cron). NOTE:
the workflow file is deliberately NOT added by the WS-J branch (CI files are
owned by the merge train); until a ``hardship-cron.yml`` exists this can be run
by hand or piggybacked on the nightly job runner.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.logging import get_logger
from app.db.base import SessionLocal
from app.services.hardship import run_maintenance

logger = get_logger(__name__)


def main() -> int:
    as_of = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        result = run_maintenance(db, as_of=as_of)
        db.commit()
        logger.info(
            "hardship_maintenance_complete",
            as_of=as_of.isoformat(),
            expired=result["expired"],
            completed=result["completed"],
        )
        return 0
    except Exception as exc:  # noqa: BLE001 — log + non-zero exit; the scheduler retries
        db.rollback()
        logger.error(
            "hardship_maintenance_failed", as_of=as_of.isoformat(), error=str(exc)
        )
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
