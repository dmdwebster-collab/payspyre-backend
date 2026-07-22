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
  * vendor activate/suspend writes — pending the lifecycle policy.

NOTIFICATION RULES (added with the Dave-template integration, 2026-07): the
static template registry (``NOTIFICATION_TYPES``) is the source of truth for
which notifications EXIST; ``platform_notification_rules`` stores only EDITS
(cadence offsets, channel toggles, per-channel subject/body overrides). The
list endpoint returns every registry type with its resolved effective config,
so the cockpit shows the full catalog with zero seeding; PUT upserts a row the
first time a type is edited.

Admin-only. Users/roles read-only; notification rules writable.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_roles
from app.core.config import settings
from app.db.base import get_db
from app.models.user import Role
from app.services import staff_accounts

router = APIRouter(dependencies=[Depends(require_roles("admin"))])


class UserRow(BaseModel):
    id: str
    email: str
    name: str
    first_name: str
    last_name: str
    phone: str | None = None
    is_active: bool
    is_verified: bool = False
    roles: list[str]
    #: Direct per-user permission grants (the Add-user grid). Role-derived
    #: permissions are NOT listed here — this is the grant set an admin edits.
    permissions: list[str] = Field(default_factory=list)
    #: ``active`` | ``invited`` | ``invite_expired``
    invite_state: str = "active"
    last_login: datetime | None = None
    created_at: datetime | None = None


class RoleRow(BaseModel):
    name: str
    description: str | None
    is_system: bool


class PermissionRow(BaseModel):
    name: str
    label: str
    resource: str
    action: str
    column: int
    row: int
    conjunction_only: bool
    seeded: bool


class PermissionCatalog(BaseModel):
    note: str
    permissions: list[PermissionRow]


class UserCreateRequest(BaseModel):
    """Create a staff user. There is deliberately NO password field —
    Dave: "that should never be a thing". The response carries an invite link
    the new user follows to set their own password."""

    email: EmailStr
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    phone: str | None = Field(default=None, max_length=20)
    roles: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)


class UserUpdateRequest(BaseModel):
    email: EmailStr | None = None
    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    phone: str | None = Field(default=None, max_length=20)


class RoleAssignRequest(BaseModel):
    roles: list[str]


class PermissionAssignRequest(BaseModel):
    permissions: list[str]


class InviteResponse(BaseModel):
    user: UserRow
    invite_url: str
    invite_expires_at: datetime | None = None


def _invite_url(token: str) -> str:
    base = settings.cors_origins_list[0] if settings.cors_origins_list else ""
    return f"{base}/accept-invite?token={token}"


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/users", response_model=list[UserRow])
def list_users(
    db: Session = Depends(get_db),
    search: str | None = Query(default=None, description="Substring match on name or email"),
    permission: str | None = Query(
        default=None, description="Only users holding this DIRECT permission grant"
    ),
    include_inactive: bool = Query(default=True),
):
    """Staff/admin access roster — who can sign in, what roles and direct
    permission grants they hold, and where they are in the invite lifecycle."""
    return staff_accounts.list_users(
        db, search=search, permission=permission, include_inactive=include_inactive
    )


@router.get("/roles", response_model=list[RoleRow])
def list_roles(db: Session = Depends(get_db)):
    """The role catalog (name + description) for context on the access roster."""
    roles = db.query(Role).order_by(Role.name.asc()).all()
    return [RoleRow(name=r.name, description=r.description, is_system=bool(r.is_system)) for r in roles]


@router.get("/permissions", response_model=PermissionCatalog)
def list_permission_catalog(db: Session = Depends(get_db)):
    """The grantable permission grid, in Dave's three-column layout, with the
    conjunction-roles note. `seeded` says whether the permission row exists in
    the DB (it always should after migration 072)."""
    return staff_accounts.permission_catalog(db)


@router.post("/users", response_model=InviteResponse, status_code=status.HTTP_201_CREATED)
def create_user(
    body: UserCreateRequest,
    db: Session = Depends(get_db),
    actor=Depends(get_current_user),
):
    """Onboard a colleague. Creates the account with NO password and returns a
    single-use invite link (valid 7 days) for them to set their own."""
    try:
        user, token = staff_accounts.create_user(
            db,
            actor=actor,
            email=str(body.email),
            first_name=body.first_name,
            last_name=body.last_name,
            phone=body.phone,
            roles=body.roles,
            permissions=body.permissions,
        )
    except staff_accounts.StaffAccountError as exc:
        raise _bad_request(exc) from exc
    return InviteResponse(
        user=UserRow(**staff_accounts.user_row(user)),
        invite_url=_invite_url(token),
        invite_expires_at=user.password_reset_expires,
    )


