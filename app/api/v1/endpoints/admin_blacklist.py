"""Blacklist admin CRUD (WS-I) — Turnkey Tools → Blacklists parity.

Admin-only (the list is sensitive: it names suspected-fraud values). Entries
are soft-deleted only (``active`` flip) so the history stays auditable; every
change also lands in ``platform_events``.

The SCREEN runs automatically at decision time (flow_orchestrator) — a match
flags to manual review, never auto-declines (Dave's rule; pinned in
``blacklists.apply_screen``).
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_roles
from app.db.base import get_db
from app.models.platform.blacklist import PlatformBlacklistEntry
from app.services import blacklists

router = APIRouter(dependencies=[Depends(require_roles("admin"))])


class BlacklistEntryRow(BaseModel):
    id: UUID
    category: str
    value: str
    reason: str
    active: bool
    created_by: str
    created_at: datetime
    deactivated_by: Optional[str] = None
    deactivated_at: Optional[datetime] = None


class AddEntryBody(BaseModel):
    category: Literal["name", "sin", "phone", "email", "drivers_license", "account_number"]
    value: str = Field(..., min_length=1, max_length=200)
    # MANDATORY: why this value is suspicious (shown in the list, audited).
    reason: str = Field(..., min_length=3, max_length=500)


def _actor_id(user) -> str:
    return str(getattr(user, "id", "") or "unknown")


@router.get("", response_model=list[BlacklistEntryRow])
def list_entries(
    category: Optional[str] = Query(None),
    include_inactive: bool = Query(False),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """The blacklist, newest first. Deactivated rows only with
    ``include_inactive=true`` (they are history, not policy)."""
    q = db.query(PlatformBlacklistEntry)
    if category:
        if category not in blacklists.CATEGORIES:
            raise HTTPException(
                status_code=422,
                detail=f"unknown category {category!r} "
                f"(expected one of {blacklists.CATEGORIES})",
            )
        q = q.filter(PlatformBlacklistEntry.category == category)
    if not include_inactive:
        q = q.filter(PlatformBlacklistEntry.active.is_(True))
    return q.order_by(PlatformBlacklistEntry.created_at.desc()).limit(limit).all()


@router.post("", response_model=BlacklistEntryRow)
def add_entry(
    body: AddEntryBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Add an entry (idempotent on an active duplicate). Reason mandatory;
    audited."""
    try:
        row = blacklists.add_entry(
            db,
            category=body.category,
            value=body.value,
            reason=body.reason,
            actor=_actor_id(user),
        )
    except blacklists.BlacklistError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    db.commit()
    db.refresh(row)
    return row


@router.delete("/{entry_id}", response_model=BlacklistEntryRow)
def deactivate_entry(
    entry_id: UUID,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Remove an entry from the active list (SOFT delete — the row and its
    audit history remain; idempotent). Audited."""
    row = blacklists.deactivate_entry(db, entry_id, actor=_actor_id(user))
    if row is None:
        raise HTTPException(status_code=404, detail="Blacklist entry not found")
    db.commit()
    db.refresh(row)
    return row
