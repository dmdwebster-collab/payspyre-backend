"""Staff/admin account administration — the write half of Settings → Accounts → Users.

Closes the gap the Settings IA rebuild was blocked on: today an admin can SEE
the roster (``GET /admin/config/users``) but cannot onboard a colleague without
a DB seed. This module adds create / update / deactivate / reactivate / role
assignment / **per-user permission grants**, plus the invite flow.

DAVE'S HARD RULE — admins NEVER set a user's initial password
-------------------------------------------------------------
TL's ``Add user`` dialog carries a Password field with an eye reveal. Dave on
that: *"that should never be a thing."* So :func:`create_user` writes
``password_hash = NULL`` and issues a single-use **invite token** (reusing the
existing ``users.password_reset_token`` / ``password_reset_expires`` columns —
no new secret store). The invitee sets their own password at
``POST /api/v1/auth/accept-invite``. There is no code path anywhere in this
module that accepts, generates, or returns a password.

PERMISSION MODEL — additive, behaviour-preserving
--------------------------------------------------
RBAC today is Role → RolePermission → Permission, resolved through
``app.core.auth``. Dave's 16-checkbox grid (the verbatim inventory actually
enumerates **19** boxes — see :data:`WORKPLACE_PERMISSIONS`) is a *per-user*
surface, not a per-role one, so this adds a sibling edge:

    User → UserPermissionGrant → Permission

``app.core.auth`` now consults role grants **then** direct grants. Because no
user has a direct grant until an admin creates one, every existing
authorization decision is unchanged. Direct grants can only ADD access; nothing
here can revoke a permission a user's role already carries.

Every mutation writes a ``platform_events`` row (``staff_user.*``) carrying the
acting admin, the target user, and the before/after of what changed. Passwords,
tokens and invite URLs are never written to the event log.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional
from uuid import UUID

from sqlalchemy.orm import Session, selectinload

from app.core.security import generate_reset_token
from app.models.platform.event import PlatformEvent
from app.models.user import (
    Permission,
    Role,
    User,
    UserPermissionGrant,
    UserRoleLink,
)

#: How long an invite / password-setup link stays valid. Deliberately longer
#: than the 1-hour forgot-password window (a new hire may not be at their desk),
#: deliberately finite (an invite is a credential-equivalent).
INVITE_TTL = timedelta(days=7)


# ---------------------------------------------------------------------------
# The workplace-permission grid (Settings → Accounts → Users → Add user)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkplacePermission:
    """One checkbox in Dave's permission grid.

    ``column``/``row`` reproduce the three-column layout verbatim so the admin
    UI can render the grid in Dave's order without hard-coding it client-side.
    ``conjunction_only`` marks the four entries covered by the dialog's amber
    note (see :data:`CONJUNCTION_NOTE`) — they grant an extra capability and are
    meaningless on their own.
    """

    name: str          # permissions.name (the grant key the API speaks)
    resource: str      # permissions.resource
    action: str        # permissions.action
    label: str         # Dave's on-screen label, verbatim
    column: int        # 1..3
    row: int           # 1..7
    conjunction_only: bool = False


#: Verbatim from the `Add user` dialog (docs/turnkey_parity/rewatch_2026-07-21/
#: 07-08_settings.md §A1.1). The dialog's own caption says "16 permission
#: checkboxes" but the transcribed three-column table enumerates 19; the table
#: is the artefact that was read off the frames, so 19 is what we ship. Flagged
#: for Dave in the PR.
WORKPLACE_PERMISSIONS: tuple[WorkplacePermission, ...] = (
    WorkplacePermission("origination.access", "origination", "access", "Loan origination", 1, 1),
    WorkplacePermission("collection.access", "collection", "access", "Collection", 2, 1),
    WorkplacePermission("risk_evaluation.access", "risk_evaluation", "access", "Risk Evaluation", 3, 1),
    WorkplacePermission("underwriting.access", "underwriting", "access", "Underwriting", 1, 2),
    WorkplacePermission("reports.access", "reports", "access", "Reports", 2, 2),
    WorkplacePermission(
        "system_administration.access", "system_administration", "access",
        "System administration", 3, 2,
    ),
    WorkplacePermission(
        "customer_management.access", "customer_management", "access",
        "Customer management", 1, 3,
    ),
    WorkplacePermission("archive.access", "archive", "access", "Archive", 2, 3),
    WorkplacePermission("loan_servicing.access", "loan_servicing", "access", "Loan servicing", 3, 3),
    WorkplacePermission("blacklist.access", "blacklist", "access", "Blacklist", 1, 4),
    WorkplacePermission(
        "repayment_transaction.edit_reverse", "repayment_transaction", "edit_reverse",
        "Edit/Reverse repayment transaction", 2, 4, conjunction_only=True,
    ),
    WorkplacePermission(
        "import_transactions.access", "import_transactions", "access",
        "Import transactions", 3, 4,
    ),
    WorkplacePermission("export.access", "export", "access", "Export", 1, 5),
    WorkplacePermission(
        "import_loans_customers.access", "import_loans_customers", "access",
        "Import loans and customers", 2, 5,
    ),
    WorkplacePermission("loan_migration.access", "loan_migration", "access", "Loan migration", 3, 5),
    WorkplacePermission(
        "assignment_officer.access", "assignment_officer", "access",
        "Assignment officer", 1, 6, conjunction_only=True,
    ),
    WorkplacePermission(
        "branch_management.access", "branch_management", "access",
        "Branch management", 2, 6, conjunction_only=True,
    ),
    WorkplacePermission(
        "document_verification.access", "document_verification", "access",
        "Document verification", 3, 6, conjunction_only=True,
    ),
    WorkplacePermission(
        "vendor_management.access", "vendor_management", "access",
        "Vendor management", 1, 7,
    ),
)

#: The amber note above the grid, verbatim.
CONJUNCTION_NOTE = (
    "NOTE: Edit/Reverse repayment transaction, Assignment officer, Branch "
    "management, Document verification are roles that just give access to "
    "additional features and work only in conjunction with any other role."
)

WORKPLACE_PERMISSION_NAMES = frozenset(p.name for p in WORKPLACE_PERMISSIONS)


class StaffAccountError(ValueError):
    """A staff-account mutation was rejected (conflict / policy / not found)."""


# ---------------------------------------------------------------------------
# Read model
# ---------------------------------------------------------------------------


def invite_state(user: User, *, now: Optional[datetime] = None) -> str:
    """Where a user sits in the invite lifecycle.

    ``active``          — has a password; can sign in.
    ``invited``         — no password yet, invite token still valid.
    ``invite_expired``  — no password and the invite lapsed; resend it.
    """
    if user.password_hash:
        return "active"
    now = now or datetime.utcnow()
    expires = user.password_reset_expires
    if user.password_reset_token and expires is not None:
        # Stored values may be tz-aware (server_default now()) — compare naively.
        expires_naive = expires.replace(tzinfo=None) if expires.tzinfo else expires
        if expires_naive > now:
            return "invited"
    return "invite_expired"


def user_row(user: User, *, role_names: Optional[list[str]] = None) -> dict[str, Any]:
    """API-shaped dict for one staff user. Never includes password material."""
    roles = (
        role_names
        if role_names is not None
        else sorted({link.role.name for link in user.roles if link.role is not None})
    )
    return {
        "id": str(user.id),
        "email": user.email,
        "name": f"{user.first_name} {user.last_name}".strip(),
        "first_name": user.first_name,
        "last_name": user.last_name,
        "phone": user.phone,
        "is_active": bool(user.is_active),
        "is_verified": bool(user.is_verified),
        "roles": roles,
        "permissions": sorted(
            g.permission.name for g in user.permission_grants if g.permission is not None
        ),
        "invite_state": invite_state(user),
        "last_login": user.last_login,
        "created_at": user.created_at,
    }


def list_users(
    db: Session,
    *,
    search: Optional[str] = None,
    permission: Optional[str] = None,
    include_inactive: bool = True,
) -> list[dict[str, Any]]:
    """The roster, with Dave's search box and `All permissions` filter.

    Filtering is done in Python over an eagerly-loaded roster (staff counts are
    two digits, and it keeps the SQL static — no interpolated predicates).
    """
    users = (
        db.query(User)
        .options(
            selectinload(User.roles).selectinload(UserRoleLink.role),
            selectinload(User.permission_grants).selectinload(UserPermissionGrant.permission),
        )
        .order_by(User.created_at.asc())
        .all()
    )
    rows = [user_row(u) for u in users]
    if not include_inactive:
        rows = [r for r in rows if r["is_active"]]
    if search:
        needle = search.strip().lower()
        rows = [
            r for r in rows
            if needle in r["email"].lower() or needle in r["name"].lower()
        ]
    if permission:
        rows = [r for r in rows if permission in r["permissions"]]
    return rows


def permission_catalog(db: Session) -> dict[str, Any]:
    """The grid definition + which entries actually exist as DB permissions."""
    known = {
        name for (name,) in db.query(Permission.name).filter(
            Permission.name.in_(sorted(WORKPLACE_PERMISSION_NAMES))
        )
    }
    return {
        "note": CONJUNCTION_NOTE,
        "permissions": [
            {
                "name": p.name,
                "label": p.label,
                "resource": p.resource,
                "action": p.action,
                "column": p.column,
                "row": p.row,
                "conjunction_only": p.conjunction_only,
                "seeded": p.name in known,
            }
            for p in WORKPLACE_PERMISSIONS
        ],
    }


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def _audit(
    db: Session,
    event_type: str,
    *,
    actor: User,
    target: User,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    """Append a ``staff_user.*`` row to platform_events (no commit — the caller
    commits so the audit row and the change land in one transaction)."""
    body: dict[str, Any] = {
        "target_user_id": str(target.id),
        "target_email": target.email,
        "actor_user_id": str(actor.id),
    }
    if payload:
        body.update(payload)
    db.add(PlatformEvent(event_type=event_type, actor=actor.email, payload=body))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_user(db: Session, user_id: UUID) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise StaffAccountError(f"User {user_id} not found")
    return user


def _resolve_roles(db: Session, names: list[str]) -> list[Role]:
    roles = db.query(Role).filter(Role.name.in_(sorted(set(names)))).all()
    found = {r.name for r in roles}
    missing = sorted(set(names) - found)
    if missing:
        raise StaffAccountError(f"Unknown role(s): {', '.join(missing)}")
    return roles


def _resolve_permissions(db: Session, names: list[str]) -> list[Permission]:
    perms = db.query(Permission).filter(Permission.name.in_(sorted(set(names)))).all()
    found = {p.name for p in perms}
    missing = sorted(set(names) - found)
    if missing:
        raise StaffAccountError(f"Unknown permission(s): {', '.join(missing)}")
    return perms


def _active_admin_count(db: Session, *, excluding: Optional[UUID] = None) -> int:
    q = (
        db.query(User.id)
        .join(UserRoleLink, UserRoleLink.user_id == User.id)
        .join(Role, Role.id == UserRoleLink.role_id)
        .filter(Role.name == "admin", User.is_active.is_(True))
    )
    if excluding is not None:
        q = q.filter(User.id != excluding)
    return q.distinct().count()


def _issue_invite(user: User, *, now: Optional[datetime] = None) -> str:
    """Stamp a fresh single-use invite token on the user and return it."""
    now = now or datetime.utcnow()
    token = generate_reset_token()
    user.password_reset_token = token
    user.password_reset_expires = now + INVITE_TTL
    return token


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def create_user(
    db: Session,
    *,
    actor: User,
    email: str,
    first_name: str,
    last_name: str,
    phone: Optional[str] = None,
    roles: Optional[list[str]] = None,
    permissions: Optional[list[str]] = None,
) -> tuple[User, str]:
    """Onboard a colleague. Returns ``(user, invite_token)``.

    The user is created WITHOUT a password (``password_hash = NULL``) and
    ``is_verified = False``; both are set when the invitee accepts the invite
    and chooses their own password. Until then they cannot sign in — the login
    path refuses a user with no password hash.
    """
    email = email.strip().lower()
    if db.query(User).filter(User.email == email).first() is not None:
        raise StaffAccountError(f"A user with email '{email}' already exists")

    role_rows = _resolve_roles(db, roles or [])
    perm_rows = _resolve_permissions(db, permissions or [])

    user = User(
        email=email,
        password_hash=None,          # Dave's rule: admins never set passwords.
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        phone=phone,
        is_active=True,
        is_verified=False,
    )
    db.add(user)
    db.flush()

    for role in role_rows:
        db.add(UserRoleLink(user_id=user.id, role_id=role.id))
    for perm in perm_rows:
        db.add(UserPermissionGrant(user_id=user.id, permission_id=perm.id, granted_by=actor.id))

    token = _issue_invite(user)
    _audit(
        db, "staff_user.created", actor=actor, target=user,
        payload={
            "roles": sorted(r.name for r in role_rows),
            "permissions": sorted(p.name for p in perm_rows),
            "invite_expires_at": user.password_reset_expires.isoformat(),
        },
    )
    db.commit()
    db.refresh(user)
    return user, token


def update_user(
    db: Session,
    user_id: UUID,
    *,
    actor: User,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    phone: Optional[str] = None,
    email: Optional[str] = None,
) -> User:
    """Edit a user's profile fields. Password is NOT an editable field here."""
    user = _get_user(db, user_id)
    changed: dict[str, Any] = {}

    if email is not None:
        new_email = email.strip().lower()
        if new_email != user.email:
            clash = db.query(User).filter(User.email == new_email).first()
            if clash is not None:
                raise StaffAccountError(f"A user with email '{new_email}' already exists")
            changed["email"] = {"from": user.email, "to": new_email}
            user.email = new_email
    for field, value in (("first_name", first_name), ("last_name", last_name), ("phone", phone)):
        if value is None:
            continue
        value = value.strip() if isinstance(value, str) else value
        if value != getattr(user, field):
            changed[field] = {"from": getattr(user, field), "to": value}
            setattr(user, field, value)

    if changed:
        _audit(db, "staff_user.updated", actor=actor, target=user, payload={"changes": changed})
        db.commit()
        db.refresh(user)
    return user


