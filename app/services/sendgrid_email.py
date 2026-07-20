"""SendGrid email adapter for magic-link sends — drop-in peer of
``ResendEmailSender`` in ``real_notification_dispatcher.py``.

The business sends transactional email through SendGrid (dedicated IP), not
Resend. This module provides ``SendGridEmailSender`` with the SAME contract as
``ResendEmailSender.send_magic_link`` so the dispatcher can pick one or the
other for the email channel without any other code change:

    sender.send_magic_link(to_email=..., token=..., ttl_seconds=...) -> SendOutcome

Design parity with the Resend sender (intentional, so the two are swappable):

- Sync, single-shot: NO retries inside the adapter (the dispatcher's two-step
  write + rollback semantics own retry/idempotency at the caller level).
- Reuses the project's ``SendOutcome`` and ``NotificationError`` hierarchy and
  the same ``_redact_pii`` helper — vendor error strings are redacted before
  they reach any log or event payload (Hard Rule #6). The plaintext token is in
  the HTML body (the recipient IS the patient) but is NEVER logged.
- The sender itself does not log on failure; it raises a classified
  ``NotificationError`` and the dispatcher emits the canonical event + surfaces.

How SendGrid differs from Resend at the SDK boundary (drives this module):

1. The SDK raises ``python_http_client.exceptions.HTTPError`` on any non-2xx
   response, with ``.status_code``, ``.body`` and ``.headers`` attributes —
   unlike Resend's bare ``Exception``. So classification reads ``.status_code``
   first (authoritative HTTP status) before falling back to message hints.
2. On success the message id is NOT in a JSON body; SendGrid returns it in the
   ``X-Message-Id`` response header. We read it from ``response.headers``.
3. The api key + from-email are injected (constructor params), never read from
   ``settings`` here — keeps the sender unit-testable without app config.
"""
from __future__ import annotations

from app.core.config import settings  # noqa: F401  (parity import; unused but mirrors peer module)
from app.core.logging import get_logger
from app.services.real_notification_dispatcher import (
    NotificationError,
    PermanentNotificationError,
    SendOutcome,
    TransientNotificationError,
    _redact_pii,
)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# SendGrid error classification — mirrors _classify_resend_error's shape.
# ---------------------------------------------------------------------------

# Message-string hints for SendGrid 4xx bodies that are unambiguously permanent
# (retry will not help). SendGrid's error bodies are JSON like
# {"errors":[{"message":"...","field":"from","help":...}]} — we lowercase the
# whole string and substring-match, same heuristic family as Resend's hints.
_SENDGRID_PERMANENT_HINTS = (
    "does not match a verified sender identity",
    "invalid",
    "the from email does not",
    "permission",
    "unauthorized",
    "forbidden",
    "maximum credits exceeded",  # plan/billing — caller retry won't fix
    "blocked",
)


def _classify_sendgrid_error(exc: Exception) -> NotificationError:
    """Map a SendGrid SDK exception to the project's NotificationError hierarchy.

    The SendGrid SDK surfaces non-2xx responses as
    ``python_http_client.exceptions.HTTPError`` carrying ``.status_code`` (int)
    and ``.body`` (bytes/str). We trust ``.status_code`` when present, then fall
    back to message-string hints, then default to transient (conservative — let
    the caller decide). All strings are PII-redacted before they land anywhere.
    """
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8", "replace")
        except Exception:  # pragma: no cover - defensive
            body = repr(body)
    raw_msg = body if body else str(exc)
    msg = _redact_pii(str(raw_msg))
    summary = f"SendGrid status={status}: {msg}"

    if isinstance(status, int):
        if status in (429, 500, 502, 503, 504):
            return TransientNotificationError(summary)
        if 400 <= status < 500:
            return PermanentNotificationError(summary)

    lowered = str(raw_msg).lower()
    for hint in _SENDGRID_PERMANENT_HINTS:
        if hint in lowered:
            return PermanentNotificationError(summary)
    return TransientNotificationError(summary)


# ---------------------------------------------------------------------------
# Sender
# ---------------------------------------------------------------------------


