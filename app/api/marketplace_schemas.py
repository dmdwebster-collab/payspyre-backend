"""Pydantic v2 request/response schemas for the marketplace API (spec §6.3).

Shared by BOTH the applicant-side (``app/api/applicant/v1/endpoints/marketplace.py``)
and clinic/vendor-side (``app/api/clinic/v1/endpoints/marketplace.py``) routers so
neither existing ``schemas.py`` has to be edited.

Money is ALWAYS integer cents. Vendor-facing lead/listing projections are
de-identified dicts produced by ``app.services.marketplace.visibility.vendor_view``
(PII-free), so the vendor read endpoints return raw dicts rather than a fixed
schema — the shape is config-driven (budget disclosure varies by lead_state).
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# --- patient-side requests --------------------------------------------------


class CreateListingRequest(BaseModel):
    treatment_categories: list[str] = Field(..., min_length=1, max_length=10)
    # Strict enum: an unknown urgency was previously accepted + silently priced at
    # the 1.0x default (mispriced lead). The set matches config/marketplace/lead_pricing.yaml.
    treatment_urgency: Literal["immediate", "this_week", "this_month", "flexible"]
    estimated_budget_cents: Optional[int] = Field(default=None, ge=0)
    location_postal_code: str = Field(..., min_length=3, max_length=12)
    max_travel_km: int = Field(default=25, ge=1, le=500)
    # Opt-in consent to share a de-identified lead with clinics (spec §8.1).
    # The client must set this true (a checked consent box); the service records
    # the marketplace_listing consent and refuses to list without it.
    consent_acknowledged: bool = False


class SelectClinicRequest(BaseModel):
    vendor_id: UUID


# --- patient-side responses -------------------------------------------------


class ListingSummary(BaseModel):
    """Owner-visible summary of a listing (the patient's own listing)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    status: str
    treatment_categories: list[str]
    treatment_urgency: str
    lead_state: str
    base_lead_price_cents: int
    expires_at: datetime


class InterestedClinic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    vendor_id: UUID
    expressed_at: datetime
    appointment_booked_at: Optional[datetime] = None


# --- vendor-side responses --------------------------------------------------


class VendorBillingEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    listing_id: UUID
    lead_charge_cents: int
    lead_charged_at: datetime
    charge_trigger: str