def set_active(db: Session, user_id: UUID, *, actor: User, is_active: bool) -> User:
    """Deactivate / reactivate a user.

    Guards (both are lockout protection, not policy): an admin may not
    deactivate themselves, and the last active admin may not be deactivated.
    Deactivating revokes every live session so access ends immediately.
    """
    user = _get_user(db, user_id)
    if user.is_active == is_active:
        return user

    if not is_active:
        if user.id == actor.id:
            raise StaffAccountError("You cannot deactivate your own account")
        is_admin = any(
            link.role is not None and link.role.name == "admin" for link in user.roles
        )
        if is_admin and _active_admin_count(db, excluding=user.id) == 0:
            raise StaffAccountError(
                "Cannot deactivate the last active admin — grant admin to another "
                "user first"
            )

    user.is_active = is_active
    revoked = 0
    if not is_active:
        from app.models.user import Session as UserSession

        sessions = (
            db.query(UserSession)
            .filter(UserSession.user_id == user.id, UserSession.is_active.is_(True))
            .all()
        )
        now = datetime.utcnow()
        for s in sessions:
            s.is_active = False
            s.revoked_at = now
        revoked = len(sessions)

    _audit(
        db,
        "staff_user.reactivated" if is_active else "staff_user.deactivated",
        actor=actor, target=user, payload={"sessions_revoked": revoked},
    )
    db.commit()
    db.refresh(user)
    return user


