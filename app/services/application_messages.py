"""Application message threads — shared logic for the clinic + admin APIs.

The vendor-facing (clinic) and admin (whole-book) routers are thin shells over
these functions; keeping the list/post/read/unread logic here means the two
sides can never drift apart on how a thread is read or how unread is counted.

Read model: "unread" for a viewer is the count of messages authored by the
*other* side that are newer than that viewer's per-(application, user) last-read
watermark (``platform_application_message_reads``). A viewer's own messages
never count as unread.

Scoping is the caller's job: the clinic router passes ``vendor_id`` (so a clinic
only ever touches its own applications' threads); the admin router passes none
(whole-book). ``get_application`` enforces the vendor filter when given one.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.message import (
    SENDER_KIND_ADMIN,
    SENDER_KIND_VENDOR,
    PlatformApplicationMessage,
    PlatformApplicationMessageRead,
)
from app.models.user import User
from app.schemas.messaging import MessageOut, ThreadSummary

# viewer side -> the side whose messages are "incoming" (and thus count as
# unread) for that viewer.
_OTHER_SIDE = {
    SENDER_KIND_VENDOR: SENDER_KIND_ADMIN,
    SENDER_KIND_ADMIN: SENDER_KIND_VENDOR,
}

_PREVIEW_LEN = 140


# --- lookups + display helpers ---------------------------------------------


def get_application(
    db: Session, application_id: UUID, vendor_id: Optional[UUID] = None
) -> Optional[PlatformCreditApplication]:
    """Fetch an application, optionally scoped to one clinic's ``vendor_id``.

    Returns ``None`` when the application doesn't exist OR (when ``vendor_id`` is
    given) belongs to another clinic — so callers surface a 404 either way and
    never leak another clinic's thread.
    """
    q = db.query(PlatformCreditApplication).filter(
        PlatformCreditApplication.id == application_id
    )
    if vendor_id is not None:
        q = q.filter(PlatformCreditApplication.vendor_id == vendor_id)
    return q.first()


def sender_display_name(user: object, sender_kind: str) -> str:
    """Human label for a message author. Falls back to a side-appropriate
    generic when the user has no name (e.g. a seeded system account)."""
    first = getattr(user, "first_name", None) or ""
    last = getattr(user, "last_name", None) or ""
    name = f"{first} {last}".strip()
    if name:
        return name
    return "PaySpyre" if sender_kind == SENDER_KIND_ADMIN else "Clinic staff"


def to_message_out(
    message: PlatformApplicationMessage, sender_name: str, viewer_user_id: UUID
) -> MessageOut:
    return MessageOut(
        id=message.id,
        application_id=message.application_id,
        sender_kind=message.sender_kind,
        sender_name=sender_name,
        body=message.body,
        created_at=message.created_at,
        mine=message.sender_user_id == viewer_user_id,
    )


# --- thread read -----------------------------------------------------------


def list_thread(
    db: Session,
    application: PlatformCreditApplication,
    viewer_user_id: UUID,
) -> list[MessageOut]:
    """All messages on one application's thread, oldest first, with author names."""
    rows = (
        db.query(PlatformApplicationMessage, User)
        .join(User, User.id == PlatformApplicationMessage.sender_user_id)
        .filter(PlatformApplicationMessage.application_id == application.id)
        .order_by(PlatformApplicationMessage.created_at.asc())
        .all()
    )
    return [
        to_message_out(msg, sender_display_name(user, msg.sender_kind), viewer_user_id)
        for msg, user in rows
    ]


# --- thread write ----------------------------------------------------------


def post_message(
    db: Session,
    *,
    application: PlatformCreditApplication,
    sender_kind: str,
    sender_user: User,
    body: str,
) -> PlatformApplicationMessage:
    """Append a message to a thread and emit an audit event. Flushes (so the
    row gets its id/created_at); the CALLER commits (and then fires the
    best-effort email ping post-commit)."""
    if sender_kind not in _OTHER_SIDE:
        raise ValueError(f"invalid sender_kind: {sender_kind!r}")

    message = PlatformApplicationMessage(
        application_id=application.id,
        sender_kind=sender_kind,
        sender_user_id=sender_user.id,
        body=body,
    )
    db.add(message)
    db.flush()  # assign id + created_at before we reference them

    _emit_message_event(db, application=application, message=message)
    return message


