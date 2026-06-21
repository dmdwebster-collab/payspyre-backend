"""Tests for hardening quick-wins:
- production fails loud when encryption-at-rest keys are unset
- Twilio sends include a status_callback so delivery state can reconcile
"""
from unittest.mock import MagicMock, patch

import pytest

from app.core.config import Settings, settings


def _prod_kwargs(**over):
    base = dict(
        ENVIRONMENT="production",
        PATIENT_JWT_SECRET="real-patient-secret",
        FLINKS_WEBHOOK_SECRET="real-flinks-secret",
        EQUIFAX_WEBHOOK_SECRET="real-equifax-secret",
        SETTINGS_ENCRYPTION_KEY="real-settings-key",
        SIN_ENCRYPTION_KEY="real-sin-key",
        USE_REAL_ADAPTERS=False,
        USE_REAL_NOTIFICATIONS=False,
    )
    base.update(over)
    return base


def test_prod_requires_settings_encryption_key():
    with pytest.raises(ValueError) as ei:
        Settings(**_prod_kwargs(SETTINGS_ENCRYPTION_KEY=""))
    assert "SETTINGS_ENCRYPTION_KEY" in str(ei.value)


def test_prod_requires_sin_encryption_key():
    with pytest.raises(ValueError) as ei:
        Settings(**_prod_kwargs(SIN_ENCRYPTION_KEY=""))
    assert "SIN_ENCRYPTION_KEY" in str(ei.value)


def test_prod_passes_when_encryption_keys_set():
    s = Settings(**_prod_kwargs())
    assert s.ENVIRONMENT == "production"


def test_twilio_includes_status_callback(monkeypatch):
    from app.services.real_notification_dispatcher import TwilioSmsSender

    monkeypatch.setattr(settings, "WEBHOOK_PUBLIC_BASE_URL", "https://api.example.com")
    with patch("twilio.rest.Client"):
        sender = TwilioSmsSender("ACxxxxxxxx", "tok", "+15555550000")
        sender._client.messages.create.return_value = MagicMock(status="queued", sid="SM1")
        sender.send_magic_link(to_phone="+15555551111", token="123456", ttl_seconds=900)
    kwargs = sender._client.messages.create.call_args.kwargs
    assert kwargs["status_callback"] == "https://api.example.com/api/webhooks/v1/notifications/twilio"


def test_twilio_omits_status_callback_when_base_unset(monkeypatch):
    from app.services.real_notification_dispatcher import TwilioSmsSender

    monkeypatch.setattr(settings, "WEBHOOK_PUBLIC_BASE_URL", "")
    with patch("twilio.rest.Client"):
        sender = TwilioSmsSender("ACxxxxxxxx", "tok", "+15555550000")
        sender._client.messages.create.return_value = MagicMock(status="queued", sid="SM1")
        sender.send_magic_link(to_phone="+15555551111", token="123456", ttl_seconds=900)
    assert "status_callback" not in sender._client.messages.create.call_args.kwargs
