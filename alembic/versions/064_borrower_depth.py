"""Borrower portal depth (WS-J full parity) — 2FA, bank accounts, ID docs, payout requests

Revision ID: 064_borrower_depth
Revises: 054_hardship
Create Date: 2026-07-20

NOTE (merge train): down_revision is pinned to the single head at branch time
(``054_hardship``). Wave-1 workstreams A–I claim slots 055–063; the
orchestrator re-chains this to the final wave-1 head if they merge first —
a one-line change here.

Four tables, all additive, no behaviour change on their own:

* ``platform_patient_second_factor`` — per-patient 2FA (TOTP or SMS-code seam)
  layered ON TOP of magic-link login; ``enforced`` is the per-patient staff
  enforcement flag.
* ``platform_patient_bank_accounts`` — masked payment-account list; ONE active
  default per patient (partial unique index); borrower may only pick the
  default — add/remove is staff-only.
* ``platform_patient_id_documents`` — write-only-from-portal ID images
  (borrower + co-borrower, front/back), current/former supersession chain,
  never deleted.
* ``platform_payout_requests`` — borrower payout-figure requests (≤30 days
  out) that create a staff task; staff answer via the WS-I forward calculator.

Statuses are strings + CHECK constraints (no new Postgres enums).
Money is integer cents.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "064_borrower_depth"
down_revision: Union[str, None] = "063_archive_misc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 2FA -----------------------------------------------------------------
    op.create_table(
        "platform_patient_second_factor",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "patient_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_patients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("totp_secret", sa.String(), nullable=True),
        sa.Column("sms_phone_e164", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("enrolled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enforced", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("patient_id", name="uq_platform_patient_2fa_patient"),
        sa.CheckConstraint("method IN ('totp', 'sms')", name="ck_platform_patient_2fa_method"),
        sa.CheckConstraint(
            "status IN ('pending', 'active')", name="ck_platform_patient_2fa_status"
        ),
    )

    # --- Bank accounts -------------------------------------------------------
    op.create_table(
        "platform_patient_bank_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "patient_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_patients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("institution_name", sa.String(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False, server_default="CAD"),
        sa.Column("account_type", sa.String(), nullable=True),
        sa.Column("routing_mask", sa.String(), nullable=True),
        sa.Column("account_mask", sa.String(), nullable=False),
        sa.Column("verified_via", sa.String(), nullable=False),
        sa.Column("external_ref", sa.String(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("added_by", sa.String(), nullable=False),
        sa.Column("removed_by", sa.String(), nullable=True),
        sa.Column("removed_reason", sa.String(), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('active', 'removed')",
            name="ck_platform_patient_bank_account_status",
        ),
        sa.CheckConstraint(
            "verified_via IN ('flinks', 'manual_staff')",
            name="ck_platform_patient_bank_account_verified_via",
        ),
    )
    op.create_index(
        "ix_platform_patient_bank_accounts_patient",
        "platform_patient_bank_accounts",
        ["patient_id"],
    )
    # At most one ACTIVE default per patient.
    op.create_index(
        "uq_platform_patient_bank_account_default",
        "platform_patient_bank_accounts",
        ["patient_id"],
        unique=True,
        postgresql_where=sa.text("is_default AND status = 'active'"),
    )

    # --- ID documents (write-only from portal) -------------------------------
    op.create_table(
        "platform_patient_id_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "patient_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_patients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("bucket", sa.String(), nullable=False),
        sa.Column("object_key", sa.String(), nullable=False),
        sa.Column("content_type", sa.String(), nullable=True),
        sa.Column("filename", sa.String(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "superseded_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_patient_id_documents.id"),
            nullable=True,
        ),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "bucket IN ('borrower_id_front', 'borrower_id_back', "
            "'co_borrower_id_front', 'co_borrower_id_back')",
            name="ck_platform_patient_id_doc_bucket",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'uploaded')",
            name="ck_platform_patient_id_doc_status",
        ),
    )
    op.create_index(
        "ix_platform_patient_id_documents_patient",
        "platform_patient_id_documents",
        ["patient_id"],
    )

    # --- Payout requests -----------------------------------------------------
    op.create_table(
        "platform_payout_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "patient_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_patients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "loan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_loans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("requested_payout_date", sa.Date(), nullable=False),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="open"),
        sa.Column("quoted_amount_cents", sa.BigInteger(), nullable=True),
        sa.Column("response_note", sa.String(), nullable=True),
        sa.Column("responded_by", sa.String(), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('open', 'responded', 'cancelled')",
            name="ck_platform_payout_request_status",
        ),
    )
    op.create_index(
        "ix_platform_payout_requests_loan", "platform_payout_requests", ["loan_id"]
    )
    op.create_index(
        "ix_platform_payout_requests_status", "platform_payout_requests", ["status"]
    )


def downgrade() -> None:
    op.drop_index("ix_platform_payout_requests_status", table_name="platform_payout_requests")
    op.drop_index("ix_platform_payout_requests_loan", table_name="platform_payout_requests")
    op.drop_table("platform_payout_requests")

    op.drop_index(
        "ix_platform_patient_id_documents_patient", table_name="platform_patient_id_documents"
    )
    op.drop_table("platform_patient_id_documents")

    op.drop_index(
        "uq_platform_patient_bank_account_default",
        table_name="platform_patient_bank_accounts",
    )
    op.drop_index(
        "ix_platform_patient_bank_accounts_patient",
        table_name="platform_patient_bank_accounts",
    )
    op.drop_table("platform_patient_bank_accounts")

    op.drop_table("platform_patient_second_factor")
