"""Communications-log service (WS-A comms hub) — the ONE place rows are written.

Dave's mandate #4: every email / SMS / dashboard notification is stored with
the exact rendered message body, per account, forever (append-only; migration
055 installs a DB trigger blocking UPDATE/DELETE). Writers:

* the notification dispatchers (real + mock) — email/SMS at send time,
* the notification processor's dashboard lane — in-app cards,
* the admin comms endpoints — staff templated sends + offline contact logs.

``record_communication`` adds + flushes on the CALLER's session and never
commits — the row lands in the same transaction as the ``notification_sent``
audit event it corresponds to, so log and audit can't diverge.

Bodies that carry credentials (magic-link sign-in codes) are stored as
:data:`HIDDEN_BODY` — the on-camera Turnkey behavior ("<Hidden for privacy
purposes>" on the welcome email containing login info).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Sequence
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models.platform.communication import (
    CHANNELS,
    DIRECTION_OUTBOUND,
    PlatformCommunicationLog,
)

# Stored instead of a body that contains credentials (magic-link codes).
HIDDEN_BODY = "<Hidden for privacy purposes>"

# Offline-contact methods (TL "Log message" dialog's Method dropdown).
OFFLINE_CONTACT_METHODS = ("phone", "in_person", "video", "mail", "other")


def record_communication(
    db: Session,
    *,
    channel: str,
    body: str,
    direction: str = DIRECTION_OUTBOUND,
    recipient: Optional[str] = None,
    subject: Optional[str] = None,
    notification_type: Optional[str] = None,
    patient_id: Optional[UUID] = None,
    application_id: Optional[UUID] = None,
    loan_id: Optional[UUID] = None,
    sent_by: str = "system",
    sent_by_user_id: Optional[UUID] = None,
    comment: Optional[str] = None,
    contact_method: Optional[str] = None,
    purpose: Optional[str] = None,
    result: Optional[str] = None,
    status: str = "recorded",
    vendor: Optional[str] = None,
    vendor_message_id: Optional[str] = None,
    source_event_id: Optional[int] = None,
    occurred_at: Optional[datetime] = None,
) -> PlatformCommunicationLog:
    """Append one communications-log row (add + flush; caller commits).

    ``channel`` must be one of :data:`~app.models.platform.communication.CHANNELS`;
    ``body`` is the FULL rendered content (or :data:`HIDDEN_BODY`). ``loan_id``
    tolerates the string UUIDs the notification pipeline carries.
    """
    if channel not in CHANNELS:
        raise ValueError(f"unknown communications channel '{channel}'")
    now = datetime.now(timezone.utc)
    row = PlatformCommunicationLog(
        id=uuid4(),
        channel=channel,
        direction=direction,
        recipient=recipient,
        subject=subject,
        body=body,
        notification_type=notification_type,
        patient_id=patient_id,
        application_id=application_id,
        loan_id=_coerce_uuid(loan_id),
        sent_by=sent_by,
        sent_by_user_id=sent_by_user_id,
        comment=comment,
        contact_method=contact_method,
        purpose=purpose,
        result=result,
        status=status,
        vendor=vendor,
        vendor_message_id=vendor_message_id,
        source_event_id=source_event_id,
        # Explicit timestamps (not just server_default) so a flushed row is
        # complete before commit and unit tests can run without a database.
        occurred_at=occurred_at or now,
        created_at=now,
    )
    db.add(row)
    db.flush()
    return row


def list_communications(
    db: Session,
    *,
    patient_id: Optional[UUID] = None,
    application_id: Optional[UUID] = None,
    loan_id: Optional[UUID] = None,
    channel: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    limit: int = 200,
    offset: int = 0,
) -> Sequence[PlatformCommunicationLog]:
    """Filtered, newest-first read of the log (bodies included)."""
    query = db.query(PlatformCommunicationLog)
    if patient_id is not None:
        query = query.filter(PlatformCommunicationLog.patient_id == patient_id)
    if application_id is not None:
        query = query.filter(PlatformCommunicationLog.application_id == application_id)
    if loan_id is not None:
        query = query.filter(PlatformCommunicationLog.loan_id == loan_id)
    if channel is not None:
        query = query.filter(PlatformCommunicationLog.channel == channel)
    if date_from is not None:
        query = query.filter(PlatformCommunicationLog.occurred_at >= date_from)
    if date_to is not None:
        query = query.filter(PlatformCommunicationLog.occurred_at <= date_to)
    return (
        query.order_by(PlatformCommunicationLog.occurred_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def to_out(row: PlatformCommunicationLog) -> dict:
    """Serialize one row for the admin API (full body, ISO timestamps)."""
    return {
        "id": str(row.id),
        "channel": row.channel,
        "direction": row.direction,
        "recipient": row.recipient,
        "subject": row.subject,
        "body": row.body,
        "notification_type": row.notification_type,
        "patient_id": str(row.patient_id) if row.patient_id else None,
        "application_id": str(row.application_id) if row.application_id else None,
        "loan_id": str(row.loan_id) if row.loan_id else None,
        "sent_by": row.sent_by,
        "sent_by_user_id": str(row.sent_by_user_id) if row.sent_by_user_id else None,
        "comment": row.comment,
        "contact_method": row.contact_method,
        "purpose": row.purpose,
        "result": row.result,
        "status": row.status,
        "vendor": row.vendor,
        "vendor_message_id": row.vendor_message_id,
        "source_event_id": row.source_event_id,
        "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _coerce_uuid(value) -> Optional[UUID]:
    """The notification pipeline carries loan ids as strings; the column is UUID."""
    if value is None or isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None
