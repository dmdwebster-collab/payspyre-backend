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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
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
