"""Clinic API dependencies.

The clinic console authenticates with the existing platform staff JWT (reusing
``app.core.auth.get_current_user``), but clinic-API reads/writes are SCOPED to
the caller's clinic. Clinics ARE the existing ``vendors`` table (spec §13); a
user is linked to a clinic via ``platform_clinic_memberships``.

``get_current_clinic_user`` resolves the authenticated user's ``vendor_id`` from
that membership table and returns a ``ClinicPrincipal``. A user with no clinic
membership is rejected with 403 — the clinic API never falls back to
platform-wide data. Endpoints filter their queries by ``principal.vendor_id``.

``get_orchestrator`` is re-exported (a thin construction helper mirroring the
applicant deps) so the financing-link endpoint can build pre-filled applications,
and so tests can override it with a mock.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.auth import get_current_user  # noqa: F401  (re-exported for endpoints)
from app.db.base import get_db
from app.services.clinic_membership import resolve_clinic_membership


@dataclass(frozen=True)
class ClinicPrincipal:
    """The authenticated clinic caller, scoped to a single clinic/vendor.

    ``role`` is the CLINIC-side membership role (``platform_clinic_memberships
    .role``, default ``staff``) — distinct from platform user roles. WS-I uses
    it to gate rate-editing on vendor-originated applications against the
    product's ``PricingConfig.interest.rate_edit_roles``.
    """

    user: object
    vendor_id: UUID
    role: str = "staff"
    # WS-G 9-role permission matrix (``platform_clinic_memberships.roles``).
    # ``None`` = legacy membership = full access (see
    # ``app.services.clinic_permissions.has_clinic_permission``).
    roles: tuple[str, ...] | None = None

    @property
    def user_id(self):
        return getattr(self.user, "id", None)


def get_current_clinic_user(
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ClinicPrincipal:
    """Resolve the authenticated user's clinic (vendor) or raise 403.

    Requires a valid staff JWT (``get_current_user``) AND a clinic membership.
    Returns a ``ClinicPrincipal`` carrying the resolved ``vendor_id`` (which the
    endpoints use to scope their queries) and the clinic membership ``role``.
    """
    membership = resolve_clinic_membership(db, user.id)
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User is not a member of any clinic",
        )
    raw_roles = getattr(membership, "roles", None)
    return ClinicPrincipal(
        user=user,
        vendor_id=membership.vendor_id,
        role=membership.role or "staff",
        roles=tuple(raw_roles) if raw_roles is not None else None,
    )


def get_orchestrator(db: Session = Depends(get_db)):
    """Construct a FlowOrchestrator for the request (mirrors applicant deps)."""
    import app.services.consent_service as consent_service
    from app.services.flow_orchestrator import FlowOrchestrator
    from app.services.verifications.dispatcher import VerificationDispatcher

    return FlowOrchestrator(db, consent_service, VerificationDispatcher(db))
