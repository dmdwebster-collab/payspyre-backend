"""Cutover-import batches (Turnkey -> PaySpyre CSV migration, WS-D)

Revision ID: 047_import_batches
Revises: 043_canonical_credit_application
Create Date: 2026-07-10

Supports the template-driven, preview-then-confirm import Dave specced from
Turnkey's Tools -> Import workplace (docs/turnkey_parity/09__WP_Tools.md):
upload a CSV per entity (customers / loans / payments / disbursements),
validate it into a preview report (NOTHING written to domain tables), then a
separate explicit CONFIRM applies the rows idempotently.

* ``platform_import_batches`` — one row per uploaded file. ``preview`` holds
  the validation report (per-row errors reference row numbers + field names,
  never PII values); ``rows`` holds the normalized row payloads the confirm
  step applies (they contain applicant PII — DB-resident like the patient
  table itself, admin-only surface, never logged); ``apply_report`` holds the
  per-row outcome of the confirm step.
  JSONB (not a row-level table) was chosen deliberately: the whole book is
  ~580 customers / ~560 loans, far below the point where per-row rows earn
  their operational cost, and the preview/apply reports are read as a unit.

* ``platform_loans.patient_id`` — nullable FK to ``platform_patients``.
  Natively-originated loans reach their borrower via application_id ->
  patient; MIGRATED loans (source='turnkey_migration', application_id NULL,
  migration 035) had NO borrower linkage at all. The loans CSV carries the
  customer's legacy id, so the import can finally attach the migrated book to
  its (also-imported) customers. Additive + nullable — no existing row or
  query breaks.

NOTE (merge train): chains from 043 per the P0 plan; if WS-A/B/C land first,
re-chain down_revision to the new head (one line).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "047_import_batches"
down_revision: Union[str, None] = "046_application_history"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ENTITY_TYPE = postgresql.ENUM(
    "customers",
    "loans",
    "payments",
    "disbursements",
    name="platform_import_entity_type",
    create_type=False,
)

BATCH_STATUS = postgresql.ENUM(
    "uploaded",
    "validated",
    "confirmed",
    "applied",
    "failed",
    "cancelled",
    name="platform_import_batch_status",
    create_type=False,
)


def upgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "CREATE TYPE platform_import_entity_type AS ENUM "
        "('customers','loans','payments','disbursements'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
    )
    op.execute(
        "DO $$ BEGIN "
        "CREATE TYPE platform_import_batch_status AS ENUM "
        "('uploaded','validated','confirmed','applied','failed','cancelled'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
    )

    op.create_table(
        "platform_import_batches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("entity_type", ENTITY_TYPE, nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("status", BATCH_STATUS, nullable=False, server_default="uploaded"),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        # Validation report: summary + per-row errors/warnings (row # + field name,
        # never PII field VALUES).
        sa.Column("preview", postgresql.JSONB(), nullable=True),
        # Normalized row payloads validation produced; the confirm step applies
        # EXACTLY these (the uploaded file itself is not retained).
        sa.Column("rows", postgresql.JSONB(), nullable=True),
        # Per-row outcome of the confirm step (applied / skipped / failed).
        sa.Column("apply_report", postgresql.JSONB(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
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
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_platform_import_batches_status_created",
        "platform_import_batches",
        ["status", "created_at"],
    )

    # Borrower linkage for migrated loans (see module docstring).
    op.add_column(
        "platform_loans",
        sa.Column(
            "patient_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_patients.id"),
            nullable=True,
        ),
    )
    op.create_index("ix_platform_loans_patient_id", "platform_loans", ["patient_id"])


def downgrade() -> None:
    op.drop_index("ix_platform_loans_patient_id", table_name="platform_loans")
    op.drop_column("platform_loans", "patient_id")
    op.drop_index(
        "ix_platform_import_batches_status_created",
        table_name="platform_import_batches",
    )
    op.drop_table("platform_import_batches")
    op.execute("DROP TYPE IF EXISTS platform_import_batch_status")
    op.execute("DROP TYPE IF EXISTS platform_import_entity_type")
