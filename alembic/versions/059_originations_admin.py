"""Originations admin depth — WS-E of the full-parity plan (video 01).

Revision ID: 059_originations_admin
Revises: 054_hardship
Create Date: 2026-07-20

NOTE (merge train): chained onto ``054_hardship`` — the single head at branch
time. Wave-1 workstreams A-D hold slots 055-058; the orchestrator re-chains
this ``down_revision`` if another workstream merges first (one-line change).

Three additive pieces, no behaviour change on their own:

1. ``platform_credit_applications.branch`` — TL keeps a branch on every file
   and the pipeline filters on it. Dave skipped the branch-offices MODULE
   ("single-branch reality") but the field stays (FULL_PARITY_PLAN omissions
   list), so imports/UI can carry it and the pipeline can filter on it.

2. ``platform_flag_definitions`` — the admin-managed flag directory
   (customer/loan flags, video 01 §flags). ``suppress_notifications=true``
   definitions gate the notification processor's vendor sends (suppress the
   send, still write the ``notification_skipped`` audit row). Soft-deactivate
   only (``is_active``), matching the decision-reason directory idiom.

3. ``platform_flag_assignments`` — a flag raised against a customer
   (``patient_id``) or a loan (``loan_id``) with a free-text reason. Clearing
   is an UPDATE (``cleared_at``/``cleared_by``), never a DELETE — the row is
   part of the file's history. Partial unique indexes stop the same active
   flag being raised twice on the same subject.

Waiting-time/pipeline sorting reuses the existing ``status_updated_at``
column (stamped by every audited status transition); an index is added here
so the queue can sort/filter on it at whole-book size.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "059_originations_admin"
down_revision: Union[str, None] = "054_hardship"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. branch field on applications (module intentionally not built).
    op.add_column(
        "platform_credit_applications",
        sa.Column("branch", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_platform_credit_applications_branch",
        "platform_credit_applications",
        ["branch"],
    )
    # Waiting-time sort/filter support for the pipeline queue.
    op.create_index(
        "ix_platform_credit_applications_status_updated_at",
        "platform_credit_applications",
        ["status_updated_at"],
    )

    # 2. Flag directory.
    op.create_table(
        "platform_flag_definitions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("code", sa.String(), nullable=False, unique=True),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        # customer | loan | both — plain string + CHECK (no PG enum churn).
        sa.Column("applies_to", sa.String(), nullable=False, server_default="both"),
        sa.Column(
            "suppress_notifications",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
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
            "applies_to IN ('customer', 'loan', 'both')",
            name="ck_platform_flag_definitions_applies_to",
        ),
    )

    # 3. Flag assignments (customer OR loan subject; cleared, never deleted).
    op.create_table(
        "platform_flag_assignments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "flag_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_flag_definitions.id"),
            nullable=False,
        ),
        sa.Column(
            "patient_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_patients.id"),
            nullable=True,
        ),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_loans.id"),
            nullable=True,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleared_by", sa.String(), nullable=True),
        sa.Column("cleared_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "(patient_id IS NOT NULL) OR (loan_id IS NOT NULL)",
            name="ck_platform_flag_assignments_subject",
        ),
    )
    op.create_index(
        "ix_platform_flag_assignments_flag_id", "platform_flag_assignments", ["flag_id"]
    )
    op.create_index(
        "ix_platform_flag_assignments_patient_id",
        "platform_flag_assignments",
        ["patient_id"],
    )
    op.create_index(
        "ix_platform_flag_assignments_loan_id", "platform_flag_assignments", ["loan_id"]
    )
    # One ACTIVE instance of a given flag per subject.
    op.create_index(
        "uq_platform_flag_assignments_active_patient",
        "platform_flag_assignments",
        ["flag_id", "patient_id"],
        unique=True,
        postgresql_where=sa.text("cleared_at IS NULL AND patient_id IS NOT NULL"),
    )
    op.create_index(
        "uq_platform_flag_assignments_active_loan",
        "platform_flag_assignments",
        ["flag_id", "loan_id"],
        unique=True,
        postgresql_where=sa.text("cleared_at IS NULL AND loan_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_platform_flag_assignments_active_loan", table_name="platform_flag_assignments"
    )
    op.drop_index(
        "uq_platform_flag_assignments_active_patient",
        table_name="platform_flag_assignments",
    )
    op.drop_index(
        "ix_platform_flag_assignments_loan_id", table_name="platform_flag_assignments"
    )
    op.drop_index(
        "ix_platform_flag_assignments_patient_id", table_name="platform_flag_assignments"
    )
    op.drop_index(
        "ix_platform_flag_assignments_flag_id", table_name="platform_flag_assignments"
    )
    op.drop_table("platform_flag_assignments")
    op.drop_table("platform_flag_definitions")
    op.drop_index(
        "ix_platform_credit_applications_status_updated_at",
        table_name="platform_credit_applications",
    )
    op.drop_index(
        "ix_platform_credit_applications_branch",
        table_name="platform_credit_applications",
    )
    op.drop_column("platform_credit_applications", "branch")