def set_roles(db: Session, user_id: UUID, *, actor: User, roles: list[str]) -> User:
    """Replace a user's role set (full PUT semantics)."""
    user = _get_user(db, user_id)
    role_rows = _resolve_roles(db, roles)
    before = sorted({link.role.name for link in user.roles if link.role is not None})
    after = sorted(r.name for r in role_rows)
    if before == after:
        return user

    if "admin" in before and "admin" not in after:
        if user.is_active and _active_admin_count(db, excluding=user.id) == 0:
            raise StaffAccountError(
                "Cannot remove admin from the last active admin — grant admin to "
                "another user first"
            )

    db.query(UserRoleLink).filter(UserRoleLink.user_id == user.id).delete(
        synchronize_session=False
    )
    for role in role_rows:
        db.add(UserRoleLink(user_id=user.id, role_id=role.id))

    _audit(
        db, "staff_user.roles_changed", actor=actor, target=user,
        payload={"from": before, "to": after},
    )
    db.commit()
    db.refresh(user)
    return user


def set_permissions(
    db: Session, user_id: UUID, *, actor: User, permissions: list[str]
) -> User:
    """Replace a user's DIRECT permission grants (the 19-checkbox grid).

    Direct grants are purely additive on top of whatever the user's roles
    already carry; clearing them can never take away role-derived access.
    """
    user = _get_user(db, user_id)
    perm_rows = _resolve_permissions(db, permissions)
    before = sorted(
        g.permission.name for g in user.permission_grants if g.permission is not None
    )
    after = sorted(p.name for p in perm_rows)
    if before == after:
        return user

    db.query(UserPermissionGrant).filter(
        UserPermissionGrant.user_id == user.id
    ).delete(synchronize_session=False)
    for perm in perm_rows:
        db.add(
            UserPermissionGrant(
                user_id=user.id, permission_id=perm.id, granted_by=actor.id
            )
        )

    _audit(
        db, "staff_user.permissions_changed", actor=actor, target=user,
        payload={
            "from": before,
            "to": after,
            "granted": sorted(set(after) - set(before)),
            "revoked": sorted(set(before) - set(after)),
        },
    )
    db.commit()
    db.refresh(user)
    return user


