"""Unit tests for the SendGrid email adapter — no DB, no network.

The ``sendgrid`` SDK is not installed in this environment, so we install
lightweight fakes into ``sys.modules`` (``sendgrid``, ``sendgrid.helpers.mail``,
``python_http_client.exceptions``) BEFORE the sender's local imports run. The
fakes let us drive the success path, error classification (5xx/429 transient,
4xx permanent) and PII redaction without touching the real SDK or a database.

If the real ``sendgrid`` package is later installed, these tests still hold:
the sender imports ``SendGridAPIClient`` / ``Mail`` locally, and we monkeypatch
those symbols on whatever module object is present in ``sys.modules``.
"""
import sys
import types

import pytest

# ---------------------------------------------------------------------------
# Fake SendGrid SDK — installed into sys.modules so the sender's local
# ``from sendgrid import SendGridAPIClient`` / ``from sendgrid.helpers.mail
# import Mail`` resolve to these. Done at import time, before the sender runs.
# ---------------------------------------------------------------------------


class _FakeHTTPError(Exception):
    """Mimics python_http_client.exceptions.HTTPError: .status_code + .body."""

    def __init__(self, status_code, body=b"", reason=""):
        super().__init__(reason or f"HTTP {status_code}")
        self.status_code = status_code
        self.body = body
        self.reason = reason


def _install_fake_sendgrid():
    if "sendgrid" not in sys.modules:
        sg = types.ModuleType("sendgrid")

        class SendGridAPIClient:  # replaced per-test via monkeypatch
            def __init__(self, api_key):
                self.api_key = api_key

            def send(self, message):  # pragma: no cover - overridden in tests
                raise NotImplementedError

        sg.SendGridAPIClient = SendGridAPIClient
        helpers = types.ModuleType("sendgrid.helpers")
        mail_mod = types.ModuleType("sendgrid.helpers.mail")

        class Mail:
            def __init__(self, from_email=None, to_emails=None, subject=None, html_content=None):
                self.from_email = from_email
                self.to_emails = to_emails
                self.subject = subject
                self.html_content = html_content

        mail_mod.Mail = Mail
        sg.helpers = helpers
        helpers.mail = mail_mod
        sys.modules["sendgrid"] = sg
        sys.modules["sendgrid.helpers"] = helpers
        sys.modules["sendgrid.helpers.mail"] = mail_mod

    if "python_http_client" not in sys.modules:
        phc = types.ModuleType("python_http_client")
        exc_mod = types.ModuleType("python_http_client.exceptions")
        exc_mod.HTTPError = _FakeHTTPError
        phc.exceptions = exc_mod
        sys.modules["python_http_client"] = phc
        sys.modules["python_http_client.exceptions"] = exc_mod


_install_fake_sendgrid()

from app.services.real_notification_dispatcher import (  # noqa: E402
    PermanentNotificationError,
    SendOutcome,
    TransientNotificationError,
)
from app.services.sendgrid_email import (  # noqa: E402
    SendGridEmailSender,
    _classify_sendgrid_error,
)


# ---------------------------------------------------------------------------
# Fake response + client helpers
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code=202, headers=None, body=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self.body = body


def _bind_client(monkeypatch, send_impl):
    """Patch SendGridAPIClient on the live ``sendgrid`` module so the sender's
    local import picks up a client whose ``.send`` runs ``send_impl``."""
    sg = sys.modules["sendgrid"]

    class _Client:
        def __init__(self, api_key):
            self.api_key = api_key

        def send(self, message):
            return send_impl(message)

    monkeypatch.setattr(sg, "SendGridAPIClient", _Client)


@pytest.fixture
def sender():
    return SendGridEmailSender(api_key="SG.test_key", from_email="noreply@payspyre.test")


# ===========================================================================
# Success path
# ===========================================================================


def test_send_success_returns_send_outcome(sender, monkeypatch):
    captured = {}

    def _send(message):
        captured["message"] = message
        return _FakeResponse(
            status_code=202,
            headers={"X-Message-Id": "sg-msg-abc123"},
        )

    _bind_client(monkeypatch, _send)

    outcome = sender.send_magic_link(
        to_email="dest@example.com", token="123456", ttl_seconds=900
    )

    assert isinstance(outcome, SendOutcome)
    assert outcome.vendor == "sendgrid"
    assert outcome.vendor_message_id == "sg-msg-abc123"
    assert outcome.status == "sent"
    # The plaintext token reaches the SDK body (recipient is the patient) but
    # the from-email + subject are wired correctly.
    msg = captured["message"]
    assert "123456" in msg.html_content
    assert msg.from_email == "noreply@payspyre.test"
    assert msg.subject == "Your PaySpyre verification code"


