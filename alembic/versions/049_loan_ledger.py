"""Immutable loan ledger — platform_loan_transactions (WS-A, Turnkey parity P0)

Revision ID: 049_loan_ledger
Revises: 048_underwriting_ops
Create Date: 2026-07-10

Dave's mandate #6 ("Ledger, not 'transactions'"): an immutable money ledger per
loan with

  * auto-generated reference numbers ``{vendor_id}-{loan_id}-{seq}``,
  * TWO dates per row — ``effective_date`` (money treated as applied) vs
    ``processing_date`` (recorded) — permission-bounded backdating comes later,
  * allocation columns (principal / interest / fees / non-accruing add-on),
  * payment types (cash/check/eft/credit_card/adjustment) for reconciliation,
  * repayment-mode column (regular/add_on/special/payoff) — created NOW so the
    money table never needs another migration when modes are wired (later WS).

IMMUTABILITY: rows are WORM — a DB trigger (same pattern as migrations 021/025)
rejects UPDATE and DELETE. Corrections are compensating ``reversal`` rows that
reference the original via ``reverses_transaction_id``.

BACKFILL (two steps):
1. Existing ``platform_loan_payments`` rows are replayed into ledger rows
   (txn_type='payment', effective = processing = received date). The
   principal/interest split is reconstructed by replaying the exact allocation
   ``record_payment`` used historically (oldest unpaid installment first,
   principal before interest within an installment) — the only historical
   truth available. Fees/add-on are 0 (no fee machinery existed before).
2. CUTOVER RECONCILIATION: for every pre-existing loan whose actuals-engine
   replay disagrees with the stored ``principal_balance_cents`` (migrated
   book, schedule-vs-ACT/365 drift) or carries replayed accrued interest, one
   non-cash ``adjustment`` row ties ledger outstanding to the operational
   balance and restarts interest accrual at the cutover date. See
   ``_reconcile_balances_at_cutover``.

Money is integer cents throughout (repo convention).
"""
from datetime import datetime
from typing import Sequence, Union
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "049_loan_ledger"
down_revision: Union[str, None] = "048_underwriting_ops"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_WORM_FUNCTION = "prevent_platform_loan_transactions_mutation"
_WORM_TRIGGER = "platform_loan_transactions_immutable"


