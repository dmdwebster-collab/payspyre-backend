"""Scheduled-reports runner job — ``python -m app.jobs.report_schedules``.

Renders every DUE report schedule (WS-H, video 05 item 6) and emails the XLSX
with the artifact attached, through the same ``report_exports`` machinery the
cockpit downloads use. The headline case is the monthly vendor statement:
a ``per_vendor`` monthly schedule fans out one vendor-scoped copy to each
vendor on the book.

DELIVERY IS INERT WITHOUT SendGrid — expected. When ``USE_REAL_NOTIFICATIONS``
is off or no capable sender resolves, schedules still RENDER (run logged
``rendered``) and nothing is emailed. Flipping to live is a credential change.

IDEMPOTENT: a schedule advances its ``last_period_key`` cursor only after a
successful render, so re-running the cron the same period is a no-op and a
missed day self-heals on the next run.

Scheduling: DO App Platform has no native cron — run via GitHub Actions cron /
DO Function / external scheduler (parked workflow, PR #178 pattern). Daily is
the natural cadence (the runner itself decides which schedules are due).
Connects via ``DATABASE_URL``.
"""
from __future__ import annotations

from app.core.logging import get_logger
from app.db.base import SessionLocal
from app.services.report_scheduler import run_due_schedules

logger = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    db = SessionLocal()
    try:
        summary = run_due_schedules(db)
        logger.info(
            "report_schedules_job_complete",
            considered=summary.schedules_considered,
            run=summary.schedules_run,
            rendered=summary.reports_rendered,
            sent=summary.emails_sent,
            statuses=summary.statuses,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 — log + non-zero exit; scheduler retries
        db.rollback()
        logger.error("report_schedules_job_failed", error=str(exc))
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
