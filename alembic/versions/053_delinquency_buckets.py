"""Month-end delinquency bucket state machine (WS-H, Turnkey parity P0)

Revision ID: 053_delinquency_buckets
Revises: 049_loan_ledger
Create Date: 2026-07-10

NOTE ON CHAINING: origin/main's migration head was 049_loan_ledger when this
branch was cut; the Phase-2 merge train re-chains down_revisions sequentially
as siblings land (P0_BUILD_PLAN convention).

Dave's collections model (04__WP_Collections §4) replaces Turnkey's rolling
1-30/31-60/61-90/>91 DPD filters with MONTH-END SNAPSHOT buckets:

    current → current_month_late → pot_30 → pot_60 (credit-bureau reported)
            → pot_90 → default

plus a segregated **insolvency** classification (consumer proposal /
bankruptcy / credit counseling — a manual, audited staff determination that
overrides derivation) and terminal **written_off** (follows charged_off).

Schema:
  * ``platform_loan_delinquency_snapshots`` — one row per (loan, month); the
    snapshot job (app.jobs.bucket_snapshot) is idempotent per month and
    UPDATES the row on re-run. ``bureau_reportable`` is a FLAG only (pot_60+);
    actual Equifax reporting is a later workstream.
  * ``platform_loans.current_bucket`` — the last assigned bucket (buckets move
    only at month-end; insolvency/charge-off flip it immediately).
  * ``platform_loans.insolvency_status`` (+ marked_at/by) — nullable; NULL (or
    'none') = not insolvent.

No backfill: buckets start accruing from the first snapshot run. Existing
loans default to current_bucket='current' until then (live DPD is served
separately by the existing aging paths — behaviour unchanged).

Money is integer cents throughout (repo convention).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "053_delinquency_buckets"
down_revision: Union[str, None] = "052_vendor_origination"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bucket = postgresql.ENUM(
        "current",
        "current_month_late",
        "pot_30",
        "pot_60",
        "pot_90",
        "default",
        "insolvency",
        "written_off",
        name="platform_delinquency_bucket",
    )
    insolvency = postgresql.ENUM(
        "none",
        "consumer_proposal",
        "bankruptcy",
        "credit_counseling",
        name="platform_insolvency_status",
    )
    bind = op.get_bind()
    bucket.create(bind, checkfirst=True)
    insolvency.create(bind, checkfirst=True)

    op.create_table(
        "platform_loan_delinquency_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_loans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # First day of the reported month; evaluation date is its LAST day.
        sa.Column("snapshot_month", sa.Date(), nullable=False),
        sa.Column(
            "bucket",
            postgresql.ENUM(name="platform_delinquency_bucket", create_type=False),
            nullable=False,
        ),
        sa.Column("days_past_due", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "amount_past_due_cents", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "outstanding_principal_cents",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "bureau_reportable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "snapshotted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "loan_id", "snapshot_month", name="uq_platform_loan_delinq_snap_month"
        ),
    )
    op.create_index(
        "ix_platform_loan_delinq_snap_month_bucket",
        "platform_loan_delinquency_snapshots",
        ["snapshot_month", "bucket"],
    )

    op.add_column(
        "platform_loans",
        sa.Column(
            "current_bucket",
            postgresql.ENUM(name="platform_delinquency_bucket", create_type=False),
            nullable=False,
            server_default="current",
        ),
    )
    op.add_column(
        "platform_loans",
        sa.Column(
            "insolvency_status",
            postgresql.ENUM(name="platform_insolvency_status", create_type=False),
            nullable=True,
        ),
    )
    op.add_column(
        "platform_loans",
        sa.Column("insolvency_marked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "platform_loans",
        sa.Column("insolvency_marked_by", sa.String(), nullable=True),
    )

    # Terminal statuses are bucketed immediately so the book reads correctly
    # even before the first snapshot run: charged_off → written_off.
    op.execute(
        "UPDATE platform_loans SET current_bucket = 'written_off' "
        "WHERE status = 'charged_off'"
    )


def downgrade() -> None:
    op.drop_column("platform_loans", "insolvency_marked_by")
    op.drop_column("platform_loans", "insolvency_marked_at")
    op.drop_column("platform_loans", "insolvency_status")
    op.drop_column("platform_loans", "current_bucket")
    op.drop_index(
        "ix_platform_loan_delinq_snap_month_bucket",
        table_name="platform_loan_delinquency_snapshots",
    )
    op.drop_table("platform_loan_delinquency_snapshots")
    for enum_name in ("platform_insolvency_status", "platform_delinquency_bucket"):
        postgresql.ENUM(name=enum_name).drop(op.get_bind(), checkfirst=True)
