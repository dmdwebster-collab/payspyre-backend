"""Archive workplace endpoints (WS-I) — Turnkey video 06 parity.

Read-only list + frozen polymorphic detail over TERMINAL records (closed
applications AND closed loan accounts), with close-reason filters. See
``app/services/archive.py`` for the vocabulary and the polymorphic-detail
contract (origination-grade for dead applications, servicing-grade + final
ledger for closed loans; both carry the frozen decision snapshot).
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_roles
from app.db.base import get_db
from app.services import archive

router = APIRouter(dependencies=[Depends(require_roles("admin", "staff"))])


@router.get("")
def list_archive(
    close_reason: Optional[str] = Query(
        None,
        description=(
            "Filter to one archive status: "
            "rejected / cancelled / expired / bank_verification_expired / "
            "repaid / written_off"
        ),
    ),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """The archive queue: every terminal application + loan, newest close
    first, with its close reason (the archive 'S' column)."""
    try:
        rows = archive.list_archive(db, close_reason=close_reason, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"close_reasons": list(archive.CLOSE_REASONS), "records": rows}


@router.get("/application/{application_id}")
def application_detail(
    application_id: UUID,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Frozen detail for a terminal application (detail_kind='origination'):
    origination data + the as-of-decision snapshot. 404 for a live (or
    unknown) application — the Archive only serves closed records."""
    detail = archive.application_archive_detail(db, application_id)
    if detail is None:
        raise HTTPException(
            status_code=404, detail="No archived application with this id"
        )
    return detail


@router.get("/loan/{loan_id}")
def loan_detail(
    loan_id: UUID,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Frozen detail for a terminal loan (detail_kind='servicing'): servicing
    roll-up + final immutable-ledger state + the originating application's
    decision snapshot. 404 for a live (or unknown) loan."""
    detail = archive.loan_archive_detail(db, loan_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="No archived loan with this id")
    return detail
