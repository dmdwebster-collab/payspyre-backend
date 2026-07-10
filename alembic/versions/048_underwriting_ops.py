"""Underwriting ops — decision-reason directories + application assignment (WS-E)

Revision ID: 048_underwriting_ops
Revises: 043_canonical_credit_application
Create Date: 2026-07-10

Two additive pieces of underwriting operational completeness (Turnkey parity,
docs/turnkey_parity/02__WP_Underwriting.md):

1. ``platform_decision_reasons`` — the admin-editable directory of REJECT and
   CANCEL reasons (Turnkey keeps these in a Settings directory; Dave: reasons
   must be "standardized ... compliant with all regulations ... not
   discriminatory and defensible"). Seeded with:
     * reject reasons mapped from the engine's stable ``decision_reasons`` codes
       (flow_engine.py) with borrower-facing wording consistent with the
       adverse-action notice (adverse_action._REASON_TEXT), plus staff-usable
       Turnkey-style codes;
     * cancel reasons (non-credit closure): customer request, duplicate
       application, vendor request, offer expired, bank verification expired,
       other.
   Rows are soft-deactivated (``active=false``), never hard-deleted — a code
   that was ever used in a decision must stay resolvable for audit.

2. Application assignment: ``assigned_to_user_id`` (FK users, SET NULL on user
   delete) + ``assigned_at`` on ``platform_credit_applications`` — the
   underwriting queue's assignee lane.

All changes are additive; no existing row or query breaks.

Seed data lives in ``app.services.decision_reasons`` (REJECT_REASON_SEEDS /
CANCEL_REASON_SEEDS + the idempotent ``seed_defaults``) — the single source of
truth shared with the test-DB fixture, whose per-test TRUNCATE would otherwise
wipe rows this migration inserted.

FLAGGED FOR DAVE: the borrower_facing_text seed wordings require his (and
counsel's) review before launch — they are editable in the directory afterwards.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "048_underwriting_ops"
down_revision: Union[str, None] = "043_canonical_credit_application"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- reason-kind enum ----------------------------------------------------
    op.execute(
        "CREATE TYPE platform_decision_reason_kind AS ENUM ('reject', 'cancel')"
    )
    kind_enum = postgresql.ENUM(name="platform_decision_reason_kind", create_type=False)

    # --- directory table -------------------------------------------------------
    op.create_table(
        "platform_decision_reasons",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", kind_enum, nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("internal_label", sa.String(), nullable=False),
        sa.Column("borrower_facing_text", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "uq_platform_decision_reasons_kind_code",
        "platform_decision_reasons",
        ["kind", "code"],
        unique=True,
    )

    # --- seeds -----------------------------------------------------------------
    # Single source of truth for the default directory contents:
    # app.services.decision_reasons (shared with the test-DB fixture, which
    # truncates + re-seeds the table before every test). Idempotent — ON
    # CONFLICT (kind, code) DO NOTHING.
    from app.services.decision_reasons import seed_defaults

    seed_defaults(op.get_bind())

    # --- application assignment --------------------------------------------------
    op.add_column(
        "platform_credit_applications",
        sa.Column(
            "assigned_to_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "platform_credit_applications",
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_platform_credit_applications_assigned_to",
        "platform_credit_applications",
        ["assigned_to_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_platform_credit_applications_assigned_to",
        "platform_credit_applications",
    )
    op.drop_column("platform_credit_applications", "assigned_at")
    op.drop_column("platform_credit_applications", "assigned_to_user_id")

    op.drop_index(
        "uq_platform_decision_reasons_kind_code", "platform_decision_reasons"
    )
    op.drop_table("platform_decision_reasons")
    op.execute("DROP TYPE IF EXISTS platform_decision_reason_kind")