def _emit_message_event(
    db: Session,
    *,
    application: PlatformCreditApplication,
    message: PlatformApplicationMessage,
) -> None:
    """Append a ``application_message_posted`` row to the platform event log
    (audit trail + future notification-processor hook). In-app unread does NOT
    depend on this — it is derived from the messages table directly."""
    from app.models.platform.event import PlatformEvent

    preview = (message.body or "")[:_PREVIEW_LEN]
    db.add(
        PlatformEvent(
            event_type="application_message_posted",
            actor=message.sender_kind,
            patient_id=application.patient_id,
            application_id=application.id,
            payload={
                "v": 1,
                "actor": {"type": message.sender_kind, "id": str(message.sender_user_id)},
                "message_id": str(message.id),
                "sender_kind": message.sender_kind,
                "sender_user_id": str(message.sender_user_id),
                "preview": preview,
            },
        )
    )
    db.flush()


def mark_read(db: Session, *, application_id: UUID, user_id: UUID) -> None:
    """Upsert this user's last-read watermark for the thread to "now". Flushes;
    the caller commits."""
    now = datetime.now(timezone.utc)
    receipt = (
        db.query(PlatformApplicationMessageRead)
        .filter(
            PlatformApplicationMessageRead.application_id == application_id,
            PlatformApplicationMessageRead.user_id == user_id,
        )
        .first()
    )
    if receipt is None:
        db.add(
            PlatformApplicationMessageRead(
                application_id=application_id, user_id=user_id, last_read_at=now
            )
        )
    else:
        receipt.last_read_at = now
    db.flush()


# --- unread + thread summaries (badge + channel list) ----------------------


def unread_count(
    db: Session,
    *,
    user_id: UUID,
    viewer_side: str,
    vendor_id: Optional[UUID] = None,
) -> int:
    """Count of incoming (other-side) messages newer than the viewer's watermark,
    across all threads the viewer can see. Clinic passes ``vendor_id`` to scope
    to its own applications; admin passes none (whole-book)."""
    other_side = _OTHER_SIDE[viewer_side]
    sql = """
        SELECT count(*)
        FROM platform_application_messages m
        JOIN platform_credit_applications a ON a.id = m.application_id
        LEFT JOIN platform_application_message_reads r
          ON r.application_id = m.application_id AND r.user_id = :uid
        WHERE m.sender_kind = :other
          AND (r.last_read_at IS NULL OR m.created_at > r.last_read_at)
    """
    params: dict = {"uid": str(user_id), "other": other_side}
    if vendor_id is not None:
        sql += " AND a.vendor_id = :vendor_id"
        params["vendor_id"] = str(vendor_id)
    n = db.execute(text(sql), params).scalar_one()
    return int(n or 0)


def thread_summaries(
    db: Session,
    *,
    user_id: UUID,
    viewer_side: str,
    vendor_id: Optional[UUID] = None,
    limit: int = 200,
) -> list[ThreadSummary]:
    """The viewer's "channel list": one row per application that has any
    messages, most-recently-active first, each with its unread count + a preview
    of the latest message."""
    other_side = _OTHER_SIDE[viewer_side]
    where_vendor = "WHERE a.vendor_id = :vendor_id" if vendor_id is not None else ""
    sql = f"""
        SELECT m.application_id AS application_id,
               p.legal_first_name AS first_name,
               p.legal_last_name AS last_name,
               count(*) AS message_count,
               max(m.created_at) AS last_message_at,
               sum(CASE WHEN m.sender_kind = :other
                         AND (r.last_read_at IS NULL OR m.created_at > r.last_read_at)
                        THEN 1 ELSE 0 END) AS unread_count,
               (SELECT m2.body FROM platform_application_messages m2
                 WHERE m2.application_id = m.application_id
                 ORDER BY m2.created_at DESC LIMIT 1) AS last_message_preview
        FROM platform_application_messages m
        JOIN platform_credit_applications a ON a.id = m.application_id
        LEFT JOIN platform_patients p ON p.id = a.patient_id
        LEFT JOIN platform_application_message_reads r
          ON r.application_id = m.application_id AND r.user_id = :uid
        {where_vendor}
        GROUP BY m.application_id, p.legal_first_name, p.legal_last_name
        ORDER BY last_message_at DESC
        LIMIT :lim
    """
    params: dict = {"uid": str(user_id), "other": other_side, "lim": limit}
    if vendor_id is not None:
        params["vendor_id"] = str(vendor_id)
    rows = db.execute(text(sql), params).mappings().all()

    summaries = []
    for row in rows:
        name = " ".join(
            p for p in (row["first_name"], row["last_name"]) if p
        ).strip() or "Unknown patient"
        preview = row["last_message_preview"]
        if preview and len(preview) > _PREVIEW_LEN:
            preview = preview[:_PREVIEW_LEN]
        summaries.append(
            ThreadSummary(
                application_id=row["application_id"],
                patient_name=name,
                message_count=int(row["message_count"] or 0),
                unread_count=int(row["unread_count"] or 0),
                last_message_at=row["last_message_at"],
                last_message_preview=preview,
            )
        )
    return summaries
