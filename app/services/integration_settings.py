"""Service layer for admin-managed platform integration settings (P8.2).

Pure functions over a SQLAlchemy Session, mirroring
`app.services.credit_products`. Stores per-provider config + credentials so the
team can manage integrations (Didit, Flinks, SendGrid, Twilio, Zumrails,
SignNow, Equifax, Google Analytics) through the admin area instead of the
developer hardcoding creds.

SECURITY — secrets are NEVER returned raw in API output. All read/list paths go
through :func:`redact`, which replaces credential VALUES with the list of key
NAMES that are set. `secrets` is also stored as plaintext JSONB today
(encryption-at-rest gap, documented on the model/migration) — so it must never
be logged or written to platform_events.
"""
from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

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


def get(db: Session, provider: str) -> Optional[PlatformIntegrationSettings]:
    """Return the settings row for a provider, or None if not configured."""
    return (
        db.query(PlatformIntegrationSettings)
        .filter(PlatformIntegrationSettings.provider == provider)
        .first()
    )


def list_all(db: Session) -> list[PlatformIntegrationSettings]:
    """Return all configured integration settings rows."""
    return (
        db.query(PlatformIntegrationSettings)
        .order_by(PlatformIntegrationSettings.provider)
        .all()
    )


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
    """
    config = config or {}
    secrets = secrets or {}

    setting = get(db, provider)
    if setting is None:
        setting = PlatformIntegrationSettings(
            provider=provider,
            config=config,
            secrets=secrets,
            enabled=enabled,
            updated_by=updated_by,
        )
        db.add(setting)
    else:
        setting.config = config
        setting.secrets = secrets
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
    return setting
