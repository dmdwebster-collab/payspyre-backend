"""Unit tests for platform integration settings — service redaction + schemas.

No live database: the service functions are pure functions over a Session, so
we drive them with a MagicMock Session (or test redaction as a pure function
over a lightweight stub row). The key invariant under test is that secret
credential VALUES never leak into API-facing output.
"""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from cryptography.fernet import Fernet

from app.api.schemas.integration_settings import (
    IntegrationSettingsRead,
    IntegrationSettingsUpsert,
)
from app.core import secret_crypto
from app.services import integration_settings as service


def _row(**overrides):
    """Lightweight stand-in for a PlatformIntegrationSettings ORM row."""
    base = dict(
        provider="sendgrid",
        config={"region": "us", "from_email": "noreply@payspyre.com"},
        secrets={"api_key": "SG.supersecret", "webhook_signing_key": "whsec_x"},
        enabled=True,
        updated_by=uuid4(),
        created_at=datetime(2026, 6, 19, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 19, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------


def test_redact_drops_secret_values():
    out = service.redact(_row())
    # The raw secret values must be nowhere in the output.
    assert "SG.supersecret" not in repr(out)
    assert "whsec_x" not in repr(out)
    assert "secrets" not in out


def test_redact_exposes_only_secret_key_names_sorted():
    out = service.redact(_row())
    assert out["secret_keys"] == ["api_key", "webhook_signing_key"]


def test_redact_preserves_non_secret_config():
    out = service.redact(_row())
    assert out["config"] == {"region": "us", "from_email": "noreply@payspyre.com"}
    assert out["provider"] == "sendgrid"
    assert out["enabled"] is True


def test_redact_handles_empty_secrets_and_config():
    out = service.redact(_row(secrets={}, config={}))
    assert out["secret_keys"] == []
    assert out["config"] == {}


def test_redact_handles_none_secrets_and_config():
    # Newly-built rows may have None before flush defaults apply.
    out = service.redact(_row(secrets=None, config=None))
    assert out["secret_keys"] == []
    assert out["config"] == {}


def test_read_schema_has_no_secrets_field():
    # The API read model must structurally be unable to carry secret values.
    assert "secrets" not in IntegrationSettingsRead.model_fields
    assert "secret_keys" in IntegrationSettingsRead.model_fields


def test_redact_output_validates_against_read_schema():
    model = IntegrationSettingsRead.model_validate(service.redact(_row()))
    dumped = model.model_dump()
    assert dumped["secret_keys"] == ["api_key", "webhook_signing_key"]
    assert "secrets" not in dumped


# --------------------------------------------------------------------------
# schema validation
# --------------------------------------------------------------------------


def test_upsert_schema_defaults():
    s = IntegrationSettingsUpsert()
    assert s.config == {}
    assert s.secrets == {}
    assert s.enabled is False


def test_upsert_schema_accepts_secrets_on_write():
    s = IntegrationSettingsUpsert(
        config={"base_url": "https://api.flinks.io"},
        secrets={"customer_id": "abc", "api_token": "tok"},
        enabled=True,
    )
    assert s.secrets["api_token"] == "tok"
    assert s.enabled is True


# --------------------------------------------------------------------------
# service get / list / upsert with a mocked Session
# --------------------------------------------------------------------------


def test_get_queries_by_provider():
    db = MagicMock()
    sentinel = _row(provider="flinks")
    db.query.return_value.filter.return_value.first.return_value = sentinel
    assert service.get(db, "flinks") is sentinel


def test_list_all_returns_rows():
    db = MagicMock()
    rows = [_row(provider="didit"), _row(provider="twilio")]
    db.query.return_value.order_by.return_value.all.return_value = rows
    assert service.list_all(db) == rows


def test_upsert_creates_new_row_when_absent():
    db = MagicMock()
    # get() -> None means create path
    db.query.return_value.filter.return_value.first.return_value = None
    uid = uuid4()

    setting = service.upsert(
        db,
        provider="zumrails",
        config={"env": "sandbox"},
        secrets={"client_secret": "shh"},
        enabled=True,
        updated_by=uid,
    )

    db.add.assert_called_once()
    db.commit.assert_called_once()
    assert setting.provider == "zumrails"
    assert setting.secrets == {"client_secret": "shh"}
    assert setting.updated_by == uid


def test_upsert_updates_existing_row_without_add():
    db = MagicMock()
    existing = _row(provider="twilio", secrets={"old": "x"}, enabled=False)
    db.query.return_value.filter.return_value.first.return_value = existing

    setting = service.upsert(
        db,
        provider="twilio",
        config={"account_sid": "AC123"},
        secrets={"auth_token": "new"},
        enabled=True,
        updated_by=None,
    )

    db.add.assert_not_called()
    db.commit.assert_called_once()
    assert setting is existing
    assert setting.secrets == {"auth_token": "new"}
    assert setting.enabled is True


def test_upsert_defaults_config_and_secrets_to_empty_dicts():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    setting = service.upsert(db, provider="equifax")
    assert setting.config == {}
    assert setting.secrets == {}


# --------------------------------------------------------------------------
# encryption-at-rest — upsert encrypts, get decrypts, redact still key-only
# --------------------------------------------------------------------------


@pytest.fixture
def fernet_key(monkeypatch):
    """Configure a fresh Fernet key (patches the crypto indirection point)."""
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setattr(secret_crypto, "_configured_key", lambda: key)
    monkeypatch.setattr(secret_crypto, "_warned_no_key", False, raising=False)
    return key


def test_upsert_persists_encrypted_secrets_not_plaintext(fernet_key):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None

    # Capture the secrets at commit time — the service decrypts the row in place
    # *after* commit (so internal callers get plaintext back), so we must snapshot
    # what was actually staged for persistence before that happens.
    persisted = {}

    def _capture_commit():
        persisted.update(db.add.call_args[0][0].secrets)

    db.commit.side_effect = _capture_commit

    setting = service.upsert(
        db,
        provider="sendgrid",
        secrets={"api_key": "SG.supersecret"},
        enabled=True,
    )

    # What was staged for the DB is CIPHERTEXT, never the plaintext.
    assert persisted["api_key"] != "SG.supersecret"
    assert persisted["api_key"].startswith(secret_crypto._MARKER)
    # But the value returned to the internal caller is DECRYPTED.
    assert setting.secrets == {"api_key": "SG.supersecret"}


def test_get_decrypts_secrets_for_internal_callers(fernet_key):
    db = MagicMock()
    stored = secret_crypto.encrypt_secrets({"api_key": "SG.supersecret"})
    row = _row(provider="sendgrid", secrets=stored)
    db.query.return_value.filter.return_value.first.return_value = row

    out = service.get(db, "sendgrid")
    assert out.secrets == {"api_key": "SG.supersecret"}


def test_round_trip_encrypt_store_decrypt_via_service(fernet_key):
    plain = {"api_key": "SG.supersecret", "webhook_signing_key": "whsec_x"}
    stored = secret_crypto.encrypt_secrets(plain)
    # Stored ciphertext is NOT the plaintext.
    for k, v in stored.items():
        assert v != plain[k]
    row = _row(secrets=stored)

    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = row
    out = service.get(db, "sendgrid")
    assert out.secrets == plain


def test_redact_only_exposes_keys_even_on_encrypted_row(fernet_key):
    stored = secret_crypto.encrypt_secrets(
        {"api_key": "SG.supersecret", "webhook_signing_key": "whsec_x"}
    )
    # redact runs on a decrypted row (the service decrypts before redacting),
    # and must still expose ONLY key names — no values, plaintext or ciphertext.
    decrypted_row = _row(secrets=secret_crypto.decrypt_secrets(stored))
    out = service.redact(decrypted_row)
    assert out["secret_keys"] == ["api_key", "webhook_signing_key"]
    assert "SG.supersecret" not in repr(out)
    assert "whsec_x" not in repr(out)
    assert secret_crypto._MARKER not in repr(out)


def test_upsert_noop_when_key_unset_stores_plaintext(monkeypatch):
    # Default dev path: no key -> plaintext pass-through (tests still work).
    monkeypatch.setattr(secret_crypto, "_configured_key", lambda: "")
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    setting = service.upsert(db, provider="twilio", secrets={"auth_token": "tok"})
    assert setting.secrets == {"auth_token": "tok"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
