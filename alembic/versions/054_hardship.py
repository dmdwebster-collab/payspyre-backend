"""Hardship v1 — deferment + due-date change, e-sign gated (WS-J, Turnkey parity P0)

Revision ID: 054_hardship
Revises: 050_repayment_modes
Create Date: 2026-07-10

NOTE (merge train): chained onto ``050_repayment_modes`` because WS-J builds
directly on WS-F's schedule-surgery primitives. The merge train re-chains this
down_revision to the final Phase-2 head (expected ``053_delinquency_buckets``)
— a one-line change here plus the pin in ``tests/test_hardship.py``.

One table: ``platform_hardship_requests`` — Dave's "Rescheduling → Hardship"
rework (03__WP_Servicing §5): every hardship change requires a borrower-signed
amendment BEFORE any back-end schedule change. The request row is the legal
paper trail: what was asked, the exact previewed effect, the e-sign envelope,
when it was signed, when it was applied, and the ``snap_back`` snapshot of the
original schedule state captured immediately before mutation.

Status flow (forward-only):
    draft → awaiting_signature → active → completed
                    ├→ declined   (borrower declined the amendment)
                    └→ expired    (signature window lapsed)
    draft/awaiting_signature → cancelled (staff withdraw)

No behaviour change on its own: rows only exist once staff use the new
permission-gated hardship endpoints, and NOTHING touches a loan schedule until
``signed_at`` is set (pinned by tests). Money is integer cents.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "054_hardship"
down_revision: Union[str, None] = "053_delinquency_buckets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    kind = postgresql.ENUM(
        "deferment", "due_date_change",
        name="platform_hardship_kind",
    )
    kind.create(op.get_bind(), checkfirst=True)

    status = postgresql.ENUM(
        "draft",
        "awaiting_signature",
        "active",
        "declined",
        "cancelled",
        "expired",
        "completed",
        name="platform_hardship_status",
    )
    status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "platform_hardship_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_loans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "kind",
            postgresql.ENUM(name="platform_hardship_kind", create_type=False),
            nullable=False,
        ),
        # Kind-specific parameters:
        #   deferment:       {"installment_ids": [uuid, ...]}
        #   due_date_change: {"new_day_of_month": 15}  OR
        #                    {"item_shifts": [{"item_id": uuid, "new_due_date": "YYYY-MM-DD"}]}
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="platform_hardship_status", create_type=False),
            nullable=False,
            server_default="draft",
        ),
        # MANDATORY (Dave): why this hardship exists + the staff working comment.
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("comment", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        # The exact schedule-effect preview computed at draft time (items
        # suspended/shifted, dates, estimated interest impact) — what the
        # borrower is shown and what the amendment summarizes.
        sa.Column("preview", postgresql.JSONB(), nullable=True),
        # E-signature (SignNow) — the amendment document.
        sa.Column("esign_document_ref", sa.String(), nullable=True),
        sa.Column("signature_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("signature_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
        # Application of the change (only ever after signed_at).
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        # Original schedule state needed to restore/complete (captured
        # immediately before mutation): prior item statuses/due dates + the
        # custom-transaction ids the deferment appended.
        sa.Column("snap_back", postgresql.JSONB(), nullable=True),
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
    )
    op.create_index(
        "ix_platform_hardship_requests_loan",
        "platform_hardship_requests",
        ["loan_id", "created_at"],
    )
    # The webhook resolves a hardship amendment by its SignNow document id.
    op.create_index(
        "ix_platform_hardship_requests_esign_ref",
        "platform_hardship_requests",
        ["esign_document_ref"],
        unique=True,
        postgresql_where=sa.text("esign_document_ref IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_platform_hardship_requests_esign_ref",
        table_name="platform_hardship_requests",
    )
    op.drop_index(
        "ix_platform_hardship_requests_loan",
        table_name="platform_hardship_requests",
    )
    op.drop_table("platform_hardship_requests")
    postgresql.ENUM(name="platform_hardship_status").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="platform_hardship_kind").drop(op.get_bind(), checkfirst=True)
