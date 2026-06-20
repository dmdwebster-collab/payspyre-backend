"""Encryption-at-rest for the borrower's Social Insurance Number (SIN).

The SIN is the single most sensitive PII field in the system (audit C-3). It is
NEVER logged, NEVER returned in any API response, and NEVER written to
``platform_events`` or any structured/analytics field. Only ``sin_last3`` is ever
surfaced. This module is the ONLY place the full SIN is encrypted/decrypted.

Scheme — Fernet envelope encryption, mirroring :mod:`app.core.secret_crypto`:
  * The key comes from a DEDICATED setting ``settings.SIN_ENCRYPTION_KEY`` — a
    urlsafe-base64 32-byte Fernet key, SEPARATE from ``SETTINGS_ENCRYPTION_KEY``
    (Hard Rule #7: SIN gets its own key so the integration-credentials key and the
    SIN key can be rotated / scoped independently and a compromise of one does not
    expose the other).
  * Fernet provides AES-128-CBC + HMAC-SHA256 authenticated encryption with a
    random IV per value, so the same SIN encrypts to different ciphertexts and
    tampering is detected on decrypt.
  * The stored value is ``"<MARKER><fernet_token>"`` where the marker
    (``"sinv1:"``) flags the value as encrypted (so we never double-encrypt) and
    versions the scheme for future rotation.

Column type (audit C-3 reconciliation): ``platform_patients.sin_encrypted`` is a
TEXT column storing this marked Fernet token as a UTF-8 string. The original
migration created it as ``BYTEA`` while the model declared ``String`` — a type
drift that would corrupt any real read/write. We reconcile on the STRING/TEXT
side: the model stays ``String`` and migration ``030_sin_column_reconcile`` ALTERs
the DB column ``BYTEA -> TEXT``. Rationale: the Fernet token is already an ASCII
string, so TEXT is the natural fit, it matches the proven ``secret_crypto``
pattern, and it avoids bytes<->str juggling in the ORM. (We did NOT adopt
pgcrypto: app-layer Fernet keeps the key out of the database entirely.)

Dev / unset-key behaviour — NO-OP PASS-THROUGH (same contract as secret_crypto):
  When ``SIN_ENCRYPTION_KEY`` is empty (default for local dev / CI),
  :func:`encrypt_sin` stores the plaintext SIN unchanged and :func:`decrypt_sin`
  returns it unchanged. A single warning is logged the first time encryption is
  skipped. This keeps dev / tests working with zero configuration; encryption
  engages only once a key is configured. Production MUST set the key (the SIN is
  too sensitive to ever persist in plaintext in prod).
"""
from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

logger = logging.getLogger(__name__)

# Prefix marking a value as SIN ciphertext produced by this module. Distinct from
# secret_crypto's "encv1:" so the two schemes/keys can never be confused.
_MARKER = "sinv1:"

# Ensures the "encryption disabled" warning is emitted at most once per process.
_warned_no_key = False


def _configured_key() -> str:
    """Return the configured SIN Fernet key string ("" when unset).

    Reads ``settings.SIN_ENCRYPTION_KEY`` via getattr so this module is safe even
    if the field is not yet wired into Settings. Tests patch this function to
    toggle the key without mutating the pydantic Settings instance.
    """
    return (getattr(settings, "SIN_ENCRYPTION_KEY", "") or "").strip()


def _get_fernet() -> Fernet | None:
    """Return a Fernet built from the configured SIN key, or None when unset.

    None signals callers to fall back to no-op pass-through. The first time this
    happens a warning is logged (once per process).
    """
    global _warned_no_key
    key = _configured_key()
    if not key:
        if not _warned_no_key:
            logger.warning(
                "SIN_ENCRYPTION_KEY is unset — SIN values are stored in PLAINTEXT "
                "(encryption-at-rest disabled). Set a dedicated Fernet key to "
                "enable encryption. This MUST be set in production."
            )
            _warned_no_key = True
        return None
    return Fernet(key.encode("utf-8"))


def _is_encrypted(value: str | None) -> bool:
    """True if ``value`` is a string already wrapped by this module."""
    return isinstance(value, str) and value.startswith(_MARKER)


def encrypt_sin(plain: str | None) -> str | None:
    """Encrypt a bare 9-digit SIN to a marked Fernet token for storage.

    Returns the stored representation (``"sinv1:<token>"``) when a key is
    configured, the plaintext unchanged when the key is unset (dev no-op), and
    ``None`` for a ``None``/empty input. Already-encrypted input (carrying the
    marker) is returned as-is so re-encryption never double-wraps.

    The input is expected to be the normalized 9 digits (see
    :func:`app.core.validation.validate_sin`'s caller). This function does not
    validate — validation is the endpoint's responsibility.
    """
    if not plain:
        return None
    if _is_encrypted(plain):
        return plain
    fernet = _get_fernet()
    if fernet is None:
        # No-op pass-through (dev / unset key): store plaintext unchanged.
        return plain
    token = fernet.encrypt(plain.encode("utf-8")).decode("utf-8")
    return f"{_MARKER}{token}"


def decrypt_sin(stored: str | None) -> str | None:
    """Decrypt a stored SIN token back to the bare 9-digit SIN.

    Values WITHOUT the marker are returned verbatim — this makes dev no-op rows
    (and any pre-encryption plaintext) read transparently. Returns ``None`` for a
    ``None``/empty input.

    Raises :class:`cryptography.fernet.InvalidToken` if a marked value cannot be
    decrypted with the configured key (wrong/rotated key, missing key, or
    tampering) — callers treat this as a hard failure, never silent garbage.

    SECURITY: the returned plaintext SIN MUST NOT be logged, persisted, or placed
    in any response. The only legitimate caller is the bureau-pull assembly.
    """
    if not stored:
        return None
    if not _is_encrypted(stored):
        # Plaintext / dev no-op value: pass through unchanged.
        return stored
    fernet = _get_fernet()
    if fernet is None:
        # Key was removed after data was encrypted — cannot recover.
        raise InvalidToken(
            "SIN is encrypted but SIN_ENCRYPTION_KEY is unset"
        )
    token = stored[len(_MARKER):].encode("utf-8")
    return fernet.decrypt(token).decode("utf-8")


def get_sin_for_bureau(patient: object) -> str | None:
    """Decrypt a patient's stored SIN for use at CREDIT-BUREAU-PULL TIME ONLY.

    Returns the bare 9-digit plaintext SIN, or ``None`` if the patient has no SIN
    on file (declined / never collected). This is the single sanctioned read path
    for the full SIN.

    SECURITY (Hard Rules #6/#7): the returned plaintext is the most sensitive PII
    in the system. The caller MUST NOT log it, persist it, place it in
    ``platform_events`` / analytics, or include it in any response — it flows
    straight into the bureau HTTP request body and nowhere else. Decryption
    failure (wrong/missing key, tampering) raises ``InvalidToken``; the caller
    treats that as a hard failure, not a silent skip.

    Takes the ``PlatformPatient`` row (duck-typed via ``sin_encrypted``) rather
    than the DB so it stays pure and testable.
    """
    stored = getattr(patient, "sin_encrypted", None)
    return decrypt_sin(stored)
