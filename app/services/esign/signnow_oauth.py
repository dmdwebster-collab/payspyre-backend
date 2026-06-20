"""SignNow OAuth2 token-acquisition layer (token cache + refresh).

``signnow_adapter.SignNowAdapter`` is deliberately side-effect free: it takes a
ready bearer access token via ``api_key`` and never fetches or refreshes it (see
its module docstring, AUTH MODEL). This module is that missing layer — the
wiring code calls :func:`get_signnow_token` to obtain a cached bearer token and
constructs the adapter with it::

    token = get_signnow_token(
        client_id=..., client_secret=..., username=..., password=...,
        base_url="https://api.signnow.com",
    )
    adapter = SignNowAdapter(api_key=token, base_url="https://api.signnow.com")

Style mirrors ``app.services.real_notification_dispatcher`` /
``app.services.payments.zumrails_adapter``: a thin httpx client, a
transient-vs-permanent error hierarchy, and **no credentials in logs or
exception messages** (client_secret / username / password never appear in a
raised error — only the HTTP status and a short, non-secret response snippet).

=============================================================================
SignNow OAuth2 ASSUMPTIONS  (isolated constants — easy to correct vs real docs)
=============================================================================
Docs: https://docs.signnow.com/

GRANT — SignNow's REST OAuth2 token endpoint:
  ``POST {base_url}/oauth2/token``
  with an HTTP Basic ``Authorization: Basic base64(client_id:client_secret)``
  header (the *application* client credential) and a form-encoded
  (``application/x-www-form-urlencoded``) body carrying a **password grant**::

      grant_type=password
      username=<account email>
      password=<account password>
      scope=*

  The response is JSON::

      {"access_token": "<bearer>", "token_type": "bearer",
       "expires_in": 2592000, "refresh_token": "<refresh>", "scope": "*"}

  ``access_token`` is what the adapter consumes. ``expires_in`` is the token
  lifetime in **seconds**; SignNow tokens are typically long-lived (~30 days)
  but we cache by the value the server returns rather than hardcoding.

  REFRESH — when a cached token is within :data:`_EXPIRY_SKEW_SECONDS` of expiry
  and the previous response carried a ``refresh_token``, we attempt a
  ``grant_type=refresh_token`` exchange (same endpoint, same Basic header). If
  refresh fails or no refresh token is held, we fall back to a fresh password
  grant. Both paths funnel through one private fetch.

  ⚠ If your account uses a different grant (e.g. authorization_code) or the
  endpoint/field names differ, the constants below (``_TOKEN_PATH``,
  ``_GRANT_PASSWORD``, ``_GRANT_REFRESH``, ``_SCOPE``) are the only knobs to
  change.

BASE URL
  Production: ``https://api.signnow.com``
  Sandbox:    ``https://api-eval.signnow.com``
  Passed in by the wiring layer (env selection lives there).

CLOCK — the cache TTL is computed against real wall-clock ``time.monotonic()``
for elapsed-time math (immune to system clock jumps) plus
``datetime.now(timezone.utc)`` only for human-readable expiry logging. Real
``time``/``Date.now``-style calls are correct here: this is app code that must
reflect actual elapsed time, not a deterministic test seam.
"""
from __future__ import annotations

import base64
import threading
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Isolated SignNow OAuth2 assumptions (see module docstring)
# ---------------------------------------------------------------------------

_TOKEN_PATH = "/oauth2/token"
_GRANT_PASSWORD = "password"
_GRANT_REFRESH = "refresh_token"
_SCOPE = "*"

#: Refresh a cached token this many seconds BEFORE its real expiry, so an
#: in-flight adapter request never races the boundary.
_EXPIRY_SKEW_SECONDS = 300

#: Fallback TTL (seconds) when the token response omits ``expires_in``.
_DEFAULT_TTL_SECONDS = 1800

_DEFAULT_TIMEOUT = 15.0


