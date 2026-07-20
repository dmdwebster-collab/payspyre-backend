"""Vendor document expiry-alert job — ``python -m app.jobs.vendor_doc_expiry``.

Daily scan of live vendor documents (MSA / contracts, WS-G migration 061) that
carry an ``expiry_date``: fires the 60/30/7-day (and post-expiry) alerts via
``vendor_doc_alerts.scan_and_alert``. IDEMPOTENT — a unique
(document, threshold) dedupe row means re-runs and overlapping schedules never
double-alert.

Email delivery is inert until SendGrid creds land (``USE_REAL_NOTIFICATIONS``);
the audit event + dedupe row are written regardless, so alerts surface in the
per-vendor changelog either way.

Scheduling: DO App Platform has no native cron — run via GitHub Actions cron /
external scheduler like the other ``app.jobs`` modules (suggested: daily
``0 13 * * *`` UTC ≈ morning Pacific). Connects via ``DATABASE_URL``.
"""
from __future__ import annotations

import argparse
from datetime import date

from app.core.logging import get_logger
from app.db.base import SessionLocal
from app.services.vendor_doc_alerts import scan_and_alert

logger = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vendor document expiry alerts")
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help="Scan as of this date (YYYY-MM-DD, default: today UTC)",
    )
    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        alerts = scan_and_alert(db, today=args.as_of)
        logger.info("vendor_doc_expiry_job_complete", alerts_recorded=alerts)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