class SendGridEmailSender:
    """Wraps ``SendGridAPIClient.send`` for magic-link emails.

    Contract-compatible with ``ResendEmailSender``: same ``send_magic_link``
    signature, same ``SendOutcome`` return, same ``NotificationError`` raises.
    The dispatcher can bind this for the email channel in place of Resend.

    api key + from-email are injected (not read from settings) so the sender is
    testable in isolation and so two senders with different keys can coexist.
    """

    def __init__(self, api_key: str, from_email: str) -> None:
        self._api_key = api_key
        self._from_email = from_email

    def send_magic_link(
        self, *, to_email: str, token: str, ttl_seconds: int
    ) -> SendOutcome:
        # Local imports keep the SendGrid SDK out of the cold-import path of the
        # dispatcher selector (parity with TwilioSmsSender's local import) and
        # let tests monkeypatch the module symbols / stub the SDK cleanly.
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail

        ttl_minutes = max(1, ttl_seconds // 60)
        # Plaintext token in the HTML body — the recipient is the patient.
        # NEVER logged. The dispatcher's hashed token in the event payload is
        # the audit-side artifact.
        html = (
            "<p>Your PaySpyre verification code is:</p>"
            f"<p><strong>{token}</strong></p>"
            f"<p>It expires in {ttl_minutes} minutes.</p>"
        )
        message = Mail(
            from_email=self._from_email,
            to_emails=to_email,
            subject="Your PaySpyre verification code",
            html_content=html,
        )
        client = SendGridAPIClient(self._api_key)
        try:
            response = client.send(message)
        except Exception as exc:  # python_http_client.exceptions.HTTPError + net
            raise _classify_sendgrid_error(exc) from exc

        # Defensive: the SDK raises on non-2xx, but guard anyway so a future SDK
        # change that returns a non-2xx response doesn't silently "succeed".
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int) and not (200 <= status_code < 300):
            raise _classify_sendgrid_error(
                _SendGridStatusError(status_code, getattr(response, "body", b""))
            )

        # SendGrid returns the message id in the X-Message-Id response header,
        # not a JSON body. Header access is dict-like across SDK versions
        # (CaseInsensitiveDict / plain dict).
        headers = getattr(response, "headers", None) or {}
        message_id = ""
        try:
            message_id = (
                headers.get("X-Message-Id")
                or headers.get("x-message-id")
                or ""
            )
        except AttributeError:
            message_id = ""
        if not message_id:
            raise TransientNotificationError(
                "SendGrid ack did not include an X-Message-Id"
            )
        # NOTE: ``SendOutcome.vendor`` is typed ``Literal["resend", "twilio"]``
        # in the (un-editable) peer module. At runtime Literal is NOT enforced,
        # so we pass the accurate "sendgrid" label — the value lands verbatim in
        # the ``notification_sent`` event's ``vendor`` field, which is what ops
        # joins on. WIRING NEEDED: widen the Literal to include "sendgrid" so the
        # type checker stops flagging this line (see report).
        return SendOutcome(
            vendor="sendgrid",  # type: ignore[arg-type]  # accurate audit label
            vendor_message_id=str(message_id),
            status="sent",
        )

    def send_message(self, *, to_email: str, subject: str, html: str) -> SendOutcome:
        """Generic email send (WS1) — same vendor/error + X-Message-Id contract
        as ``send_magic_link`` with a caller-supplied subject + body."""
        from sendgrid.helpers.mail import Mail

        message = Mail(
            from_email=self._from_email,
            to_emails=to_email,
            subject=subject,
            html_content=html,
        )
        return self._dispatch(message)

    def send_report(
        self,
        *,
        to_email: str,
        subject: str,
        html: str,
        attachment_filename: str,
        attachment_bytes: bytes,
        attachment_mime: str = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    ) -> SendOutcome:
        """Email with one file attachment (WS-H scheduled reports) — same
        vendor/error + X-Message-Id contract as ``send_message``. The
        attachment is base64-encoded per the SendGrid v3 API; nothing is
        persisted — the artifact exists only inside this request."""
        import base64

        from sendgrid.helpers.mail import (
            Attachment,
            Disposition,
            FileContent,
            FileName,
            FileType,
            Mail,
        )

        message = Mail(
            from_email=self._from_email,
            to_emails=to_email,
            subject=subject,
            html_content=html,
        )
        message.attachment = Attachment(
            FileContent(base64.b64encode(attachment_bytes).decode("ascii")),
            FileName(attachment_filename),
            FileType(attachment_mime),
            Disposition("attachment"),
        )
        return self._dispatch(message)

    def _dispatch(self, message) -> SendOutcome:
        """Shared send + response contract for the generic-mail paths."""
        from sendgrid import SendGridAPIClient

        client = SendGridAPIClient(self._api_key)
        try:
            response = client.send(message)
        except Exception as exc:  # python_http_client.exceptions.HTTPError + net
            raise _classify_sendgrid_error(exc) from exc

        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int) and not (200 <= status_code < 300):
            raise _classify_sendgrid_error(
                _SendGridStatusError(status_code, getattr(response, "body", b""))
            )

        headers = getattr(response, "headers", None) or {}
        message_id = ""
        try:
            message_id = headers.get("X-Message-Id") or headers.get("x-message-id") or ""
        except AttributeError:
            message_id = ""
        if not message_id:
            raise TransientNotificationError("SendGrid ack did not include an X-Message-Id")
        return SendOutcome(
            vendor="sendgrid",  # type: ignore[arg-type]  # accurate audit label
            vendor_message_id=str(message_id),
            status="sent",
        )


class _SendGridStatusError(Exception):
    """Synthetic carrier so a non-2xx *response* (not an SDK exception) can flow
    through ``_classify_sendgrid_error`` with the same .status_code/.body shape."""

    def __init__(self, status_code: int, body) -> None:
        super().__init__(f"SendGrid returned status {status_code}")
        self.status_code = status_code
        self.body = body
