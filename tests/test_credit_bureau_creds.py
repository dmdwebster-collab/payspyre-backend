"""DB-free tests for CreditBureauService credential resolution.

CreditBureauService.__init__ now resolves Equifax/TransUnion API keys through
``integration_creds.resolve`` — the enabled settings-area row wins, with the env
``Settings`` (EQUIFAX_API_KEY / TRANSUNION_API_KEY) as the per-field fallback.
``db=None`` skips the settings-area lookup (env only), preserving prior behavior.

No network, no database: ``integration_settings.get`` and the settings singleton
are monkeypatched, mirroring tests/test_integration_creds.py.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services import credit_bureau as cb
from app.services import integration_creds as ic


def _row(secrets=None, enabled=True):
    return SimpleNamespace(secrets=secrets or {}, config={}, enabled=enabled)


def test_db_none_uses_env_keys(monkeypatch):
    monkeypatch.setattr(ic.settings, "EQUIFAX_API_KEY", "eq_env", raising=False)
    monkeypatch.setattr(ic.settings, "TRANSUNION_API_KEY", "tu_env", raising=False)

    svc = cb.CreditBureauService()  # db defaults to None -> env only

    assert svc.equifax_client is not None
    assert svc.equifax_client.api_key == "eq_env"
    assert svc.transunion_client is not None
    assert svc.transunion_client.api_key == "tu_env"


def test_unconfigured_bureau_yields_none_client(monkeypatch):
    monkeypatch.setattr(ic.settings, "EQUIFAX_API_KEY", "", raising=False)
    monkeypatch.setattr(ic.settings, "TRANSUNION_API_KEY", "", raising=False)

    svc = cb.CreditBureauService()

    assert svc.equifax_client is None
    assert svc.transunion_client is None


def test_settings_area_row_wins_over_env(monkeypatch):
    # Settings-area rows keyed by provider name (bureau_name).
    rows = {
        "equifax": _row(secrets={"api_key": "eq_settings"}),
        "transunion": _row(secrets={"api_key": "tu_settings"}),
    }
    monkeypatch.setattr(ic.integration_settings, "get", lambda db, p: rows.get(p))
    monkeypatch.setattr(ic.settings, "EQUIFAX_API_KEY", "eq_env", raising=False)
    monkeypatch.setattr(ic.settings, "TRANSUNION_API_KEY", "tu_env", raising=False)

    svc = cb.CreditBureauService(db=MagicMock())

    assert svc.equifax_client.api_key == "eq_settings"
    assert svc.transunion_client.api_key == "tu_settings"


def test_disabled_row_falls_back_to_env(monkeypatch):
    monkeypatch.setattr(
        ic.integration_settings, "get",
        lambda db, p: _row(secrets={"api_key": "from_settings"}, enabled=False),
    )
    monkeypatch.setattr(ic.settings, "EQUIFAX_API_KEY", "eq_env", raising=False)
    monkeypatch.setattr(ic.settings, "TRANSUNION_API_KEY", "tu_env", raising=False)

    svc = cb.CreditBureauService(db=MagicMock())

    assert svc.equifax_client.api_key == "eq_env"
    assert svc.transunion_client.api_key == "tu_env"
