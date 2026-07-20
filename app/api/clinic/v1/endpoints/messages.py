"""Clinic-facing application messaging (vendor side of the vendor⇄PaySpyre chat).

Staff-authenticated (platform JWT), SCOPED to the caller's clinic: every route
resolves the application through ``application_messages.get_application`` with
the caller's ``vendor_id``, so a clinic can only read/post on threads for its
own applications (a foreign application id returns 404, never leaks).

Messages posted here are ``sender_kind='vendor'``. The admin side lives at
``/api/v1/admin/applications/{id}/messages`` and mirrors this file.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.clinic.v1.deps import ClinicPrincipal, get_current_clinic_user
from app.db.base import get_db
from app.models.platform.message import SENDER_KIND_VENDOR
from app.schemas.messaging import (
    MarkReadOut,
    MessageCreate,
    MessageOut,
    ThreadSummary,
    UnreadCountOut,
)
from app.services import application_messages, message_notifications

router = APIRouter(tags=["clinic-messages"])

_VIEWER_SIDE = SENDER_KIND_VENDOR


def _application_or_404(db: Session, application_id: UUID, principal: ClinicPrincipal):
    application = application_messages.get_application(
        db, application_id, vendor_id=principal.vendor_id
    )
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
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
):
    application = _application_or_404(db, application_id, principal)
    return application_messages.list_thread(db, application, principal.user_id)


@router.post(
    "/applications/{application_id}/messages",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
)
def post_message(
    application_id: UUID,
    body: MessageCreate,
    db: Session = Depends(get_db),
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
):
    application = _application_or_404(db, application_id, principal)
    message = application_messages.post_message(
        db,
        application=application,
        sender_kind=SENDER_KIND_VENDOR,
        sender_user=principal.user,
        body=body.body,
    )
    db.commit()
    db.refresh(message)
    # Best-effort email ping to PaySpyre — after commit, never blocks the post.
    message_notifications.notify_new_message(
        db, application=application, message=message, sender_user=principal.user
    )
    name = application_messages.sender_display_name(principal.user, SENDER_KIND_VENDOR)
    return application_messages.to_message_out(message, name, principal.user_id)


@router.post(
    "/applications/{application_id}/messages/read", response_model=MarkReadOut
)
def mark_thread_read(
    application_id: UUID,
    db: Session = Depends(get_db),
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
):
    application = _application_or_404(db, application_id, principal)
    application_messages.mark_read(
        db, application_id=application.id, user_id=principal.user_id
    )
    db.commit()
    return MarkReadOut(application_id=application.id, unread_count=0)


@router.get("/messages/unread-count", response_model=UnreadCountOut)
def unread_count(
    db: Session = Depends(get_db),
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
):
    n = application_messages.unread_count(
        db,
        user_id=principal.user_id,
        viewer_side=_VIEWER_SIDE,
        vendor_id=principal.vendor_id,
    )
    return UnreadCountOut(unread_count=n)


@router.get("/messages/threads", response_model=list[ThreadSummary])
def list_threads(
    db: Session = Depends(get_db),
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
):
    return application_messages.thread_summaries(
        db,
        user_id=principal.user_id,
        viewer_side=_VIEWER_SIDE,
        vendor_id=principal.vendor_id,
    )
