"""Pydantic request/response models for the clinic (practice-facing) API.

Shapes are dictated by the frontend clinic console contract in
``payspyre-frontend/src/lib/clinicApi.ts`` + ``clinicMock.ts``. Field names and
the (narrowed) status vocabulary intentionally match those TS interfaces so the
console can drop the mocks and call the real endpoints unchanged.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# The clinic console only distinguishes four buckets (clinicMock.ts
# ClinicApplicationStatus). The platform application status enum has nine
# values; ``app.api.clinic.v1.status_map`` collapses them into these.
ClinicApplicationStatus = Literal["started", "approved", "declined", "manual_review"]


# --- products --------------------------------------------------------------


class ClinicProduct(BaseModel):
    """Mirror of the frontend ``ClinicProduct`` interface."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    code: str
    name: str
    vertical: str
    min_amount_cents: int
    max_amount_cents: int
    currency: str


# --- applications ----------------------------------------------------------


class ClinicApplication(BaseModel):
    """Mirror of the frontend ``ClinicApplication`` interface."""

    id: UUID
    patient_name: str
    patient_contact: str
    product_name: str
    amount_cents: int
    currency: str
    status: ClinicApplicationStatus
    created_at: datetime


# --- dashboard -------------------------------------------------------------


class ClinicDashboardSummary(BaseModel):
    """Mirror of the frontend ``ClinicDashboardSummary`` interface."""

    started: int = 0
    approved: int = 0
    declined: int = 0
    manual_review: int = 0
    total: int = 0


# --- financing links -------------------------------------------------------


class CreateFinancingLinkBody(BaseModel):
    """Mirror of the frontend ``CreateFinancingLinkBody`` interface."""

    credit_product_id: UUID
    amount_cents: int = Field(..., gt=0)
    patient_name: str = Field(..., min_length=1)
    patient_contact: str = Field(..., min_length=1)


class ClinicFinancingLink(BaseModel):
    """Mirror of the frontend ``ClinicFinancingLink`` interface."""

    application_ref: str
    url: str
    patient_name: str
    amount_cents: int
    product_name: str
