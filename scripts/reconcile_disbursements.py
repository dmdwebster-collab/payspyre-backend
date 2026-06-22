"""Reconcile loan disbursements stuck ``in_progress`` — ``python scripts/reconcile_disbursements.py``.

A Zumrails disbursement that returns a non-terminal status at push time
(PENDING / IN_PROGRESS / UNKNOWN) is left at ``disbursement_status=in_progress``
forever if no terminal webhook ever arrives. This sweep is the safety net: it
finds loans wedged in_progress past a staleness threshold, polls Zumrails for
each transaction's *current* status, and advances them via the existing
``loan_lifecycle.on_disbursement_complete`` / ``on_disbursement_failed`` entry
points (idempotent + forward-only — no duplicated state logic here).

Idempotent — safe to re-run any number of times. Connects via ``DATABASE_URL``.
Gracefully no-ops (exit 0) if the Zumrails provider is disabled / unconfigured.

Scheduling: like the delinquency job, DO App Platform has no native cron, so run
this from a GitHub Actions cron, a DO Function, an external cron service, or by
hand. A few times a day is plenty given the threshold below.

Usage:
    python scripts/reconcile_disbursements.py            # default 6h threshold
    RECONCILE_STUCK_HOURS=2 python scripts/reconcile_disbursements.py
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta

from app.core.logging import get_logger
from app.db.base import SessionLocal
from app.services import loan_lifecycle

logger = get_logger(__name__)


def main() -> int:
    hours = float(os.environ.get("RECONCILE_STUCK_HOURS", "6"))
    db = SessionLocal()
    try:
        result = loan_lifecycle.reconcile_stuck_disbursements(
            db, stuck_for=timedelta(hours=hours)
        )
        logger.info("disbursement_reconcile_complete", result=str(result))
        return 0
    except Exception as exc:  # noqa: BLE001 — log + non-zero exit; the scheduler retries
        db.rollback()
        logger.error("disbursement_reconcile_failed", error=str(exc))
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
