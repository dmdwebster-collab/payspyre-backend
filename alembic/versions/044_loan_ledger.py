"""Immutable loan ledger — platform_loan_transactions (WS-A, Turnkey parity P0)

Revision ID: 044_loan_ledger
Revises: 043_canonical_credit_application
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

BACKFILL: existing ``platform_loan_payments`` rows are replayed into ledger
rows (txn_type='payment', effective = processing = received date). The
principal/interest split is reconstructed by replaying the exact allocation
``record_payment`` used historically (oldest unpaid installment first,
principal before interest within an installment) — the only historical truth
available. Fees/add-on are 0 (no fee machinery existed before this ledger).

Money is integer cents throughout (repo convention).
"""
from datetime import datetime
from typing import Sequence, Union
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "044_loan_ledger"
down_revision: Union[str, None] = "043_canonical_credit_application"
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
             :effective_date, :processing_date, 'migration_044_backfill',
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
