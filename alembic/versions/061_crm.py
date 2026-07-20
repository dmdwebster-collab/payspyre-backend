"""Vendor + Customer CRM (WS-G, Turnkey full-parity plan)

Revision ID: 061_crm
Revises: 054_hardship
Create Date: 2026-07-20

NOTE (merge train): chained onto ``054_hardship`` — the single alembic head at
the time WS-G branched. Wave-1 workstreams A-F/H-J land migrations 055-060 and
062-064 in parallel worktrees; the orchestrator re-chains this
``down_revision`` onto the final wave-1 head (a one-line change here plus the
pin in ``tests/test_crm_parity_g.py``).

Vendor CRM depth (video 09 ``/tools/vendors``):
  * ``platform_industry_categories``            — directory (Settings→directories),
    seeded with Dave's 8 healthcare categories; ``vendors.industry_category_id``.
  * ``platform_vendor_contacts``                — multiple contacts per vendor
    (name / position / phone / email, star = primary), soft-deleted.
  * ``platform_vendor_bank_accounts``           — admin-managed disbursement
    destinations. MASKED AT REST: only institution / transit / last-4 are
    stored — full account capture rides the wave-2 Zumrails wallet work
    (money-path, human-reviewed).
  * ``platform_vendor_documents``               — MSA / contract attachments
    (presigned-upload object keys, bytes never touch the API) with
    ``effective_date`` / ``expiry_date``; ``platform_vendor_document_expiry_alerts``
    dedupes the 60/30/7-day expiry notifications (one row per doc+threshold).
  * ``platform_vendor_onboarding``              — invited → docs_collected →
    msa_signed → live checklist with per-step timestamps.
  * ``platform_clinic_roles``                   — the 9-role vendor-side
    permission matrix directory (video 09 f0067-f0071); per-user assignment via
    ``platform_clinic_memberships.roles`` (JSONB list; NULL = legacy
    full-access so existing memberships keep working unchanged).

Customer CRM (video 09 ``/tools/manageCustomers``):
  * ``platform_customer_block_reasons``         — block-reason directory (seeded).
  * ``platform_customer_blocks``                — lock/block audit rows with a
    MANDATORY reason; at most one ACTIVE block per patient (partial unique
    index on ``unblocked_at IS NULL``). Blocked = no new originations;
    servicing unaffected.

No behaviour change on its own: new tables start empty, both added columns are
nullable, and NULL ``roles`` preserves today's clinic access exactly.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "061_crm"
down_revision: Union[str, None] = "060_settings_suite"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Dave's industry-category directory (video 09 f0051, defined in
# Settings→directories). Seeded active, in on-camera order.
INDUSTRY_CATEGORY_SEED = (
    "Dental Goods & Services",
    "Healthcare - Cosmetic",
    "Healthcare - Vision",
    "Healthcare - Hearing",
    "Healthcare - Medical Weight Loss",
    "Healthcare - Dermatology",
    "Healthcare - Medical",
    "Healthcare - Veterinary",
)

# The 9-role vendor-user permission matrix (video 09 f0067-f0071). The two
# ``is_addon`` roles "just give access to additional features and work only in
# conjunction with any other role" (on-screen note, verbatim).
CLINIC_ROLE_SEED = (
    ("loan_origination", "Loan origination", "Create and submit applications on behalf of patients.", False),
    ("loan_servicing", "Loan servicing", "View and work the clinic's active loan book.", False),
    ("monitoring", "Monitoring", "Dashboards and portfolio performance views.", False),
    ("collection", "Collection", "Past-due follow-up surfaces for the clinic's own loans.", False),
    ("assignment_officer", "Assignment officer", "Add-on: assign files to clinic users. Works only in conjunction with another role.", True),
    ("vendor_management", "Vendor management", "Manage the clinic's own profile, contacts and users.", False),
    ("archive", "Archive", "View the clinic's closed/archived loans.", False),
    ("document_verification", "Document verification", "Add-on: verify uploaded documents. Works only in conjunction with another role.", True),
    ("export", "Export", "Download the clinic's reports and exports.", False),
)

CUSTOMER_BLOCK_REASON_SEED = (
    ("suspected_fraud", "Suspected fraud"),
    ("identity_mismatch", "Identity could not be verified"),
    ("payment_abuse", "Repeated NSF / payment abuse"),
    ("abusive_conduct", "Abusive conduct toward staff"),
    ("legal_request", "Legal / regulatory request"),
    ("customer_request", "Customer requested account lock"),
    ("deceased", "Customer deceased"),
    ("other", "Other (see reason text)"),
)


def upgrade() -> None:
    # --- industry categories directory + vendor link -----------------------
    op.create_table(
        "platform_industry_categories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False, unique=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    categories = sa.table(
        "platform_industry_categories",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("name", sa.String),
        sa.column("sort_order", sa.Integer),
    )
    op.bulk_insert(
        categories,
        [
            {"id": str(_seed_uuid(f"industry:{name}")), "name": name, "sort_order": i}
            for i, name in enumerate(INDUSTRY_CATEGORY_SEED)
        ],
    )

    op.add_column(
        "vendors",
        sa.Column(
            "industry_category_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_industry_categories.id"),
            nullable=True,
        ),
    )

    # --- vendor contacts ---------------------------------------------------
    op.create_table(
        "platform_vendor_contacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "vendor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vendors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("position", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=30), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_platform_vendor_contacts_vendor", "platform_vendor_contacts", ["vendor_id"]
    )

    # --- vendor bank accounts (masked at rest: last-4 only) ----------------
    op.create_table(
        "platform_vendor_bank_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "vendor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vendors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("bank_name", sa.String(length=255), nullable=False),
        sa.Column("institution_number", sa.String(length=3), nullable=True),
        sa.Column("transit_number", sa.String(length=5), nullable=True),
        sa.Column("account_number_last4", sa.String(length=4), nullable=False),
        sa.Column("account_holder", sa.String(length=255), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_platform_vendor_bank_accounts_vendor",
        "platform_vendor_bank_accounts",
        ["vendor_id"],
    )

    # --- vendor documents (MSA / contracts) + expiry-alert dedupe ----------
    op.create_table(
        "platform_vendor_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "vendor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vendors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("doc_type", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False, unique=True),
        sa.Column("content_type", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("expiry_date", sa.Date(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_platform_vendor_documents_vendor", "platform_vendor_documents", ["vendor_id"]
    )
    op.create_index(
        "ix_platform_vendor_documents_expiry",
        "platform_vendor_documents",
        ["expiry_date"],
        postgresql_where=sa.text("expiry_date IS NOT NULL AND deleted_at IS NULL"),
    )

    op.create_table(
        "platform_vendor_document_expiry_alerts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_vendor_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("threshold_days", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "document_id", "threshold_days", name="uq_vendor_doc_expiry_alert"
        ),
    )

    # --- vendor onboarding checklist ---------------------------------------
    op.create_table(
        "platform_vendor_onboarding",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "vendor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vendors.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="invited"),
        sa.Column("invited_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("docs_collected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("msa_signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("live_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # --- clinic role directory + per-user assignment -----------------------
    op.create_table(
        "platform_clinic_roles",
        sa.Column("key", sa.String(length=50), primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_addon", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    roles = sa.table(
        "platform_clinic_roles",
        sa.column("key", sa.String),
        sa.column("name", sa.String),
        sa.column("description", sa.Text),
        sa.column("is_addon", sa.Boolean),
        sa.column("sort_order", sa.Integer),
    )
    op.bulk_insert(
        roles,
        [
            {
                "key": key,
                "name": name,
                "description": description,
                "is_addon": is_addon,
                "sort_order": i,
            }
            for i, (key, name, description, is_addon) in enumerate(CLINIC_ROLE_SEED)
        ],
    )

    # NULL = legacy membership, full clinic access (pre-matrix behaviour).
    op.add_column(
        "platform_clinic_memberships",
        sa.Column("roles", postgresql.JSONB(), nullable=True),
    )

    # --- customer lock/block ----------------------------------------------
    op.create_table(
        "platform_customer_block_reasons",
        sa.Column("code", sa.String(length=50), primary_key=True),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    reasons = sa.table(
        "platform_customer_block_reasons",
        sa.column("code", sa.String),
        sa.column("label", sa.String),
        sa.column("sort_order", sa.Integer),
    )
    op.bulk_insert(
        reasons,
        [
            {"code": code, "label": label, "sort_order": i}
            for i, (code, label) in enumerate(CUSTOMER_BLOCK_REASON_SEED)
        ],
    )

    op.create_table(
        "platform_customer_blocks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "patient_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_patients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reason_code", sa.String(length=50), nullable=False),
        sa.Column("reason_text", sa.Text(), nullable=False),
        sa.Column("blocked_by", sa.String(), nullable=False),
        sa.Column("blocked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("unblocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unblocked_by", sa.String(), nullable=True),
        sa.Column("unblock_note", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_platform_customer_blocks_patient", "platform_customer_blocks", ["patient_id"]
    )
    # At most ONE active block per patient.
    op.create_index(
        "uq_platform_customer_blocks_active",
        "platform_customer_blocks",
        ["patient_id"],
        unique=True,
        postgresql_where=sa.text("unblocked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_platform_customer_blocks_active", table_name="platform_customer_blocks")
    op.drop_index("ix_platform_customer_blocks_patient", table_name="platform_customer_blocks")
    op.drop_table("platform_customer_blocks")
    op.drop_table("platform_customer_block_reasons")
    op.drop_column("platform_clinic_memberships", "roles")
    op.drop_table("platform_clinic_roles")
    op.drop_table("platform_vendor_onboarding")
    op.drop_table("platform_vendor_document_expiry_alerts")
    op.drop_index("ix_platform_vendor_documents_expiry", table_name="platform_vendor_documents")
    op.drop_index("ix_platform_vendor_documents_vendor", table_name="platform_vendor_documents")
    op.drop_table("platform_vendor_documents")
    op.drop_index("ix_platform_vendor_bank_accounts_vendor", table_name="platform_vendor_bank_accounts")
    op.drop_table("platform_vendor_bank_accounts")
    op.drop_index("ix_platform_vendor_contacts_vendor", table_name="platform_vendor_contacts")
    op.drop_table("platform_vendor_contacts")
    op.drop_column("vendors", "industry_category_id")
    op.drop_table("platform_industry_categories")


def _seed_uuid(name: str):
    """Deterministic UUID for seed rows (stable across environments)."""
    import uuid

    return uuid.uuid5(uuid.NAMESPACE_URL, f"payspyre:crm:{name}")
