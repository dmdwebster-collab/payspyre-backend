"""Loan lifecycle: agreement + disbursement tracking + statements

Revision ID: 027_loan_lifecycle
Revises: 026_create_platform_loans
Create Date: 2026-06-19

Part of the LMS lifecycle wiring (P9.x). Migration 026 created the loan spine
(loans + schedule + payments). This migration connects the standalone pieces
(SignNow e-sign + Zumrails disbursement) into a real loan *lifecycle* and adds
the servicing artifacts the lifecycle produces (periodic statements).

What it adds to ``platform_loans``:
  - ``agreement_status``    — where the SignNow loan-agreement signature is at:
        not_sent -> sent -> signed (or declined).
  - ``agreement_ref``       — the SignNow document id (durable vendor ref).
  - ``disbursement_status`` — where the Zumrails payout is at:
        not_started -> in_progress -> completed (or failed).
  - ``disbursement_ref``    — the Zumrails transaction id (durable vendor ref).

New table ``platform_loan_statements`` — one periodic billing statement per
period, snapshotting the opening/closing balance and the principal/interest
paid in the window. Money is integer cents (BigInteger), matching the spine.

Downgrade drops the statements table, then the loan columns, then the enum
types. Real, reversible. NOTE: another agent's migration 028 chains off this
revision id — keep ``revision = "027_loan_lifecycle"`` exact.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "027_loan_lifecycle"
down_revision: Union[str, None] = "026_create_platform_loans"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ===========================================================================
    # NEW ENUM TYPES for agreement + disbursement lifecycle
    # ===========================================================================
    op.execute("""
        CREATE TYPE platform_loan_agreement_status AS ENUM (
            'not_sent',
            'sent',
            'signed',
            'declined'
        )
    """)
    op.execute("""
        CREATE TYPE platform_loan_disbursement_status AS ENUM (
            'not_started',
            'in_progress',
            'completed',
            'failed'
        )
    """)

    agreement_enum = postgresql.ENUM(
        name="platform_loan_agreement_status", create_type=False
    )
    disbursement_enum = postgresql.ENUM(
        name="platform_loan_disbursement_status", create_type=False
    )

    # ===========================================================================
    # EXTEND platform_loans
    # ===========================================================================
    op.add_column(
        "platform_loans",
        sa.Column(
            "agreement_status",
            agreement_enum,
            nullable=False,
            server_default="not_sent",
        ),
    )
    # SignNow document id of the loan agreement. NULL until an agreement is sent.
    op.add_column(
        "platform_loans",
        sa.Column("agreement_ref", sa.String(), nullable=True),
    )
    op.add_column(
        "platform_loans",
        sa.Column(
            "disbursement_status",
            disbursement_enum,
            nullable=False,
            server_default="not_started",
        ),
    )
    # Zumrails transaction id of the disbursement. NULL until disbursement starts.
    op.add_column(
        "platform_loans",
        sa.Column("disbursement_ref", sa.String(), nullable=True),
    )

    # External-ref lookups for idempotent webhook ingestion (SignNow doc id /
    # Zumrails txn id -> the loan they belong to).
    op.create_index(
        "idx_platform_loans_agreement_ref",
        "platform_loans",
        ["agreement_ref"],
        postgresql_where=sa.text("agreement_ref IS NOT NULL"),
    )
    op.create_index(
        "idx_platform_loans_disbursement_ref",
        "platform_loans",
        ["disbursement_ref"],
        postgresql_where=sa.text("disbursement_ref IS NOT NULL"),
    )

    # ===========================================================================
    # CREATE platform_loan_statements TABLE
    # ===========================================================================
    op.create_table(
        "platform_loan_statements",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_loans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The billing window this statement covers (inclusive of activity in it).
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        # Principal balance at the start / end of the window.
        sa.Column("opening_balance_cents", sa.BigInteger(), nullable=False),
        sa.Column("closing_balance_cents", sa.BigInteger(), nullable=False),
        # Principal + interest actually paid during the window.
        sa.Column("principal_paid_cents", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("interest_paid_cents", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
    )

    op.create_index(
        "idx_platform_loan_statements_loan",
        "platform_loan_statements",
        ["loan_id"],
    )
    # One statement per (loan, period) — idempotent statement generation.
    op.create_index(
        "idx_platform_loan_statements_loan_period",
        "platform_loan_statements",
        ["loan_id", "period_start", "period_end"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_platform_loan_statements_loan_period",
        table_name="platform_loan_statements",
    )
    op.drop_index(
        "idx_platform_loan_statements_loan",
        table_name="platform_loan_statements",
    )
    op.drop_table("platform_loan_statements")

    op.drop_index("idx_platform_loans_disbursement_ref", table_name="platform_loans")
    op.drop_index("idx_platform_loans_agreement_ref", table_name="platform_loans")

    op.drop_column("platform_loans", "disbursement_ref")
    op.drop_column("platform_loans", "disbursement_status")
    op.drop_column("platform_loans", "agreement_ref")
    op.drop_column("platform_loans", "agreement_status")

    op.execute("DROP TYPE IF EXISTS platform_loan_disbursement_status")
    op.execute("DROP TYPE IF EXISTS platform_loan_agreement_status")
