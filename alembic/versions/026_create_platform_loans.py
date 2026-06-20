"""Create platform loan-servicing (LMS) tables

Revision ID: 026_create_platform_loans
Revises: 025_create_platform_integration_settings
Create Date: 2026-06-19

Part of P9.0: Loan Servicing (LMS) Spine.

The platform's mandate is to replace Turnkey Lender as both LOS (origination —
already built across PRs P1–P8) AND LMS (loan management / servicing — this PR).
This is the FOUNDATIONAL servicing layer, a first slice — not the whole LMS:

  - platform_loans          — a funded loan booked from an approved application.
  - platform_loan_schedule  — the amortization schedule (one row per installment).
  - platform_loan_payments  — payments received against a loan.

Money is stored in integer cents (BigInteger) throughout — never float — mirroring
the *_amount_cents convention already used on platform_credit_applications and
platform_credit_products.

Lifecycle (status enum on platform_loans):
  pending_disbursement -> active -> paid_off
                                 \-> delinquent
                                 \-> charged_off
  cancelled (terminal, pre-disbursement)

A loan is created in 'pending_disbursement' (schedule generated, balance = full
principal) and only moves to 'active' once funds are actually disbursed (Zumrails
payout) AND the loan agreement is executed (SignNow). Those seams are NOT wired in
this PR — see service layer docstrings + the PR report.

Downgrade drops tables (children first), then enum types. Real, reversible.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "026_create_platform_loans"
down_revision: Union[str, None] = "025_create_platform_integration_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ===========================================================================
    # CREATE NEW ENUM TYPES
    # ===========================================================================

    # platform_loans.status enum
    op.execute("""
        CREATE TYPE platform_loan_status AS ENUM (
            'pending_disbursement',
            'active',
            'paid_off',
            'delinquent',
            'charged_off',
            'cancelled'
        )
    """)

    # platform_loan_schedule.status enum
    op.execute("""
        CREATE TYPE platform_loan_schedule_status AS ENUM (
            'scheduled',
            'paid',
            'partial',
            'late',
            'waived'
        )
    """)

    loan_status_enum = postgresql.ENUM(name="platform_loan_status", create_type=False)
    schedule_status_enum = postgresql.ENUM(name="platform_loan_schedule_status", create_type=False)

    # ===========================================================================
    # CREATE platform_loans TABLE
    # ===========================================================================

    op.create_table(
        "platform_loans",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # The approved application this loan was booked from.
        sa.Column(
            "application_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_credit_applications.id"),
            nullable=False,
        ),

        # Original loan terms (snapshot at booking; the amortization schedule is
        # derived from these and stored separately).
        sa.Column("principal_cents", sa.BigInteger(), nullable=False),
        # Annual interest rate in basis points (1% = 100 bps). e.g. 1299 = 12.99% APR.
        sa.Column("annual_rate_bps", sa.Integer(), nullable=False),
        sa.Column("term_months", sa.Integer(), nullable=False),

        sa.Column("status", loan_status_enum, nullable=False, server_default="pending_disbursement"),

        # Set when funds are actually disbursed (Zumrails payout). NULL until then.
        sa.Column("disbursed_at", sa.DateTime(timezone=True), nullable=True),

        # Outstanding principal. Initialized to principal_cents at booking and
        # reduced as payments are applied.
        sa.Column("principal_balance_cents", sa.BigInteger(), nullable=False),

        sa.Column("currency", sa.String(), nullable=False, server_default="CAD"),

        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )

    op.create_index("idx_platform_loans_application", "platform_loans", ["application_id"])
    op.create_index("idx_platform_loans_status", "platform_loans", ["status"])

    # ===========================================================================
    # CREATE platform_loan_schedule TABLE
    # ===========================================================================

    op.create_table(
        "platform_loan_schedule",
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
        # 1-based installment index within the loan.
        sa.Column("installment_number", sa.Integer(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),

        # Split of the equal monthly payment for THIS installment.
        sa.Column("principal_cents", sa.BigInteger(), nullable=False),
        sa.Column("interest_cents", sa.BigInteger(), nullable=False),
        # total_cents = principal_cents + interest_cents (the scheduled payment).
        sa.Column("total_cents", sa.BigInteger(), nullable=False),

        sa.Column("status", schedule_status_enum, nullable=False, server_default="scheduled"),
        # Cumulative cents applied to this installment (0..total_cents typically).
        sa.Column("paid_cents", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
    )

    op.create_index("idx_platform_loan_schedule_loan", "platform_loan_schedule", ["loan_id"])
    # Each installment number is unique within a loan.
    op.create_index(
        "idx_platform_loan_schedule_loan_installment",
        "platform_loan_schedule",
        ["loan_id", "installment_number"],
        unique=True,
    )

    # ===========================================================================
    # CREATE platform_loan_payments TABLE
    # ===========================================================================

    op.create_table(
        "platform_loan_payments",
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
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        # Free-form rail/method tag: 'zumrails', 'manual', 'eft', 'card', etc.
        sa.Column("method", sa.String(), nullable=False),
        # External rail reference, e.g. a Zumrails transaction id. Nullable for
        # manual / adjustment entries.
        sa.Column("external_ref", sa.String(), nullable=True),

        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )

    op.create_index("idx_platform_loan_payments_loan", "platform_loan_payments", ["loan_id"])
    # External-ref lookup for idempotent webhook ingestion (e.g. Zumrails txn id).
    op.create_index(
        "idx_platform_loan_payments_external_ref",
        "platform_loan_payments",
        ["external_ref"],
        postgresql_where=sa.text("external_ref IS NOT NULL"),
    )

    # ===========================================================================
    # CREATE TRIGGERS FOR updated_at
    # ===========================================================================

    op.execute("""
        CREATE OR REPLACE FUNCTION update_platform_loans_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER platform_loans_updated_at
        BEFORE UPDATE ON platform_loans
        FOR EACH ROW
        EXECUTE FUNCTION update_platform_loans_updated_at();
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION update_platform_loan_payments_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER platform_loan_payments_updated_at
        BEFORE UPDATE ON platform_loan_payments
        FOR EACH ROW
        EXECUTE FUNCTION update_platform_loan_payments_updated_at();
    """)


def downgrade() -> None:
    # Drop triggers + functions
    op.execute("DROP TRIGGER IF EXISTS platform_loan_payments_updated_at ON platform_loan_payments")
    op.execute("DROP FUNCTION IF EXISTS update_platform_loan_payments_updated_at()")
    op.execute("DROP TRIGGER IF EXISTS platform_loans_updated_at ON platform_loans")
    op.execute("DROP FUNCTION IF EXISTS update_platform_loans_updated_at()")

    # Drop indexes (table drops would cascade these, but explicit for clarity)
    op.drop_index("idx_platform_loan_payments_external_ref", table_name="platform_loan_payments")
    op.drop_index("idx_platform_loan_payments_loan", table_name="platform_loan_payments")
    op.drop_index("idx_platform_loan_schedule_loan_installment", table_name="platform_loan_schedule")
    op.drop_index("idx_platform_loan_schedule_loan", table_name="platform_loan_schedule")
    op.drop_index("idx_platform_loans_status", table_name="platform_loans")
    op.drop_index("idx_platform_loans_application", table_name="platform_loans")

    # Drop tables (children first)
    op.drop_table("platform_loan_payments")
    op.drop_table("platform_loan_schedule")
    op.drop_table("platform_loans")

    # Drop enum types
    op.execute("DROP TYPE IF EXISTS platform_loan_schedule_status")
    op.execute("DROP TYPE IF EXISTS platform_loan_status")
