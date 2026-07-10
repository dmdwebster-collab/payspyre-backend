"""3-year address + employment history and co-borrower file linking (Dave's spec)

Revision ID: 046_application_history
Revises: 043_canonical_credit_application
Create Date: 2026-07-10

Dave's structural data-model mandates (P0 schema pack, WS-C):

  * "Ideally we want three years of both address and employment information to
    look at stability factors." — two new child tables of
    ``platform_credit_applications``:
      - ``platform_application_address_history``
      - ``platform_application_employment_history``
    One row per historical (or current) entry, matching the migration-043 idiom
    of a structured child table for repeatable data (see
    ``platform_application_secondary_incomes``). The CURRENT address/employment
    remain the existing scalar columns on the application (back-compat); history
    rows EXTEND them, and edits to the current entry are versioned into history
    rather than overwritten (``entry_source = 'versioned_edit'``).

  * New enum ``platform_employment_type`` — Dave: "Employed, really we are
    looking at full-time, part-time, seasonally, self-employed": full_time /
    part_time / seasonal / self_employed / retired / unemployed / other.

  * Co-borrower separate-file linking: co-applicants are ALREADY separate,
    fully-capable application rows (own documents / verifications) linked via
    ``co_applicant_of_application_id`` + ``applicant_role``. The minimal missing
    delta is (a) ``relationship_to_primary`` (Dave's co-borrower dialog has a
    required Relationship field) and (b) an index on the linkage column so a
    primary file can enumerate its linked co-borrower files.

All changes are ADDITIVE and nullable — no existing row or query breaks.
Money is integer cents throughout (repo convention). No SIN anywhere here.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "046_application_history"
down_revision: Union[str, None] = "043_canonical_credit_application"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- new enum type -------------------------------------------------------
    op.execute(
        """
        CREATE TYPE platform_employment_type AS ENUM (
            'full_time', 'part_time', 'seasonal', 'self_employed',
            'retired', 'unemployed', 'other'
        )
        """
    )

    # --- address history child table ----------------------------------------
    op.create_table(
        "platform_application_address_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "application_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_credit_applications.id"),
            nullable=False,
        ),
        # Address fields (mirror the current residence_* scalar columns)
        sa.Column("street", sa.String(), nullable=True),
        sa.Column("unit", sa.String(), nullable=True),
        sa.Column("city", sa.String(), nullable=True),
        sa.Column("province", sa.String(), nullable=True),
        sa.Column("postal_code", sa.String(), nullable=True),
        sa.Column("residential_status", sa.String(), nullable=True),
        sa.Column("monthly_housing_payment_cents", sa.BigInteger(), nullable=True),
        # Range + current flag. from_date is nullable so a versioned snapshot of
        # a pre-history current entry (whose exact start is unknown) round-trips.
        sa.Column("from_date", sa.Date(), nullable=True),
        sa.Column("to_date", sa.Date(), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.false()),
        # 'applicant' (declared on intake) | 'versioned_edit' (auto snapshot of
        # the previous current entry when the current scalar fields are edited).
        sa.Column("entry_source", sa.String(), nullable=False, server_default="applicant"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_platform_application_address_history_application_id",
        "platform_application_address_history",
        ["application_id"],
    )

    # --- employment history child table --------------------------------------
    op.create_table(
        "platform_application_employment_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "application_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_credit_applications.id"),
            nullable=False,
        ),
        sa.Column("employer_name", sa.String(), nullable=True),
        sa.Column("job_title", sa.String(), nullable=True),
        sa.Column(
            "employment_type",
            postgresql.ENUM(name="platform_employment_type", create_type=False),
            nullable=True,
        ),
        # Income fields (same shape as the primary/secondary income lines so the
        # underwriting layer can treat all income data uniformly).
        sa.Column(
            "income_type",
            postgresql.ENUM(name="platform_income_type", create_type=False),
            nullable=True,
        ),
        sa.Column("net_monthly_income_cents", sa.BigInteger(), nullable=True),
        sa.Column("pay_frequency", sa.String(), nullable=True),
        sa.Column("from_date", sa.Date(), nullable=True),
        sa.Column("to_date", sa.Date(), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("entry_source", sa.String(), nullable=False, server_default="applicant"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_platform_application_employment_history_application_id",
        "platform_application_employment_history",
        ["application_id"],
    )

    # --- co-borrower separate-file linking (minimal delta) -------------------
    op.add_column(
        "platform_credit_applications",
        sa.Column("relationship_to_primary", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_platform_credit_applications_co_applicant_of_application_id",
        "platform_credit_applications",
        ["co_applicant_of_application_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_platform_credit_applications_co_applicant_of_application_id",
        "platform_credit_applications",
    )
    op.drop_column("platform_credit_applications", "relationship_to_primary")

    op.drop_index(
        "ix_platform_application_employment_history_application_id",
        "platform_application_employment_history",
    )
    op.drop_table("platform_application_employment_history")

    op.drop_index(
        "ix_platform_application_address_history_application_id",
        "platform_application_address_history",
    )
    op.drop_table("platform_application_address_history")

    op.execute("DROP TYPE IF EXISTS platform_employment_type")
