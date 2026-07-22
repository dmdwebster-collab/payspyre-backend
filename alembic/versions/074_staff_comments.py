"""Internal staff comments (Dave's Comments tab on application + loan).

Revision ID: 074_staff_comments
Revises: 073_risk_score_model
Create Date: 2026-07-22

RE-CHAINED 2026-07-22: this revision was authored against ``072`` while the
sibling branch ``feat/risk-score-model`` was still open. That branch merged
first (PR #204), so ``073_risk_score_model`` landed on ``main`` and
``down_revision`` was repointed at it — leaving ``072`` with a single child and
the history linear. Nothing here depends on 073's tables; the re-chain is
ordering only.

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
down_revision = "073_risk_score_model"
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
