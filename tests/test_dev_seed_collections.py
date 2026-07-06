"""Past-due Collections seed — books real loans into each delinquency bucket.

Task 2: seed several loans in past-due states spread across the delinquency
buckets so Collections + the aging/queue reports are demoable. Uses the live test
DB (the seed drives the real origination engine), mirroring test_demo_simulation.
"""
from datetime import date

from sqlalchemy.orm import Session

from app.api.v1.endpoints.admin_collections import _bucket_for, _earliest_unpaid_due
from app.models.platform.loan import PlatformLoan
from app.services import dev_seed_collections


class TestSeedPastDueAccounts:
    def test_seeds_one_account_per_bucket_marked_delinquent(self, db_session: Session):
        result = dev_seed_collections.seed_past_due_accounts(db_session)

        # One account per bucket in the plan.
        assert result["seeded_count"] == len(dev_seed_collections._BUCKET_PLAN)
        assert result["aging"]["loans_marked_delinquent"] == result["seeded_count"]

        # Every seeded loan is now delinquent and lands in its intended DPD bucket.
        today = date.today()
        seen_buckets = set()
        for acct in result["accounts"]:
            loan = (
                db_session.query(PlatformLoan)
                .filter(PlatformLoan.id == acct["loan_id"])
                .one()
            )
            assert loan.status == "delinquent", acct

            due = _earliest_unpaid_due(db_session, loan.id)
            assert due is not None
            dpd = max(0, (today - due).days)
            # The earliest unpaid installment is overdue by the planned DPD (±1 day
            # for month-length rounding in the schedule shift).
            assert abs(dpd - acct["days_past_due"]) <= 1, (acct, dpd)
            seen_buckets.add(_bucket_for(dpd))

        # The seed spreads across the distinct delinquency buckets (current-late,
        # 30, 60, 90, 120+) — i.e. it is not all bunched in one tier.
        assert seen_buckets == {"1-29", "30-59", "60-89", "90-119", "120+"}

    def test_amounts_are_realistic_and_positive(self, db_session: Session):
        result = dev_seed_collections.seed_past_due_accounts(db_session)
        for acct in result["accounts"]:
            assert acct["principal_cents"] > 0
            assert acct["principal_balance_cents"] > 0
