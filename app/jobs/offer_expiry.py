"""Offer-expiry sweep — ``python -m app.jobs.offer_expiry`` (WS-D).

Flips every ``offered`` loan-offer past its ``expires_at`` to ``expired`` and
lapses fully-expired approvals (application → ``expired``) via
``loan_offers.expire_offers``. Idempotent — safe to re-run; an already-expired
offer never matches again. Connects via ``DATABASE_URL``. Read-path hooks in the
offers endpoints also expire lazily, so this is the belt for offers no one
opened.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.logging import get_logger
from app.db.base import SessionLocal
from app.services import loan_offers

logger = get_logger(__name__)


def main() -> int:
    now = datetime.now(timezone.utc)
    db = SessionLocal()
    try:
        result = loan_offers.expire_offers(db, now=now)
        db.commit()
        logger.info(
            "offer_expiry_complete",
            offers_expired=result.offers_expired,
            applications_expired=result.applications_expired,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 — log + non-zero exit; scheduler retries
        db.rollback()
        logger.error("offer_expiry_failed", error=str(exc))
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
