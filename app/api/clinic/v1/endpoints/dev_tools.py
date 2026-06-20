"""Dev/staging-only clinic seeding helpers.

There is no production API to create a clinic membership (the clinic API only
READS the user→vendor link). That makes the clinic console + vendor marketplace
impossible to demo or test on a fresh environment. This endpoint creates a
vendor (clinic) + staff user + membership in one call and returns a ready-to-use
clinic JWT, so staging can exercise the whole clinic/vendor surface end-to-end.

NEVER mounted in production (see clinic router's ENVIRONMENT gate).
"""
from __future__ import annotations

import secrets
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.security import create_access_token, get_password_hash
from app.db.base import get_db
from app.models.loan import Vendor
from app.models.platform.clinic_membership import PlatformClinicMembership
from app.models.user import User

router = APIRouter(prefix="/dev", tags=["clinic-dev"])


class SeedClinicRequest(BaseModel):
    email: Optional[str] = None
    password: Optional[str] = None
    business_name: Optional[str] = None


class SeedClinicResponse(BaseModel):
    email: str
    password: str
    user_id: str
    vendor_id: str
    jwt: str


@router.post("/seed-clinic", response_model=SeedClinicResponse)
def seed_clinic(body: SeedClinicRequest, db: Session = Depends(get_db)):
    """Create a vendor + staff user + clinic membership; return a clinic JWT.

    Idempotent only by virtue of random defaults — each call with no body makes a
    fresh clinic. The returned ``jwt`` is a staff access token (``sub`` = user id)
    that ``get_current_clinic_user`` resolves to ``vendor_id`` via the membership.
    """
    suffix = secrets.token_hex(4)
    email = body.email or f"clinic-{suffix}@example.com"
    password = body.password or secrets.token_urlsafe(12)
    business_name = body.business_name or f"Demo Dental {suffix}"

    vendor = Vendor(
        business_name=business_name,
        business_type="corporation",
        contact_name="Demo Owner",
        email=f"vendor-{suffix}@example.com",
        phone="+15555550123",
        address_line1="123 Demo St",
        city="Toronto",
        province="ON",
        postal_code="M5V2T6",
    )
    db.add(vendor)
    db.flush()

    user = User(
        email=email,
        password_hash=get_password_hash(password),
        first_name="Clinic",
        last_name="Staff",
        is_active=True,
        is_verified=True,
    )
    db.add(user)
    db.flush()

    db.add(PlatformClinicMembership(user_id=user.id, vendor_id=vendor.id, role="staff"))
    db.commit()

    token = create_access_token({"sub": str(user.id)})
    return SeedClinicResponse(
        email=email,
        password=password,
        user_id=str(user.id),
        vendor_id=str(vendor.id),
        jwt=token,
    )
