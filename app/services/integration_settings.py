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
from app.services import integration_mode
from app.schemas.integration_config import (
    SECRET_KEYS,
    IntegrationConfigError,
    provider_config_field_metadata,
    resolve_provider_config,
    validate_provider_config,
)


def redact(setting: PlatformIntegrationSettings) -> dict[str, Any]:
    """Return an API-safe dict for a settings row.

    Secret VALUES are dropped entirely; only the sorted list of secret key
    NAMES that are set is exposed (as `secret_keys`). This is the single
    chokepoint that guarantees credential values never reach API responses.
    """
    secrets = setting.secrets or {}
    mode = integration_mode.normalize_mode(getattr(setting, "mode", None))
    missing_live = integration_mode.missing_live_credentials(
        None, setting.provider, secrets=secrets
    )
    return {
        "provider": setting.provider,
        # SIMULATOR / LIVE — the first-class integration mode. The UI renders a
        # toggle bound to this; ``can_enable_live`` / ``missing_live_credentials``
        # tell it whether the Live position is selectable yet and, if not, why.
        "mode": mode,
        "can_enable_live": not missing_live,
        "missing_live_credentials": missing_live,
        # Behaviour config is READABLE and, for providers with a typed schema
        # (Flinks / Equifax), resolved against its defaults so the admin UI sees
        # every knob even on a row saved before the schema existed. Credentials
        # are not in here — they are in `secrets` and never leave the server.
        "config": resolve_provider_config(setting.provider, setting.config),
        # Per-field editability so the UI can never render a knob as editable
        # when nothing reads it. Fields flagged ``informational`` MUST be shown
        # read-only; every other field names the code that consumes it.
        "config_meta": provider_config_field_metadata(setting.provider),
        "secret_keys": sorted(secrets.keys()),
        "expected_secret_keys": list(SECRET_KEYS.get(setting.provider, ())),
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


def _mirror_flinks_test_mode(provider: str, config: dict[str, Any], mode: str) -> dict[str, Any]:
    """Keep #207's Flinks ``config.test_mode`` in lock-step with ``mode``.

    ``mode`` is authoritative; ``test_mode`` is the legacy knob it subsumed. We
    mirror so any lingering reader of ``test_mode`` sees the same truth as a
    reader of ``mode`` (simulator -> test_mode True, live -> test_mode False).
    """
    if provider != "flinks":
        return config
    config = dict(config or {})
    config["test_mode"] = mode == integration_mode.SIMULATOR
    return config


def upsert(
    db: Session,
    provider: str,
    config: Optional[dict[str, Any]] = None,
    secrets: Optional[dict[str, Any]] = None,
    enabled: bool = False,
    updated_by: Optional[UUID] = None,
    mode: Optional[str] = None,
) -> PlatformIntegrationSettings:
    """Create or replace the settings row for a provider.

    On update, config/secrets/enabled/mode are fully replaced with the supplied
    values (mirrors a PUT). `config` and `secrets` default to empty dicts.
    ``mode`` defaults to the existing row's mode, else ``simulator``.

    Switching to ``live`` requires the provider's credentials to be present (in
    the supplied ``secrets``); this raises ``ValueError`` otherwise so the API
    returns a clear 400 rather than silently enabling an un-credentialed live
    integration. A mode CHANGE is audited to ``platform_events``.

    Supplied `secrets` are envelope-encrypted before persistence (no-op
    pass-through when no key is configured). The returned row carries the
    DECRYPTED secrets so internal callers see the values they just wrote.
    """
    existing = get(db, provider)
    prior_mode = (
        integration_mode.normalize_mode(getattr(existing, "mode", None))
        if existing is not None
        else integration_mode.DEFAULT_MODE
    )
    # Resolve the target mode: explicit value wins, else keep the prior mode.
    try:
        new_mode = integration_mode.normalize_mode(mode) if mode is not None else prior_mode
    except integration_mode.IntegrationModeError as exc:
        raise ValueError(str(exc)) from exc

    # Live requires credentials — validate against the secrets being written.
    if new_mode == integration_mode.LIVE:
        try:
            integration_mode.assert_live_allowed(db, provider, secrets=secrets or {})
        except integration_mode.IntegrationModeError as exc:
            raise ValueError(str(exc)) from exc

    # Providers with a typed behaviour schema (Flinks / Equifax) are validated
    # and stored with defaults materialised; every other provider keeps the
    # free-form dict exactly as before.
    try:
        config = validate_provider_config(provider, config)
    except IntegrationConfigError as exc:
        raise ValueError(str(exc)) from exc
    config = _mirror_flinks_test_mode(provider, config, new_mode)
    # Encrypt the plaintext credential values before they ever touch the DB.
    encrypted_secrets = encrypt_secrets(secrets or {})

    setting = existing
    if setting is None:
        setting = PlatformIntegrationSettings(
            provider=provider,
            config=config,
            secrets=encrypted_secrets,
            enabled=enabled,
            mode=new_mode,
            updated_by=updated_by,
        )
        db.add(setting)
    else:
        setting.config = config
        setting.secrets = encrypted_secrets
        setting.enabled = enabled
        setting.mode = new_mode
        setting.updated_by = updated_by

    if new_mode != prior_mode:
        integration_mode.record_mode_change(
            db,
            provider,
            old_mode=prior_mode,
            new_mode=new_mode,
            actor=str(updated_by) if updated_by else None,
        )

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


def set_mode(
    db: Session,
    provider: str,
    mode: str,
    updated_by: Optional[UUID] = None,
) -> PlatformIntegrationSettings:
    """Flip ONLY a provider's Simulator/Live mode, leaving config/secrets intact.

    The focused toggle behind ``PUT /integration-settings/{provider}/mode``.
    Reuses the stored credentials to validate a switch to live and audits the
    change. Creating a brand-new row this way is allowed (defaults elsewhere);
    switching a not-yet-configured provider to live fails the credential check
    with a clear error, exactly as Dave requires.
    """
    try:
        target = integration_mode.normalize_mode(mode)
    except integration_mode.IntegrationModeError as exc:
        raise ValueError(str(exc)) from exc

    existing = get(db, provider)
    prior_mode = (
        integration_mode.normalize_mode(getattr(existing, "mode", None))
        if existing is not None
        else integration_mode.DEFAULT_MODE
    )
    stored_secrets = dict(getattr(existing, "secrets", None) or {}) if existing else {}

    if target == integration_mode.LIVE:
        try:
            integration_mode.assert_live_allowed(db, provider, secrets=stored_secrets)
        except integration_mode.IntegrationModeError as exc:
            raise ValueError(str(exc)) from exc

    if existing is None:
        setting = PlatformIntegrationSettings(
            provider=provider,
            config=_mirror_flinks_test_mode(provider, {}, target),
            secrets={},
            enabled=False,
            mode=target,
            updated_by=updated_by,
        )
        db.add(setting)
    else:
        existing.mode = target
        existing.config = _mirror_flinks_test_mode(provider, existing.config or {}, target)
        existing.updated_by = updated_by
        # Re-encrypt is unnecessary — secrets untouched; but `get` decrypted them
        # in place, so re-encrypt to avoid persisting plaintext on commit.
        existing.secrets = encrypt_secrets(stored_secrets)
        setting = existing

    if target != prior_mode:
        integration_mode.record_mode_change(
            db,
            provider,
            old_mode=prior_mode,
            new_mode=target,
            actor=str(updated_by) if updated_by else None,
        )

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError(
            f"Could not update mode for provider '{provider}'"
        ) from exc
    db.refresh(setting)
    return _decrypt_row(setting)
