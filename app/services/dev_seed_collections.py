"""Dev/staging-only seed: past-due loans across the delinquency buckets.

Gives Collections (and the delinquency aging / queue reports) real data to work
against without waiting for time to pass. Each seeded account is originated
through the SAME engine the real flow uses — ``demo_simulation.run_demo_application``
drives the orchestrator + loan_servicing to book an APPROVED, funded, active loan
with a real amortization schedule — and is THEN back-dated so its earliest unpaid
installment lands at a chosen days-past-due (DPD). The nightly aging job
(``loan_servicing.run_delinquency_aging``) is then run so the loans flip to
``delinquent`` exactly as they would in production.

DPD → bucket mapping mirrors ``delinquency_buckets.aging_bucket`` (Dave's
platform-wide vocabulary): 1-30 / 31-60 / 61-90 / >91. The plan below also
covers the POT ladder's deeper cuts so Collections sees a spread.

This is DEV/STAGING ONLY — it is invoked from the token-gated admin-dev endpoint
and the off-prod seed script, never mounted or runnable in production. It writes
loan/application state exclusively through sanctioned entry points (the flow
orchestrator via the demo runner); it never assigns ``application.status`` directly.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.platform.loan import PlatformLoan, PlatformLoanScheduleItem
from app.services import demo_simulation, loan_servicing

# (label, days_past_due) — one representative account per delinquency bucket.
# The DPD is chosen to sit safely inside each bucket (not on a boundary).
_BUCKET_PLAN: list[tuple[str, int]] = [
    ("current-month-late", 12),   # ageing 1-30   / pot: current_month_late
    ("30-days", 45),              # ageing 31-60  / pot_30
    ("60-days", 75),              # ageing 61-90  / pot_60
    ("90-days", 100),             # ageing 91plus / default (>90, Dave's rule)
    ("120-plus", 150),            # ageing 91plus / default (deepest account)
]

# Realistic dental-financing principals per bucket (integer cents), so the queue /
# aging dollar figures look plausible rather than all identical. Kept within the
# seeded dental product's amount range so origination never trips a min/max guard.
_AMOUNTS_CENTS: dict[str, int] = {
    "current-month-late": 1_800_000,   # $18,000
    "30-days": 3_200_000,              # $32,000
    "60-days": 2_050_000,              # $20,500
    "90-days": 2_450_000,              # $24,500
    "120-plus": 4_100_000,             # $41,000
}


def seed_past_due_accounts(db: Session) -> dict:
    """Create one past-due loan per delinquency bucket; return a summary trace.

    Each account is a fully-originated, funded, active loan (real schedule) whose
    earliest unpaid installment has been back-dated to the bucket's target DPD.
    After seeding, the delinquency aging pass is run so the loans are marked
    ``delinquent`` — the exact state Collections operates on.

    Raises ``ValueError`` (surfaced as 409 by the endpoint) if there is no active
    credit product to originate against — create one first.
    """
    seeded: list[dict] = []

    for label, dpd in _BUCKET_PLAN:
        amount_cents = _AMOUNTS_CENTS[label]
        # Originate through the real engine at an approving score. post_payment=False
        # leaves every installment unpaid, so installment #1 is the earliest unpaid
        # and is what we back-date to control DPD.
        trace = demo_simulation.run_demo_application(
            db, score=720, amount_cents=amount_cents, post_payment=False
        )
        loan_info = trace.get("loan")
        if loan_info is None:
            # Defensive: an approving score should always book a loan. Skip rather
            # than abort the whole seed if the ruleset ever changes.
            continue

        loan = db.query(PlatformLoan).filter(PlatformLoan.id == loan_info["loan_id"]).one()
        _backdate_to_dpd(db, loan, dpd)
        seeded.append(
            {
                "bucket": label,
                "days_past_due": dpd,
                "loan_id": str(loan.id),
                "principal_cents": loan.principal_cents,
                "principal_balance_cents": loan.principal_balance_cents,
            }
        )

    # Run the real nightly aging pass so the seeded loans transition to
    # ``delinquent`` and their overdue installments to ``late`` — identical to
    # how production reaches this state.
    aging = loan_servicing.run_delinquency_aging(db, date.today())

    return {
        "seeded_count": len(seeded),
        "accounts": seeded,
        "aging": {
            "installments_flagged_late": aging.installments_flagged_late,
            "loans_marked_delinquent": len(aging.loans_marked_delinquent),
        },
    }


def _backdate_to_dpd(db: Session, loan: PlatformLoan, dpd: int) -> None:
    """Shift the loan's whole schedule back so installment #1 is ``dpd`` days overdue.

    Collections computes DPD from the EARLIEST unpaid installment's ``due_date``
    (admin_collections._earliest_unpaid_due). We move the entire schedule by the
    same delta so the amortization spacing stays intact and the first installment's
    due date is exactly ``today - dpd``.
    """
    schedule = (
        db.query(PlatformLoanScheduleItem)
        .filter(PlatformLoanScheduleItem.loan_id == loan.id)
        .order_by(PlatformLoanScheduleItem.installment_number)
        .all()
    )
    if not schedule:
        return
    target_first_due = date.today() - timedelta(days=dpd)
    shift = schedule[0].due_date - target_first_due
    for item in schedule:
        item.due_date = item.due_date - shift
    # Also back-date disbursement so the account's origination predates its first
    # due date (keeps the timeline internally consistent for the UI).
    loan.disbursed_at = datetime.now(timezone.utc) - timedelta(days=dpd + 30)
    db.commit()
    db.refresh(loan)