@router.patch("/users/{user_id}", response_model=UserRow)
def update_user(
    user_id: UUID,
    body: UserUpdateRequest,
    db: Session = Depends(get_db),
    actor=Depends(get_current_user),
):
    """Edit a user's profile. Password is not editable from here, by design."""
    try:
        user = staff_accounts.update_user(
            db, user_id, actor=actor,
            first_name=body.first_name, last_name=body.last_name,
            phone=body.phone, email=str(body.email) if body.email else None,
        )
    except staff_accounts.StaffAccountError as exc:
        raise _bad_request(exc) from exc
    return UserRow(**staff_accounts.user_row(user))


@router.post("/users/{user_id}/deactivate", response_model=UserRow)
def deactivate_user(
    user_id: UUID, db: Session = Depends(get_db), actor=Depends(get_current_user)
):
    """Disable sign-in and revoke every live session. Refuses self-deactivation
    and refuses to remove the last active admin."""
    try:
        user = staff_accounts.set_active(db, user_id, actor=actor, is_active=False)
    except staff_accounts.StaffAccountError as exc:
        raise _bad_request(exc) from exc
    return UserRow(**staff_accounts.user_row(user))


@router.post("/users/{user_id}/reactivate", response_model=UserRow)
def reactivate_user(
    user_id: UUID, db: Session = Depends(get_db), actor=Depends(get_current_user)
):
    """Re-enable a deactivated account (their password, if set, still works)."""
    try:
        user = staff_accounts.set_active(db, user_id, actor=actor, is_active=True)
    except staff_accounts.StaffAccountError as exc:
        raise _bad_request(exc) from exc
    return UserRow(**staff_accounts.user_row(user))


@router.put("/users/{user_id}/roles", response_model=UserRow)
def set_user_roles(
    user_id: UUID,
    body: RoleAssignRequest,
    db: Session = Depends(get_db),
    actor=Depends(get_current_user),
):
    """Replace the user's role set (PUT semantics — send the full list)."""
    try:
        user = staff_accounts.set_roles(db, user_id, actor=actor, roles=body.roles)
    except staff_accounts.StaffAccountError as exc:
        raise _bad_request(exc) from exc
    return UserRow(**staff_accounts.user_row(user))


@router.put("/users/{user_id}/permissions", response_model=UserRow)
def set_user_permissions(
    user_id: UUID,
    body: PermissionAssignRequest,
    db: Session = Depends(get_db),
    actor=Depends(get_current_user),
):
    """Replace the user's DIRECT permission grants (PUT semantics). Additive
    only — clearing grants never removes role-derived access."""
    try:
        user = staff_accounts.set_permissions(
            db, user_id, actor=actor, permissions=body.permissions
        )
    except staff_accounts.StaffAccountError as exc:
        raise _bad_request(exc) from exc
    return UserRow(**staff_accounts.user_row(user))


@router.post("/users/{user_id}/invite", response_model=InviteResponse)
def resend_invite(
    user_id: UUID, db: Session = Depends(get_db), actor=Depends(get_current_user)
):
    """Mint a fresh invite link for a user who has not yet set a password.
    Refused once they have — established accounts use forgot-password."""
    try:
        user, token = staff_accounts.resend_invite(db, user_id, actor=actor)
    except staff_accounts.StaffAccountError as exc:
        raise _bad_request(exc) from exc
    return InviteResponse(
        user=UserRow(**staff_accounts.user_row(user)),
        invite_url=_invite_url(token),
        invite_expires_at=user.password_reset_expires,
    )


# ---------------------------------------------------------------------------
# Notification rules — full catalog view + per-type edits
# ---------------------------------------------------------------------------


class NotificationRuleRow(BaseModel):
    notification_type: str
    offsets: list[int]
    email_enabled: bool
    dashboard_enabled: bool
    sms_enabled: bool
    content_overrides: dict
    enabled: bool
    has_sms_template: bool     # registry ships an SMS body for this type
    customized: bool           # a DB row exists (differs from shipped defaults)


