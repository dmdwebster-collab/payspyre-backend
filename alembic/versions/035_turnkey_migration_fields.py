"""Turnkey loan-book migration fields on platform_loans

Revision ID: 035_turnkey_migration_fields
Revises: 033_payment_idempotency_and_event_index
Create Date: 2026-06-22

Lets the legacy Turnkey book be imported as PlatformLoan rows that have no PaySpyre
application:
- ``application_id`` becomes NULLABLE (a migrated loan has no application). The
  existing unique constraint still holds — Postgres treats NULLs as distinct.
- ``source`` ('application' default | 'turnkey_migration') records provenance.
- ``legacy_account_number`` stores the Turnkey acct# for tracing + IDEMPOTENT
  re-import (partial unique index when present).
- A CHECK constraint keeps natively-originated loans honest: only a migrated loan
  may have a NULL application_id.

NOTE: parallel work numbered the vendor-dashboard migration 034; this chains off 033.
If 034 merges first, bump down_revision to it (one line) to keep a single head.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "035_turnkey_migration_fields"
down_revision: Union[str, None] = "033_payment_idempotency_and_event_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "platform_loans",
        sa.Column("source", sa.String(), nullable=False, server_default="application"),
    )
    op.add_column(
        "platform_loans",
        sa.Column("legacy_account_number", sa.String(), nullable=True),
    )
    op.alter_column("platform_loans", "application_id", existing_type=sa.dialects.postgresql.UUID(), nullable=True)
    op.create_index(
        "uq_platform_loans_legacy_acct",
        "platform_loans",
        ["legacy_account_number"],
        unique=True,
        postgresql_where=sa.text("legacy_account_number IS NOT NULL"),
    )
    op.create_check_constraint(
        "ck_platform_loans_application_or_migration",
        "platform_loans",
        "source = 'turnkey_migration' OR application_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("ck_platform_loans_application_or_migration", "platform_loans", type_="check")
    op.drop_index("uq_platform_loans_legacy_acct", table_name="platform_loans")
    op.alter_column("platform_loans", "application_id", existing_type=sa.dialects.postgresql.UUID(), nullable=False)
    op.drop_column("platform_loans", "legacy_account_number")
    op.drop_column("platform_loans", "source")
