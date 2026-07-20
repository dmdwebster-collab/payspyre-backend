"""Admin-facing application messaging (PaySpyre side of the vendor⇄PaySpyre chat).

Whole-book (no vendor scoping) + RBAC-gated (``admin``/``staff``) — the PaySpyre
ops console. Mirrors ``app/api/clinic/v1/endpoints/messages.py``; messages
posted here are ``sender_kind='admin'``. ``require_roles`` doubles as the
current-user dependency, so each route receives the acting admin user.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.auth import require_roles
from app.db.base import get_db
from app.models.platform.message import SENDER_KIND_ADMIN
from app.models.user import User
from app.schemas.messaging import (
    MarkReadOut,
    MessageCreate,
    MessageOut,
    ThreadSummary,
    UnreadCountOut,
)
from app.services import application_messages, message_notifications

router = APIRouter(tags=["admin-messages"])

_VIEWER_SIDE = SENDER_KIND_ADMIN


def _application_or_404(db: Session, application_id: UUID):
    application = application_messages.get_application(db, application_id)
    if application is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Application not found"
        )
    return application


@router.get(
    "/applications/{application_id}/messages", response_model=list[MessageOut]
)
def list_messages(
    application_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "staff")),
):
    application = _application_or_404(db, application_id)
    return application_messages.list_thread(db, application, user.id)


@router.post(
    "/applications/{application_id}/messages",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
)
def post_message(
    application_id: UUID,
    body: MessageCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "staff")),
):
    application = _application_or_404(db, application_id)
    message = application_messages.post_message(
        db,
        application=application,
        sender_kind=SENDER_KIND_ADMIN,
        sender_user=user,
        body=body.body,
    )
    db.commit()
    db.refresh(message)
    # Best-effort email ping to the clinic — after commit, never blocks the post.
    message_notifications.notify_new_message(
        db, application=application, message=message, sender_user=user
    )
    name = application_messages.sender_display_name(user, SENDER_KIND_ADMIN)
    return application_messages.to_message_out(message, name, user.id)


@router.post(
    "/applications/{application_id}/messages/read", response_model=MarkReadOut
)
def mark_thread_read(
    application_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "staff")),
):
    application = _application_or_404(db, application_id)
    application_messages.mark_read(db, application_id=application.id, user_id=user.id)
    db.commit()
    return MarkReadOut(application_id=application.id, unread_count=0)


@router.get("/messages/unread-count", response_model=UnreadCountOut)
def unread_count(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "staff")),
):
    n = application_messages.unread_count(
        db, user_id=user.id, viewer_side=_VIEWER_SIDE
    )
    return UnreadCountOut(unread_count=n)


@router.get("/messages/threads", response_model=list[ThreadSummary])
def list_threads(
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "staff")),
):
    return application_messages.thread_summaries(
        db, user_id=user.id, viewer_side=_VIEWER_SIDE
    )
