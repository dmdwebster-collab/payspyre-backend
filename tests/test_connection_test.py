"""Tests for the integration connection-test framework. No network — httpx + the
token helpers are stubbed. Verifies pass/fail mapping and that secrets never leak
into the result reason."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx

from app.services import connection_test as ct


def _row(config=None, secrets=None):
    return SimpleNamespace(config=config or {}, secrets=secrets or {})


def _resp(status_code, json_body=None):
    return httpx.Response(
        status_code,
        request=httpx.Request("GET", "https://example.test"),
        json=json_body if json_body is not None else None,
    )


def test_unknown_provider():
    r = ct.test_connection(MagicMock(), "does-not-exist")
    assert r.ok is False
    assert "Unknown provider" in r.reason


def test_pending_provider_is_honest(monkeypatch):
    monkeypatch.setattr(ct.integration_settings, "get", lambda db, p: None)
    for provider in ("didit", "flinks", "equifax"):
        r = ct.test_connection(MagicMock(), provider)
        assert r.ok is False
        assert "not available yet" in r.reason


def test_sendgrid_ok(monkeypatch):
    monkeypatch.setattr(ct.integration_settings, "get", lambda db, p: _row(secrets={"api_key": "SG.x"}))
    monkeypatch.setattr(ct.httpx, "get", lambda *a, **k: _resp(200))
    r = ct.test_connection(MagicMock(), "sendgrid")
    assert r.ok is True
    assert r.reason == "Authenticated successfully."


def test_sendgrid_auth_rejected_and_no_secret_leak(monkeypatch):
    monkeypatch.setattr(ct.integration_settings, "get", lambda db, p: _row(secrets={"api_key": "SG.SUPERSECRET"}))
    monkeypatch.setattr(ct.httpx, "get", lambda *a, **k: _resp(401))
    r = ct.test_connection(MagicMock(), "sendgrid")
    assert r.ok is False
    assert "rejected" in r.reason.lower()
    assert "SG.SUPERSECRET" not in r.reason


def test_missing_credential_is_reported(monkeypatch):
    monkeypatch.setattr(ct.integration_settings, "get", lambda db, p: _row(secrets={}))
    monkeypatch.setattr(ct.settings, "SENDGRID_API_KEY", "", raising=False)
    r = ct.test_connection(MagicMock(), "sendgrid")
    assert r.ok is False
    assert "missing" in r.reason.lower()


def test_twilio_uses_settings_area_creds(monkeypatch):
    captured = {}

    def _get(url, *a, **k):
        captured["url"] = url
        captured["auth"] = k.get("auth")
        return _resp(200)

    monkeypatch.setattr(
        ct.integration_settings, "get",
        lambda db, p: _row(secrets={"account_sid": "ACfromsettings", "auth_token": "tok"}),
    )
    monkeypatch.setattr(ct.httpx, "get", _get)
    r = ct.test_connection(MagicMock(), "twilio")
    assert r.ok is True
    assert "ACfromsettings" in captured["url"]
    assert captured["auth"] == ("ACfromsettings", "tok")


def test_zumrails_reuses_token_helper(monkeypatch):
    monkeypatch.setattr(
        ct.integration_settings, "get",
        lambda db, p: _row(config={"base_url": "https://z"}, secrets={"api_key": "k", "api_secret": "s"}),
    )
    import app.services.payments.zumrails_auth as za

    monkeypatch.setattr(za, "get_zumrails_token", lambda **kw: "token")
    assert ct.test_connection(MagicMock(), "zumrails").ok is True

    def _raise(**kw):
        raise za.PermanentZumrailsAuthError("rejected")

    monkeypatch.setattr(za, "get_zumrails_token", _raise)
    r = ct.test_connection(MagicMock(), "zumrails")
    assert r.ok is False
    assert "rejected" in r.reason.lower()


def test_timeout_is_handled(monkeypatch):
    monkeypatch.setattr(ct.integration_settings, "get", lambda db, p: _row(secrets={"api_key": "SG.x"}))

    def _timeout(*a, **k):
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr(ct.httpx, "get", _timeout)
    r = ct.test_connection(MagicMock(), "sendgrid")
    assert r.ok is False
    assert "timed out" in r.reason.lower()
