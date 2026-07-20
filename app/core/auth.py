from datetime import datetime, timedelta
from typing import Optional, Dict, Any, TYPE_CHECKING
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import (
    decode_token,
    set_request_user_context,
    clear_request_user_context
)
from app.db.base import get_db
from app.schemas.auth import TokenPayload

if TYPE_CHECKING:
    from app.models.user import User

security = HTTPBearer()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
):
    from app.models.user import User
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    payload = decode_token(credentials.credentials)
    if payload is None:
        raise credentials_exception

    token_payload = TokenPayload(**payload)
    if token_payload.type != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
        )

    user = db.query(User).filter(User.id == token_payload.sub).first()
    if user is None:
        raise credentials_exception

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is disabled",
        )

    if not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email not verified",
        )

    return user


def get_current_active_user(current_user = Depends(get_current_user)):
    if not current_user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")
    return current_user


def require_roles(*required_roles: str):
    def role_checker(current_user = Depends(get_current_user)):
        user_roles = {role.role.name for role in current_user.roles}
        if not any(role in user_roles for role in required_roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"User does not have required roles. Required: {required_roles}, Has: {user_roles}",
            )
        return current_user
    return role_checker


def require_permission(resource: str, action: str):
    def permission_checker(current_user = Depends(get_current_user)):
        for user_role in current_user.roles:
            role = user_role.role
            for role_perm in role.permissions:
                perm = role_perm.permission
                if perm.resource == resource and perm.action == action:
                    return current_user

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"User does not have permission: {action} on {resource}",
        )
    return permission_checker


def user_has_permission_or_admin(user, resource: str, action: str) -> bool:
    """Non-dependency form of ``require_permission_or_admin`` for CONDITIONAL
    gates — where the permission is only needed for some request shapes (e.g.
    a backdated effective_date needs ``ledger.backdate``; a same-day one
    doesn't). Same semantics: admin is implicitly allowed everywhere."""
    user_roles = {user_role.role.name for user_role in getattr(user, "roles", [])}
    if "admin" in user_roles:
        return True
    for user_role in getattr(user, "roles", []):
        for role_perm in user_role.role.permissions:
            perm = role_perm.permission
            if perm.resource == resource and perm.action == action:
                return True
    return False


def require_permission_or_admin(resource: str, action: str):
    """Permission gate with an implicit admin allowance (WS-J hardship model).

    Dave's mandate for restricted servicing surfaces (hardship, write-off):
    "user-defined availability so that junior staff members can't …" — a plain
    ``staff`` role is NOT enough; the user needs the specific Role→Permission
    grant (resource/action) OR the ``admin`` role, which is implicitly allowed
    everywhere.
    """

    def permission_checker(current_user=Depends(get_current_user)):
        user_roles = {user_role.role.name for user_role in current_user.roles}
        if "admin" in user_roles:
            return current_user
        for user_role in current_user.roles:
            for role_perm in user_role.role.permissions:
                perm = role_perm.permission
                if perm.resource == resource and perm.action == action:
                    return current_user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"User does not have permission: {action} on {resource}",
        )

    return permission_checker


# P8.1 (H-4) — removed the broken X-API-Key authentication path (`get_api_user` +
# `get_current_user_or_api_key`). It compared the raw `X-API-Key` header directly
# against the bcrypt `ApiKey.key_hash`, so it could never match a correctly-created
# key (failed closed) — and the only way to make it "work" would have been storing
# keys in plaintext. Both functions were unused by any endpoint; JWT (`get_current_user`)
# is the live auth path. The admin-gated /api-keys management endpoints + ApiKey model
# remain (not a vulnerability); removing that now-inert feature is an optional follow-up.


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests: dict[str, list[datetime]] = {}

    def is_allowed(self, identifier: str) -> bool:
        now = datetime.utcnow()
        cutoff = now - timedelta(seconds=self.window_seconds)

        if identifier not in self.requests:
            self.requests[identifier] = []

        self.requests[identifier] = [
            req_time for req_time in self.requests[identifier] if req_time > cutoff
        ]

        if len(self.requests[identifier]) >= self.max_requests:
            return False

        self.requests[identifier].append(now)
        return True


auth_rate_limiter = RateLimiter(
    settings.RATE_LIMIT_AUTH_REQUESTS,
    settings.RATE_LIMIT_AUTH_WINDOW
)

read_rate_limiter = RateLimiter(
    settings.RATE_LIMIT_READ_REQUESTS,
    settings.RATE_LIMIT_READ_WINDOW
)

