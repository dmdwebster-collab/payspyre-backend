"""Internal staff comments (Dave's Comments tab on application + loan).

Revision ID: 074_staff_comments
Revises: 072_settings_backend_gaps
Create Date: 2026-07-22

RE-CHAINING NOTE: a sibling branch (``feat/risk-score-model``) is taking
``073``. This revision is numbered ``074`` but chains off ``072`` because
``073`` does not exist on ``main`` yet — whichever of the two merges SECOND
must repoint its ``down_revision`` at the other so ``main`` keeps a single
linear head. Nothing here depends on 073's tables.

Creates ``platform_staff_comments``: one internal note by a PaySpyre operator
about exactly one subject (an application XOR a loan; enforced by a CHECK).

Append-only by construction — no ``updated_at``, no ``deleted_at``. See the
model docstring for the reasoning; the short version is that an internal
underwriting note is only useful as evidence if it provably cannot be rewritten
after the fact. Corrections are follow-up rows.

Purely additive: a new table, no changes to existing ones, no backfill.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "074_staff_comments"
down_revision = "072_settings_backend_gaps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_staff_comments",
        # No server_default: ids are minted by the model (default=uuid4),
        # matching 051 / 071 and the rest of the platform_* tables.
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "application_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_credit_applications.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_loans.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "author_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(application_id IS NOT NULL) <> (loan_id IS NOT NULL)",
            name="ck_platform_staff_comments_one_subject",
        ),
    )
    op.create_index(
        "ix_platform_staff_comments_application",
        "platform_staff_comments",
        ["application_id", "created_at"],
    )
    op.create_index(
        "ix_platform_staff_comments_loan",
        "platform_staff_comments",
        ["loan_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_platform_staff_comments_loan", table_name="platform_staff_comments"
    )
    op.drop_index(
        "ix_platform_staff_comments_application", table_name="platform_staff_comments"
    )
    op.drop_table("platform_staff_comments")
