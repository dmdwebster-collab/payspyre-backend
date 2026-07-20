"""Daily auto-collection job — ``python -m app.jobs.auto_collection``.

Runs the WS-G auto-collection engine (``auto_collection.run_auto_collection``):
PAD pre-notifications N business days ahead, then Zumrails collection pulls for
every installment due today (plus policy retries of failed pulls).

FLAG-GATED OFF BY DEFAULT — MONEY PATH. The run is a strict no-op unless BOTH:
  * ``AUTO_COLLECTION_ENABLED=true`` (app settings / env), AND
  * the ``zumrails`` integration_settings row is enabled and buildable
    ("real adapters on" — point it at the Zumrails sandbox for a staging
    simulation; unit tests inject a fake adapter directly).

Idempotent — safe to re-run any number of times per day: attempts are claimed
under a UNIQUE (schedule_item, attempt #) constraint before any vendor call,
and pre-notifications dedupe on their pad_key. Connects via ``DATABASE_URL``.

Scheduling mirrors the delinquency/dunning jobs (external cron — DO App
Platform has no native cron). Run it BEFORE the notification processor so the
pre-notification events it emits get delivered promptly. The GitHub Actions
cron line is documented in the PR (deliberately NOT added to the workflows by
this workstream).
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import SessionLocal
from app.services.auto_collection import run_auto_collection

logger = get_logger(__name__)


def main() -> int:
    as_of = datetime.now(timezone.utc).date()

    # Cheap pre-check: fully inert (no DB connection) while the flag is off.
    if not settings.AUTO_COLLECTION_ENABLED:
        logger.info("auto_collection_disabled", as_of=as_of.isoformat())
        return 0

    db = SessionLocal()
    try:
        result = run_auto_collection(db, as_of)
        db.commit()
        logger.info(
            "auto_collection_complete",
            as_of=as_of.isoformat(),
            enabled=result.enabled,
            adapter_available=result.adapter_available,
            pre_notifications=result.pre_notifications_emitted,
            planned=result.charges_planned,
            initiated=result.charges_initiated,
            settled_sync=result.settled_synchronously,
            skipped=result.charges_skipped,
            errors=result.errors,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 — log + non-zero exit; scheduler retries
        db.rollback()
        logger.error("auto_collection_failed", as_of=as_of.isoformat(), error=str(exc))
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
