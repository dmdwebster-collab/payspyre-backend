"""P0 T3 — backends for the six dead Originations controls (2026-07-21 review).

Revision ID: 069_dead_button_backends
Revises: 068_dave_status_model
Create Date: 2026-07-22

NOTE FOR RE-CHAINING: sibling P0 branches may also claim ``069``. This revision
only depends on ``068_dave_status_model``; if another 069 merges first, change
``revision``/``down_revision`` here and nothing else — there are no cross-table
dependencies beyond ``platform_patient_bank_accounts`` (064) and
``platform_credit_applications``/``platform_patients``.

Two changes:

1. ``platform_patient_bank_accounts`` gains the Canadian routing fields Dave's
   "Add Bank Account" dialog collects, which the 064 masked-only shape had no
   slot for:

   * ``institution_number`` CHAR-ish TEXT(3) — stored as TEXT precisely so a
     leading zero survives ("003" must never become 3).
   * ``transit_number`` TEXT(5) — same reason.
   * ``account_holder`` TEXT — the "Account Holder" column of his tab.
   * ``account_number_encrypted`` TEXT — the FULL account number, Fernet-
     encrypted at rest via ``app.core.secret_crypto`` (same envelope scheme as
     integration credentials). It is never returned by any API; the display
     value stays ``account_mask``. Needed because a PAD debit cannot be built
     from a mask.
   * ``source`` TEXT — Dave's literal column ("Flinks" / "Manual"). Derived
     from ``verified_via`` for existing rows so the tab renders for history.

   The 064 CHECK on ``verified_via`` is untouched: manual staff adds keep
   writing ``manual_staff``.

2. ``platform_credit_report_pulls`` — NEW. Where a Hard/Soft bureau pull is
   stored against the application (Dave's Credit Report tab). ``simulated`` is
   a first-class column, not an inference: the Equifax subscriber agreement is
   NOT signed, every pull today comes from the mock adapter, and the record
   must say so permanently so no one ever mistakes a stored mock report for a
   real bureau file.

All SQL is static (bandit B608).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "069_dead_button_backends"
down_revision: Union[str, None] = "068_dave_status_model"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: Backfill for rows created before this migration. Static SQL.
_BACKFILL_SOURCE = (
    "UPDATE platform_patient_bank_accounts "
    "SET source = CASE WHEN verified_via = 'flinks' THEN 'flinks' ELSE 'manual' END "
    "WHERE source IS NULL"
)


def upgrade() -> None:
    # --- 1. bank-account routing fields -------------------------------------
    op.add_column(
        "platform_patient_bank_accounts",
        sa.Column("institution_number", sa.String(length=3), nullable=True),
    )
    op.add_column(
        "platform_patient_bank_accounts",
        sa.Column("transit_number", sa.String(length=5), nullable=True),
    )
    op.add_column(
        "platform_patient_bank_accounts",
        sa.Column("account_holder", sa.String(), nullable=True),
    )
    op.add_column(
        "platform_patient_bank_accounts",
        sa.Column("account_number_encrypted", sa.Text(), nullable=True),
    )
    op.add_column(
        "platform_patient_bank_accounts",
        sa.Column("source", sa.String(), nullable=True),
    )
    op.execute(_BACKFILL_SOURCE)
    op.create_check_constraint(
        "ck_platform_patient_bank_account_source",
        "platform_patient_bank_accounts",
        "source IS NULL OR source IN ('flinks', 'manual')",
    )

    # --- 2. credit-bureau pull records --------------------------------------
    op.create_table(
        "platform_credit_report_pulls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "application_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_credit_applications.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "patient_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_patients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Which party the pull was run against.
        sa.Column("subject", sa.String(), nullable=False, server_default="borrower"),
        sa.Column("pull_type", sa.String(), nullable=False),
        sa.Column("bureau", sa.String(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("result", sa.String(), nullable=False),
        sa.Column("bankruptcy", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "report",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # TRUE whenever the report did not come from a live bureau connection.
        sa.Column("simulated", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("requested_by", sa.String(), nullable=False),
        sa.Column(
            "verification_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_verifications.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "pull_type IN ('soft', 'hard')", name="ck_platform_credit_report_pull_type"
        ),
        sa.CheckConstraint(
            "subject IN ('borrower', 'co_borrower')",
            name="ck_platform_credit_report_pull_subject",
        ),
    )
    op.create_index(
        "ix_platform_credit_report_pulls_application",
        "platform_credit_report_pulls",
        ["application_id"],
    )
    op.create_index(
        "ix_platform_credit_report_pulls_patient",
        "platform_credit_report_pulls",
        ["patient_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_platform_credit_report_pulls_patient", table_name="platform_credit_report_pulls"
    )
    op.drop_index(
        "ix_platform_credit_report_pulls_application",
        table_name="platform_credit_report_pulls",
    )
    op.drop_table("platform_credit_report_pulls")

    op.drop_constraint(
        "ck_platform_patient_bank_account_source",
        "platform_patient_bank_accounts",
        type_="check",
    )
    op.drop_column("platform_patient_bank_accounts", "source")
    op.drop_column("platform_patient_bank_accounts", "account_number_encrypted")
    op.drop_column("platform_patient_bank_accounts", "account_holder")
    op.drop_column("platform_patient_bank_accounts", "transit_number")
    op.drop_column("platform_patient_bank_accounts", "institution_number")
