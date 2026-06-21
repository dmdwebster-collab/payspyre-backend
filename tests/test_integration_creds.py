"""Tests for the settings-area cred resolver (env fallback)."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services import integration_creds as ic


def _row(secrets=None, config=None, enabled=True):
    return SimpleNamespace(secrets=secrets or {}, config=config or {}, enabled=enabled)


def test_prefers_enabled_settings_area_row(monkeypatch):
    monkeypatch.setattr(ic.integration_settings, "get", lambda db, p: _row(secrets={"api_key": "from_settings"}))
    monkeypatch.setattr(ic.settings, "SENDGRID_API_KEY", "from_env", raising=False)
    out = ic.resolve(MagicMock(), "sendgrid", secret_keys=["api_key"], env={"api_key": "SENDGRID_API_KEY"})
    assert out["api_key"] == "from_settings"


def test_falls_back_to_env_when_no_row(monkeypatch):
    monkeypatch.setattr(ic.integration_settings, "get", lambda db, p: None)
    monkeypatch.setattr(ic.settings, "SENDGRID_API_KEY", "from_env", raising=False)
    out = ic.resolve(MagicMock(), "sendgrid", secret_keys=["api_key"], env={"api_key": "SENDGRID_API_KEY"})
    assert out["api_key"] == "from_env"


def test_disabled_row_falls_back_to_env(monkeypatch):
    monkeypatch.setattr(
        ic.integration_settings, "get",
        lambda db, p: _row(secrets={"api_key": "from_settings"}, enabled=False),
    )
    monkeypatch.setattr(ic.settings, "SENDGRID_API_KEY", "from_env", raising=False)
    out = ic.resolve(MagicMock(), "sendgrid", secret_keys=["api_key"], env={"api_key": "SENDGRID_API_KEY"})
    assert out["api_key"] == "from_env"


def test_db_none_is_env_only(monkeypatch):
    monkeypatch.setattr(ic.settings, "SENDGRID_API_KEY", "from_env", raising=False)
    out = ic.resolve(None, "sendgrid", secret_keys=["api_key"], env={"api_key": "SENDGRID_API_KEY"})
    assert out["api_key"] == "from_env"


def test_config_keys_and_missing_default_empty(monkeypatch):
    monkeypatch.setattr(
        ic.integration_settings, "get",
        lambda db, p: _row(config={"api_base_url": "https://x"}),
    )
    out = ic.resolve(
        MagicMock(), "didit",
        secret_keys=["api_key"], config_keys=["api_base_url", "workflow_id"],
    )
    assert out["api_base_url"] == "https://x"
    assert out["workflow_id"] == ""  # missing → empty, not KeyError
    assert out["api_key"] == ""