def upgrade() -> None:
    txn_type = postgresql.ENUM(
        "payment", "disbursement", "fee", "adjustment", "reversal",
        name="platform_loan_txn_type",
    )
    payment_type = postgresql.ENUM(
        "cash", "check", "eft", "credit_card", "adjustment",
        name="platform_loan_payment_type",
    )
    repayment_mode = postgresql.ENUM(
        "regular", "add_on", "special", "payoff",
        name="platform_loan_repayment_mode",
    )
    bind = op.get_bind()
    txn_type.create(bind, checkfirst=True)
    payment_type.create(bind, checkfirst=True)
    repayment_mode.create(bind, checkfirst=True)

    op.create_table(
        "platform_loan_transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_loans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("reference", sa.String(), nullable=False),
        sa.Column(
            "txn_type",
            postgresql.ENUM(name="platform_loan_txn_type", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "payment_type",
            postgresql.ENUM(name="platform_loan_payment_type", create_type=False),
            nullable=True,
        ),
        sa.Column(
            "repayment_mode",
            postgresql.ENUM(name="platform_loan_repayment_mode", create_type=False),
            nullable=True,
        ),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("principal_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("interest_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("fees_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("add_on_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("processing_date", sa.Date(), nullable=False),
        sa.Column(
            "reverses_transaction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_loan_transactions.id"),
            nullable=True,
        ),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("comment", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("loan_id", "seq", name="uq_platform_loan_txn_loan_seq"),
        sa.CheckConstraint("amount_cents >= 0", name="ck_platform_loan_txn_amount_nonneg"),
        sa.CheckConstraint(
            "txn_type != 'reversal' OR reverses_transaction_id IS NOT NULL",
            name="ck_platform_loan_txn_reversal_ref",
        ),
    )
    op.create_index(
        "ix_platform_loan_txn_loan_effective",
        "platform_loan_transactions",
        ["loan_id", "effective_date"],
    )
    op.create_index(
        "ix_platform_loan_txn_reference", "platform_loan_transactions", ["reference"]
    )

    # --- WORM trigger: the ledger is append-only ---------------------------
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_WORM_FUNCTION}()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION
                'platform_loan_transactions is an immutable ledger (WORM). Corrections must be reversal rows, never UPDATE/DELETE.';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_WORM_TRIGGER}
        BEFORE UPDATE OR DELETE ON platform_loan_transactions
        FOR EACH ROW
        EXECUTE FUNCTION {_WORM_FUNCTION}();
        """
    )

    _backfill_from_payments(bind)
    _reconcile_balances_at_cutover(bind)


def _backfill_from_payments(bind) -> None:
    """Create ledger rows from existing platform_loan_payments.

    The historical principal/interest split is reconstructed by replaying the
    same allocation record_payment applied at the time: payments consume
    installments oldest-first, principal before interest within an installment.
    Any cash beyond the schedule stays on the row as principal (overpayment).
    """
    loans = bind.execute(
        sa.text(
            """
            SELECT DISTINCT l.id, a.vendor_id
            FROM platform_loans l
            JOIN platform_loan_payments p ON p.loan_id = l.id
            LEFT JOIN platform_credit_applications a ON a.id = l.application_id
            """
        )
    ).fetchall()

    insert = sa.text(
        """
        INSERT INTO platform_loan_transactions
            (id, loan_id, seq, reference, txn_type, payment_type, repayment_mode,
             amount_cents, principal_cents, interest_cents, fees_cents, add_on_cents,
             effective_date, processing_date, created_by, comment, created_at)
        VALUES
            (:id, :loan_id, :seq, :reference, 'payment', :payment_type, 'regular',
             :amount_cents, :principal_cents, :interest_cents, 0, 0,
             :effective_date, :processing_date, 'migration_049_backfill',
             'Backfilled from platform_loan_payments (schedule-replay allocation)',
             :created_at)
        """
    )

    for loan_id, vendor_id in loans:
        schedule = bind.execute(
            sa.text(
                """
                SELECT installment_number, principal_cents, interest_cents, total_cents
                FROM platform_loan_schedule
                WHERE loan_id = :loan_id
                ORDER BY installment_number
                """
            ),
            {"loan_id": loan_id},
        ).fetchall()
        payments = bind.execute(
            sa.text(
                """
                SELECT amount_cents, received_at, method, created_at
                FROM platform_loan_payments
                WHERE loan_id = :loan_id
                ORDER BY received_at, created_at
                """
            ),
            {"loan_id": loan_id},
        ).fetchall()

        # Replay allocation across ONE shared fill state (mirrors record_payment).
        paid = {row[0]: 0 for row in schedule}
        seq = 0
        for amount_cents, received_at, method, created_at in payments:
            seq += 1
            remaining = amount_cents
            principal_alloc = 0
            interest_alloc = 0
            for number, principal_cents, interest_cents, total_cents in schedule:
                if remaining <= 0:
                    break
                outstanding = total_cents - paid[number]
                if outstanding <= 0:
                    continue
                applied = min(remaining, outstanding)
                principal_outstanding = max(0, principal_cents - paid[number])
                principal_applied = min(applied, principal_outstanding)
                principal_alloc += principal_applied
                interest_alloc += applied - principal_applied
                paid[number] += applied
                remaining -= applied
            # Cash beyond the schedule: keep as principal (overpayment policy).
            principal_alloc += max(0, remaining)

            effective = received_at.date() if isinstance(received_at, datetime) else received_at
            bind.execute(
                insert,
                {
                    "id": str(uuid4()),
                    "loan_id": loan_id,
                    "seq": seq,
                    "reference": f"{vendor_id or 'none'}-{loan_id}-{seq}",
                    "payment_type": _payment_type_for_method(method),
                    "amount_cents": amount_cents,
                    "principal_cents": principal_alloc,
                    "interest_cents": interest_alloc,
                    "effective_date": effective,
                    "processing_date": effective,
                    "created_at": created_at,
                },
            )


def _accrue_cents(outstanding: int, rate_bps: int, days: int) -> int:
    """ACT/365 simple interest for a span, round-half-even — the exact math of
    app.services.interest_engine.accrue_interest_cents, reimplemented here so
    the migration never imports app code (migrations must stay frozen)."""
    if days <= 0 or outstanding <= 0 or rate_bps <= 0:
        return 0
    numerator = outstanding * rate_bps * days
    denominator = 10_000 * 365
    quotient, remainder = divmod(numerator, denominator)
    doubled = remainder * 2
    if doubled > denominator or (doubled == denominator and quotient % 2 == 1):
        quotient += 1
    return quotient


def _reconcile_balances_at_cutover(bind) -> None:
    """CUTOVER RECONCILIATION: tie every pre-existing loan's ledger to its
    operational balance, and restart interest accrual at the cutover date.

    Pre-existing loans fall into two camps the actuals engine cannot fully
    reconstruct on its own:

      * migrated / legacy loans (e.g. turnkey_migration): their
        ``principal_balance_cents`` was established OUTSIDE the payments table
        (no ledger history exists), so a pure replay would report the ORIGINAL
        principal as outstanding and years of phantom per-diem interest;
      * natively-serviced loans: their historical payments were allocated by
        the schedule engine (period interest), so a pure ACT/365 replay can
        drift from the operational balance by cents and carry a retroactive
        interest residue.

    For every loan where the engine's replay at the cutover date disagrees with
    the stored balance (or carries accrued interest), ONE non-cash
    ``adjustment`` row is appended, effective at the cutover date, that:

      * clears the replayed accrued interest (interest_cents), and
      * moves outstanding principal onto the stored ``principal_balance_cents``
        (principal_cents; NEGATIVE when outstanding must move UP — allocation
        columns are signed, only amount_cents is constrained non-negative).

    After the row: ledger outstanding == stored balance and interest due == 0.
    Everything forward is pure actuals. Interest accrued before cutover is the
    schedule engine's business (already embedded in the stored balance /
    historical allocations) — flagged for Dave in the PR.
    """
    from datetime import date as _date

    cutover = _date.today()

    loans = bind.execute(
        sa.text(
            """
            SELECT l.id, l.principal_cents, l.principal_balance_cents,
                   l.annual_rate_bps, l.disbursed_at, a.vendor_id
            FROM platform_loans l
            LEFT JOIN platform_credit_applications a ON a.id = l.application_id
            """
        )
    ).fetchall()

    insert = sa.text(
        """
        INSERT INTO platform_loan_transactions
            (id, loan_id, seq, reference, txn_type, payment_type, repayment_mode,
             amount_cents, principal_cents, interest_cents, fees_cents, add_on_cents,
             effective_date, processing_date, created_by, comment)
        VALUES
            (:id, :loan_id, :seq, :reference, 'adjustment', 'adjustment', NULL,
             :amount_cents, :principal_cents, :interest_cents, 0, 0,
             :effective_date, :effective_date, 'migration_049_backfill', :comment)
        """
    )
    comment = (
        "Ledger cutover reconciliation (non-cash): ties actuals-engine "
        "outstanding to the operational balance and restarts interest accrual "
        "at cutover"
    )

    for loan_id, principal_cents, stored_balance, rate_bps, disbursed_at, vendor_id in loans:
        rows = bind.execute(
            sa.text(
                """
                SELECT effective_date, principal_cents, interest_cents, seq
                FROM platform_loan_transactions
                WHERE loan_id = :loan_id AND txn_type IN ('payment', 'adjustment')
                ORDER BY effective_date, seq
                """
            ),
            {"loan_id": loan_id},
        ).fetchall()

        # Replay the engine's walk (same semantics as interest_engine
        # .compute_balances: accrue per span, interest first, excess interest
        # redirects to principal, clamps at zero).
        accrual_start = disbursed_at.date() if disbursed_at is not None else None
        outstanding = max(0, principal_cents)
        interest_due = 0
        cursor = accrual_start
        for eff, prin_paid, int_paid, _seq in rows:
            if eff > cutover:
                continue
            if cursor is not None and eff > cursor:
                interest_due += _accrue_cents(outstanding, rate_bps, (eff - cursor).days)
                cursor = eff
            excess = max(0, (int_paid or 0) - interest_due)
            interest_due = max(0, interest_due - (int_paid or 0))
            outstanding = max(0, outstanding - (prin_paid or 0) - excess)
        if cursor is not None and cutover > cursor:
            interest_due += _accrue_cents(outstanding, rate_bps, (cutover - cursor).days)

        principal_gap = outstanding - stored_balance  # >0: reduce; <0: raise
        if principal_gap == 0 and interest_due == 0:
            continue  # already consistent — no reconciliation row

        seq = (
            bind.execute(
                sa.text(
                    "SELECT COALESCE(MAX(seq), 0) FROM platform_loan_transactions "
                    "WHERE loan_id = :loan_id"
                ),
                {"loan_id": loan_id},
            ).scalar()
            + 1
        )
        bind.execute(
            insert,
            {
                "id": str(uuid4()),
                "loan_id": loan_id,
                "seq": seq,
                "reference": f"{vendor_id or 'none'}-{loan_id}-{seq}",
                "amount_cents": abs(principal_gap) + interest_due,
                "principal_cents": principal_gap,
                "interest_cents": interest_due,
                "effective_date": cutover,
                "comment": comment,
            },
        )


def _payment_type_for_method(method) -> str:
    """Map the free-text payment ``method`` to the reconciliation enum."""
    m = (method or "").lower()
    if "cash" in m:
        return "cash"
    if "check" in m or "cheque" in m:
        return "check"
    if "card" in m:
        return "credit_card"
    if "adjust" in m:
        return "adjustment"
    # zumrails_collection / manual / bank transfer / unknown → EFT (bank rail).
    return "eft"


def downgrade() -> None:
    op.execute(
        f"DROP TRIGGER IF EXISTS {_WORM_TRIGGER} ON platform_loan_transactions"
    )
    op.execute(f"DROP FUNCTION IF EXISTS {_WORM_FUNCTION}()")
    op.drop_index("ix_platform_loan_txn_reference", table_name="platform_loan_transactions")
    op.drop_index(
        "ix_platform_loan_txn_loan_effective", table_name="platform_loan_transactions"
    )
    op.drop_table("platform_loan_transactions")
    for enum_name in (
        "platform_loan_repayment_mode",
        "platform_loan_payment_type",
        "platform_loan_txn_type",
    ):
        postgresql.ENUM(name=enum_name).drop(op.get_bind(), checkfirst=True)