def test_send_success_lowercase_header(sender, monkeypatch):
    _bind_client(
        monkeypatch,
        lambda m: _FakeResponse(status_code=202, headers={"x-message-id": "lower-id"}),
    )
    outcome = sender.send_magic_link(
        to_email="dest@example.com", token="000111", ttl_seconds=600
    )
    assert outcome.vendor_message_id == "lower-id"


def test_success_without_message_id_is_transient(sender, monkeypatch):
    """A 2xx ack with no X-Message-Id is treated as a (transient) failure —
    mirrors ResendEmailSender's missing-id guard."""
    _bind_client(monkeypatch, lambda m: _FakeResponse(status_code=202, headers={}))
    with pytest.raises(TransientNotificationError):
        sender.send_magic_link(to_email="dest@example.com", token="abc", ttl_seconds=900)


# ===========================================================================
# Error classification
# ===========================================================================


@pytest.mark.parametrize("status_code", [429, 500, 502, 503, 504])
def test_5xx_and_429_classify_transient(sender, monkeypatch, status_code):
    def _send(message):
        raise _FakeHTTPError(status_code=status_code, body=b'{"errors":[{"message":"upstream"}]}')

    _bind_client(monkeypatch, _send)
    with pytest.raises(TransientNotificationError) as ei:
        sender.send_magic_link(to_email="dest@example.com", token="x", ttl_seconds=900)
    assert f"status={status_code}" in str(ei.value)


@pytest.mark.parametrize("status_code", [400, 401, 403, 413, 422])
def test_4xx_classify_permanent(sender, monkeypatch, status_code):
    def _send(message):
        raise _FakeHTTPError(status_code=status_code, body=b'{"errors":[{"message":"bad request"}]}')

    _bind_client(monkeypatch, _send)
    with pytest.raises(PermanentNotificationError) as ei:
        sender.send_magic_link(to_email="dest@example.com", token="x", ttl_seconds=900)
    assert f"status={status_code}" in str(ei.value)


def test_non_2xx_response_without_raise_is_classified(sender, monkeypatch):
    """If the SDK ever RETURNS a non-2xx response instead of raising, the
    sender must still classify it (defensive guard) — 403 -> permanent."""
    _bind_client(monkeypatch, lambda m: _FakeResponse(status_code=403, body=b"forbidden"))
    with pytest.raises(PermanentNotificationError):
        sender.send_magic_link(to_email="dest@example.com", token="x", ttl_seconds=900)


def test_network_error_without_status_is_transient(sender, monkeypatch):
    """A bare exception (no .status_code, no permanent hint) -> conservative transient."""
    def _send(message):
        raise ConnectionError("connection reset by peer")

    _bind_client(monkeypatch, _send)
    with pytest.raises(TransientNotificationError):
        sender.send_magic_link(to_email="dest@example.com", token="x", ttl_seconds=900)


def test_message_hint_without_status_is_permanent():
    """No status, but a permanent hint in the message -> permanent."""
    exc = Exception("The from email does not match a verified sender identity")
    result = _classify_sendgrid_error(exc)
    assert isinstance(result, PermanentNotificationError)


# ===========================================================================
# PII redaction
# ===========================================================================


def test_pii_redacted_from_classified_error_email():
    """An email address echoed in a SendGrid error body must be redacted in the
    classified exception message (Hard Rule #6)."""
    exc = _FakeHTTPError(
        status_code=400,
        body=b'{"errors":[{"message":"address victim@example.com is invalid"}]}',
    )
    result = _classify_sendgrid_error(exc)
    assert "victim@example.com" not in str(result)
    assert "<email-redacted>" in str(result)


def test_pii_redacted_from_classified_error_phone():
    exc = _FakeHTTPError(
        status_code=422,
        body=b"recipient +1 415 555 0199 rejected",
    )
    result = _classify_sendgrid_error(exc)
    assert "555 0199" not in str(result)
    assert "<phone-redacted>" in str(result)


def test_pii_redacted_through_send_path(sender, monkeypatch):
    """End-to-end: PII in the SDK error never survives into the raised error."""
    def _send(message):
        raise _FakeHTTPError(
            status_code=400,
            body=b'{"errors":[{"message":"to=leak@patient.com not allowed"}]}',
        )

    _bind_client(monkeypatch, _send)
    with pytest.raises(PermanentNotificationError) as ei:
        sender.send_magic_link(to_email="leak@patient.com", token="x", ttl_seconds=900)
    assert "leak@patient.com" not in str(ei.value)
    assert "<email-redacted>" in str(ei.value)
