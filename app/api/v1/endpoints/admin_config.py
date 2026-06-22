"""Lender/admin portal Phase 3 — configuration surfaces (read).

Phase 3 is the cockpit's config layer. Most of it already has a backend:
  * CREDIT PRODUCTS (the product builder, M8) — full CRUD lives at
    /api/v1/credit-products (admin-gated writes, versioned + audited); the cockpit
    surfaces it directly, so nothing is duplicated here.
  * RBAC visibility — this module: a read-only view of staff users and their
    roles so operators can see who has access.

Deliberately NOT built here (each needs business input or live creds, not eng):
  * decision-rule / scorecard editor — `credit_product.decision_ruleset` is a
    NAMED reference to underwriting logic; authoring rulesets is a business call.
  * notification templates — copy is business-owned; provider creds already live
    in the integration-settings area.
  * vendor activate/suspend writes — pending the lifecycle policy.

Read-only, admin-only.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session, selectinload

from app.core.auth import require_roles
from app.db.base import get_db
from app.models.user import Role, User

router = APIRouter(dependencies=[Depends(require_roles("admin"))])


class UserRow(BaseModel):
    id: str
    email: str
    name: str
    is_active: bool
    roles: list[str]
    last_login: datetime | None


class RoleRow(BaseModel):
    name: str
    description: str | None
    is_system: bool


@router.get("/users", response_model=list[UserRow])
def list_users(db: Session = Depends(get_db)):
    """Staff/admin access roster — who can sign in and what roles they hold."""
    users = (
        db.query(User)
        .options(selectinload(User.roles))
        .order_by(User.created_at.asc())
        .all()
    )
    # role name lookup (UserRoleLink → Role)
    role_by_id = {r.id: r.name for r in db.query(Role).all()}
    out = []
    for u in users:
        names = sorted({role_by_id.get(link.role_id, "?") for link in u.roles})
        out.append(UserRow(
            id=str(u.id), email=u.email,
            name=f"{u.first_name} {u.last_name}".strip(),
            is_active=bool(u.is_active), roles=names, last_login=u.last_login,
        ))
    return out


@router.get("/roles", response_model=list[RoleRow])
def list_roles(db: Session = Depends(get_db)):
    """The role catalog (name + description) for context on the access roster."""
    roles = db.query(Role).order_by(Role.name.asc()).all()
    return [RoleRow(name=r.name, description=r.description, is_system=bool(r.is_system)) for r in roles]
