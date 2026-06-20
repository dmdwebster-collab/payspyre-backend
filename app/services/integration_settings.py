"""Service layer for admin-managed platform integration settings (P8.2).

Pure functions over a SQLAlchemy Session, mirroring
`app.services.credit_products`. Stores per-provider config + credentials so the
team can manage integrations (Didit, Flinks, SendGrid, Twilio, Zumrails,
SignNow, Equifax, Google Analytics) through the admin area instead of the
developer hardcoding creds.

SECURITY — secrets are NEVER returned raw in API output. All read/list paths go
through :func:`redact`, which replaces credential VALUES with the list of key
NAMES that are set.

ENCRYPTION-AT-REST — `secrets` values are envelope-encrypted at the app layer
before persistence (see :mod:`app.core.secret_crypto`). The JSONB column shape
is unchanged. :func:`upsert` encrypts on write; :func:`get` and :func:`list_all`
return rows whose `secrets` have been DECRYPTED for internal callers (adapters
need the real values). :func:`redact` (API output) operates on the decrypted
dict's KEYS only and never exposes values. When `SETTINGS_ENCRYPTION_KEY` is
unset (dev/CI) encryption is a no-op pass-through and values stay plaintext;
pre-encryption plaintext rows are read transparently and upgraded to ciphertext
on their next upsert. Secrets must never be logged or written to platform_events.
"""
from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.secret_crypto import decrypt_secrets, encrypt_secrets
from app.models.platform.integration_settings import PlatformIntegrationSettings


def redact(setting: PlatformIntegrationSettings) -> dict[str, Any]:
    """Return an API-safe dict for a settings row.

    Secret VALUES are dropped entirely; only the sorted list of secret key
    NAMES that are set is exposed (as `secret_keys`). This is the single
    chokepoint that guarantees credential values never reach API responses.
    """
    secrets = setting.secrets or {}
    return {
        "provider": setting.provider,
        "config": setting.config or {},
        "secret_keys": sorted(secrets.keys()),
        "enabled": setting.enabled,
        "updated_by": setting.updated_by,
        "created_at": setting.created_at,
        "updated_at": setting.updated_at,
    }


def _decrypt_row(setting: PlatformIntegrationSettings) -> PlatformIntegrationSettings:
    """Decrypt a row's `secrets` in place so internal callers see real values.

    Adapters need the plaintext credential values; `redact` only ever reads the
    KEYS. Pre-encryption plaintext rows pass through unchanged (decrypt is a
    no-op on non-ciphertext values). Read paths do not commit, so replacing the
    in-memory `secrets` dict does not persist plaintext back to the DB.
    """
    setting.secrets = decrypt_secrets(setting.secrets)
    return setting


def get(db: Session, provider: str) -> Optional[PlatformIntegrationSettings]:
    """Return the settings row for a provider (secrets DECRYPTED), or None."""
    setting = (
        db.query(PlatformIntegrationSettings)
        .filter(PlatformIntegrationSettings.provider == provider)
        .first()
    )
    return _decrypt_row(setting) if setting is not None else None


def list_all(db: Session) -> list[PlatformIntegrationSettings]:
    """Return all configured settings rows (secrets DECRYPTED)."""
    rows = (
        db.query(PlatformIntegrationSettings)
        .order_by(PlatformIntegrationSettings.provider)
        .all()
    )
    return [_decrypt_row(row) for row in rows]


def upsert(
    db: Session,
    provider: str,
    config: Optional[dict[str, Any]] = None,
    secrets: Optional[dict[str, Any]] = None,
    enabled: bool = False,
    updated_by: Optional[UUID] = None,
) -> PlatformIntegrationSettings:
    """Create or replace the settings row for a provider.

    On update, config/secrets/enabled are fully replaced with the supplied
    values (mirrors a PUT). `config` and `secrets` default to empty dicts.

    Supplied `secrets` are envelope-encrypted before persistence (no-op
    pass-through when no key is configured). The returned row carries the
    DECRYPTED secrets so internal callers see the values they just wrote.
    """
    config = config or {}
    # Encrypt the plaintext credential values before they ever touch the DB.
    encrypted_secrets = encrypt_secrets(secrets or {})

    setting = get(db, provider)
    if setting is None:
        setting = PlatformIntegrationSettings(
            provider=provider,
            config=config,
            secrets=encrypted_secrets,
            enabled=enabled,
            updated_by=updated_by,
        )
        db.add(setting)
    else:
        setting.config = config
        setting.secrets = encrypted_secrets
        setting.enabled = enabled
        setting.updated_by = updated_by

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError(
            f"Could not save integration settings for provider '{provider}'"
        ) from exc
    db.refresh(setting)
    # Return decrypted secrets to the caller (refresh reloaded ciphertext).
    return _decrypt_row(setting)
