"""Month-end delinquency bucket snapshot job — ``python -m app.jobs.bucket_snapshot``.

Assigns Dave's month-end buckets (current / current_month_late / pot_30 /
pot_60 / pot_90 / default / insolvency / written_off) to every servicing loan
and persists one snapshot row per (loan, month)
(``delinquency_buckets.run_bucket_snapshot``).

IDEMPOTENT PER MONTH: re-runs UPDATE the same month's snapshot rows (the
derivation is deterministic at the month-end date). By default the job
snapshots the most recently COMPLETED month-end — run it on a month's last
day (late evening) or any day of the following month; both land on the same
month. ``--month YYYY-MM`` re-snapshots a specific month.

This is a REPORTING/COLLECTIONS layer: the nightly DPD aging job
(``app.jobs.delinquency``) keeps running unchanged and still owns the
``late``/``delinquent`` statuses.

Scheduling: DO App Platform has no native cron — run via GitHub Actions cron /
DO Function / external scheduler (documented in the PR; no workflow-file
change here). Suggested cron: ``30 23 28-31 * *`` guarded by the job's own
month-end default, or simply ``15 0 1 * *`` (first of month, snapshots the
just-ended month). Connects via ``DATABASE_URL``.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime

from app.core.logging import get_logger
from app.db.base import SessionLocal
from app.services.delinquency_buckets import run_bucket_snapshot

logger = get_logger(__name__)


def _parse_month(value: str) -> date:
    """``YYYY-MM`` → first-of-month date."""
    return datetime.strptime(value, "%Y-%m").date().replace(day=1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Month-end delinquency bucket snapshot")
    parser.add_argument(
        "--month",
        type=_parse_month,
        default=None,
        help="Month to snapshot as YYYY-MM (default: most recently completed month-end)",
    )
    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        result = run_bucket_snapshot(db, args.month)
        logger.info(
            "bucket_snapshot_complete",
            snapshot_month=result.snapshot_month.isoformat(),
            month_end=result.month_end.isoformat(),
            loans_snapshotted=result.loans_snapshotted,
            bucket_counts=result.bucket_counts,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 — log + non-zero exit; the scheduler retries
        db.rollback()
        logger.error("bucket_snapshot_failed", error=str(exc))
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
