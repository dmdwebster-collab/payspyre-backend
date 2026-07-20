"""Repayment modes + scheduled-transaction surgery (WS-F, Turnkey parity P0)

Revision ID: 050_repayment_modes
Revises: 049_loan_ledger
Create Date: 2026-07-10

Two additions, both minimal (03__WP_Servicing — Dave's spec):

1. ``platform_loan_schedule_status`` enum gains ``suspended`` — a staff-parked
   installment (scheduled-transaction surgery). The delinquency-aging and
   dunning jobs skip suspended items; the money is still owed. Idempotent via
   ``ADD VALUE IF NOT EXISTS`` inside an ``autocommit_block`` (``ALTER TYPE …
   ADD VALUE`` must run outside a transaction on older Postgres — same pattern
   as migration 024).

2. ``platform_loan_custom_transactions`` — staff-added one-off custom
   scheduled transactions (date / amount / repayment mode / MANDATORY comment)
   layered ON TOP of the amortization plan, which is never altered (Dave's
   borrower-protection rule). Rows are cancelled by status flip, never
   hard-deleted; ``processed_transaction_id`` links the immutable ledger row
   once executed (auto-collection WS-G / staff manual payment).

The repayment-mode enum + column on the money ledger already exist (049 created
them ahead of time so the WORM table never needs another migration).

No behaviour change on its own: suspended/custom rows only exist once staff use
the new permission-gated surgery endpoints. Money is integer cents.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "050_repayment_modes"
down_revision: Union[str, None] = "049_loan_ledger"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Schedule-item status: add 'suspended' (surgery). Outside-transaction
    #    ADD VALUE, idempotent (IF NOT EXISTS) — migration-024 pattern.
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE platform_loan_schedule_status "
            "ADD VALUE IF NOT EXISTS 'suspended'"
        )

    # 2. Custom scheduled transactions.
    custom_status = postgresql.ENUM(
        "scheduled", "processed", "cancelled",
        name="platform_loan_custom_txn_status",
    )
    custom_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "platform_loan_custom_transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_loans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scheduled_date", sa.Date(), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column(
            "repayment_mode",
            postgresql.ENUM(name="platform_loan_repayment_mode", create_type=False),
            nullable=False,
            server_default="regular",
        ),
        sa.Column(
            "status",
            postgresql.ENUM(name="platform_loan_custom_txn_status", create_type=False),
            nullable=False,
            server_default="scheduled",
        ),
        sa.Column(
            "processed_transaction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_loan_transactions.id"),
            nullable=True,
        ),
        sa.Column("comment", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("cancelled_by", sa.String(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "amount_cents > 0", name="ck_platform_loan_custom_txn_amount_positive"
        ),
    )
    op.create_index(
        "ix_platform_loan_custom_txn_loan",
        "platform_loan_custom_transactions",
        ["loan_id", "scheduled_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_platform_loan_custom_txn_loan",
        table_name="platform_loan_custom_transactions",
    )
    op.drop_table("platform_loan_custom_transactions")
    postgresql.ENUM(name="platform_loan_custom_txn_status").drop(
        op.get_bind(), checkfirst=True
    )
    # Postgres has no DROP VALUE for enums: 'suspended' stays on
    # platform_loan_schedule_status (harmless — nothing writes it once the
    # surgery endpoints are gone). Same one-way convention as migration 024.
    op.execute("SELECT 1")
