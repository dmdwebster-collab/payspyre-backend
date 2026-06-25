"""Dev/staging-only admin seeding helper.

There is no production API to grant a user the ``admin`` RBAC role, which makes
the lender/admin operations cockpit impossible to sign into on a fresh
environment (the clinic dev-seed creates a *staff* clinic user, not an admin).
This endpoint creates a user and links the ``admin`` Role (creating it if the
catalog lacks it), returning ready-to-use login credentials + a staff JWT — so
staging can exercise the whole `/api/v1/admin/*` cockpit end-to-end.

`require_roles("admin")` resolves a user's roles via the UserRoleLink → Role
chain (``user.roles[*].role.name``), so the seed must create that link, not just
set a string.

NEVER mounted in production (same ENVIRONMENT gate as the clinic dev-seed).
"""
from __future__ import annotations

import secrets
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_access_token, get_password_hash
from app.db.base import get_db
from app.models.user import Role, User, UserRoleLink
from app.services import demo_simulation

router = APIRouter(prefix="/dev", tags=["admin-dev"])


class RunDemoBody(BaseModel):
    score: int = 720          # 720 approves, 640 → manual review, 550 declines
    amount_cents: int = 2_500_000
    post_payment: bool = True


@router.post("/run-demo-application")
def run_demo_application(body: RunDemoBody, db: Session = Depends(get_db)):
    """Run one fully-simulated application end to end and return the step trace.

    Drives the real engine (orchestrator + loan_servicing) on mock adapters, so
    the whole lifecycle — decision, booked loan, amortization, payment, statement,
    payoff — is produced with real numbers and zero integration creds.
    """
    try:
        return demo_simulation.run_demo_application(
            db, score=body.score, amount_cents=body.amount_cents, post_payment=body.post_payment
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


def _require_seed_token(x_dev_seed_token: Optional[str] = Header(default=None)) -> None:
    """Guard: the seeder is inert unless DEV_SEED_TOKEN is configured AND matched.

    This is a FULL-ADMIN granter, so — unlike the clinic/staff seeder — it must not
    be openly callable just because dev tools are enabled. A constant-time compare
    avoids leaking the token via timing.
    """
    configured = settings.DEV_SEED_TOKEN or ""
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin seeding is disabled (no DEV_SEED_TOKEN configured).",
        )
    if not x_dev_seed_token or not secrets.compare_digest(x_dev_seed_token, configured):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing X-Dev-Seed-Token.",
        )


class SeedAdminRequest(BaseModel):
    # EmailStr at the request boundary: a reserved/invalid TLD would create a
    # user who then 403s on every login (the login response model re-validates
    # the email) — reject it here as a 422 instead.
    email: Optional[EmailStr] = None
    password: Optional[str] = None
    role: str = "admin"  # "admin" (full cockpit) or "staff" (read + servicing)


class SeedAdminResponse(BaseModel):
    email: str
    password: str
    user_id: str
    role: str
    jwt: str


@router.post("/seed-admin", response_model=SeedAdminResponse,
             dependencies=[Depends(_require_seed_token)])
def seed_admin(body: SeedAdminRequest, db: Session = Depends(get_db)):
    """Create a user + link the given RBAC role; return login creds + a JWT.

    Each call with no body mints a fresh admin (random email/password). The
    returned ``jwt`` is a staff access token (``sub`` = user id) that the cockpit
    accepts directly; the email/password also work at ``POST /api/v1/auth/login``.
    """
    role_name = (body.role or "admin").strip().lower()
    if role_name not in ("admin", "staff"):
        raise HTTPException(status_code=422, detail="role must be 'admin' or 'staff'")

    suffix = secrets.token_hex(4)
    email = body.email or f"admin-{suffix}@example.com"
    password = body.password or secrets.token_urlsafe(12)

    # Find-or-create the Role so a fresh catalog still works.
    role = db.query(Role).filter(Role.name == role_name).first()
    if role is None:
        role = Role(name=role_name, description=f"{role_name} (seeded)", is_system=True)
        db.add(role)
        db.flush()

    user = User(
        email=email,
        password_hash=get_password_hash(password),
        first_name="Ops",
        last_name="Admin" if role_name == "admin" else "Staff",
        is_active=True,
        is_verified=True,
    )
    db.add(user)
    try:
        db.flush()  # a caller-supplied duplicate email collides here, not only at commit
        db.add(UserRoleLink(user_id=user.id, role_id=role.id))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A user with email {email!r} already exists.",
        )

    token = create_access_token({"sub": str(user.id)})
    return SeedAdminResponse(
        email=email, password=password, user_id=str(user.id), role=role_name, jwt=token,
    )
