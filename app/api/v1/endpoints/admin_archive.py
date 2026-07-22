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
            "Filter to one archive status (Dave's 'All statuses' chip). The "
            "vocabulary is derived from the status registry — see "
            "``close_reasons`` / ``close_reason_options`` in the response."
        ),
    ),
    assignee_id: Optional[str] = Query(
        None,
        description=(
            "Dave's 'All assignations' chip: a user id to show only that "
            "assignee's closed files, or the literal 'unassigned'. Omit for all."
        ),
    ),
    sort: str = Query("closed_at", description="closed_at | id"),
    order: str = Query("desc", description="asc | desc"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """The archive queue: every terminal application + loan, server-paginated,
    with its close reason (the archive 'S' column), assignee ('A' column) and
    an exact ``total`` for the "N record(s) · page X of Y" footer."""
    resolved_assignee: Optional[object] = None
    if assignee_id is not None:
        if assignee_id == archive.UNASSIGNED:
            resolved_assignee = archive.UNASSIGNED
        else:
            try:
                resolved_assignee = UUID(assignee_id)
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "assignee_id must be a user UUID or the literal "
                        f"'{archive.UNASSIGNED}'"
                    ),
                )
    try:
        page = archive.list_archive(
            db,
            close_reason=close_reason,
            assignee_id=resolved_assignee,
            sort=sort,
            order=order,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "close_reasons": list(archive.CLOSE_REASONS),
        "close_reason_options": [dict(o) for o in archive.CLOSE_REASON_OPTIONS],
        "sort_fields": list(archive.SORT_FIELDS),
        **page,
    }


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
