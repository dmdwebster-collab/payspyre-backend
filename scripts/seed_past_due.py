"""Dev/staging seed: past-due loans across the delinquency buckets.

Populates Collections + the delinquency aging/queue reports with real data so the
lender cockpit's Collections surface can be demoed. Each account is originated
through the real engine (orchestrator + loan_servicing) and then back-dated into a
delinquency bucket — see app/services/dev_seed_collections.py.

Usage:  python -m scripts.seed_past_due     (or: python scripts/seed_past_due.py)

OFF-PROD ONLY: refuses to run when ENVIRONMENT=production. Connects via
DATABASE_URL. Requires at least one ACTIVE credit product to originate against.
"""
from __future__ import annotations

import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.services import dev_seed_collections


def main() -> int:
    if settings.ENVIRONMENT == "production":
        print("Refusing to seed past-due accounts in production.", file=sys.stderr)
        return 1

    engine = create_engine(settings.DATABASE_URL)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    try:
        result = dev_seed_collections.seed_past_due_accounts(db)
    except ValueError as exc:
        print(f"Seed failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — surface + non-zero exit
        db.rollback()
        print(f"Seed errored: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()

    print(f"Seeded {result['seeded_count']} past-due account(s):")
    for acct in result["accounts"]:
        dollars = acct["principal_balance_cents"] / 100
        print(
            f"  - {acct['bucket']:<20} {acct['days_past_due']:>3} DPD  "
            f"${dollars:>12,.2f}  loan={acct['loan_id']}"
        )
    print(
        f"Aging pass: {result['aging']['loans_marked_delinquent']} loan(s) marked "
        f"delinquent, {result['aging']['installments_flagged_late']} installment(s) late."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
