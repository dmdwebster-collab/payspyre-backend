"""Hardship v1 endpoints (WS-J) — /api/v1/admin/loans/{loan_id}/hardship.

PERMISSION MODEL (Dave: hardship is "controlled by user permissions" — the tab
has "user-defined availability so that junior staff members can't write off
accounts or do adjustment of terms"): every route here requires the dedicated
``hardship``/``create`` Role→Permission grant. ``admin`` is implicitly allowed;
a plain ``staff`` role is NOT (unlike the rest of the /admin/loans surface).
The non-prod dev force-sign additionally requires ``hardship``/``apply`` and
lives in admin_dev_tools (never mounted in production).

Flow surface:
    POST   …/hardship                          create draft (returns the preview)
    GET    …/hardship                          list requests for the loan
    GET    …/hardship/{request_id}             detail (request + preview + snap_back)
    POST   …/hardship/{request_id}/send-for-signature
    POST   …/hardship/{request_id}/cancel      staff withdraw (mandatory comment)

THE HARD RULE: no schedule mutation before the borrower signs — apply happens
only via the SignNow webhook (or the non-prod dev force-sign). There is no
staff "apply" endpoint, deliberately.
"""
from __future__ import annotations

from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status as http_status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_any_permission_or_admin
from app.db.base import get_db
from app.models.platform.hardship import PlatformHardshipRequest
from app.models.platform.loan import PlatformLoan
from app.services import hardship
from app.services.hardship import HardshipError

# WS-F granular permissions: the new hardship/manage umbrella grant (seeded in
# migration 060) OR the legacy hardship/create grant (kept valid so existing
# role assignments keep working), OR the implicit admin allowance.
router = APIRouter(
    dependencies=[
        Depends(
            require_any_permission_or_admin(
                ("hardship", "manage"), ("hardship", "create")
            )
        )
    ]
)


def _get_loan(db: Session, loan_id: UUID) -> PlatformLoan:
    loan = db.query(PlatformLoan).filter(PlatformLoan.id == loan_id).first()
    if loan is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="Loan not found")
    return loan


def _get_request(db: Session, loan_id: UUID, request_id: UUID) -> PlatformHardshipRequest:
    request = (
        db.query(PlatformHardshipRequest)
        .filter(
            PlatformHardshipRequest.id == request_id,
            PlatformHardshipRequest.loan_id == loan_id,
        )
        .first()
    )
    if request is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Hardship request not found"
        )
    return request


def _actor_id(user) -> str:
    return str(getattr(user, "id", "") or "unknown")


def _serialize(request: PlatformHardshipRequest) -> dict:
    return {
        "id": str(request.id),
        "loan_id": str(request.loan_id),
        "kind": request.kind,
        "status": request.status,
        "params": request.params,
        "reason": request.reason,
        "comment": request.comment,
        "preview": request.preview,
        "esign_document_ref": request.esign_document_ref,
        "signature_requested_at": (
            request.signature_requested_at.isoformat()
            if request.signature_requested_at
            else None
        ),
        "signature_expires_at": (
            request.signature_expires_at.isoformat()
            if request.signature_expires_at
            else None
        ),
        "signed_at": request.signed_at.isoformat() if request.signed_at else None,
        "applied_at": request.applied_at.isoformat() if request.applied_at else None,
        "completed_at": request.completed_at.isoformat() if request.completed_at else None,
        "snap_back": request.snap_back,
        "created_by": request.created_by,
        "cancelled_by": request.cancelled_by,
        "created_at": request.created_at.isoformat() if request.created_at else None,
    }


class CreateHardshipBody(BaseModel):
    kind: Literal["deferment", "due_date_change"]
    # deferment:       {"installment_ids": [...]}
    # due_date_change: {"new_day_of_month": 15} or {"item_shifts": [...]}
    params: dict
    reason: str = Field(..., min_length=1, max_length=2000)
    comment: str = Field(..., min_length=1, max_length=2000)


class CancelBody(BaseModel):
    comment: str = Field(..., min_length=1, max_length=2000)


@router.post("/{loan_id}/hardship", status_code=http_status.HTTP_201_CREATED)
def create_hardship_request(
    loan_id: UUID,
    body: CreateHardshipBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Create a DRAFT hardship request. The response carries the exact
    schedule-effect preview (incl. the interest-keeps-accruing disclosure and
    integer-cents estimate). Nothing touches the schedule."""
    loan = _get_loan(db, loan_id)
    try:
        request = hardship.create_request(
            db,
            loan,
            kind=body.kind,
            params=body.params,
            reason=body.reason,
            comment=body.comment,
            actor=_actor_id(user),
        )
    except HardshipError as exc:
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail=str(exc))
    db.commit()
    return _serialize(request)


@router.get("/{loan_id}/hardship")
def list_hardship_requests(
    loan_id: UUID,
    status_filter: Optional[str] = None,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    _get_loan(db, loan_id)
    q = db.query(PlatformHardshipRequest).filter(
        PlatformHardshipRequest.loan_id == loan_id
    )
    if status_filter:
        q = q.filter(PlatformHardshipRequest.status == status_filter)
    rows = q.order_by(PlatformHardshipRequest.created_at.desc()).all()
    return [_serialize(r) for r in rows]


@router.get("/{loan_id}/hardship/{request_id}")
def get_hardship_request(
    loan_id: UUID,
    request_id: UUID,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    return _serialize(_get_request(db, loan_id, request_id))


@router.post("/{loan_id}/hardship/{request_id}/send-for-signature")
def send_hardship_for_signature(
    loan_id: UUID,
    request_id: UUID,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Send the amendment to the borrower for e-signature (SignNow). In
    simulation mode (no adapter/template configured) the request still moves to
    ``awaiting_signature`` and the non-prod dev force-sign substitutes."""
    loan = _get_loan(db, loan_id)
    request = _get_request(db, loan_id, request_id)
    try:
        hardship.send_for_signature(db, request, loan, actor=_actor_id(user))
    except HardshipError as exc:
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail=str(exc))
    db.commit()
    return _serialize(request)


@router.post("/{loan_id}/hardship/{request_id}/cancel")
def cancel_hardship_request(
    loan_id: UUID,
    request_id: UUID,
    body: CancelBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Staff withdraw (draft / awaiting_signature only). Mandatory comment."""
    _get_loan(db, loan_id)
    request = _get_request(db, loan_id, request_id)
    try:
        hardship.cancel(db, request, actor=_actor_id(user), comment=body.comment)
    except HardshipError as exc:
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail=str(exc))
    db.commit()
    return _serialize(request)
