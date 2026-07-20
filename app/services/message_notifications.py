"""Best-effort email ping for new application messages (the Slack ping).

When one side posts a message, the *other* side gets an email nudge so they
don't have to live in the dashboard — this is the actual replacement for the
Slack notification. Design constraints:

* **Non-blocking.** The message is already committed by the time this runs;
  every failure here is caught and logged, never raised. A flaky email must not
  fail the post.
* **Inert by default.** Does nothing unless ``USE_REAL_NOTIFICATIONS`` is on
  (mirrors the rest of the notification stack) — so tests/dev never send. Tests
  inject a fake ``sender`` to exercise the path.
* **Recipient by direction:**
    - vendor → PaySpyre: send to ``settings.PLATFORM_MESSAGES_INBOX`` (the ops
      inbox). Empty → skipped.
    - PaySpyre → vendor: send to the clinic's own staff emails (resolved from
      ``platform_clinic_memberships``), falling back to the vendor contact email.

The email sender is built the same way the real dispatcher builds its email
channel (settings-area creds via ``integration_creds.resolve``, env fallback).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.message import (
    SENDER_KIND_VENDOR,
    PlatformApplicationMessage,
)
from app.services.real_notification_dispatcher import _redact_pii

logger = get_logger(__name__)


def notify_new_message(
    db: Session,
    *,
    application: PlatformCreditApplication,
    message: PlatformApplicationMessage,
    sender_user: object,
    sender: Optional[object] = None,
) -> None:
    """Fire a best-effort email to the counterparty. Swallows all errors."""
    try:
        if sender is None and not settings.USE_REAL_NOTIFICATIONS:
            return
        recipients = _resolve_recipients(db, application, message.sender_kind)
        if not recipients:
            logger.info(
                "application_message_email_no_recipients",
                application_id=str(application.id),
                sender_kind=message.sender_kind,
            )
            return
        sender = sender or _build_email_sender(db)
        subject, html = _render(application, message, sender_user)
        for to_email in recipients:
            try:
                sender.send_message(to_email=to_email, subject=subject, html=html)
            except Exception as exc:  # per-recipient: one bad address ≠ skip rest
                logger.warning(
                    "application_message_email_failed",
                    application_id=str(application.id),
                    error=_redact_pii(str(exc)),
                )
    except Exception as exc:  # pragma: no cover - defensive: never break a post
        logger.warning(
            "application_message_notify_error",
            application_id=str(application.id),
            error=_redact_pii(str(exc)),
        )


def _resolve_recipients(
    db: Session, application: PlatformCreditApplication, sender_kind: str
) -> list[str]:
    if sender_kind == SENDER_KIND_VENDOR:
        # Clinic → PaySpyre: notify the ops inbox.
        inbox = (settings.PLATFORM_MESSAGES_INBOX or "").strip()
        return [inbox] if inbox else []

    # PaySpyre (admin) → clinic: notify the clinic's staff, fall back to the
    # vendor contact email on file.
    from app.models.loan import Vendor
    from app.services.clinic_membership import resolve_clinic_user_emails

    recipients: list[str] = []
    if application.vendor_id is not None:
        recipients = resolve_clinic_user_emails(db, application.vendor_id)
        if not recipients:
            vendor = (
                db.query(Vendor).filter(Vendor.id == application.vendor_id).first()
            )
            if vendor is not None and vendor.email:
                recipients = [vendor.email]
    return recipients


def _render(
    application: PlatformCreditApplication,
    message: PlatformApplicationMessage,
    sender_user: object,
) -> tuple[str, str]:
    from app.services.application_messages import sender_display_name

    who = sender_display_name(sender_user, message.sender_kind)
    if message.sender_kind == SENDER_KIND_VENDOR:
        subject = f"New message from {who} on a PaySpyre application"
    else:
        subject = "New message from PaySpyre about your application"
    # Body is intentionally low-detail: it nudges the recipient to open the
    # secure console rather than reproducing thread content in email.
    preview = (message.body or "")[:280]
    html = (
        f"<p>{who} sent a message on application "
        f"<strong>{application.id}</strong>:</p>"
        f"<blockquote>{preview}</blockquote>"
        "<p>Open the PaySpyre console to read and reply.</p>"
    )
    return subject, html


def _build_email_sender(db: Session):
    """Mirror ``RealNotificationDispatcher._build_sender`` for the email channel:
    prefer settings-area creds (Dave's mandate), env fallback."""
    from app.services.integration_creds import resolve

    if settings.EMAIL_PROVIDER == "sendgrid":
        from app.services.sendgrid_email import SendGridEmailSender

        c = resolve(
            db,
            "sendgrid",
            secret_keys=["api_key"],
            config_keys=["from_email"],
            env={"api_key": "SENDGRID_API_KEY", "from_email": "SENDGRID_FROM_EMAIL"},
        )
        return SendGridEmailSender(api_key=c["api_key"], from_email=c["from_email"])

    from app.services.real_notification_dispatcher import ResendEmailSender

    c = resolve(
        db,
        "resend",
        secret_keys=["api_key"],
        config_keys=["from_email"],
        env={"api_key": "RESEND_API_KEY", "from_email": "RESEND_FROM_EMAIL"},
    )
    return ResendEmailSender(api_key=c["api_key"], from_email=c["from_email"])
