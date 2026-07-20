"""Admin audit-trail search over the append-only PlatformEvent WORM log (M6).

Lender/admin portal Phase 1. The system-of-record for "who did what" — read-only
search by event type / actor / entity / time. Admin-only (tighter than the rest
of the read cockpit, since it can expose cross-entity activity).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from fastapi import HTTPException

from app.core.auth import get_current_user, require_roles
from app.db.base import get_db
from app.models.platform.event import PlatformEvent
from app.services import audit_diffs

router = APIRouter(dependencies=[Depends(require_roles("admin"))])


class AuditEventRow(BaseModel):
    id: int
    occurred_at: datetime
    event_type: str
    actor: str
    application_id: Optional[UUID] = None
    patient_id: Optional[UUID] = None
    correlation_id: Optional[UUID] = None
    payload: dict


@router.get("", response_model=list[AuditEventRow])
def search_audit(
    event_type: Optional[str] = Query(None),
    actor: Optional[str] = Query(None),
    application_id: Optional[UUID] = Query(None),
    patient_id: Optional[UUID] = Query(None),
    since: Optional[datetime] = Query(None, alias="from"),
    until: Optional[datetime] = Query(None, alias="to"),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Newest-first audit search. All filters optional and AND-combined."""
    q = db.query(PlatformEvent)
    if event_type:
        q = q.filter(PlatformEvent.event_type == event_type)
    if actor:
        q = q.filter(PlatformEvent.actor == actor)
    if application_id is not None:
        q = q.filter(PlatformEvent.application_id == application_id)
    if patient_id is not None:
        q = q.filter(PlatformEvent.patient_id == patient_id)
    if since is not None:
        q = q.filter(PlatformEvent.occurred_at >= since)
    if until is not None:
        q = q.filter(PlatformEvent.occurred_at <= until)
    rows = q.order_by(PlatformEvent.occurred_at.desc()).limit(limit).all()
    return [
        AuditEventRow(
            id=r.id,
            occurred_at=r.occurred_at,
            event_type=r.event_type,
            actor=r.actor,
            application_id=r.application_id,
            patient_id=r.patient_id,
            correlation_id=r.correlation_id,
            payload=r.payload or {},
        )
        for r in rows
    ]


@router.get("/diffs")
def record_diffs(
    application_id: Optional[UUID] = Query(None),
    loan_id: Optional[UUID] = Query(None),
    event_type: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """WS-I: a record's platform_events rendered as HUMAN-READABLE old→new
    field diffs ("status: under_review → approved") instead of raw payloads —
    the audit tab as a change history.

    Scope by ``application_id`` (the event's own column) and/or ``loan_id``
    (matched against the payload's ``loan_id``, the ledger/servicing event
    convention). At least one of the two is required. Newest first.
    """
    if application_id is None and loan_id is None:
        raise HTTPException(
            status_code=422, detail="application_id or loan_id is required"
        )
    q = db.query(PlatformEvent)
    if application_id is not None:
        q = q.filter(PlatformEvent.application_id == application_id)
    if loan_id is not None:
        q = q.filter(PlatformEvent.payload["loan_id"].astext == str(loan_id))
    if event_type:
        q = q.filter(PlatformEvent.event_type == event_type)
    rows = q.order_by(PlatformEvent.id.desc()).limit(limit).all()
    return {
        "application_id": str(application_id) if application_id else None,
        "loan_id": str(loan_id) if loan_id else None,
        "events": [
            audit_diffs.render_event_diff(
                r.id, r.occurred_at, r.event_type, r.actor, r.payload
            )
            for r in rows
        ],
    }
