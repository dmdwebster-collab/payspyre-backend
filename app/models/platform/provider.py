"""Providers — the practitioner/location a deal is written under, owned by a vendor.

Until now "provider" was FREE TEXT on the application
(``PlatformCreditApplication.provider_name``) and the Originations provider
dropdown was assembled by SELECTing DISTINCT over application history. That makes
the list a by-product of past data entry: a typo becomes a permanent option, a
brand-new vendor has an empty dropdown, and a provider who leaves can never be
retired.

A provider now belongs to its VENDOR — the vendor record is where the roster
lives, which is also how legacy servicing exports state it. The free-text
``provider_name`` column is retained (it holds history no table can reconstruct)
and is written alongside ``provider_id`` so nothing that reads it breaks.
"""
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.base import Base

#: ``source`` values — how the provider record came into being.
PROVIDER_SOURCE_MANUAL = "manual"
PROVIDER_SOURCE_IMPORT = "portfolio_import"


class PlatformProvider(Base):
    """One practitioner / practice location that a vendor writes deals under."""

    __tablename__ = "platform_providers"
    __table_args__ = (
        # A vendor's roster has no duplicate names.
        UniqueConstraint("vendor_id", "name", name="uq_platform_providers_vendor_name"),
        Index("ix_platform_providers_vendor", "vendor_id"),
        # Migration 083 also creates a functional UNIQUE index on
        # ``(vendor_id, lower(name))`` so "Dr. Jane Roe" and "DR. JANE ROE" cannot
        # both exist. It has no model expression (a functional index is a DB-only
        # artefact, like the ledger's WORM trigger); the service layer matches on
        # a case-folded key so it never trips it.
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    vendor_id = Column(
        UUID(as_uuid=True),
        ForeignKey("vendors.id", ondelete="CASCADE"),
        nullable=False,
    )

    #: Display name exactly as the vendor states it ("Dr. Jane Roe").
    name = Column(String, nullable=False)
    #: The source system's own identifier for this provider, when it has one.
    external_code = Column(String, nullable=True)
    #: A retired provider stays on historical loans but leaves the dropdown.
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    #: 'manual' | 'portfolio_import' — provenance, so an imported roster is
    #: distinguishable from one an admin typed in.
    source = Column(
        String, nullable=False, default=PROVIDER_SOURCE_MANUAL, server_default=PROVIDER_SOURCE_MANUAL
    )

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    vendor = relationship("Vendor")

    def __repr__(self) -> str:
        return f"<PlatformProvider(id={self.id}, vendor_id={self.vendor_id}, active={self.is_active})>"
