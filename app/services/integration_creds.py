"""Resolve a provider's effective credentials/config from the settings area, with
an env fallback.

Dave's mandate is that the team enters all integration creds through the settings
area (``platform_integration_settings``). Several adapters historically read from
env ``settings.*`` instead. ``resolve()`` lets a builder prefer the enabled
settings-area row and fall back to env per field — so entering creds in the
settings area takes effect immediately, while existing env config keeps working
until a row is created.

Never logs secret values.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.services import integration_settings


def resolve(
    db: Optional[Session],
    provider: str,
    *,
    secret_keys: Iterable[str] = (),
    config_keys: Iterable[str] = (),
    env: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Return a flat ``{key: value}`` of the requested secret + config values.

    Preference order per key: the enabled settings-area row, then the env fallback
    in ``env`` (mapping result-key -> ``settings`` attribute name). Missing values
    come back as ``""``. ``db=None`` skips the settings-area lookup (env only),
    which keeps construction working in contexts without a session.
    """
    row = None
    if db is not None:
        candidate = integration_settings.get(db, provider)
        if candidate is not None and getattr(candidate, "enabled", False):
            row = candidate

    secrets = (getattr(row, "secrets", None) or {}) if row is not None else {}
    config = (getattr(row, "config", None) or {}) if row is not None else {}

    out: dict[str, str] = {}
    for k in secret_keys:
        out[k] = str(secrets.get(k) or "")
    for k in config_keys:
        out[k] = str(config.get(k) or "")

    if env:
        for result_key, env_attr in env.items():
            if not out.get(result_key):
                out[result_key] = str(getattr(settings, env_attr, "") or "")

    return out
