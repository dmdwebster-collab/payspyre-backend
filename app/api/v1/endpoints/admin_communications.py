"""Admin communications hub (WS-A) — /api/v1/admin/communications.

Dave's mandate #4 surface: the cockpit view over the append-only
``platform_communications_log`` (every email/SMS/dashboard notification with
its FULL rendered body — legal evidence for collections/court), plus the two
staff write actions from the TL Contacts tab:

* **Send Email / Send SMS** — a templated message rendered from the
  notification registry with a merge context, dispatched through the same
  outbox/dispatcher path as automated notifications (with SendGrid/Twilio
  creds absent the mock dispatcher records it inert — expected until Dave's
  creds land) and logged with full body + acting staff user.
* **+ Log message** — an offline contact (phone call / in-person) with a
  MANDATORY comment.

There are deliberately NO update or delete routes — the table is append-only
at the DB level (migration 055 trigger). RBAC: ``admin``/``staff``, the same
gate as the rest of the cockpit read/ops surface.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status as http_status
from jinja2 import TemplateError
from sqlalchemy.orm import Session

from app.core.auth import require_roles
from app.core.config import settings
from app.db.base import get_db
from app.models.platform.communication import CHANNEL_OFFLINE, PlatformCommunicationLog
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.loan import PlatformLoan
from app.models.platform.patient import PlatformPatient
from app.models.user import User
from app.schemas.communications import (
    CommunicationOut,
    OfflineContactBody,
    TemplatedSendBody,
    TemplatedSendOut,
    TemplateInfo,
    TemplatePreviewBody,
    TemplatePreviewOut,
)
from app.services import communications_log
from app.services.notification_render import (
    NOTIFICATION_TYPES,
    UnknownNotificationType,
    render_email,
    render_sms,
)
from app.services.real_notification_dispatcher import (
    PermanentNotificationError,
    TransientNotificationError,
)

router = APIRouter(tags=["admin-communications"])


def _actor(user: User) -> str:
    return getattr(user, "email", None) or str(user.id)


def _build_dispatcher(db: Session):
    """Same selector as the notification processor: real vendors only when
    USE_REAL_NOTIFICATIONS is on; otherwise the mock records the send inert
    (renders + logs, no vendor call) — expected while SendGrid creds are absent."""
    if settings.USE_REAL_NOTIFICATIONS:
        from app.services.real_notification_dispatcher import RealNotificationDispatcher

        return RealNotificationDispatcher(db)
    from app.services.mock_notification_dispatcher import MockNotificationDispatcher

    return MockNotificationDispatcher(db)


def _validate_anchors(
    db: Session,
    *,
    patient_id: Optional[UUID],
    application_id: Optional[UUID],
    loan_id: Optional[UUID],
) -> None:
    """404 on a dangling anchor (cleaner than a 500 FK violation on flush)."""
    if patient_id is not None and db.get(PlatformPatient, patient_id) is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Patient not found"
        )
    if (
        application_id is not None
        and db.get(PlatformCreditApplication, application_id) is None
    ):
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Application not found"
        )
    if loan_id is not None and db.get(PlatformLoan, loan_id) is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Loan not found"
        )


@router.get("", response_model=list[CommunicationOut])
def list_communications(
    patient_id: Optional[UUID] = Query(None),
    application_id: Optional[UUID] = Query(None),
    loan_id: Optional[UUID] = Query(None),
    channel: Optional[str] = Query(None, pattern="^(email|sms|dashboard|offline)$"),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "staff")),
):
    """The per-account communications record, newest first, FULL bodies."""
    return communications_log.list_communications(
        db,
        patient_id=patient_id,
        application_id=application_id,
        loan_id=loan_id,
        channel=channel,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )


@router.get("/templates", response_model=list[TemplateInfo])
def list_templates(
    user: User = Depends(require_roles("admin", "staff")),
):
    """Registry listing for the cockpit's Use Template dropdown."""
    out = []
    for ntype, spec in sorted(NOTIFICATION_TYPES.items()):
        channels = ["email"] + (["sms"] if spec.sms_template else [])
        out.append(
            TemplateInfo(
                notification_type=ntype,
                channels=channels,
                email_subject_template=spec.email_subject,
            )
        )
    return out


