"""GA4 Measurement Protocol — optional server-side product-analytics path.

Companion to ``posthog_bridge`` but a DIFFERENT destination and purpose:

- PostHog (``posthog_bridge``)  = INTERNAL observability of ``platform_events``.
- Google Analytics (this file) = PRODUCT analytics, server-side mirror via the
  GA4 Measurement Protocol (``https://www.google-analytics.com/mp/collect``).

Both can coexist; they are independent fan-outs.

This module is NOT wired into the orchestrator yet — it is a standalone
``send_ga_event(...)`` helper plus its unit test. Wiring (where to call it from)
is reported separately.

PII rules — mirror ``posthog_bridge`` discipline (Hard Rule #6, non-negotiable):

- ``client_id`` is ALWAYS a ``sha256(...)[:16]`` hash of whatever raw id the
  caller passes (e.g. application_id). Never a raw UUID, email, or phone.
- Event ``params`` are passed through a scalar allowlist: only
  ``str``/``int``/``float``/``bool`` values survive, and only for keys the
  caller explicitly supplies. Nested dicts/lists (which could smuggle PII from
  a ``rich_payload``) are dropped. Callers are still expected not to pass PII
  keys — this is defence in depth, not a licence to pass names/emails.

Operational discipline:

- No-op (returns ``False``, zero network I/O) when ``GA_MEASUREMENT_ID`` or
  ``GA_API_SECRET`` is unset, or when ``OBSERVABILITY_ENABLED`` is False.
- Never raises — a GA failure must not break the caller or its DB transaction.
- Uses a short-timeout ``httpx`` POST. The Measurement Protocol returns 2xx
  with an empty body and does not surface validation errors on the prod
  endpoint, so we only care that the request was dispatched.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

_GA_COLLECT_URL = "https://www.google-analytics.com/mp/collect"
_TIMEOUT_SECONDS = 5.0


def _ga_config(db: object = None) -> tuple[bool, str, str]:
    """Return ``(enabled, measurement_id, api_secret)`` from settings.

    Read through this indirection (and via ``getattr`` with defaults) so the GA
    keys can live wherever the team wires them: the enabled settings-area row
    (``PlatformIntegrationSettings``, provider="google_analytics") is preferred,
    with the env ``Settings`` (``GA_MEASUREMENT_ID`` / ``GA_API_SECRET``) as the
    fallback per field. ``db=None`` skips the settings-area lookup (env only),
    keeping call sites without a session working. ``OBSERVABILITY_ENABLED`` is
    always read from env. Tests patch this function rather than the frozen
    pydantic ``Settings`` model.
    """
    from app.core.config import settings
    from app.services.integration_creds import resolve

    enabled = bool(getattr(settings, "OBSERVABILITY_ENABLED", False))
    creds = resolve(
        db, "google_analytics",
        config_keys=["measurement_id", "api_secret"],
        env={"measurement_id": "GA_MEASUREMENT_ID", "api_secret": "GA_API_SECRET"},
    )
    return enabled, creds["measurement_id"], creds["api_secret"]


def _hashed_client_id(raw_id: object) -> str:
    """Return first 16 hex chars of ``sha256(str(raw_id))``.

    Identical discipline to ``posthog_bridge._hashed_id`` — never emit a raw
    UUID/email/phone as the GA client_id.
    """
    return hashlib.sha256(str(raw_id).encode()).hexdigest()[:16]


def _safe_params(params: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Keep only scalar param values (str/int/float/bool).

    Drops ``None`` and any nested dict/list — the same shape-based allowlist as
    ``posthog_bridge._safe_properties``, so a nested ``rich_payload`` cannot
    leak PII into a GA event.
    """
    if not params:
        return {}
    out: dict[str, Any] = {}
    for key, value in params.items():
        # bool is a subclass of int — listing it first is harmless but explicit.
        if isinstance(value, (bool, int, float, str)):
            out[key] = value
    return out


def send_ga_event(
    *,
    event_name: str,
    raw_client_id: object,
    params: Optional[dict[str, Any]] = None,
    client: Optional[httpx.Client] = None,
    db: object = None,
) -> bool:
    """Send a single event to GA4 via the Measurement Protocol.

    Returns ``True`` if a request was dispatched, ``False`` on no-op (feature
    disabled / unconfigured) or on swallowed failure. Never raises.

    ``raw_client_id`` is hashed before transmission; ``params`` are scrubbed to
    scalars. Pass an ``httpx.Client`` to reuse a connection pool (and for tests
    to mount a respx transport). Pass ``db`` to resolve GA creds from the
    settings area; ``db=None`` falls back to env-only.
    """
    try:
        enabled, measurement_id, api_secret = _ga_config(db)

        if not enabled:
            return False
        if not measurement_id or not api_secret:
            return False

        payload = {
            "client_id": _hashed_client_id(raw_client_id),
            "events": [
                {
                    "name": event_name,
                    "params": _safe_params(params),
                }
            ],
        }

        owns_client = client is None
        http = client or httpx.Client(timeout=_TIMEOUT_SECONDS)
        try:
            http.post(
                _GA_COLLECT_URL,
                params={
                    "measurement_id": measurement_id,
                    "api_secret": api_secret,
                },
                json=payload,
            )
        finally:
            if owns_client:
                http.close()
        return True

    except Exception:  # noqa: BLE001
        # GA failure must never propagate — mirror posthog_bridge: log at
        # WARNING so it surfaces in Sentry/CloudWatch without breaking callers.
        logger.warning(
            "ga_measurement_protocol.send_ga_event failed silently", exc_info=True
        )
        return False
