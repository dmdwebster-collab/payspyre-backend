"""Canonical credit-application field set + status workflow (Dave's spec)

Revision ID: 043_canonical_credit_application
Revises: 042_application_documents
Create Date: 2026-07-06

Expands ``platform_credit_applications`` into the full canonical credit
application Dave specced, and extends the status workflow.

All changes are ADDITIVE and nullable — no existing row or query breaks:

  * New enums:
      - ``platform_income_type``  (income source classification)
      - ``platform_car_ownership`` (vehicle-ownership classification)
  * Two new values on ``platform_application_status``: ``origination`` and
    ``underwriting`` (Dave's named workflow states). Enum values can only be
    ADDED in Postgres, never removed, so this is forward-only. ``ADD VALUE ...
    IF NOT EXISTS`` is idempotent.
  * Structured scored-field columns on ``platform_credit_applications``
    (personal / ID / residence / primary income / financial). SIN is NOT added
    here — the full SIN stays encrypted on ``platform_patients.sin_encrypted``.
  * A child table ``platform_application_secondary_incomes`` — one row per
    declared secondary income source (repeatable/optional).

Money is integer cents throughout (repo convention).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "043_canonical_credit_application"
down_revision: Union[str, None] = "042_application_documents"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- new enum types ----------------------------------------------------
    op.execute(
        """
        CREATE TYPE platform_income_type AS ENUM (
            'employed_full_time', 'employed_part_time', 'employed_seasonal',
            'self_employed', 'retirement_pension', 'disability',
            'employment_insurance', 'other'
        )
        """
    )
    op.execute(
        """
        CREATE TYPE platform_car_ownership AS ENUM (
            'fully_paid', 'financing', 'leasing', 'none'
        )
        """
    )

    # --- extend the application status workflow (additive, forward-only) ----
    # ADD VALUE cannot run inside a transaction block on older PG; alembic wraps
    # migrations in a txn, so run each with an autocommit block. IF NOT EXISTS
    # keeps the migration idempotent on re-run.
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE platform_application_status ADD VALUE IF NOT EXISTS 'origination'"
        )
        op.execute(
            "ALTER TYPE platform_application_status ADD VALUE IF NOT EXISTS 'underwriting'"
        )

    income_type = postgresql.ENUM(name="platform_income_type", create_type=False)
    car_ownership = postgresql.ENUM(name="platform_car_ownership", create_type=False)

    # --- canonical columns on platform_credit_applications -----------------
    ca = "platform_credit_applications"

    # Personal
    op.add_column(ca, sa.Column("first_name", sa.String(), nullable=True))
    op.add_column(ca, sa.Column("middle_name", sa.String(), nullable=True))
    op.add_column(ca, sa.Column("last_name", sa.String(), nullable=True))
    op.add_column(ca, sa.Column("date_of_birth", sa.Date(), nullable=True))
    op.add_column(ca, sa.Column("marital_status", sa.String(), nullable=True))
    op.add_column(ca, sa.Column("number_of_dependents", sa.Integer(), nullable=True))
    op.add_column(ca, sa.Column("citizenship", sa.String(), nullable=True))
    op.add_column(ca, sa.Column("education", sa.String(), nullable=True))
    op.add_column(ca, sa.Column("main_phone", sa.String(), nullable=True))
    op.add_column(ca, sa.Column("alternative_phone", sa.String(), nullable=True))
    op.add_column(ca, sa.Column("email", sa.String(), nullable=True))

    # ID verification
    op.add_column(ca, sa.Column("id_type", sa.String(), nullable=True))
    op.add_column(ca, sa.Column("id_number", sa.String(), nullable=True))
    op.add_column(ca, sa.Column("id_province_of_issue", sa.String(), nullable=True))
    op.add_column(ca, sa.Column("id_expiry", sa.Date(), nullable=True))

    # Residence
    op.add_column(ca, sa.Column("residence_street", sa.String(), nullable=True))
    op.add_column(ca, sa.Column("residence_unit", sa.String(), nullable=True))
    op.add_column(ca, sa.Column("residence_city", sa.String(), nullable=True))
    op.add_column(ca, sa.Column("residence_province", sa.String(), nullable=True))
    op.add_column(ca, sa.Column("residence_postal_code", sa.String(), nullable=True))
    op.add_column(ca, sa.Column("time_at_address_years", sa.Integer(), nullable=True))
    op.add_column(ca, sa.Column("time_at_address_months", sa.Integer(), nullable=True))
    op.add_column(ca, sa.Column("residential_status", sa.String(), nullable=True))
    op.add_column(ca, sa.Column("monthly_housing_payment_cents", sa.BigInteger(), nullable=True))

    # Primary income
    op.add_column(ca, sa.Column("income_type", income_type, nullable=True))
    op.add_column(ca, sa.Column("net_monthly_income_cents", sa.BigInteger(), nullable=True))
    op.add_column(ca, sa.Column("next_pay_date", sa.Date(), nullable=True))
    op.add_column(ca, sa.Column("pay_frequency", sa.String(), nullable=True))
    op.add_column(ca, sa.Column("employer_name", sa.String(), nullable=True))
    op.add_column(ca, sa.Column("hire_date", sa.Date(), nullable=True))
    op.add_column(ca, sa.Column("job_title", sa.String(), nullable=True))
    op.add_column(ca, sa.Column("work_phone", sa.String(), nullable=True))
    op.add_column(ca, sa.Column("work_phone_ext", sa.String(), nullable=True))
    op.add_column(ca, sa.Column("ok_to_contact_at_work", sa.Boolean(), nullable=True))

    # Financial
    op.add_column(ca, sa.Column("number_of_credit_accounts", sa.Integer(), nullable=True))
    op.add_column(ca, sa.Column("car_ownership", car_ownership, nullable=True))
    op.add_column(ca, sa.Column("monthly_car_payment_cents", sa.BigInteger(), nullable=True))
    op.add_column(ca, sa.Column("non_discretionary_expenses_cents", sa.BigInteger(), nullable=True))

    # --- secondary income child table --------------------------------------
    op.create_table(
        "platform_application_secondary_incomes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "application_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_credit_applications.id"),
            nullable=False,
        ),
        sa.Column(
            "income_type",
            postgresql.ENUM(name="platform_income_type", create_type=False),
            nullable=True,
        ),
        sa.Column("net_monthly_income_cents", sa.BigInteger(), nullable=True),
        sa.Column("pay_frequency", sa.String(), nullable=True),
        sa.Column("next_pay_date", sa.Date(), nullable=True),
        sa.Column("employer_name", sa.String(), nullable=True),
        sa.Column("job_title", sa.String(), nullable=True),
        sa.Column("hire_date", sa.Date(), nullable=True),
        sa.Column("work_phone", sa.String(), nullable=True),
        sa.Column("work_phone_ext", sa.String(), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_platform_application_secondary_incomes_application_id",
        "platform_application_secondary_incomes",
        ["application_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_platform_application_secondary_incomes_application_id",
        "platform_application_secondary_incomes",
    )
    op.drop_table("platform_application_secondary_incomes")

    ca = "platform_credit_applications"
    for col in (
        "non_discretionary_expenses_cents", "monthly_car_payment_cents", "car_ownership",
        "number_of_credit_accounts",
        "ok_to_contact_at_work", "work_phone_ext", "work_phone", "job_title", "hire_date",
        "employer_name", "pay_frequency", "next_pay_date", "net_monthly_income_cents",
        "income_type",
        "monthly_housing_payment_cents", "residential_status", "time_at_address_months",
        "time_at_address_years", "residence_postal_code", "residence_province",
        "residence_city", "residence_unit", "residence_street",
        "id_expiry", "id_province_of_issue", "id_number", "id_type",
        "email", "alternative_phone", "main_phone", "education", "citizenship",
        "number_of_dependents", "marital_status", "date_of_birth", "last_name",
        "middle_name", "first_name",
    ):
        op.drop_column(ca, col)

    op.execute("DROP TYPE IF EXISTS platform_car_ownership")
    op.execute("DROP TYPE IF EXISTS platform_income_type")
    # NOTE: added enum values on platform_application_status ('origination',
    # 'underwriting') are NOT dropped — Postgres cannot remove an enum value, and
    # they are harmless if unused. Downgrade leaves them in place by design.