# ---------------------------------------------------------------------------
# Error hierarchy (transient vs permanent — mirrors the adapters)
# ---------------------------------------------------------------------------


class SignNowAuthError(Exception):
    """Base class for SignNow token-acquisition failures (never carries creds)."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SignNowAuthTransientError(SignNowAuthError):
    """Retryable — vendor 5xx / 429 / network / timeout. A caller retry is plausible."""


class SignNowAuthPermanentError(SignNowAuthError):
    """Non-retryable — 4xx (bad client cred, bad username/password). Retry won't help."""


# ---------------------------------------------------------------------------
# Cache primitives
# ---------------------------------------------------------------------------


@dataclass
class _CachedToken:
    access_token: str
    refresh_token: Optional[str]
    #: monotonic deadline (seconds) at which the token is considered expired.
    expires_at_monotonic: float

    def is_fresh(self, now_monotonic: float) -> bool:
        return now_monotonic < (self.expires_at_monotonic - _EXPIRY_SKEW_SECONDS)


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


class SignNowTokenProvider:
    """In-process, thread-safe SignNow OAuth2 token cache with refresh.

    One instance per credential set caches a single token and refreshes it as
    expiry approaches. ``get_token()`` is the only public surface; it is safe to
    call from multiple worker threads — a lock serializes the (rare) fetch so we
    don't stampede SignNow on a cold/expired cache.
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        username: str,
        password: str,
        base_url: str,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._username = username
        self._password = password
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._lock = threading.Lock()
        self._cached: Optional[_CachedToken] = None

    def get_token(self, *, force_refresh: bool = False) -> str:
        """Return a fresh bearer access token, fetching/refreshing if needed."""
        now = time.monotonic()
        cached = self._cached
        if not force_refresh and cached is not None and cached.is_fresh(now):
            return cached.access_token

        with self._lock:
            # Re-check under the lock — another thread may have just refreshed.
            now = time.monotonic()
            cached = self._cached
            if not force_refresh and cached is not None and cached.is_fresh(now):
                return cached.access_token

            # Prefer a refresh_token exchange when we hold one; else password grant.
            if cached is not None and cached.refresh_token:
                try:
                    self._cached = self._fetch(
                        grant_type=_GRANT_REFRESH,
                        refresh_token=cached.refresh_token,
                    )
                    return self._cached.access_token
                except SignNowAuthError:
                    # Refresh path failed (expired/revoked refresh token) — fall
                    # back to a full password grant. No creds in the log line.
                    logger.info("signnow_token_refresh_fallback_to_password")

            self._cached = self._fetch(grant_type=_GRANT_PASSWORD)
            return self._cached.access_token

    # -- internals ----------------------------------------------------------

    def _fetch(
        self,
        *,
        grant_type: str,
        refresh_token: Optional[str] = None,
    ) -> _CachedToken:
        """POST the token endpoint and parse the response into a cache entry."""
        url = f"{self._base_url}{_TOKEN_PATH}"
        form: dict[str, str] = {"grant_type": grant_type, "scope": _SCOPE}
        if grant_type == _GRANT_REFRESH:
            assert refresh_token is not None
            form["refresh_token"] = refresh_token
        else:
            form["username"] = self._username
            form["password"] = self._password

        headers = {
            "Authorization": _basic_auth_header(self._client_id, self._client_secret),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }

        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(url, headers=headers, data=form)
        except httpx.TimeoutException as exc:
            raise SignNowAuthTransientError(
                "SignNow token request timed out"
            ) from exc
        except httpx.HTTPError as exc:
            raise SignNowAuthTransientError(
                f"SignNow token transport error: {type(exc).__name__}"
            ) from exc

        payload = self._parse(response)
        access_token = payload.get("access_token")
        if not access_token:
            raise SignNowAuthPermanentError(
                "SignNow token response missing access_token",
                status_code=response.status_code,
            )

        ttl = payload.get("expires_in")
        try:
            ttl_seconds = int(ttl) if ttl is not None else _DEFAULT_TTL_SECONDS
        except (TypeError, ValueError):
            ttl_seconds = _DEFAULT_TTL_SECONDS
        if ttl_seconds <= 0:
            ttl_seconds = _DEFAULT_TTL_SECONDS

        logger.info(
            "signnow_token_acquired",
            grant_type=grant_type,
            expires_in_seconds=ttl_seconds,
        )
        return _CachedToken(
            access_token=str(access_token),
            refresh_token=(
                str(payload["refresh_token"])
                if payload.get("refresh_token")
                else refresh_token
            ),
            expires_at_monotonic=time.monotonic() + ttl_seconds,
        )

    @staticmethod
    def _parse(response: httpx.Response) -> dict:
        """Classify HTTP status → transient/permanent, then return parsed JSON.

        No credentials enter the raised message — only HTTP status and a short,
        non-secret slice of the response body (SignNow error bodies carry an
        error code / description, not the submitted password).
        """
        status = response.status_code
        snippet = (response.text or "")[:200]
        if status == 429 or status // 100 == 5:
            raise SignNowAuthTransientError(
                f"SignNow token transient error: HTTP {status} {snippet}",
                status_code=status,
            )
        if status // 100 == 4:
            raise SignNowAuthPermanentError(
                f"SignNow token permanent error: HTTP {status} {snippet}",
                status_code=status,
            )
        if status // 100 != 2:
            raise SignNowAuthTransientError(
                f"SignNow token unexpected HTTP {status}", status_code=status
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise SignNowAuthPermanentError(
                "SignNow token response was not valid JSON", status_code=status
            ) from exc
        if not isinstance(data, dict):
            raise SignNowAuthPermanentError(
                "SignNow token response was not a JSON object", status_code=status
            )
        return data


# ---------------------------------------------------------------------------
# Module-level cache + convenience function
# ---------------------------------------------------------------------------

#: One provider per distinct credential set (keyed so different accounts /
#: environments don't share a token). Guarded by ``_PROVIDERS_LOCK``.
_PROVIDERS: dict[tuple, SignNowTokenProvider] = {}
_PROVIDERS_LOCK = threading.Lock()


def _provider_key(
    client_id: str, username: str, base_url: str
) -> tuple[str, str, str]:
    # The secret/password are intentionally excluded from the key (we don't want
    # them as dict keys); client_id + username + base_url uniquely identify a
    # credential set in practice.
    return (client_id, username, base_url.rstrip("/"))


def get_signnow_token(
    *,
    client_id: str,
    client_secret: str,
    username: str,
    password: str,
    base_url: str,
    timeout: float = _DEFAULT_TIMEOUT,
    force_refresh: bool = False,
) -> str:
    """Return a cached SignNow OAuth2 bearer token for the given credentials.

    The bearer token returned here is exactly what ``SignNowAdapter(api_key=...)``
    consumes. Caches per credential set in-process with expiry + refresh; a
    second call within the token TTL does NOT re-hit SignNow.

    Raises :class:`SignNowAuthTransientError` (retryable) or
    :class:`SignNowAuthPermanentError` (not retryable) on failure.
    """
    key = _provider_key(client_id, username, base_url)
    provider = _PROVIDERS.get(key)
    if provider is None:
        with _PROVIDERS_LOCK:
            provider = _PROVIDERS.get(key)
            if provider is None:
                provider = SignNowTokenProvider(
                    client_id=client_id,
                    client_secret=client_secret,
                    username=username,
                    password=password,
                    base_url=base_url,
                    timeout=timeout,
                )
                _PROVIDERS[key] = provider
    return provider.get_token(force_refresh=force_refresh)


def reset_signnow_token_cache() -> None:
    """Clear the module-level provider cache (test hook; not used in app code)."""
    with _PROVIDERS_LOCK:
        _PROVIDERS.clear()
