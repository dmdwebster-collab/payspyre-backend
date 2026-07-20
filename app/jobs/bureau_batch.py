"""Equifax month-end batch job — ``python -m app.jobs.bureau_batch``. TEST MODE.

Generates the Metro2-style flat file for Pot-60+ bureau-reportable accounts
(from the WS-H month-end delinquency snapshots) and stores it in
``platform_bureau_batches``. NOTHING is transmitted — the Equifax subscriber
agreement is pending (Dave item); transmission is the follow-up job.

IDEMPOTENT PER MONTH: re-runs UPDATE the month's row (bumping
``generation_count``). Defaults to the most recently completed month-end —
Dave's cadence: "on 12:01, the first possible second of a new month".
``--month YYYY-MM`` re-generates a specific month.

Scheduling (PARKED-CRON pattern, PR #178 convention): DO App Platform has no
native cron and CI workflow files are owned by the merge train — this job is
deliberately NOT wired to a scheduler here. Run by hand, or add a GitHub
Actions cron (suggested: ``1 0 1 * *``) once ops wires the DB secret. Runs
AFTER app.jobs.bucket_snapshot (it reads that job's snapshots). Connects via
``DATABASE_URL``.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime

from app.core.logging import get_logger
from app.db.base import SessionLocal
from app.services.bureau_reporting import run_month_end_batch

logger = get_logger(__name__)


def _parse_month(value: str) -> date:
    """``YYYY-MM`` → first-of-month date."""
    return datetime.strptime(value, "%Y-%m").date().replace(day=1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Equifax month-end batch generation (test mode — stored, not sent)"
    )
    parser.add_argument(
        "--month",
        type=_parse_month,
        default=None,
        help="Month to generate as YYYY-MM (default: most recently completed month-end)",
    )
    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        result = run_month_end_batch(db, args.month)
        logger.info(
            "bureau_batch_complete",
            batch_month=result.batch_month.isoformat(),
            account_count=result.account_count,
            total_balance_cents=result.total_balance_cents,
            file_name=result.file_name,
            generation_count=result.generation_count,
            mode="test",
        )
        return 0
    except Exception as exc:  # noqa: BLE001 — log + non-zero exit; the scheduler retries
        db.rollback()
        logger.error("bureau_batch_failed", error=str(exc))
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