write_rate_limiter = RateLimiter(
    settings.RATE_LIMIT_WRITE_REQUESTS,
    settings.RATE_LIMIT_WRITE_WINDOW
)

webhook_rate_limiter = RateLimiter(
    settings.RATE_LIMIT_WEBHOOK_REQUESTS,
    settings.RATE_LIMIT_WEBHOOK_WINDOW
)


# ============================================================================
# User Context Management for RLS
# ============================================================================

def _extract_user_vendor_borrower_id(user, db: Session) -> Dict[str, Optional[str]]:
    """
    Extract vendor_id or borrower_id from user if they have a vendor/borrower role.

    This function queries related tables to find the vendor or borrower associated
    with the user. This is a placeholder that should be updated based on your
    actual user-vendor/borrower relationship model.

    Args:
        user: The authenticated User object
        db: Database session

    Returns:
        Dictionary with vendor_id and/or borrower_id
    """
    vendor_id = None
    borrower_id = None

    # Get user roles
    user_roles = {role.role.name for role in user.roles}

    # If user has vendor role, find their vendor
    if "vendor" in user_roles:
        # TODO: Update this based on your actual user-vendor relationship
        # For now, we'll return None and assume you have a user_vendor table
        # or vendor.user_id field
        pass

    # If user has borrower role, find their borrower
    if "borrower" in user_roles or "patient" in user_roles:
        # TODO: Update this based on your actual user-borrower relationship
        # For now, we'll return None and assume you have a user_borrower table
        # or borrower.user_id field
        pass

    return {"vendor_id": vendor_id, "borrower_id": borrower_id}


def get_current_user_from_token(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db)
):
    from app.models.user import User

    """
    Authenticate user from JWT token and set RLS context.

    This is a dependency that both authenticates the user AND sets the
    request user context for RLS policies. Use this in your endpoints
    instead of get_current_user when you need RLS enabled.

    Args:
        credentials: Bearer token credentials
        db: Database session

    Returns:
        The authenticated User object

    Raises:
        HTTPException: If authentication fails
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    payload = decode_token(credentials.credentials)
    if payload is None:
        raise credentials_exception

    token_payload = TokenPayload(**payload)
    if token_payload.type != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
        )

    user = db.query(User).filter(User.id == token_payload.sub).first()
    if user is None:
        raise credentials_exception

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is disabled",
        )

    if not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email not verified",
        )

    # Set user context for RLS
    user_roles = [role.role.name for role in user.roles]
    primary_role = user_roles[0] if user_roles else "staff"

    # Extract vendor_id/borrower_id if applicable
    context_data = _extract_user_vendor_borrower_id(user, db)

    set_request_user_context(
        user_id=str(user.id),
        role=primary_role,
        vendor_id=context_data.get("vendor_id"),
        borrower_id=context_data.get("borrower_id"),
    )

    return user


class RLSAuthMiddleware:
    """
    Middleware to manage RLS user context across requests.

    This middleware ensures that user context is set after authentication
    and cleared at the end of each request to prevent context leakage.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Create Request object for middleware chain
        request = Request(scope, receive)

        async def call_next(request):
            # Process the request through the app
            response = await self.app(scope, receive, send)

            # Clear user context at end of request
            clear_request_user_context()

            return response

        # Process the request
        response = await call_next(request)

        return response


# ============================================================================
# Simplified Auth Dependencies (without RLS context)
# ============================================================================

def get_optional_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False)),
    db: Session = Depends(get_db)
) -> Optional:
    """
    Get current user if authenticated, otherwise return None.

    This does NOT set RLS context. Use for public endpoints that may
    optionally use authenticated user data.
    """
    from app.models.user import User

    if not credentials:
        return None

    payload = decode_token(credentials.credentials)
    if payload is None:
        return None

    try:
        token_payload = TokenPayload(**payload)
        if token_payload.type != "access":
            return None

        user = db.query(User).filter(User.id == token_payload.sub).first()
        if user and user.is_active and user.is_verified:
            return user
    except Exception:
        return None

    return None


def rate_limit(limiter: RateLimiter, identifier: Optional[str] = None):
    async def rate_limiter_dependency(request: Request):
        if not settings.RATE_LIMIT_ENABLED:
            return

        key = identifier or request.client.host
        if not limiter.is_allowed(key):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests",
                headers={
                    "Retry-After": str(limiter.window_seconds),
                    "X-RateLimit-Limit": str(limiter.max_requests),
                    "X-RateLimit-Window": str(limiter.window_seconds),
                },
            )
    return rate_limiter_dependency