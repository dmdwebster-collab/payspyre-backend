import secrets
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from contextvars import ContextVar

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Context variable for storing user context across async calls
_user_context: ContextVar[Optional[Dict[str, Any]]] = ContextVar("_user_context", default=None)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(
    data: dict,
    expires_delta: Optional[timedelta] = None,
    token_type: str = "access"
) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire, "type": token_type})
    encoded_jwt = jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return encoded_jwt


def create_refresh_token(data: dict) -> tuple[str, str]:
    token = secrets.token_urlsafe(64)
    expires_delta = timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS)
    expire = datetime.utcnow() + expires_delta
    return token, expire


def decode_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except JWTError:
        return None


def generate_verification_token() -> str:
    return secrets.token_urlsafe(64)


def generate_reset_token() -> str:
    return secrets.token_urlsafe(64)


def generate_api_key() -> str:
    return f"ps_{secrets.token_urlsafe(32)}"


def hash_api_key(api_key: str) -> str:
    return pwd_context.hash(api_key)


def verify_api_key(plain_key: str, hashed_key: str) -> bool:
    return pwd_context.verify(plain_key, hashed_key)


def verify_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
    import hmac
    import hashlib

    expected = hmac.new(
        secret.encode(),
        payload,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


# ============================================================================
# User Context for RLS
# ============================================================================

def set_request_user_context(user_id: str, role: str, vendor_id: Optional[str] = None, borrower_id: Optional[str] = None) -> None:
    """
    Set the user context for the current request.

    This should be called by the authentication middleware or dependency
    after user authentication is complete. The context is stored in a
    ContextVar and can be accessed by get_request_user_context().

    Args:
        user_id: The authenticated user's ID
        role: The user's role (admin, vendor, borrower, staff, patient)
        vendor_id: Vendor ID if user is a vendor
        borrower_id: Borrower ID if user is a borrower
    """
    context = {
        "user_id": str(user_id),
        "role": role,
        "vendor_id": str(vendor_id) if vendor_id else None,
        "borrower_id": str(borrower_id) if borrower_id else None,
    }
    _user_context.set(context)


def get_request_user_context() -> Optional[Dict[str, Any]]:
    """
    Get the user context for the current request.

    Returns a dictionary with:
        - user_id: The current user's ID
        - role: The user's role (admin, vendor, borrower, staff, patient)
        - vendor_id: Vendor ID if applicable
        - borrower_id: Borrower ID if applicable

    Returns None if no user context is set (e.g., public endpoint).
    """
    return _user_context.get(None)


def clear_request_user_context() -> None:
    """
    Clear the user context for the current request.

    This should be called at the end of request processing to ensure
    no context leaks between requests.
    """
    _user_context.set(None)


# ============================================================================
# User Context Helper Functions
# ============================================================================

def get_current_user_id() -> Optional[str]:
    """
    Get the current user ID from the request context.

    Convenience function for RLS and other security checks.

    Returns:
        The current user's ID, or None if not authenticated.
    """
    context = get_request_user_context()
    return context["user_id"] if context else None


def get_current_user_role() -> Optional[str]:
    """
    Get the current user's role from the request context.

    Convenience function for role-based access control.

    Returns:
        The current user's role, or None if not authenticated.
    """
    context = get_request_user_context()
    return context["role"] if context else None


def get_vendor_id() -> Optional[str]:
    """
    Get the vendor ID from the request context.

    Convenience function for vendor-scoped operations.

    Returns:
        The vendor ID if user is a vendor, None otherwise.
    """
    context = get_request_user_context()
    return context.get("vendor_id") if context else None


def get_borrower_id() -> Optional[str]:
    """
    Get the borrower ID from the request context.

    Convenience function for borrower-scoped operations.

    Returns:
        The borrower ID if user is a borrower, None otherwise.
    """
    context = get_request_user_context()
    return context.get("borrower_id") if context else None