@router.post("/preview", response_model=TemplatePreviewOut)
def preview_template(
    body: TemplatePreviewBody,
    user: User = Depends(require_roles("admin", "staff")),
):
    """Render a template against a merge context WITHOUT sending (the compose
    dialog's Preview link)."""
    subject, rendered = _render(body.notification_type, body.channel, body.context)
    return TemplatePreviewOut(
        notification_type=body.notification_type,
        channel=body.channel,
        subject=subject,
        body=rendered,
    )


@router.post(
    "/send", response_model=TemplatedSendOut, status_code=http_status.HTTP_201_CREATED
)
def send_templated(
    body: TemplatedSendBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "staff")),
):
    """Send a templated email/SMS to a borrower from the cockpit.

    Renders the registry template with the merge context and dispatches through
    the standard notification path (suppression checks + audit event + comms
    log all included). The full rendered body lands in the communications log
    attributed to the acting staff user.
    """
    _validate_anchors(
        db,
        patient_id=body.patient_id,
        application_id=body.application_id,
        loan_id=body.loan_id,
    )
    # Render up-front so template/context errors are a clean 4xx (the
    # dispatcher would re-render identically before its vendor call).
    _render(body.notification_type, body.channel, body.context)

    dispatcher = _build_dispatcher(db)
    try:
        event_id = dispatcher.send_notification(
            patient_id=body.patient_id,
            notification_type=body.notification_type,
            contact_method=body.channel,
            context=body.context,
            application_id=body.application_id,
            loan_id=str(body.loan_id) if body.loan_id else None,
            sent_by=_actor(user),
            sent_by_user_id=user.id,
            comment=body.comment,
        )
    except PermanentNotificationError as exc:
        db.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except TransientNotificationError as exc:
        db.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_502_BAD_GATEWAY,
            detail="Notification vendor unavailable; not sent. Try again.",
        ) from exc
    db.commit()

    row = (
        db.query(PlatformCommunicationLog)
        .filter(PlatformCommunicationLog.source_event_id == event_id)
        .order_by(PlatformCommunicationLog.created_at.desc())
        .first()
    )
    return TemplatedSendOut(
        status="sent", source_event_id=event_id, communication=row
    )


@router.post(
    "/offline",
    response_model=CommunicationOut,
    status_code=http_status.HTTP_201_CREATED,
)
def log_offline_contact(
    body: OfflineContactBody,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "staff")),
):
    """Log an offline contact (phone call / in-person) — mandatory comment.

    "Log any collection efforts or contacts — they call, they ask for a payout
    as of particular dates." Anchored to at least one of patient / application
    / loan. Append-only: there is no edit or delete.
    """
    if not (body.patient_id or body.application_id or body.loan_id):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one of patient_id, application_id, loan_id is required",
        )
    _validate_anchors(
        db,
        patient_id=body.patient_id,
        application_id=body.application_id,
        loan_id=body.loan_id,
    )
    row = communications_log.record_communication(
        db,
        channel=CHANNEL_OFFLINE,
        direction=body.direction,
        body=body.comment,
        patient_id=body.patient_id,
        application_id=body.application_id,
        loan_id=body.loan_id,
        sent_by=_actor(user),
        sent_by_user_id=user.id,
        comment=body.comment,
        contact_method=body.contact_method,
        purpose=body.purpose,
        result=body.result,
        status="recorded",
        occurred_at=body.occurred_at,
    )
    db.commit()
    db.refresh(row)
    return row


def _render(
    notification_type: str, channel: str, context: dict
) -> tuple[Optional[str], str]:
    """Render (subject, body) for a send/preview, mapping errors to HTTP."""
    try:
        if channel == "email":
            return render_email(notification_type, context)
        return None, render_sms(notification_type, context)
    except UnknownNotificationType as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail=f"Unknown notification_type '{notification_type}'",
        ) from exc
    except ValueError as exc:  # email-only type on the sms channel
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except TemplateError as exc:  # missing merge field (StrictUndefined) etc.
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Template render failed: {exc}",
        ) from exc
