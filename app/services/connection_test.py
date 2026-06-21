"""Per-provider connection tests for the integration settings area.

Lets the team validate credentials right after entering them in the settings area
(Dave's mandate) — a cheap, **auth-only, side-effect-free** call per provider
(token fetch / scopes read / account read). It NEVER sends a message, pulls a
bureau report, creates a verification session, or moves money, and it NEVER echoes
secret material: the returned ``reason`` is a fixed, redacted sentence + HTTP
status class only.

Credentials are read from the settings-area row (``integration_settings.get``),
falling back to env ``settings.*`` for providers still configured via env. This is
also the first step of the cred-source unification — the test path reads the
settings area regardless of where the live adapter currently reads from.

Providers whose real auth HTTP path isn't implemented yet (Didit/Flinks) or whose
health endpoint needs vendor confirmation (Equifax/TransUnion) return a clear
"not yet available" result rather than a misleading pass/fail.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.services import integration_settings

logger = get_logger(__name__)

_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)


@dataclass(frozen=True)
class ConnectionTestResult:
    ok: bool
    reason: str  # redacted — never contains secret material
    checked_at: datetime


class _ProbeError(Exception):
    """A probe failure carrying a pre-redacted, user-safe reason."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _need(d: dict, *keys: str, env: str | None = None) -> str:
    """Read the first present key from the settings dict, else the env fallback."""
    for k in keys:
        v = (d or {}).get(k)
        if v:
            return str(v)
    if env:
        v = getattr(settings, env, "")
        if v:
            return str(v)
    raise _ProbeError("A required credential or config value is missing — fill it in and retry.")


def _status_reason(status_code: int) -> str:
    if status_code in (401, 403):
        return "Authentication was rejected by the provider — check the credentials."
    if status_code == 404:
        return "Reached the provider, but the endpoint was not found."
    if 500 <= status_code < 600:
        return "The provider returned a server error — try again shortly."
    return f"The provider returned an unexpected status ({status_code})."


def _check_2xx(resp: httpx.Response) -> None:
    if resp.status_code // 100 != 2:
        raise _ProbeError(_status_reason(resp.status_code))


# --- per-provider probes (auth-only, no side effects) ----------------------


def _probe_zumrails(config: dict, secrets: dict) -> None:
    from app.services.payments.zumrails_auth import (
        PermanentZumrailsAuthError,
        TransientZumrailsAuthError,
        get_zumrails_token,
    )

    try:
        get_zumrails_token(
            api_key=_need(secrets, "api_key"),
            api_secret=_need(secrets, "api_secret"),
            base_url=_need(config, "base_url", "api_base_url"),
            force_refresh=True,
        )
    except PermanentZumrailsAuthError:
        raise _ProbeError("Authentication was rejected by Zumrails — check the api key/secret.")
    except TransientZumrailsAuthError:
        raise _ProbeError("Could not reach Zumrails — try again shortly.")


def _probe_signnow(config: dict, secrets: dict) -> None:
    from app.services.esign.signnow_oauth import (
        SignNowAuthPermanentError,
        SignNowAuthTransientError,
        get_signnow_token,
    )

    try:
        get_signnow_token(
            client_id=_need(secrets, "client_id"),
            client_secret=_need(secrets, "client_secret"),
            username=_need(secrets, "username"),
            password=_need(secrets, "password"),
            base_url=(config or {}).get("base_url") or "https://api.signnow.com",
            force_refresh=True,
        )
    except SignNowAuthPermanentError:
        raise _ProbeError("Authentication was rejected by SignNow — check the client/user credentials.")
    except SignNowAuthTransientError:
        raise _ProbeError("Could not reach SignNow — try again shortly.")


def _probe_sendgrid(config: dict, secrets: dict) -> None:
    # GET /v3/scopes validates the API key without sending anything.
    api_key = _need(secrets, "api_key", env="SENDGRID_API_KEY")
    resp = httpx.get(
        "https://api.sendgrid.com/v3/scopes",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=_TIMEOUT,
    )
    _check_2xx(resp)


def _probe_twilio(config: dict, secrets: dict) -> None:
    # GET the Account resource validates SID + auth token without sending an SMS.
    sid = _need(secrets, "account_sid", env="TWILIO_ACCOUNT_SID")
    token = _need(secrets, "auth_token", env="TWILIO_AUTH_TOKEN")
    resp = httpx.get(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json",
        auth=(sid, token),
        timeout=_TIMEOUT,
    )
    _check_2xx(resp)


def _probe_ga(config: dict, secrets: dict) -> None:
    # The Measurement Protocol /debug/mp/collect validates the keys + payload and
    # records nothing. It returns validationMessages for a bad id/secret.
    mid = _need(config, "measurement_id", env="GA_MEASUREMENT_ID")
    secret = _need(secrets, "api_secret", env="GA_API_SECRET")
    resp = httpx.post(
        "https://www.google-analytics.com/debug/mp/collect",
        params={"measurement_id": mid, "api_secret": secret},
        json={"client_id": "connection-test", "events": [{"name": "connection_test"}]},
        timeout=_TIMEOUT,
    )
    _check_2xx(resp)
    try:
        messages = resp.json().get("validationMessages") or []
    except Exception:  # noqa: BLE001 — non-JSON body just means we can't introspect
        messages = []
    if messages:
        raise _ProbeError("Google Analytics rejected the measurement id / api secret.")


def _probe_pending(config: dict, secrets: dict) -> None:
    raise _ProbeError(
        "A connection test for this provider is not available yet — its auth "
        "endpoint is pending implementation/vendor confirmation."
    )


_PROBES: dict[str, Callable[[dict, dict], None]] = {
    "zumrails": _probe_zumrails,
    "signnow": _probe_signnow,
    "sendgrid": _probe_sendgrid,
    "twilio": _probe_twilio,
    "ga": _probe_ga,
    "google_analytics": _probe_ga,
    # Real auth HTTP path not implemented yet / endpoint pending vendor confirmation:
    "didit": _probe_pending,
    "flinks": _probe_pending,
    "equifax": _probe_pending,
    "transunion": _probe_pending,
}

SUPPORTED_PROVIDERS = sorted(_PROBES.keys())


def test_connection(db, provider: str) -> ConnectionTestResult:
    """Validate a provider's stored credentials with a cheap auth-only call.

    Reads creds from the settings-area row (env fallback) and returns a structured,
    secret-free result. Never raises."""
    now = datetime.now(timezone.utc)
    probe = _PROBES.get(provider)
    if probe is None:
        return ConnectionTestResult(False, f"Unknown provider '{provider}'.", now)

    row = integration_settings.get(db, provider)
    config = dict(getattr(row, "config", None) or {}) if row is not None else {}
    secrets = dict(getattr(row, "secrets", None) or {}) if row is not None else {}

    try:
        probe(config, secrets)
        return ConnectionTestResult(True, "Authenticated successfully.", now)
    except _ProbeError as exc:
        logger.info("connection_test_failed", provider=provider, reason=exc.reason)
        return ConnectionTestResult(False, exc.reason, now)
    except httpx.TimeoutException:
        return ConnectionTestResult(False, "Timed out reaching the provider.", now)
    except httpx.HTTPError:
        return ConnectionTestResult(False, "Could not reach the provider.", now)
    except Exception:  # noqa: BLE001 — never leak an internal error / secret to the caller
        logger.exception("connection_test_error", provider=provider)
        return ConnectionTestResult(False, "Unexpected error during the connection test.", now)
