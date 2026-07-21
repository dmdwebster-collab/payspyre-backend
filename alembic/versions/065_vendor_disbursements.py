"""Vendor self-serve disbursements (W2-DISB, Turnkey parity video 10)

Revision ID: 065_vendor_disbursements
Revises: 064_borrower_depth
Create Date: 2026-07-20

NOTE (merge train): down_revision is pinned to the single head at branch time
(``064_borrower_depth``). If the wave-2 integration takes other slots first the
orchestrator re-chains this to the final head — a one-line change here.

ONE additive table, ``platform_vendor_disbursements`` — the money-OUT ledger
for payouts of collected funds from PaySpyre to a vendor (clinic). The vendor
wallet (MTD collected / due / available) is DERIVED on read from the existing
money ledger (``platform_loan_transactions``) minus this table's settled +
in-flight payouts and the clearing holdback — nothing here changes existing
money/interest/ledger math. No new Postgres enums (statuses are strings +
CHECK constraints). Money is integer cents. Creating the table moves no money:
the whole engine is a no-op until ``VENDOR_DISBURSEMENTS_ENABLED`` is flipped.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "065_vendor_disbursements"
down_revision: Union[str, None] = "064_borrower_depth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "platform_vendor_disbursements",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "vendor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vendors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column(
            "fee_cents", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column("holdback_cutoff", sa.Date(), nullable=False),
        sa.Column("period_year", sa.Integer(), nullable=True),
        sa.Column("period_month", sa.Integer(), nullable=True),
        sa.Column("client_transaction_id", sa.String(), nullable=False),
        sa.Column("external_ref", sa.String(), nullable=True),
        sa.Column("return_code", sa.String(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("requested_by", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "client_transaction_id",
            name="uq_platform_vendor_disbursement_client_txn",
        ),
        sa.CheckConstraint(
            "amount_cents > 0", name="ck_platform_vendor_disbursement_amount_pos"
        ),
        sa.CheckConstraint(
            "fee_cents >= 0", name="ck_platform_vendor_disbursement_fee_nonneg"
        ),
        sa.CheckConstraint(
            "kind IN ('auto_monthly', 'extra')",
            name="ck_platform_vendor_disbursement_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed', 'cancelled')",
            name="ck_platform_vendor_disbursement_status",
        ),
    )
    op.create_index(
        "ix_platform_vendor_disbursement_vendor_status",
        "platform_vendor_disbursements",
        ["vendor_id", "status"],
    )
    op.create_index(
        "ix_platform_vendor_disbursement_external_ref",
        "platform_vendor_disbursements",
        ["external_ref"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_platform_vendor_disbursement_external_ref",
        table_name="platform_vendor_disbursements",
    )
    op.drop_index(
        "ix_platform_vendor_disbursement_vendor_status",
        table_name="platform_vendor_disbursements",
    )
    op.drop_table("platform_vendor_disbursements")
