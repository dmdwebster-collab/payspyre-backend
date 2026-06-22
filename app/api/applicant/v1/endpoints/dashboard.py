"""Borrower-facing in-app ("dashboard") notification read API.

The notification processor's dashboard channel writes a ``dashboard_notification``
``platform_events`` row carrying the rendered ``subject``/``body`` plus
``notification_type`` / ``loan_id`` / ``source_event_id`` and the borrower's
``patient_id`` (see ``NotificationProcessor._record_dashboard``). This module is
the READ side the borrower portal calls.

``platform_events`` is append-only / immutable, so we never mutate the original
notification to mark it read. Instead, marking-read emits a *new*
``dashboard_notification_read`` event whose payload references the read
notification's event id. The list/count endpoints derive each notification's
``read`` flag from the existence of such a receipt event — which makes
re-marking naturally idempotent.

Every endpoint scopes strictly to ``claims.patient_id``: dashboard events carry
``patient_id`` directly on the event row, so we filter on it and never leak
another borrower's notifications.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.applicant.v1.deps import ApplicantClaims, get_current_applicant
from app.core.logging import get_logger
from app.db.base import get_db
from app.models.platform.event import PlatformEvent

logger = get_logger(__name__)
router = APIRouter(prefix="/notifications", tags=["applicant-notifications"])

DASHBOARD_EVENT_TYPE = "dashboard_notification"
DASHBOARD_READ_EVENT_TYPE = "dashboard_notification_read"


# --- response shapes --------------------------------------------------------


class NotificationOut(BaseModel):
    id: int
    subject: Optional[str] = None
    body: Optional[str] = None
    notification_type: Optional[str] = None
    loan_id: Optional[str] = None
    created_at: Optional[str] = None
    read: bool


class UnreadCountOut(BaseModel):
    unread_count: int


class MarkReadOut(BaseModel):
    id: int
    read: bool


# --- helpers ----------------------------------------------------------------

# A dashboard_notification is "read" iff a dashboard_notification_read receipt
# event exists referencing its id in payload->>'read_event_id'. Expressed as a
# correlated NOT EXISTS so we can both filter (unread_only) and project the flag.
_READ_EXISTS_SQL = """
    EXISTS (
        SELECT 1 FROM platform_events r
        WHERE r.event_type = :read_type
          AND r.payload->>'read_event_id' = e.id::text
    )
"""


# --- endpoints --------------------------------------------------------------


@router.get("", response_model=list[NotificationOut])
def list_notifications(
    unread_only: bool = Query(False),
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
):
    """This patient's dashboard notifications, newest first.

    ``read`` is derived from the existence of a ``dashboard_notification_read``
    receipt for each event id. ``?unread_only=true`` filters to those without a
    receipt.
    """
    sql = f"""
        SELECT
            e.id AS id,
            e.occurred_at AS occurred_at,
            e.payload->>'subject' AS subject,
            e.payload->>'body' AS body,
            e.payload->>'notification_type' AS notification_type,
            e.payload->>'loan_id' AS loan_id,
            {_READ_EXISTS_SQL} AS read
        FROM platform_events e
        WHERE e.event_type = :etype
          AND e.patient_id = :pid
        {{unread_filter}}
        ORDER BY e.id DESC
    """
    unread_filter = f"AND NOT {_READ_EXISTS_SQL}" if unread_only else ""
    rows = (
        db.execute(
            text(sql.format(unread_filter=unread_filter)),
            {
                "etype": DASHBOARD_EVENT_TYPE,
                "pid": str(claims.patient_id),
                "read_type": DASHBOARD_READ_EVENT_TYPE,
            },
        )
        .mappings()
        .all()
    )
    return [
        NotificationOut(
            id=row["id"],
            subject=row["subject"],
            body=row["body"],
            notification_type=row["notification_type"],
            loan_id=row["loan_id"],
            created_at=row["occurred_at"].isoformat() if row["occurred_at"] else None,
            read=bool(row["read"]),
        )
        for row in rows
    ]


@router.get("/unread-count", response_model=UnreadCountOut)
def unread_count(
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
):
    """Count of this patient's dashboard notifications with no read receipt."""
    sql = f"""
        SELECT COUNT(*) AS n
        FROM platform_events e
        WHERE e.event_type = :etype
          AND e.patient_id = :pid
          AND NOT {_READ_EXISTS_SQL}
    """
    n = db.execute(
        text(sql),
        {
            "etype": DASHBOARD_EVENT_TYPE,
            "pid": str(claims.patient_id),
            "read_type": DASHBOARD_READ_EVENT_TYPE,
        },
    ).scalar_one()
    return UnreadCountOut(unread_count=int(n or 0))


@router.post("/{event_id}/read", response_model=MarkReadOut)
def mark_read(
    event_id: int,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
):
    """Mark one dashboard notification read.

    platform_events is append-only, so we DON'T mutate the original event —
    we append a ``dashboard_notification_read`` receipt referencing its id.
    Idempotent: if a receipt already exists, this is a no-op. Authorization is
    by ownership — a notification not belonging to ``claims.patient_id`` (or
    that doesn't exist / isn't a dashboard notification) returns 404 so we don't
    leak other borrowers' events.
    """
    notif = (
        db.query(PlatformEvent)
        .filter(PlatformEvent.id == event_id)
        .filter(PlatformEvent.event_type == DASHBOARD_EVENT_TYPE)
        .filter(PlatformEvent.patient_id == claims.patient_id)
        .first()
    )
    if notif is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found"
        )

    already = db.execute(
        text(
            """
            SELECT 1 FROM platform_events
            WHERE event_type = :read_type
              AND payload->>'read_event_id' = :rid
            LIMIT 1
            """
        ),
        {"read_type": DASHBOARD_READ_EVENT_TYPE, "rid": str(event_id)},
    ).first()
    if already is None:
        db.add(
            PlatformEvent(
                event_type=DASHBOARD_READ_EVENT_TYPE,
                actor="patient",
                patient_id=claims.patient_id,
                application_id=notif.application_id,
                payload={
                    "v": 1,
                    "actor": {"type": "patient", "id": str(claims.patient_id)},
                    "read_event_id": event_id,
                    "patient_id": str(claims.patient_id),
                },
            )
        )
        db.commit()

    return MarkReadOut(id=event_id, read=True)
