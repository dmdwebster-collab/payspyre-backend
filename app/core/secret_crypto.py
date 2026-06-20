"""App-layer envelope encryption for integration credential values.

Closes the encryption-at-rest gap on
``platform_integration_settings.secrets`` (plaintext JSONB) WITHOUT a schema
change. The JSONB column keeps the same shape — a flat ``{key: value}`` map of
credential names to values — but each *value* is replaced with a versioned,
marked ciphertext string before persistence and decrypted on read.

Scheme — envelope encryption with :class:`cryptography.fernet.Fernet`:
  * The key comes from ``settings.SETTINGS_ENCRYPTION_KEY`` (a urlsafe-base64
    32-byte Fernet key). Fernet provides AES-128-CBC + HMAC-SHA256 authenticated
    encryption with a random IV per value, so identical plaintexts produce
    distinct ciphertexts and tampering is detected on decrypt.
  * Each encrypted value is stored as ``"<MARKER><fernet_token>"`` where the
    marker (``"encv1:"``) both flags the value as encrypted (so we never
    double-encrypt) and versions the scheme for future rotation.

Dev / unset-key behaviour — NO-OP PASS-THROUGH:
  When ``SETTINGS_ENCRYPTION_KEY`` is empty (the default for local dev / CI),
  :func:`encrypt_secrets` stores plaintext unchanged and :func:`decrypt_secrets`
  returns input unchanged. A single warning is logged the first time encryption
  is skipped so the gap is visible in logs without spamming them. This keeps
  dev / tests working with zero configuration; encryption only engages once a
  key is configured.

Operational note — MIXED rows are safe. ``decrypt_secrets`` decrypts only
values carrying the marker and returns everything else verbatim, so existing
plaintext rows written before a key was configured are read transparently and
are migrated to ciphertext on their next ``upsert``.
"""
from __future__ import annotations

import logging
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

logger = logging.getLogger(__name__)

# Prefix marking a value as ciphertext produced by this module. Doubles as a
# scheme version tag so the format can evolve (encv2:, ...) without ambiguity.
_MARKER = "encv1:"

# Ensures the "encryption disabled" warning is emitted at most once per process.
_warned_no_key = False


def _configured_key() -> str:
    """Return the configured Fernet key string ("" when unset).

    Indirection point: reads ``settings.SETTINGS_ENCRYPTION_KEY`` via getattr so
    this module is safe before that field is wired into Settings (config.py is
    owned elsewhere — see WIRING NEEDED). Tests patch this function to toggle the
    key without mutating the pydantic Settings instance.
    """
    return (getattr(settings, "SETTINGS_ENCRYPTION_KEY", "") or "").strip()


def _get_fernet() -> Fernet | None:
    """Return a Fernet built from the configured key, or None when unset.

    Returning None signals callers to fall back to no-op pass-through. The
    first time this happens a warning is logged (once per process).
    """
    global _warned_no_key
    key = _configured_key()
    if not key:
        if not _warned_no_key:
            logger.warning(
                "SETTINGS_ENCRYPTION_KEY is unset — integration credential "
                "secrets are stored in PLAINTEXT (encryption-at-rest disabled). "
                "Set a Fernet key to enable encryption."
            )
            _warned_no_key = True
        return None
    return Fernet(key.encode("utf-8"))


def _is_encrypted(value: Any) -> bool:
    """True if ``value`` is a string already wrapped by this module."""
    return isinstance(value, str) and value.startswith(_MARKER)


def encrypt_secrets(plain: dict[str, Any] | None) -> dict[str, Any]:
    """Return a copy of ``plain`` with each value encrypted to a marked token.

    Shape is preserved (same keys). When the key is unset, returns the input
    values unchanged (no-op pass-through). Already-encrypted values (carrying
    the marker) are left as-is, so re-encrypting an already-encrypted dict is
    idempotent and never double-wraps.

    Non-string values are JSON-coercible scalars in practice; each value is
    serialized to its ``str`` form before encryption. (Credential values are
    strings; this is a defensive fallback, not a structured-secret feature.)
    """
    if not plain:
        return {}

    fernet = _get_fernet()
    if fernet is None:
        # No-op pass-through (dev / unset key): store plaintext unchanged.
        return dict(plain)

    out: dict[str, Any] = {}
    for key, value in plain.items():
        if _is_encrypted(value):
            # Idempotent: do not double-encrypt an already-marked value.
            out[key] = value
            continue
        token = fernet.encrypt(str(value).encode("utf-8")).decode("utf-8")
        out[key] = f"{_MARKER}{token}"
    return out


def decrypt_secrets(stored: dict[str, Any] | None) -> dict[str, Any]:
    """Return a copy of ``stored`` with marked values decrypted to plaintext.

    Values WITHOUT the marker are returned verbatim — this makes pre-encryption
    plaintext rows (and dev no-op rows) read transparently. When the key is
    unset, returns input unchanged.

    Raises :class:`cryptography.fernet.InvalidToken` if a marked value cannot be
    decrypted with the configured key (wrong/rotated key or tampering) — callers
    should treat that as a hard failure rather than silently returning garbage.
    """
    if not stored:
        return {}

    fernet = _get_fernet()

    out: dict[str, Any] = {}
    for key, value in stored.items():
        if not _is_encrypted(value):
            # Plaintext / non-string value: pass through unchanged.
            out[key] = value
            continue
        if fernet is None:
            # Key was removed after data was encrypted — cannot recover.
            raise InvalidToken(
                f"secret '{key}' is encrypted but SETTINGS_ENCRYPTION_KEY is unset"
            )
        token = value[len(_MARKER):].encode("utf-8")
        out[key] = fernet.decrypt(token).decode("utf-8")
    return out
