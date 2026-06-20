"""Resolve the authenticated user's clinic (vendor) for the clinic API.

Clinics ARE the existing ``vendors`` table (spec §13). A user is granted access
to a clinic via a ``platform_clinic_memberships`` row. This service resolves the
user's ``vendor_id`` from that table; the clinic ``deps.get_current_clinic_user``
dependency turns a missing membership into a 403.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.models.platform.clinic_membership import PlatformClinicMembership


def resolve_clinic_vendor_id(db: Session, user_id) -> UUID | None:
    """Return the ``vendor_id`` of the clinic the user belongs to, or ``None``.

    If the user has multiple memberships, the earliest-created one wins
    (deterministic, single-clinic assumption for the console today).
    """
    membership = (
        db.query(PlatformClinicMembership)
        .filter(PlatformClinicMembership.user_id == user_id)
        .order_by(PlatformClinicMembership.created_at.asc())
        .first()
    )
    if membership is None:
        return None
    return membership.vendor_id
