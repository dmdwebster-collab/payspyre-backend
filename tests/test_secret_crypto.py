"""Unit tests for app-layer envelope encryption of credential secrets.

No DB, no network. We toggle the encryption key on the live ``settings`` object
(restored by a fixture) to exercise both the configured and the unset (dev
no-op) paths. A freshly generated Fernet key is used so the suite never depends
on real key material.
"""
import pytest
from cryptography.fernet import Fernet, InvalidToken

from app.core import secret_crypto


@pytest.fixture
def fernet_key(monkeypatch):
    """Configure a fresh Fernet key and reset the warn-once flag.

    Patches the ``_configured_key`` indirection rather than the pydantic
    Settings instance (which rejects unknown fields) so tests run regardless of
    whether SETTINGS_ENCRYPTION_KEY has been wired into config.py yet.
    """
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setattr(secret_crypto, "_configured_key", lambda: key)
    monkeypatch.setattr(secret_crypto, "_warned_no_key", False, raising=False)
    return key


@pytest.fixture
def no_key(monkeypatch):
    """Force the unset-key (dev no-op) path."""
    monkeypatch.setattr(secret_crypto, "_configured_key", lambda: "")
    monkeypatch.setattr(secret_crypto, "_warned_no_key", False, raising=False)


MARKER = secret_crypto._MARKER


# --------------------------------------------------------------------------
# round-trip with a configured key
# --------------------------------------------------------------------------


def test_round_trip_encrypt_then_decrypt(fernet_key):
    plain = {"api_key": "SG.supersecret", "auth_token": "tok_123"}
    encrypted = secret_crypto.encrypt_secrets(plain)
    assert secret_crypto.decrypt_secrets(encrypted) == plain


def test_ciphertext_is_not_plaintext(fernet_key):
    plain = {"api_key": "SG.supersecret"}
    encrypted = secret_crypto.encrypt_secrets(plain)
    # Same keys, but the value is a marked token, not the raw secret.
    assert set(encrypted.keys()) == {"api_key"}
    assert encrypted["api_key"] != "SG.supersecret"
    assert "SG.supersecret" not in encrypted["api_key"]
    assert encrypted["api_key"].startswith(MARKER)


def test_distinct_ciphertexts_for_repeated_plaintext(fernet_key):
    # Fernet uses a random IV, so encrypting twice yields different tokens.
    a = secret_crypto.encrypt_secrets({"k": "same"})["k"]
    b = secret_crypto.encrypt_secrets({"k": "same"})["k"]
    assert a != b


# --------------------------------------------------------------------------
# idempotency — no double-wrapping
# --------------------------------------------------------------------------


def test_encrypt_is_idempotent_no_double_wrap(fernet_key):
    plain = {"api_key": "secret"}
    once = secret_crypto.encrypt_secrets(plain)
    twice = secret_crypto.encrypt_secrets(once)
    # Already-marked values are left untouched (not re-encrypted).
    assert twice == once
    # And it still decrypts back to the original plaintext.
    assert secret_crypto.decrypt_secrets(twice) == plain


def test_decrypt_passes_through_plaintext_values(fernet_key):
    # A row written before encryption (no marker) decrypts transparently.
    mixed = {"plain_old": "value", "encrypted": secret_crypto.encrypt_secrets({"x": "v"})["x"] }
    out = secret_crypto.decrypt_secrets(mixed)
    assert out["plain_old"] == "value"
    assert out["encrypted"] == "v"


# --------------------------------------------------------------------------
# no-op pass-through when the key is unset (dev)
# --------------------------------------------------------------------------


def test_encrypt_is_noop_when_key_unset(no_key):
    plain = {"api_key": "SG.supersecret"}
    out = secret_crypto.encrypt_secrets(plain)
    assert out == plain
    assert not out["api_key"].startswith(MARKER)


def test_decrypt_is_noop_when_key_unset(no_key):
    stored = {"api_key": "SG.supersecret"}
    assert secret_crypto.decrypt_secrets(stored) == stored


def test_decrypt_raises_if_key_removed_after_encryption(fernet_key, monkeypatch):
    encrypted = secret_crypto.encrypt_secrets({"api_key": "secret"})
    # Simulate the key being unset after data was already encrypted.
    monkeypatch.setattr(secret_crypto, "_configured_key", lambda: "")
    with pytest.raises(InvalidToken):
        secret_crypto.decrypt_secrets(encrypted)


def test_decrypt_raises_on_wrong_key(fernet_key, monkeypatch):
    encrypted = secret_crypto.encrypt_secrets({"api_key": "secret"})
    other = Fernet.generate_key().decode("utf-8")
    monkeypatch.setattr(secret_crypto, "_configured_key", lambda: other)
    with pytest.raises(InvalidToken):
        secret_crypto.decrypt_secrets(encrypted)


# --------------------------------------------------------------------------
# empty / None handling
# --------------------------------------------------------------------------


def test_empty_and_none_inputs(fernet_key):
    assert secret_crypto.encrypt_secrets({}) == {}
    assert secret_crypto.encrypt_secrets(None) == {}
    assert secret_crypto.decrypt_secrets({}) == {}
    assert secret_crypto.decrypt_secrets(None) == {}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
