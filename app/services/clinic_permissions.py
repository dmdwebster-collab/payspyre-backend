"""The 9-role vendor-side permission matrix (WS-G, video 09 f0067-f0071).

Turnkey's "Create vendor user" screen shows nine permission checkboxes; Dave
runs his whole practice on them. Our model:

* ``platform_clinic_roles``            — the seeded directory (source of truth
  for keys + the add-on flag).
* ``platform_clinic_memberships.roles``— per-user assignment: a JSON list of
  role keys. **NULL = legacy membership = full access** — every membership
  created before migration 061 keeps exactly the access it has today.
* Enforcement — ``require_clinic_permission`` FastAPI dependency, layered on
  the existing ``get_current_clinic_user`` scoping dependency (deps are the
  ONLY auth boundary of ``/api/clinic/v1``).

The on-screen TL note (verbatim): "Assignment officer, Document verification
are roles that just give access to additional features and work only in
conjunction with any other role." — ``validate_role_assignment`` enforces
that: an assignment consisting ONLY of add-on roles is rejected.

Everything here except the dependency is pure (DB-free tested).
"""
from __future__ import annotations

from typing import Iterable, Optional, Sequence

from fastapi import Depends, HTTPException, status

from app.api.clinic.v1.deps import ClinicPrincipal, get_current_clinic_user

# The nine role keys (mirror the migration-061 seed of platform_clinic_roles).
CLINIC_ROLE_KEYS: tuple[str, ...] = (
    "loan_origination",
    "loan_servicing",
    "monitoring",
    "collection",
    "assignment_officer",
    "vendor_management",
    "archive",
    "document_verification",
    "export",
)

# Roles that only augment other roles (TL on-screen note, verbatim above).
ADDON_ROLE_KEYS: frozenset[str] = frozenset({"assignment_officer", "document_verification"})


def validate_role_assignment(roles: Sequence[str]) -> list[str]:
    """Validate a proposed per-user role list; return a list of error strings.

    Rules:
      * at least one role;
      * every key must be one of the nine directory keys;
      * no duplicates;
      * add-on roles (assignment_officer / document_verification) require at
        least one non-add-on role alongside them.
    """
    errors: list[str] = []
    if not roles:
        errors.append("At least one role is required.")
        return errors
    unknown = [r for r in roles if r not in CLINIC_ROLE_KEYS]
    if unknown:
        errors.append(f"Unknown role(s): {', '.join(sorted(set(unknown)))}.")
    if len(set(roles)) != len(roles):
        errors.append("Duplicate roles in assignment.")
    valid = [r for r in roles if r in CLINIC_ROLE_KEYS]
    if valid and all(r in ADDON_ROLE_KEYS for r in valid):
        errors.append(
            "Assignment officer and Document verification are add-on roles and "
            "work only in conjunction with another role."
        )
    return errors


def has_clinic_permission(roles: Optional[Iterable[str]], permission: str) -> bool:
    """PURE: does a membership's role list grant ``permission``?

    ``roles=None`` is a LEGACY membership (created before the matrix existed)
    and grants everything — migrating existing clinic users to explicit roles
    is an admin action, not a breaking migration. An explicit empty list grants
    nothing (an admin deliberately locked the user down).
    """
    if permission not in CLINIC_ROLE_KEYS:
        raise ValueError(f"Unknown clinic permission: {permission!r}")
    if roles is None:
        return True
    return permission in set(roles)


def has_any_clinic_permission(
    roles: Optional[Iterable[str]], permissions: Sequence[str]
) -> bool:
    """PURE: does a membership's role list grant AT LEAST ONE of ``permissions``?

    Same legacy semantics as :func:`has_clinic_permission` (``roles=None`` =>
    full access, ``[]`` => nothing). ``permissions`` must be non-empty; every
    key is validated so a typo can never silently widen a route.
    """
    if not permissions:
        raise ValueError("At least one permission is required.")
    unknown = [p for p in permissions if p not in CLINIC_ROLE_KEYS]
    if unknown:
        raise ValueError(f"Unknown clinic permission(s): {sorted(set(unknown))!r}")
    if roles is None:
        return True
    held = set(roles)
    return any(p in held for p in permissions)


def require_clinic_permission(*permissions: str):
    """Dependency factory: 403 unless the clinic principal holds one of ``permissions``.

    Layered ON TOP of ``get_current_clinic_user`` (so vendor scoping + the
    membership requirement still apply). Multiple permissions are OR-ed — a
    route reachable from more than one workplace (e.g. the loan book, which
    both Servicing and Collection staff use) declares every role that may
    reach it. Usage::

        @router.post("", dependencies=[Depends(require_clinic_permission("loan_origination"))])
        @router.get("", dependencies=[Depends(require_clinic_permission("loan_servicing", "collection"))])

    The returned dependency carries ``__clinic_permissions__`` so the
    coverage test in ``tests/test_clinic_authz.py`` can prove that EVERY
    clinic route declares a permission.
    """
    if not permissions:
        raise ValueError("At least one permission is required.")
    for permission in permissions:  # fail at import time, not request time
        if permission not in CLINIC_ROLE_KEYS:
            raise ValueError(f"Unknown clinic permission: {permission!r}")

    required = tuple(permissions)

    def _dependency(
        principal: ClinicPrincipal = Depends(get_current_clinic_user),
    ) -> ClinicPrincipal:
        if not has_any_clinic_permission(principal.roles, required):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Your clinic role does not include "
                    + " or ".join(f"'{p}'" for p in required)
                    + "."
                ),
            )
        return principal

    _dependency.__clinic_permissions__ = required
    return _dependency
