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
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, EmailStr
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.auth import require_permission_or_admin, require_roles
from app.core.config import settings
from app.core.security import create_access_token, get_password_hash
from app.db.base import get_db
from app.models.platform.loan import PlatformLoan
from app.models.user import Permission, Role, RolePermission, User, UserRoleLink
from app.services import demo_simulation, dev_seed_collections

router = APIRouter(prefix="/dev", tags=["admin-dev"])


class MarkLoanSignedResponse(BaseModel):
    loan_id: str
    agreement_status: str


@router.post(
    "/loans/{loan_id}/mark-signed",
    response_model=MarkLoanSignedResponse,
    dependencies=[Depends(require_roles("admin", "staff"))],
)
def mark_loan_signed(loan_id: UUID, db: Session = Depends(get_db)):
    """DEV/staging: mark a loan's agreement ``signed`` so the disburse maker-checker
    can be demoed.

    The real signed-callback (``on_agreement_signed``) marks signed AND immediately
    fires disbursement — which would make the manual maker-checker disburse a no-op.
    This isolates just the signing so a tester can then request → approve a disburse
    and watch it execute. Admin/staff-gated and only mounted off-prod.
    """
    loan = db.query(PlatformLoan).filter(PlatformLoan.id == loan_id).first()
    if loan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Loan not found")
    loan.agreement_status = "signed"
    db.commit()
    db.refresh(loan)
    return MarkLoanSignedResponse(loan_id=str(loan.id), agreement_status=loan.agreement_status)


@router.post(
    "/hardship/{request_id}/force-sign",
    dependencies=[Depends(require_permission_or_admin("hardship", "apply"))],
)
def force_sign_hardship(request_id: UUID, db: Session = Depends(get_db)):
    """DEV/staging: substitute for the borrower's e-signature on a hardship
    amendment (WS-J simulation mode — no SignNow adapter configured).

    Runs the SAME path as the real SignNow completion webhook
    (``hardship.mark_signed``): sets ``signed_at`` first, then applies the
    change via the WS-F surgery primitives. Gated on the dedicated
    hardship/apply permission (admin implicit) and only mounted off-prod —
    in production the borrower's real signature is the ONLY way a hardship
    change applies.
    """
    from app.models.platform.hardship import PlatformHardshipRequest
    from app.services import hardship as hardship_service

    request = (
        db.query(PlatformHardshipRequest)
        .filter(PlatformHardshipRequest.id == request_id)
        .first()
    )
    if request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Hardship request not found"
        )
    loan = db.query(PlatformLoan).filter(PlatformLoan.id == request.loan_id).first()
    if loan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Loan not found")
    try:
        hardship_service.mark_signed(db, request, loan, actor="dev:force-sign")
    except hardship_service.HardshipError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    db.commit()
    return {
        "hardship_request_id": str(request.id),
        "status": request.status,
        "signed_at": request.signed_at.isoformat() if request.signed_at else None,
        "applied_at": request.applied_at.isoformat() if request.applied_at else None,
    }


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
    # "admin" (full cockpit), "staff" (read + servicing), or "hardship_officer"
    # (staff + the dedicated hardship/create + hardship/apply Role→Permission
    # grants — WS-J; lets staging exercise the non-admin permission gate).
    role: str = "admin"


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
    if role_name not in ("admin", "staff", "hardship_officer"):
        raise HTTPException(
            status_code=422,
            detail="role must be 'admin', 'staff' or 'hardship_officer'",
        )

    suffix = secrets.token_hex(4)
    email = body.email or f"admin-{suffix}@example.com"
    password = body.password or secrets.token_urlsafe(12)

    # Find-or-create the Role so a fresh catalog still works.
    role = db.query(Role).filter(Role.name == role_name).first()
    if role is None:
        role = Role(name=role_name, description=f"{role_name} (seeded)", is_system=True)
        db.add(role)
        db.flush()

    if role_name == "hardship_officer":
        # WS-J: attach the dedicated hardship permissions to the role
        # (find-or-create both the Permission rows and the links).
        for action in ("create", "apply"):
            perm = (
                db.query(Permission)
                .filter(Permission.resource == "hardship", Permission.action == action)
                .first()
            )
            if perm is None:
                perm = Permission(
                    name=f"hardship:{action}",
                    description=f"Hardship v1 — {action} (seeded)",
                    resource="hardship",
                    action=action,
                )
                db.add(perm)
                db.flush()
            link = (
                db.query(RolePermission)
                .filter(
                    RolePermission.role_id == role.id,
                    RolePermission.permission_id == perm.id,
                )
                .first()
            )
            if link is None:
                db.add(RolePermission(role_id=role.id, permission_id=perm.id))
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


class SeedPastDueResponse(BaseModel):
    seeded_count: int
    accounts: list[dict]
    aging: dict


@router.post(
    "/seed-past-due",
    response_model=SeedPastDueResponse,
    dependencies=[Depends(_require_seed_token)],
)
def seed_past_due(db: Session = Depends(get_db)):
    """DEV/staging: seed several past-due loans across the delinquency buckets.

    Creates one fully-originated, funded loan per bucket (current-month-late / 30 /
    60 / 90 / 120+), back-dated so Collections and the delinquency aging + queue
    reports have data to work against. Each account is booked through the real
    engine and then marked delinquent by the real aging pass — so the state is
    identical to how it arises in production.

    Guarded exactly like ``/seed-admin``: inert unless ``DEV_SEED_TOKEN`` is set AND
    presented in ``X-Dev-Seed-Token``, and the whole router is only mounted off
    production. Returns a 409 if there is no active credit product to originate
    against (create one first).
    """
    try:
        return dev_seed_collections.seed_past_due_accounts(db)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
