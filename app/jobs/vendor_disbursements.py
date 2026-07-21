"""Monthly vendor auto-disbursement job — ``python -m app.jobs.vendor_disbursements``.

Runs the W2-DISB free monthly sweep
(``vendor_disbursements.run_monthly_auto_disbursement``): for every vendor with
a positive available balance, push the whole balance to the vendor via Zumrails
(fee-free). Extra intra-month payouts are the vendor-initiated path (the clinic
endpoint), not this job.

FLAG-GATED OFF BY DEFAULT — MONEY-OUT PATH. The run is a strict no-op unless
BOTH:
  * ``VENDOR_DISBURSEMENTS_ENABLED=true`` (app settings / env), AND
  * the ``zumrails`` integration_settings row is enabled and buildable
    ("real adapters on" — point it at the Zumrails sandbox for a staging
    simulation; unit tests inject a fake adapter directly).
A vendor with no resolved disbursement recipient is additionally skipped.

Idempotent — safe to re-run any number of times in a month: each monthly payout
is claimed under a UNIQUE deterministic ``vdisb-auto-{vendor}-{YYYYMM}`` id
before any vendor call, so a duplicate run finds the claim and never re-pushes.
Connects via ``DATABASE_URL``.

Scheduling mirrors the auto-collection / delinquency / dunning jobs (external
cron — DO App Platform has no native cron; run e.g. on the 1st of each month).
The GitHub Actions cron line is documented in the PR (deliberately NOT added to
the workflows by this workstream — it stays PARKED until the flag is flipped).
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import SessionLocal
from app.services.vendor_disbursements import run_monthly_auto_disbursement

logger = get_logger(__name__)


def main() -> int:
    as_of = datetime.now(timezone.utc).date()

    # Cheap pre-check: fully inert (no DB connection) while the flag is off.
    if not settings.VENDOR_DISBURSEMENTS_ENABLED:
        logger.info("vendor_disbursement_disabled", as_of=as_of.isoformat())
        return 0

    db = SessionLocal()
    try:
        result = run_monthly_auto_disbursement(db, as_of)
        logger.info(
            "vendor_disbursement_job_complete",
            as_of=as_of.isoformat(),
            enabled=result.enabled,
            adapter_available=result.adapter_available,
            scanned=result.vendors_scanned,
            initiated=result.payouts_initiated,
            settled=result.payouts_settled,
            skipped=result.skipped,
            duplicates=result.duplicates,
            errors=result.errors,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 — log + non-zero exit; scheduler retries
        db.rollback()
        logger.error(
            "vendor_disbursement_job_failed",
            as_of=as_of.isoformat(),
            error=str(exc),
        )
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
