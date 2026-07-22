"""Clinic account self-service (spec §3.6) — Phase 4.

GET  /clinic/v1/account/profile                — read-only vendor profile.
POST /clinic/v1/account/profile/change-requests — submit an admin-routed
                                                   change request.

DECISION (LOCKED): vendors CANNOT edit their profile directly. The profile is
read-only. A change is submitted as a PENDING ``PlatformVendorProfileChangeRequest``
row that an admin approves/rejects OUT-OF-BAND. NO path in this module mutates
the ``vendors`` row.

SCOPING: clinics ARE the ``vendors`` table (spec §13). ``get_current_clinic_user``
resolves the caller's ``vendor_id`` (via ``platform_clinic_memberships``); every
read/write here is scoped to ``principal.vendor_id``, so one clinic can never see
or affect another's profile.

WHITELIST (enforced server-side, not trusted from the client): only
``contact_name``, ``email``, ``phone``, ``address_line1``, ``address_line2``,
``city``, ``province``, ``postal_code`` may be requested. Any never-requestable /
admin-only key (``business_name``, ``dba_name``, ``business_type``, ``status``,
``compliance_score``, ``license_number``, ``license_expiry``) → HTTP 422.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api.clinic.v1.deps import ClinicPrincipal, get_current_clinic_user
from app.services.clinic_permissions import require_clinic_permission
from app.db.base import get_db
from app.models.loan import Vendor
from app.models.platform.profile_change_request import (
    PlatformVendorProfileChangeRequest,
)

router = APIRouter(tags=["clinic-account"])


# --- whitelist -------------------------------------------------------------
# The ONLY keys a vendor may request a change to. Enforced server-side.
WHITELIST_FIELDS: frozenset[str] = frozenset(
    {
        "contact_name",
        "email",
        "phone",
        "address_line1",
        "address_line2",
        "city",
        "province",
        "postal_code",
    }
)

# Admin-only / never-requestable fields (documented; rejection is whitelist-based
# so any non-whitelisted key — including these — is refused).
NEVER_REQUESTABLE_FIELDS: frozenset[str] = frozenset(
    {
        "business_name",
        "dba_name",
        "business_type",
        "status",
        "compliance_score",
        "license_number",
        "license_expiry",
    }
)


# --- schemas ---------------------------------------------------------------


class VendorAddress(BaseModel):
    line1: str
    line2: Optional[str] = None
    city: str
    province: str
    postal_code: str


class VendorProfile(BaseModel):
    id: UUID
    business_name: str
    dba_name: Optional[str] = None
    business_type: str
    contact_name: str
    email: str
    phone: str
    address: VendorAddress
    status: str
    compliance_score: Optional[float] = None
    license_number: Optional[str] = None
    license_expiry: Optional[str] = None


class ProfileChangeRequestBody(BaseModel):
    # All optional; the whitelist is enforced server-side. ``extra="forbid"`` makes
    # any unknown key (e.g. an admin-only field) a 422 at validation time, and we
    # ALSO re-check the whitelist explicitly below so the rule is enforced even if
    # the schema is ever loosened.
    model_config = ConfigDict(extra="forbid")

    contact_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    province: Optional[str] = None
    postal_code: Optional[str] = None
    note: Optional[str] = None


class ProfileChangeRequest(BaseModel):
    id: UUID
    vendor_id: UUID
    requested_changes: dict[str, Optional[str]]
    status: str
    note: Optional[str] = None
    created_at: datetime


# --- shaping ---------------------------------------------------------------


def to_vendor_profile(vendor: Vendor) -> VendorProfile:
    return VendorProfile(
        id=vendor.id,
        business_name=vendor.business_name,
        dba_name=vendor.dba_name,
        business_type=vendor.business_type,
        contact_name=vendor.contact_name,
        email=vendor.email,
        phone=vendor.phone,
        address=VendorAddress(
            line1=vendor.address_line1,
            line2=vendor.address_line2,
            city=vendor.city,
            province=vendor.province,
            postal_code=vendor.postal_code,
        ),
        status=vendor.status,
        compliance_score=(
            float(vendor.compliance_score) if vendor.compliance_score is not None else None
        ),
        license_number=vendor.license_number,
        license_expiry=(
            vendor.license_expiry.isoformat() if vendor.license_expiry is not None else None
        ),
    )


def to_change_request(row: PlatformVendorProfileChangeRequest) -> ProfileChangeRequest:
    return ProfileChangeRequest(
        id=row.id,
        vendor_id=row.vendor_id,
        requested_changes=row.requested_changes or {},
        status=row.status,
        note=row.note,
        created_at=row.created_at,
    )


# --- routes ----------------------------------------------------------------


@router.get(
    "/account/profile",
    response_model=VendorProfile,
    dependencies=[Depends(require_clinic_permission("vendor_management", "monitoring"))],
)
def get_profile(
    db: Session = Depends(get_db),
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
):
    """Return the caller's own (read-only) vendor profile."""
    vendor = db.query(Vendor).filter(Vendor.id == principal.vendor_id).first()
    if vendor is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vendor not found")
    return to_vendor_profile(vendor)


@router.post(
    "/account/profile/change-requests",
    response_model=ProfileChangeRequest,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_clinic_permission("vendor_management"))],
)
def create_change_request(
    body: ProfileChangeRequestBody,
    db: Session = Depends(get_db),
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
):
    """Persist a PENDING, admin-routed profile change request.

    Does NOT mutate the ``vendors`` row. Enforces the whitelist server-side: any
    non-whitelisted (admin-only) key → 422. ``note`` is metadata, not a requested
    change, and is stored on its own column.
    """
    # ``extra="forbid"`` already rejects unknown keys; we still re-derive the
    # requested changes from the whitelist so trust never sits with the client.
    submitted = body.model_dump(exclude_unset=True)
    note = submitted.pop("note", None)

    # Belt-and-suspenders: refuse any key that is not whitelisted.
    forbidden = set(submitted) - WHITELIST_FIELDS
    if forbidden:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Fields not requestable by vendor: {sorted(forbidden)}",
        )

    requested_changes = {k: v for k, v in submitted.items() if k in WHITELIST_FIELDS}
    if not requested_changes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No requestable fields supplied",
        )

    change_request = PlatformVendorProfileChangeRequest(
        vendor_id=principal.vendor_id,
        requested_changes=requested_changes,
        status="pending",
        note=note,
    )
    db.add(change_request)
    db.commit()
    db.refresh(change_request)
    return to_change_request(change_request)
