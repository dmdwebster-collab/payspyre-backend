"""Unit tests for the unified integration SIMULATOR / LIVE mode framework.

DB-FREE: ``integration_mode`` resolution is driven with a lightweight stub row
(no Session), and the upsert live-credential gate is driven with a MagicMock
Session. The invariant under test is Dave's mandate: simulator is the safe
default, and live cannot be enabled without the provider's credentials.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.services import integration_mode
from app.services import integration_settings as service


class _Row(SimpleNamespace):
    pass


def _stub(monkeypatch, row):
    """Make integration_settings.get return ``row`` regardless of db/provider."""
    monkeypatch.setattr(service, "get", lambda db, provider: row)
    monkeypatch.setattr(
        "app.services.integration_settings.get", lambda db, provider: row, raising=False
    )


# --------------------------------------------------------------------------
# normalize / resolve
# --------------------------------------------------------------------------


def test_default_mode_is_simulator_when_no_row(monkeypatch):
    _stub(monkeypatch, None)
    assert integration_mode.resolve_mode(MagicMock(), "signnow") == "simulator"
    assert integration_mode.is_simulator(MagicMock(), "signnow") is True
    assert integration_mode.is_live(MagicMock(), "signnow") is False


def test_db_none_resolves_simulator():
    assert integration_mode.resolve_mode(None, "flinks") == "simulator"


def test_normalize_mode_rejects_garbage():
    with pytest.raises(integration_mode.IntegrationModeError):
        integration_mode.normalize_mode("staging")
    assert integration_mode.normalize_mode(None) == "simulator"
    assert integration_mode.normalize_mode("LIVE") == "live"


def test_resolve_live_row(monkeypatch):
    _stub(monkeypatch, _Row(mode="live", secrets={}, config={}))
    assert integration_mode.is_live(MagicMock(), "signnow") is True


def test_forces_simulator_only_when_row_present(monkeypatch):
    _stub(monkeypatch, None)
    assert integration_mode.forces_simulator(MagicMock(), "flinks") is False
    _stub(monkeypatch, _Row(mode="simulator", secrets={}, config={}))
    assert integration_mode.forces_simulator(MagicMock(), "flinks") is True


# --------------------------------------------------------------------------
# live credential gating
# --------------------------------------------------------------------------


def test_missing_live_credentials_for_zumrails():
    missing = integration_mode.missing_live_credentials(
        None, "zumrails", secrets={"api_key": "k"}
    )
    assert missing == ["api_secret"]
    assert integration_mode.can_enable_live(
        None, "zumrails", secrets={"api_key": "k", "api_secret": "s"}
    )


def test_signnow_live_accepts_static_bearer_or_oauth_quartet():
    # Static bearer alone is enough.
    assert integration_mode.missing_live_credentials(
        None, "signnow", secrets={"api_key": "bearer"}
    ) == []
    # Full OAuth quartet is enough.
    assert integration_mode.missing_live_credentials(
        None,
        "signnow",
        secrets={
            "client_id": "c",
            "client_secret": "s",
            "username": "u",
            "password": "p",
        },
    ) == []
    # Nothing -> the whole quartet is reported outstanding.
    assert set(
        integration_mode.missing_live_credentials(None, "signnow", secrets={})
    ) == {"client_id", "client_secret", "username", "password"}


def test_assert_live_allowed_raises_without_creds():
    with pytest.raises(integration_mode.IntegrationModeError):
        integration_mode.assert_live_allowed(None, "zumrails", secrets={})


# --------------------------------------------------------------------------
# upsert / set_mode enforcement + audit
# --------------------------------------------------------------------------


def test_upsert_to_live_without_creds_is_rejected():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    with pytest.raises(ValueError, match="missing required credentials"):
        service.upsert(db, provider="zumrails", secrets={}, enabled=True, mode="live")


def test_upsert_to_live_with_creds_succeeds_and_audits():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    setting = service.upsert(
        db,
        provider="zumrails",
        secrets={"api_key": "KEY_zzz", "api_secret": "SECRET_qqq"},
        enabled=True,
        mode="live",
        updated_by=uuid4(),
    )
    assert setting.mode == "live"
    # db.add called for the row AND the mode-change audit event.
    assert db.add.call_count >= 2
    audit = [
        c.args[0]
        for c in db.add.call_args_list
        if getattr(c.args[0], "event_type", None) == integration_mode.MODE_CHANGED_EVENT
    ]
    assert audit and audit[0].payload["new_mode"] == "live"
    # No secret material in the audit payload.
    assert "SECRET_qqq" not in repr(audit[0].payload)
    assert "KEY_zzz" not in repr(audit[0].payload)


def test_flinks_test_mode_mirrors_mode():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    # Simulator -> test_mode True.
    sim = service.upsert(db, provider="flinks", secrets={"api_key": "k"}, mode="simulator")
    assert sim.config["test_mode"] is True
    # Live -> test_mode False.
    live = service.upsert(db, provider="flinks", secrets={"api_key": "k"}, mode="live", enabled=True)
    assert live.config["test_mode"] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
