"""Marketplace lead-engine models (spec §2.8, §2.9, §6).

The marketplace turns the platform into a two-sided lead engine: a patient opts in
to list their financing need (``PlatformMarketplaceListing``), vendors (clinics)
browse de-identified leads and express interest (``PlatformMarketplaceVendorInterest``),
and a lead charge is levied at a configurable trigger point (§6.2).

PII NEVER crosses the wire to a vendor until the patient selects that vendor —
the visibility layer (``app/services/marketplace/visibility.py``) redacts listings
into a vendor-safe view (FSA only, budget range/exact/hidden per lead state).

Money is integer cents throughout (``*_cents``), matching the loan/credit models.
``treatment_categories`` is a Postgres TEXT[] (ARRAY(String)); the whole test suite
runs on Postgres so the native array type is available.

Denormalized columns (``lead_state``, ``verification_depth``) are snapshots copied
from ``platform_patients`` at listing time so the marketplace can filter/price leads
without joining + re-deriving on every query. They are plain TEXT here (not the
``platform_lead_state`` / ``platform_verification_depth`` enums) so a listing keeps a
stable historical snapshot even if those enums later gain members.
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    String,
    Integer,
    BigInteger,
    ForeignKey,
    func,
)
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from sqlalchemy.orm import relationship

from app.db.base import Base


class PlatformMarketplaceListing(Base):
    """A patient's opt-in marketplace listing (spec §2.8).

    status: 'active' | 'paused' | 'closed' | 'expired'
    treatment_urgency: 'immediate' | 'this_week' | 'this_month' | 'flexible'
    """

    __tablename__ = "platform_marketplace_listings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    patient_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_patients.id"),
        nullable=False,
    )
    status = Column(String, nullable=False, default="active")

    # ---- What the patient needs -------------------------------------------
    treatment_categories = Column(ARRAY(String), nullable=False)
    treatment_urgency = Column(String, nullable=False)
    estimated_budget_cents = Column(BigInteger, nullable=True)
    location_postal_code = Column(String, nullable=False)
    max_travel_km = Column(Integer, nullable=False, default=25)

    # ---- Lead enrichment (denormalized from platform_patients) -------------
    lead_state = Column(String, nullable=False)
    verification_depth = Column(String, nullable=False)

    # ---- Marketplace economics --------------------------------------------
    # Filled by the pricing engine (config/marketplace/lead_pricing.yaml) at create.
    base_lead_price_cents = Column(Integer, nullable=False)
    accepted_vendor_id = Column(UUID(as_uuid=True), nullable=True)
    accepted_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)

    # Relationships
    patient = relationship("PlatformPatient")
    interests = relationship(
        "PlatformMarketplaceVendorInterest",
        back_populates="listing",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<PlatformMarketplaceListing(id={self.id}, status={self.status}, "
            f"lead_state={self.lead_state})>"
        )


class PlatformMarketplaceVendorInterest(Base):
    """A vendor's expression of interest in a listing — and its lead charge (spec §2.9).

    charge_trigger: 'expressed_interest' | 'patient_selected' | 'appointment_booked'
                    | 'treatment_completed'  (which event levied ``lead_charge_cents``).
    """

    __tablename__ = "platform_marketplace_vendor_interest"
    # One interest row per (vendor, listing) — makes express_interest idempotent at
    # the DB level (mirrors migration 031's unique index; declared here so the model
    # is the source of truth and the invariant is visible in the ORM).
    __table_args__ = (
        Index(
            "idx_marketplace_interest_vendor_listing",
            "vendor_id",
            "listing_id",
            unique=True,
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    listing_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_marketplace_listings.id", ondelete="CASCADE"),
        nullable=False,
    )
    vendor_id = Column(UUID(as_uuid=True), nullable=False)

    expressed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    selected_by_patient_at = Column(DateTime(timezone=True), nullable=True)
    appointment_booked_at = Column(DateTime(timezone=True), nullable=True)

    lead_charge_cents = Column(Integer, nullable=True)
    lead_charged_at = Column(DateTime(timezone=True), nullable=True)
    charge_trigger = Column(String, nullable=True)

    # Relationships
    listing = relationship("PlatformMarketplaceListing", back_populates="interests")

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<PlatformMarketplaceVendorInterest(id={self.id}, "
            f"listing_id={self.listing_id}, vendor_id={self.vendor_id})>"
        )
