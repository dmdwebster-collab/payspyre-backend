"""Customer Profile as a first-class entity (Dave's Credit Application v1.0)

Revision ID: 071_customer_profile
Revises: 070_loan_closed_at
Create Date: 2026-07-22

MERGE ORDER (coordinated): this chains from ``070_loan_closed_at``, the archive
migration on sibling PR #201, which itself chains from ``069_dead_button_backends``
(PR #199, already on main). **PR #202 must therefore merge AFTER PR #201** — until
#201 lands, the migrations check cannot resolve this down_revision, and that
failure is expected rather than a defect.

Creates:

* ``platform_customer_profiles`` — one lifecycle row per patient (version
  counter, lock, soft-delete). 1:1 with ``platform_patients`` (unique
  ``patient_id``); we extend the patient rather than introduce a rival identity
  table because the patient id is referenced platform-wide.
* ``platform_customer_profile_fields`` — versioned field values, one row per
  (profile, block, block_index, field_key) VERSION. Edits never overwrite: the
  prior row becomes ``is_current=false`` with ``superseded_at`` /
  ``superseded_by_id`` set (Dave's mandate — prior values become "former"). The
  key space is exactly ``app/services/customer_profile_schema.py``.

Adds to ``platform_credit_applications`` (all nullable + additive — every
existing application keeps working untouched, and ``self_reported`` plus the
migration-043 columns are left exactly as they are):

* ``customer_profile_id`` — the profile this application was built from;
* ``profile_version``    — the profile revision in force when it was created;
* ``profile_snapshot``   — FROZEN copy of the profile values used for the
  decision (extends the existing ``product_config_snapshot`` pattern: the
  decision must be reproducible even after the borrower edits their profile);
* ``profile_snapshot_at``.

Backfill: intentionally NOT run here. Materialising profiles from ~N existing
applications is a data operation with judgement calls (which application wins
when a patient has several, how to treat conflicting values), so it lives in
``app/services/customer_profile.backfill_profile_from_application`` and is driven
by ``POST /api/v1/admin/customer-profiles/backfill`` — idempotent, audited, and
re-runnable. Existing applications work with a NULL ``customer_profile_id``.

All SQL is emitted through the Alembic op API / fully static strings (bandit
B608 clean).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "071_customer_profile"
down_revision: Union[str, None] = "070_loan_closed_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "platform_customer_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "patient_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_patients.id"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("schema_version", sa.String(), nullable=False, server_default="1.0"),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(), nullable=True),
        sa.Column("lock_reason", sa.String(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.String(), nullable=True),
        sa.Column("delete_reason", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.UniqueConstraint("patient_id", name="uq_customer_profile_patient"),
    )
    op.create_index(
        "ix_platform_customer_profiles_patient_id",
        "platform_customer_profiles",
        ["patient_id"],
    )

    op.create_table(
        "platform_customer_profile_fields",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_customer_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("block", sa.String(), nullable=False),
        sa.Column("block_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("field_key", sa.String(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source", sa.String(), nullable=False, server_default="self_reported"),
        sa.Column("profile_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "superseded_by_id",
            sa.BigInteger(),
            sa.ForeignKey("platform_customer_profile_fields.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_platform_customer_profile_fields_profile_id",
        "platform_customer_profile_fields",
        ["profile_id"],
    )
    op.create_index(
        "ix_customer_profile_fields_current",
        "platform_customer_profile_fields",
        ["profile_id", "block", "block_index", "field_key", "is_current"],
    )

    # --- application -> profile link + frozen decision snapshot ---------------
    op.add_column(
        "platform_credit_applications",
        sa.Column(
            "customer_profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_customer_profiles.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "platform_credit_applications",
        sa.Column("profile_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "platform_credit_applications",
        sa.Column(
            "profile_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
    )
    op.add_column(
        "platform_credit_applications",
        sa.Column("profile_snapshot_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_platform_credit_applications_customer_profile_id",
        "platform_credit_applications",
        ["customer_profile_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_platform_credit_applications_customer_profile_id",
        table_name="platform_credit_applications",
    )
    op.drop_column("platform_credit_applications", "profile_snapshot_at")
    op.drop_column("platform_credit_applications", "profile_snapshot")
    op.drop_column("platform_credit_applications", "profile_version")
    op.drop_column("platform_credit_applications", "customer_profile_id")

    op.drop_index(
        "ix_customer_profile_fields_current",
        table_name="platform_customer_profile_fields",
    )
    op.drop_index(
        "ix_platform_customer_profile_fields_profile_id",
        table_name="platform_customer_profile_fields",
    )
    op.drop_table("platform_customer_profile_fields")

    op.drop_index(
        "ix_platform_customer_profiles_patient_id",
        table_name="platform_customer_profiles",
    )
    op.drop_table("platform_customer_profiles")
