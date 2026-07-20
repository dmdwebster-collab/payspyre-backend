"""Auto-collection engine — per-loan auto-charge switch + collection attempts (WS-G, Turnkey parity P0)

Revision ID: 051_auto_collection
Revises: 049_loan_ledger
Create Date: 2026-07-10

Turnkey auto-charges scheduled PAD payments on their due dates; PaySpyre only
had borrower-initiated Pay Now. This migration adds the persistence for the
flag-gated auto-collection engine (``app/services/auto_collection.py``):

1. ``platform_loans.auto_charge_enabled`` (nullable Boolean) — the per-loan
   "Disable Auto-Charges" kill switch from the Servicing workplace
   (03__WP_Servicing §f0026-f0031). NULL means "inherit the platform default"
   (enabled — but the ENGINE itself is inert until the
   ``AUTO_COLLECTION_ENABLED`` feature flag is set, so no live behaviour
   changes with this migration). Explicit True/False is a staff decision
   (or an automatic dead-account disable).
2. ``platform_loans.auto_charge_disabled_reason`` — mandatory free-text reason
   captured when auto-charges are disabled (staff action requires it; the
   dead-account path writes ``dead-account return code: <code>``).
3. ``platform_collection_attempts`` — one row per (schedule item, attempt #):
   the idempotency spine of the engine. The UNIQUE (schedule_item_id,
   attempt_number) constraint makes a duplicate cron run / crash-restart
   physically unable to double-charge: an attempt row is claimed (committed)
   BEFORE the Zumrails call, and the deterministic
   ``client_transaction_id = autocol-{schedule_item_id}-{attempt_number}``
   gives the payment rail a stable dedupe handle per attempt.

Money is integer cents (repo convention). No enum types — ``outcome`` is a
CHECK-constrained string so adding outcomes never needs an ALTER TYPE.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "051_auto_collection"
down_revision: Union[str, None] = "050_repayment_modes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- per-loan auto-charge switch (Disable Auto-Charges) -----------------
    op.add_column(
        "platform_loans",
        sa.Column("auto_charge_enabled", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "platform_loans",
        sa.Column("auto_charge_disabled_reason", sa.String(), nullable=True),
    )

    # --- collection attempts (idempotency spine) ----------------------------
    op.create_table(
        "platform_collection_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_loans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "schedule_item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_loan_schedule.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        # Deterministic idempotency ref sent to Zumrails as ClientTransactionId:
        # ``autocol-{schedule_item_id}-{attempt_number}``.
        sa.Column("client_transaction_id", sa.String(), nullable=False),
        # Zumrails transaction id — NULL until the create-collection call returns.
        sa.Column("external_ref", sa.String(), nullable=True),
        sa.Column(
            "outcome", sa.String(), nullable=False, server_default="pending"
        ),
        # Vendor return / failure code on a failed pull (drives NSF + dead-account).
        sa.Column("return_code", sa.String(), nullable=True),
        # Adapter-level error detail when the create call itself blew up.
        sa.Column("error", sa.String(), nullable=True),
        # The NSF fee ledger row charged for THIS failed attempt (idempotency
        # marker: at most one NSF fee per failed attempt).
        sa.Column(
            "nsf_fee_transaction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_loan_transactions.id"),
            nullable=True,
        ),
        sa.Column(
            "initiated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by", sa.String(), nullable=False, server_default="auto_collection"
        ),
        sa.UniqueConstraint(
            "schedule_item_id",
            "attempt_number",
            name="uq_platform_collection_attempt_item_n",
        ),
        sa.CheckConstraint(
            "amount_cents > 0", name="ck_platform_collection_attempt_amount_pos"
        ),
        sa.CheckConstraint(
            "outcome IN ('pending', 'completed', 'failed', 'cancelled')",
            name="ck_platform_collection_attempt_outcome",
        ),
    )
    op.create_index(
        "ix_platform_collection_attempts_loan",
        "platform_collection_attempts",
        ["loan_id"],
    )
    # The webhook resolves an attempt by the Zumrails transaction id.
    op.create_index(
        "ix_platform_collection_attempts_ref",
        "platform_collection_attempts",
        ["external_ref"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_platform_collection_attempts_ref",
        table_name="platform_collection_attempts",
    )
    op.drop_index(
        "ix_platform_collection_attempts_loan",
        table_name="platform_collection_attempts",
    )
    op.drop_table("platform_collection_attempts")
    op.drop_column("platform_loans", "auto_charge_disabled_reason")
    op.drop_column("platform_loans", "auto_charge_enabled")
