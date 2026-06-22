"""Admin-side review of vendor profile change requests (spec §3.6).

Vendors submit profile change requests via the clinic API
(``POST /api/clinic/v1/account/profile/change-requests``) which persist a
PENDING ``PlatformVendorProfileChangeRequest`` and NEVER mutate the ``vendors``
row. THIS module is the admin counterpart: an admin lists the queue and
approves/rejects requests. Approval is the ONLY path that writes the requested
changes onto the ``vendors`` row.

All routes require the ``admin`` role (``require_roles("admin")``). Mounted at
``/api/v1/admin/vendor-profile-change-requests`` (see ``app/api/v1/api.py``).

Security:
- The whitelist (``WHITELIST_FIELDS``, imported from the clinic account endpoint
  so there is ONE source of truth) is RE-ENFORCED at approval time. Even if a
  non-whitelisted key somehow reached ``requested_changes``, it is skipped when
  applying to the vendor row — admin-only fields (status, license, compliance,
  business identity) can never be changed through this flow.
- Approve/reject act only on a ``pending`` request; a request already decided
  returns 409 (idempotency / no double-apply).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.clinic.v1.endpoints.account import WHITELIST_FIELDS
from app.core.auth import get_current_user, require_roles
from app.db.base import get_db
from app.models.loan import Vendor
from app.models.platform.profile_change_request import PlatformVendorProfileChangeRequest

router = APIRouter()


# --- schemas ---------------------------------------------------------------


class AdminChangeRequest(BaseModel):
    id: UUID
    vendor_id: UUID
    requested_changes: dict[str, Optional[str]]
    status: str
    note: Optional[str] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    review_note: Optional[str] = None
    created_at: datetime


class ReviewDecisionBody(BaseModel):
    """Optional admin decision note attached to an approve/reject."""

    review_note: Optional[str] = None


# --- shaping ---------------------------------------------------------------


def to_admin_change_request(row: PlatformVendorProfileChangeRequest) -> AdminChangeRequest:
    return AdminChangeRequest(
        id=row.id,
        vendor_id=row.vendor_id,
        requested_changes=row.requested_changes or {},
        status=row.status,
        note=row.note,
        reviewed_by=row.reviewed_by,
        reviewed_at=row.reviewed_at,
        review_note=row.review_note,
        created_at=row.created_at,
    )


def _get_pending_or_raise(
    db: Session, request_id: UUID
) -> PlatformVendorProfileChangeRequest:
    row = (
        db.query(PlatformVendorProfileChangeRequest)
        .filter(PlatformVendorProfileChangeRequest.id == request_id)
        .first()
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Change request not found"
        )
    if row.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Change request is already {row.status}",
        )
    return row


def _reviewer_id(user) -> str:
    return str(getattr(user, "id", "") or getattr(user, "email", "") or "unknown")


# --- routes ----------------------------------------------------------------


@router.get(
    "",
    response_model=list[AdminChangeRequest],
    dependencies=[Depends(require_roles("admin"))],
)
def list_change_requests(
    status_filter: Optional[str] = Query(None, alias="status"),
    vendor_id: Optional[UUID] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Admin queue of vendor profile change requests, newest first.

    Optional filters: ``status`` (pending|approved|rejected) and ``vendor_id``.
    Admin-scoped — returns requests across ALL vendors (no vendor scoping).
    """
    q = db.query(PlatformVendorProfileChangeRequest)
    if status_filter is not None:
        q = q.filter(PlatformVendorProfileChangeRequest.status == status_filter)
    if vendor_id is not None:
        q = q.filter(PlatformVendorProfileChangeRequest.vendor_id == vendor_id)
    rows = (
        q.order_by(PlatformVendorProfileChangeRequest.created_at.desc())
        .limit(limit)
        .all()
    )
    return [to_admin_change_request(r) for r in rows]


@router.get(
    "/{request_id}",
    response_model=AdminChangeRequest,
    dependencies=[Depends(require_roles("admin"))],
)
def get_change_request(
    request_id: UUID,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    row = (
        db.query(PlatformVendorProfileChangeRequest)
        .filter(PlatformVendorProfileChangeRequest.id == request_id)
        .first()
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Change request not found"
        )
    return to_admin_change_request(row)


@router.post(
    "/{request_id}/approve",
    response_model=AdminChangeRequest,
    dependencies=[Depends(require_roles("admin"))],
)
def approve_change_request(
    request_id: UUID,
    body: ReviewDecisionBody | None = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Approve a pending request and APPLY its whitelisted changes to the vendor.

    This is the only path that writes vendor-requested changes onto ``vendors``.
    The whitelist is re-enforced here: any non-whitelisted key in
    ``requested_changes`` is skipped (defense in depth).
    """
    row = _get_pending_or_raise(db, request_id)

    vendor = db.query(Vendor).filter(Vendor.id == row.vendor_id).first()
    if vendor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Vendor not found"
        )

    # Apply ONLY whitelisted keys — admin-only fields can never be set this way.
    for key, value in (row.requested_changes or {}).items():
        if key in WHITELIST_FIELDS:
            setattr(vendor, key, value)

    row.status = "approved"
    row.reviewed_by = _reviewer_id(user)
    row.reviewed_at = datetime.now(timezone.utc)
    if body is not None:
        row.review_note = body.review_note

    db.commit()
    db.refresh(row)
    return to_admin_change_request(row)


@router.post(
    "/{request_id}/reject",
    response_model=AdminChangeRequest,
    dependencies=[Depends(require_roles("admin"))],
)
def reject_change_request(
    request_id: UUID,
    body: ReviewDecisionBody | None = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Reject a pending request. Does NOT mutate the ``vendors`` row."""
    row = _get_pending_or_raise(db, request_id)

    row.status = "rejected"
    row.reviewed_by = _reviewer_id(user)
    row.reviewed_at = datetime.now(timezone.utc)
    if body is not None:
        row.review_note = body.review_note

    db.commit()
    db.refresh(row)
    return to_admin_change_request(row)