def resend_invite(db: Session, user_id: UUID, *, actor: User) -> tuple[User, str]:
    """Issue a fresh invite token. Refuses once the user has a password — an
    established account uses the ordinary forgot-password flow, so an admin can
    never mint a login link for a colleague's live account."""
    user = _get_user(db, user_id)
    if user.password_hash:
        raise StaffAccountError(
            "This user has already set a password — they should use the "
            "'forgot password' flow rather than a new invite"
        )
    token = _issue_invite(user)
    _audit(
        db, "staff_user.invite_resent", actor=actor, target=user,
        payload={"invite_expires_at": user.password_reset_expires.isoformat()},
    )
    db.commit()
    db.refresh(user)
    return user, token


def accept_invite(db: Session, token: str, new_password: str) -> Optional[User]:
    """Complete an invite: the invitee sets their OWN password.

    Only matches a user who has never had a password (``password_hash IS
    NULL``) — an ordinary password reset stays on the existing
    ``/auth/reset-password`` path, untouched. Accepting also verifies the
    account (the token was delivered to the invited address, which is exactly
    what email verification proves).
    """
    user = (
        db.query(User)
        .filter(
            User.password_reset_token == token,
            User.password_reset_expires > datetime.utcnow(),
            User.password_hash.is_(None),
        )
        .first()
    )
    if user is None:
        return None

    from app.core.security import get_password_hash

    user.password_hash = get_password_hash(new_password)
    user.password_reset_token = None
    user.password_reset_expires = None
    user.is_verified = True
    db.add(
        PlatformEvent(
            event_type="staff_user.invite_accepted",
            actor=user.email,
            payload={"target_user_id": str(user.id), "target_email": user.email},
        )
    )
    db.commit()
    db.refresh(user)
    return user
