"""Underwriting depth (WS-D) — multi-offer approvals + 5-band scorecards

Revision ID: 058_underwriting_depth
Revises: 054_hardship
Create Date: 2026-07-20

Workstream D of the full-parity plan (docs/turnkey_parity/FULL_PARITY_PLAN.md).
NOTE FOR THE MERGE TRAIN: ``down_revision`` targets ``054_hardship`` — the single
head at the time this branch was cut. Parallel wave-1 workstreams claim slots
055-057; the orchestrator re-chains down_revisions sequentially at merge time.

Three additive pieces:

1. ``platform_loan_offers`` — the Turnkey "Create Loan Offers" model, Dave's
   way (02__WP_Underwriting.md f85-f87 + §5 "Offers"): an underwriter approves
   up to N offers (default 3, configurable); the borrower reviews them on their
   dashboard and picks EXACTLY ONE. Acceptance books the loan with the offer's
   terms and voids the unpicked siblings; offers expire after N days (default
   30). A partial unique index enforces at most one ACCEPTED offer per
   application at the database level.

2. ``platform_scorecards`` — Dave mandate #3: editable, additive bin-based
   scorecards over VERIFIED data with natural min/max (no clamping) and 5
   bands (Excellent/Good/Average/Weak/Poor-Fail), each band carrying a
   decision outcome + credit limit + rate. NO seed rows: with no scorecard
   configured the flow engine keeps today's 680/600 two-cut exactly, so the
   default decision path does not change.

3. ``platform_vendor_scorecards`` — per-vendor scorecard assignment (override
   of the platform default; one row per vendor, enforced unique).

All changes are additive; no existing table, row, or query is touched.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "058_underwriting_depth"
down_revision: Union[str, None] = "054_hardship"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- enums ---------------------------------------------------------------
    op.execute(
        "CREATE TYPE platform_loan_offer_status AS ENUM "
        "('offered', 'accepted', 'void', 'expired', 'withdrawn')"
    )
    offer_status = postgresql.ENUM(name="platform_loan_offer_status", create_type=False)

    op.execute(
        "CREATE TYPE platform_scorecard_status AS ENUM ('draft', 'active', 'archived')"
    )
    scorecard_status = postgresql.ENUM(name="platform_scorecard_status", create_type=False)

    # --- loan offers ---------------------------------------------------------
    op.create_table(
        "platform_loan_offers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "application_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_credit_applications.id"),
            nullable=False,
        ),
        sa.Column(
            "credit_product_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_credit_products.id"),
            nullable=False,
        ),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("term_months", sa.Integer(), nullable=False),
        sa.Column("annual_rate_bps", sa.Integer(), nullable=False),
        sa.Column(
            "payment_frequency", sa.String(), nullable=False, server_default="monthly"
        ),
        sa.Column("start_date", sa.Date(), nullable=True),
        sa.Column("first_due_date", sa.Date(), nullable=True),
        sa.Column("status", offer_status, nullable=False, server_default="offered"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_platform_loan_offers_application",
        "platform_loan_offers",
        ["application_id"],
    )
    op.create_index(
        "ix_platform_loan_offers_status_expires",
        "platform_loan_offers",
        ["status", "expires_at"],
    )
    # At most ONE accepted offer per application — the DB-level backstop for the
    # "borrower picks exactly one" invariant (the service also row-locks).
    op.execute(
        "CREATE UNIQUE INDEX uq_platform_loan_offers_one_accepted "
        "ON platform_loan_offers (application_id) WHERE status = 'accepted'"
    )

    # --- scorecards ----------------------------------------------------------
    op.create_table(
        "platform_scorecards",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", scorecard_status, nullable=False, server_default="draft"),
        sa.Column(
            "is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        # The editable definition: additive bin-based attributes + the 5 bands
        # (validated by app.services.scorecards.ScorecardDefinition on write).
        sa.Column("attributes", postgresql.JSONB(), nullable=False),
        sa.Column("bands", postgresql.JSONB(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    # At most one platform-default scorecard.
    op.execute(
        "CREATE UNIQUE INDEX uq_platform_scorecards_one_default "
        "ON platform_scorecards (is_default) WHERE is_default"
    )

    # --- per-vendor scorecard assignment ------------------------------------
    op.create_table(
        "platform_vendor_scorecards",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "vendor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vendors.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "scorecard_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_scorecards.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_platform_vendor_scorecards_scorecard",
        "platform_vendor_scorecards",
        ["scorecard_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_platform_vendor_scorecards_scorecard", "platform_vendor_scorecards"
    )
    op.drop_table("platform_vendor_scorecards")

    op.execute("DROP INDEX IF EXISTS uq_platform_scorecards_one_default")
    op.drop_table("platform_scorecards")

    op.execute("DROP INDEX IF EXISTS uq_platform_loan_offers_one_accepted")
    op.drop_index("ix_platform_loan_offers_status_expires", "platform_loan_offers")
    op.drop_index("ix_platform_loan_offers_application", "platform_loan_offers")
    op.drop_table("platform_loan_offers")

    op.execute("DROP TYPE platform_scorecard_status")
    op.execute("DROP TYPE platform_loan_offer_status")
