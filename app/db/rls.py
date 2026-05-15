"""
Row Level Security (RLS) context management.

This module provides utilities to set tenant context for RLS policies.
Each database request should set the appropriate context variables to
ensure multi-tenant data isolation.
"""
from functools import wraps
from typing import Optional, Callable, Dict, Any

from fastapi import Depends
from sqlalchemy import event, text
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_request_user_context, get_current_user_id
from app.core.auth import get_current_user_from_token
from app.db.base import get_db
from app.models.user import UserRole


def set_rls_context(
    db: Session,
    user_id: str,
    user_role: str,
    vendor_id: Optional[str] = None,
    borrower_id: Optional[str] = None,
) -> None:
    """
    Set RLS context variables for the current database session.

    These variables are used by RLS policies to enforce data isolation.

    Args:
        db: Database session
        user_id: Current user's ID
        user_role: User's role (admin, vendor, borrower, staff, patient)
        vendor_id: Vendor ID (required if user_role is vendor)
        borrower_id: Borrower ID (required if user_role is borrower)
    """
    # Clear any existing context
    db.execute(text("SET LOCAL app.current_user_id = NULL"))
    db.execute(text("SET LOCAL app.current_user_role = NULL"))
    db.execute(text("SET LOCAL app.vendor_id = NULL"))
    db.execute(text("SET LOCAL app.borrower_id = NULL"))

    # Set new context
    db.execute(text(f"SET LOCAL app.current_user_id = '{user_id}'"))
    db.execute(text(f"SET LOCAL app.current_user_role = '{user_role}'"))

    if user_role == UserRole.VENDOR.value and vendor_id:
        db.execute(text(f"SET LOCAL app.vendor_id = '{vendor_id}'"))
    elif user_role == UserRole.BORROWER.value and borrower_id:
        db.execute(text(f"SET LOCAL app.borrower_id = '{borrower_id}'"))


def rls_context(
    vendor_id: Optional[str] = None,
    borrower_id: Optional[str] = None,
) -> Callable:
    """
    Decorator to automatically set RLS context for a function.

    Usage:
        @rls_context(vendor_id="123...")
        def get_vendor_applications(db: Session):
            # RLS policies will automatically filter to this vendor's data
            return db.query(LoanApplication).all()

    Args:
        vendor_id: Vendor ID for vendor users
        borrower_id: Borrower ID for borrower users
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            db = kwargs.get('db') or next(
                (arg for arg in args if isinstance(arg, Session)),
                None
            )

            if not db:
                raise ValueError("RLS context decorator requires 'db' Session parameter")

            # Get user context from request state (set by auth middleware)
            from app.core.security import get_request_user_context
            user_context = get_request_user_context()

            if user_context:
                set_rls_context(
                    db,
                    user_id=user_context['user_id'],
                    user_role=user_context['role'],
                    vendor_id=vendor_id,
                    borrower_id=borrower_id,
                )

            return func(*args, **kwargs)

        return wrapper
    return decorator


class RLSMiddleware:
    """
    Middleware to automatically set RLS context for each database request.

    This should be added to the FastAPI dependency chain to ensure
    every database operation has proper tenant context.
    """

    def __init__(self, db: Session):
        self.db = db

    def set_context(
        self,
        user_id: str,
        user_role: str,
        vendor_id: Optional[str] = None,
        borrower_id: Optional[str] = None,
    ) -> None:
        """Set RLS context for this session."""
        set_rls_context(
            self.db,
            user_id=user_id,
            user_role=user_role,
            vendor_id=vendor_id,
            borrower_id=borrower_id,
        )

    def clear_context(self) -> None:
        """Clear RLS context (useful for admin operations)."""
        self.db.execute(text("SET LOCAL app.current_user_id = NULL"))
        self.db.execute(text("SET LOCAL app.vendor_id = NULL"))
        self.db.execute(text("SET LOCAL app.borrower_id = NULL"))

    def get_context(self) -> Dict[str, Any]:
        """
        Get current RLS context from database.

        Useful for debugging and testing.
        """
        result = self.db.execute(text("SELECT current_setting('app.current_user_id', true)"))
        user_id = result.scalar()

        result = self.db.execute(text("SELECT current_setting('app.current_user_role', true)"))
        user_role = result.scalar()

        result = self.db.execute(text("SELECT current_setting('app.vendor_id', true)"))
        vendor_id = result.scalar()

        result = self.db.execute(text("SELECT current_setting('app.borrower_id', true)"))
        borrower_id = result.scalar()

        return {
            "user_id": user_id,
            "user_role": user_role,
            "vendor_id": vendor_id,
            "borrower_id": borrower_id,
        }


# Auto-set RLS context on connection
@event.listens_for(Session, 'before_flush')
def set_rls_before_flush(session, context, instances):
    """
    Automatically set RLS context before database flush.

    This ensures RLS policies are applied during ORM operations.
    """
    from app.core.security import get_request_user_context

    try:
        user_context = get_request_user_context()
        if user_context:
            set_rls_context(
                session,
                user_id=user_context['user_id'],
                user_role=user_context['role'],
                vendor_id=user_context.get('vendor_id'),
                borrower_id=user_context.get('borrower_id'),
            )
    except Exception:
        # No request context (e.g., migration script)
        pass


# ============================================================================
# FastAPI Dependency for RLS
# ============================================================================

async def get_rls_middleware(
    db: Session = Depends(get_db),
) -> RLSMiddleware:
    """
    FastAPI dependency to provide RLS middleware for a request.

    The user context is automatically set by get_current_user_from_token
    dependency, so this just provides the RLSMiddleware instance for
    explicit context manipulation if needed.

    Usage in endpoints:
        @app.get("/applications")
        def list_applications(
            current_user: User = Depends(get_current_user_from_token),
            rls: RLSMiddleware = Depends(get_rls_middleware),
            db: Session = Depends(get_db),
        ):
            # RLS context is already set by get_current_user_from_token
            # You can explicitly override if needed:
            # rls.set_context(user_id=..., user_role=..., vendor_id=...)

            return db.query(LoanApplication).all()  # RLS filtered automatically
    """
    # User context is already set by get_current_user_from_token
    # We just return the middleware instance for potential manual overrides
    return RLSMiddleware(db)