class NotificationRuleUpdate(BaseModel):
    offsets: list[int] | None = None
    email_enabled: bool | None = None
    dashboard_enabled: bool | None = None
    sms_enabled: bool | None = None
    content_overrides: dict | None = Field(
        default=None,
        description='{"email": {"subject": .., "body": ..}, "sms": {"body": ..}} — '
        "Jinja strings; empty dict clears all overrides",
    )
    enabled: bool | None = None


def _rule_row(db: Session, ntype: str) -> NotificationRuleRow:
    from app.models.platform.notification_rule import PlatformNotificationRule
    from app.services.notification_config import get_rule
    from app.services.notification_render import NOTIFICATION_TYPES

    resolved = get_rule(db, ntype)
    row = db.get(PlatformNotificationRule, ntype)
    spec = NOTIFICATION_TYPES.get(ntype)
    return NotificationRuleRow(
        notification_type=ntype,
        offsets=list(resolved.offsets),
        email_enabled="email" in resolved.enabled_channels,
        dashboard_enabled="dashboard" in resolved.enabled_channels,
        sms_enabled="sms" in resolved.enabled_channels,
        content_overrides=resolved.content_overrides,
        enabled=resolved.enabled,
        has_sms_template=bool(spec and spec.sms_template),
        customized=row is not None,
    )


@router.get("/notification-rules", response_model=list[NotificationRuleRow])
def list_notification_rules(db: Session = Depends(get_db)):
    """Every notification type the platform can send, with its effective
    config. Types come from the static registry (DB rows only store edits), so
    this is complete without any seeding."""
    from app.services.notification_render import NOTIFICATION_TYPES

    return [_rule_row(db, ntype) for ntype in sorted(NOTIFICATION_TYPES)]


@router.put("/notification-rules/{notification_type}", response_model=NotificationRuleRow)
def update_notification_rule(
    notification_type: str,
    body: NotificationRuleUpdate,
    db: Session = Depends(get_db),
):
    """Upsert the edit row for one notification type. Only provided fields
    change; unspecified fields keep their current effective value (which is
    materialized into the row on first edit). Content overrides are validated
    as Jinja before save so a typo can't break sends at dispatch time."""
    from app.models.platform.notification_rule import PlatformNotificationRule
    from app.services.notification_config import get_rule
    from app.services.notification_render import NOTIFICATION_TYPES

    if notification_type not in NOTIFICATION_TYPES:
        raise HTTPException(status_code=404, detail="unknown notification type")

    if body.content_overrides is not None:
        for channel, ov in body.content_overrides.items():
            if channel not in ("email", "sms", "dashboard") or not isinstance(ov, dict):
                raise HTTPException(status_code=422, detail=f"bad override for channel {channel!r}")
            for key in ("subject", "body"):
                tpl = ov.get(key)
                if tpl is None:
                    continue
                try:
                    # Syntax check only: render against a permissive dummy that
                    # tolerates any variable name.
                    from jinja2 import Environment, Undefined, select_autoescape

                    Environment(undefined=Undefined, autoescape=select_autoescape(["html"])).from_string(str(tpl)).render()
                except Exception as exc:  # noqa: BLE001 — surface template error verbatim
                    raise HTTPException(
                        status_code=422,
                        detail=f"invalid Jinja in {channel}.{key}: {exc}",
                    ) from exc

    row = db.get(PlatformNotificationRule, notification_type)
    if row is None:
        current = get_rule(db, notification_type)
        row = PlatformNotificationRule(
            notification_type=notification_type,
            offsets=list(current.offsets),
            email_enabled="email" in current.enabled_channels,
            dashboard_enabled="dashboard" in current.enabled_channels,
            sms_enabled="sms" in current.enabled_channels,
            content_overrides=dict(current.content_overrides),
            enabled=current.enabled,
        )
        db.add(row)

    if body.offsets is not None:
        row.offsets = [int(n) for n in body.offsets]
    if body.email_enabled is not None:
        row.email_enabled = body.email_enabled
    if body.dashboard_enabled is not None:
        row.dashboard_enabled = body.dashboard_enabled
    if body.sms_enabled is not None:
        row.sms_enabled = body.sms_enabled
    if body.content_overrides is not None:
        row.content_overrides = body.content_overrides
    if body.enabled is not None:
        row.enabled = body.enabled

    db.commit()
    return _rule_row(db, notification_type)
