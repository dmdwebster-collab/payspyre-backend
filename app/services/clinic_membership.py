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


def resolve_clinic_membership(db: Session, user_id) -> PlatformClinicMembership | None:
    """Return the user's clinic membership row, or ``None``.

    If the user has multiple memberships, the earliest-created one wins
    (deterministic, single-clinic assumption for the console today). The row
    carries both the ``vendor_id`` scope and the clinic-side ``role`` (used to
    gate rate-editing on vendor-originated applications, WS-I).
    """
    return (
        db.query(PlatformClinicMembership)
        .filter(PlatformClinicMembership.user_id == user_id)
        .order_by(PlatformClinicMembership.created_at.asc())
        .first()
    )


def resolve_clinic_vendor_id(db: Session, user_id) -> UUID | None:
    """Return the ``vendor_id`` of the clinic the user belongs to, or ``None``."""
    membership = resolve_clinic_membership(db, user_id)
    if membership is None:
        return None
    return membership.vendor_id


def resolve_clinic_user_emails(db: Session, vendor_id) -> list[str]:
    """Distinct emails of the active staff users who belong to a clinic.

    Used to notify a clinic's staff when PaySpyre posts a message on one of
    their applications. Returns ``[]`` when the clinic has no members.
    """
    from app.models.user import User

    rows = (
        db.query(User.email)
        .join(PlatformClinicMembership, PlatformClinicMembership.user_id == User.id)
        .filter(PlatformClinicMembership.vendor_id == vendor_id)
        .filter(User.is_active.is_(True))
        .all()
    )
    # Preserve determinism + dedupe while keeping only truthy emails.
    seen: list[str] = []
    for (email,) in rows:
        if email and email not in seen:
            seen.append(email)
    return seen
