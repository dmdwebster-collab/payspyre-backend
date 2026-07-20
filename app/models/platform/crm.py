"""Vendor + Customer CRM models (WS-G, migration ``061_crm``).

Vendor side (video 09 ``/tools/vendors``): industry-category directory,
per-vendor contacts, admin-managed bank accounts (MASKED AT REST — only
institution / transit / last-4 are stored; full account capture rides the
wave-2 Zumrails wallet work), MSA/contract document attachments with expiry
tracking + a dedupe table for the 60/30/7-day expiry alerts, the onboarding
checklist (invited → docs_collected → msa_signed → live), and the 9-role
vendor-user permission directory (per-user assignment lives on
``platform_clinic_memberships.roles``).

Customer side (video 09 ``/tools/manageCustomers``): the block-reason
directory and the lock/block audit rows (mandatory reason; at most one ACTIVE
block per patient — partial unique index). A blocked customer gets no NEW
originations; servicing of existing loans is unaffected.
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID

from app.db.base import Base

# Vendor document types accepted by the CRM upload endpoints. ``msa`` drives
# the onboarding automation (confirming an MSA upload advances the checklist).
VENDOR_DOCUMENT_TYPES = (
    "msa",
    "contract",
    "vendor_application",
    "insurance",
    "license",
    "other",
)

# Onboarding checklist statuses, forward-only in this order.
ONBOARDING_STATUSES = ("invited", "docs_collected", "msa_signed", "live")


class PlatformIndustryCategory(Base):
    """Directory row for vendor industry categories (Settings→directories)."""

    __tablename__ = "platform_industry_categories"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name = Column(String(100), nullable=False, unique=True)
    active = Column(Boolean, nullable=False, default=True)
    sort_order = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PlatformVendorContact(Base):
    """A contact person on a vendor (name / position / phone / email)."""

    __tablename__ = "platform_vendor_contacts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    vendor_id = Column(
        UUID(as_uuid=True), ForeignKey("vendors.id", ondelete="CASCADE"), nullable=False
    )
    name = Column(String(255), nullable=False)
    position = Column(String(255), nullable=True)
    phone = Column(String(30), nullable=True)
    email = Column(String(255), nullable=True)
    is_primary = Column(Boolean, nullable=False, default=False)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_platform_vendor_contacts_vendor", "vendor_id"),)


class PlatformVendorBankAccount(Base):
    """Admin-managed vendor disbursement destination — masked at rest.

    Only ``institution_number`` / ``transit_number`` / ``account_number_last4``
    are stored; there is deliberately NO full-account-number column. Display is
    always ``•••• last4``.
    """

    __tablename__ = "platform_vendor_bank_accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    vendor_id = Column(
        UUID(as_uuid=True), ForeignKey("vendors.id", ondelete="CASCADE"), nullable=False
    )
    bank_name = Column(String(255), nullable=False)
    institution_number = Column(String(3), nullable=True)
    transit_number = Column(String(5), nullable=True)
    account_number_last4 = Column(String(4), nullable=False)
    account_holder = Column(String(255), nullable=True)
    is_primary = Column(Boolean, nullable=False, default=False)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_platform_vendor_bank_accounts_vendor", "vendor_id"),)


class PlatformVendorDocument(Base):
    """An MSA/contract attachment on a vendor (presigned-upload object key)."""

    __tablename__ = "platform_vendor_documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    vendor_id = Column(
        UUID(as_uuid=True), ForeignKey("vendors.id", ondelete="CASCADE"), nullable=False
    )
    doc_type = Column(String(50), nullable=False)
    title = Column(String(255), nullable=False)
    object_key = Column(Text, nullable=False, unique=True)
    content_type = Column(String(100), nullable=True)
    status = Column(String(20), nullable=False, default="pending")  # pending|uploaded
    effective_date = Column(Date, nullable=True)
    expiry_date = Column(Date, nullable=True)
    uploaded_at = Column(DateTime(timezone=True), nullable=True)
    created_by = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_platform_vendor_documents_vendor", "vendor_id"),)


class PlatformVendorDocumentExpiryAlert(Base):
    """Dedupe row: one expiry alert per (document, threshold_days)."""

    __tablename__ = "platform_vendor_document_expiry_alerts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    document_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_vendor_documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    threshold_days = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("document_id", "threshold_days", name="uq_vendor_doc_expiry_alert"),
    )


class PlatformVendorOnboarding(Base):
    """Onboarding checklist for a vendor: invited → docs_collected → msa_signed → live."""

    __tablename__ = "platform_vendor_onboarding"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    vendor_id = Column(
        UUID(as_uuid=True),
        ForeignKey("vendors.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    status = Column(String(20), nullable=False, default="invited")
    invited_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    docs_collected_at = Column(DateTime(timezone=True), nullable=True)
    msa_signed_at = Column(DateTime(timezone=True), nullable=True)
    live_at = Column(DateTime(timezone=True), nullable=True)
    note = Column(Text, nullable=True)
    updated_by = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PlatformClinicRole(Base):
    """Directory row for the 9-role vendor-user permission matrix (video 09)."""

    __tablename__ = "platform_clinic_roles"

    key = Column(String(50), primary_key=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    # Add-on roles (assignment_officer, document_verification) "work only in
    # conjunction with any other role" — enforced at assignment time.
    is_addon = Column(Boolean, nullable=False, default=False)
    sort_order = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PlatformCustomerBlockReason(Base):
    """Directory row for customer lock/block reasons."""

    __tablename__ = "platform_customer_block_reasons"

    code = Column(String(50), primary_key=True)
    label = Column(String(255), nullable=False)
    active = Column(Boolean, nullable=False, default=True)
    sort_order = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PlatformCustomerBlock(Base):
    """A lock/block on a customer — the audit row for TL's "Lock user".

    A row with ``unblocked_at IS NULL`` is the ACTIVE block (partial unique
    index guarantees at most one). Blocked customers get no NEW originations;
    existing-loan servicing is unaffected.
    """

    __tablename__ = "platform_customer_blocks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    patient_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_patients.id", ondelete="CASCADE"),
        nullable=False,
    )
    reason_code = Column(String(50), nullable=False)
    reason_text = Column(Text, nullable=False)
    blocked_by = Column(String, nullable=False)
    blocked_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    unblocked_at = Column(DateTime(timezone=True), nullable=True)
    unblocked_by = Column(String, nullable=True)
    unblock_note = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_platform_customer_blocks_patient", "patient_id"),
        Index(
            "uq_platform_customer_blocks_active",
            "patient_id",
            unique=True,
            postgresql_where=(unblocked_at.is_(None)),
        ),
    )
