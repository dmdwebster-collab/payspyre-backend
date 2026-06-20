"""Zumrails token-acquisition layer (token cache + expiry).

``zumrails_adapter.ZumrailsAdapter`` exchanges api_key/api_secret at
``POST /api/authorize`` on every call chain via its private ``_get_token`` (it
does NOT cache). This module is a cached front for that same exchange so the
wiring layer can fetch a token once and reuse it across operations / adapter
constructions, mirroring the SignNow split (``signnow_oauth``):

    token = get_zumrails_token(api_key=..., api_secret=..., base_url=...)
    adapter = ZumrailsAdapter(
        api_key=token, api_secret="", base_url=..., auth_mode="static_bearer",
    )

Style mirrors ``zumrails_adapter`` / ``real_notification_dispatcher``: a thin
httpx client, a transient-vs-permanent error hierarchy, and **no credentials in
logs or exception messages** (api_secret / password never appear in a raised
error — only the HTTP status and a short, non-secret response snippet).

=============================================================================
ZUMRAILS AUTH ASSUMPTIONS  (mirrors the adapter's documented shape)
=============================================================================
AUTH — token exchange (see ZumrailsAdapter docstring / ``_get_token``):
  ``POST {base_url}/api/authorize`` with JSON body
  ``{"username": <api_key>, "password": <api_secret>}`` returns
  ``{"result": {"Token": "<jwt>"}}`` (case-tolerant). That JWT is the bearer
  token sent as ``Authorization: Bearer <jwt>`` on subsequent calls.

EXPIRY — Zumrails' authorize response is not documented to carry an explicit
  TTL here, so we cache for :data:`_DEFAULT_TTL_SECONDS` (conservative) and
  refresh past that. If the response begins returning an ``ExpiresIn`` /
  ``expires_in`` field, it is honored when present. ``_EXPIRY_SKEW_SECONDS``
  refreshes slightly early so an in-flight request never races the boundary.

CLOCK — TTL math uses real ``time.monotonic()`` (immune to wall-clock jumps).
  A real monotonic-clock read is correct here: app code that must reflect actual
  elapsed time, not a deterministic test seam.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Isolated assumptions (mirror the adapter)
# ---------------------------------------------------------------------------

_AUTHORIZE_PATH = "/api/authorize"

#: Conservative cached token lifetime (seconds) when no TTL is in the response.
_DEFAULT_TTL_SECONDS = 3600

#: Refresh this many seconds before real expiry.
_EXPIRY_SKEW_SECONDS = 120

_DEFAULT_TIMEOUT = 15.0


# ---------------------------------------------------------------------------
# Error hierarchy (transient vs permanent — mirrors zumrails_adapter)
# ---------------------------------------------------------------------------


class ZumrailsAuthError(Exception):
    """Base class for Zumrails token-acquisition failures (never carries creds)."""


class TransientZumrailsAuthError(ZumrailsAuthError):
    """Retryable — vendor 5xx / 429 / network / timeout. A caller retry is plausible."""


class PermanentZumrailsAuthError(ZumrailsAuthError):
    """Non-retryable — 4xx / bad credentials / no token in body. Retry won't help."""


# ---------------------------------------------------------------------------
# Cache primitives
# ---------------------------------------------------------------------------


@dataclass
class _CachedToken:
    token: str
    #: monotonic deadline (seconds) at which the token is considered expired.
    expires_at_monotonic: float

    def is_fresh(self, now_monotonic: float) -> bool:
        return now_monotonic < (self.expires_at_monotonic - _EXPIRY_SKEW_SECONDS)


class ZumrailsTokenProvider:
    """In-process, thread-safe Zumrails token cache with expiry.

    One instance per credential set caches a single token and re-authorizes once
    it nears expiry. ``get_token()`` serializes the (rare) fetch under a lock so
    a cold/expired cache doesn't stampede ``/api/authorize``.
    """

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        base_url: str,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._lock = threading.Lock()
        self._cached: Optional[_CachedToken] = None

    def get_token(self, *, force_refresh: bool = False) -> str:
        """Return a cached bearer token, re-authorizing if needed."""
        now = time.monotonic()
        cached = self._cached
        if not force_refresh and cached is not None and cached.is_fresh(now):
            return cached.token

        with self._lock:
            now = time.monotonic()
            cached = self._cached
            if not force_refresh and cached is not None and cached.is_fresh(now):
                return cached.token
            self._cached = self._authorize()
            return self._cached.token

    # -- internals ----------------------------------------------------------

    def _authorize(self) -> _CachedToken:
        url = f"{self._base_url}{_AUTHORIZE_PATH}"
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(
                    url,
                    headers={"Content-Type": "application/json"},
                    json={"username": self._api_key, "password": self._api_secret},
                )
        except httpx.TimeoutException as exc:
            raise TransientZumrailsAuthError(
                "Zumrails authorize request timed out"
            ) from exc
        except httpx.HTTPError as exc:
            raise TransientZumrailsAuthError(
                f"Zumrails authorize transport error: {type(exc).__name__}"
            ) from exc

        payload = self._parse(response)
        result = payload.get("result") or payload.get("Result") or {}
        token = (
            (result.get("Token") if isinstance(result, dict) else None)
            or (result.get("token") if isinstance(result, dict) else None)
            or payload.get("token")
        )
        if not token:
            raise PermanentZumrailsAuthError(
                "Zumrails authorize response had no Token"
            )

        ttl_seconds = self._extract_ttl(payload, result)
        logger.info("zumrails_token_acquired", expires_in_seconds=ttl_seconds)
        return _CachedToken(
            token=str(token),
            expires_at_monotonic=time.monotonic() + ttl_seconds,
        )

    @staticmethod
    def _extract_ttl(payload: dict, result: Any) -> int:
        candidates = []
        if isinstance(result, dict):
            candidates += [result.get("ExpiresIn"), result.get("expires_in")]
        candidates += [payload.get("ExpiresIn"), payload.get("expires_in")]
        for raw in candidates:
            if raw is None:
                continue
            try:
                ttl = int(raw)
            except (TypeError, ValueError):
                continue
            if ttl > 0:
                return ttl
        return _DEFAULT_TTL_SECONDS

    @staticmethod
    def _parse(response: httpx.Response) -> dict:
        """Classify HTTP status → transient/permanent, then return parsed JSON.

        No credentials enter the raised message — only HTTP status and a short,
        non-secret slice of the response body.
        """
        status = response.status_code
        snippet = (response.text or "")[:200]
        if status in (429, 500, 502, 503, 504):
            raise TransientZumrailsAuthError(
                f"Zumrails authorize transient error: HTTP {status} {snippet}"
            )
        if 400 <= status < 500:
            raise PermanentZumrailsAuthError(
                f"Zumrails authorize permanent error: HTTP {status} {snippet}"
            )
        if status // 100 != 2:
            raise TransientZumrailsAuthError(
                f"Zumrails authorize unexpected HTTP {status}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise PermanentZumrailsAuthError(
                "Zumrails authorize response was not valid JSON"
            ) from exc
        if not isinstance(data, dict):
            raise PermanentZumrailsAuthError(
                "Zumrails authorize response was not a JSON object"
            )
        return data


# ---------------------------------------------------------------------------
# Module-level cache + convenience function
# ---------------------------------------------------------------------------

_PROVIDERS: dict[tuple, ZumrailsTokenProvider] = {}
_PROVIDERS_LOCK = threading.Lock()


def _provider_key(api_key: str, base_url: str) -> tuple[str, str]:
    # api_secret intentionally excluded from the key (don't want it as a dict
    # key); api_key + base_url uniquely identify a credential set in practice.
    return (api_key, base_url.rstrip("/"))


def get_zumrails_token(
    *,
    api_key: str,
    api_secret: str,
    base_url: str,
    timeout: float = _DEFAULT_TIMEOUT,
    force_refresh: bool = False,
) -> str:
    """Return a cached Zumrails bearer token for the given credentials.

    Hits ``POST /api/authorize`` once and caches the JWT in-process with expiry;
    a second call within the token TTL does NOT re-authorize. The token returned
    is the bearer the adapter sends — pass it to a ``static_bearer`` adapter, or
    use it directly.

    Raises :class:`TransientZumrailsAuthError` (retryable) or
    :class:`PermanentZumrailsAuthError` (not retryable) on failure.
    """
    key = _provider_key(api_key, base_url)
    provider = _PROVIDERS.get(key)
    if provider is None:
        with _PROVIDERS_LOCK:
            provider = _PROVIDERS.get(key)
            if provider is None:
                provider = ZumrailsTokenProvider(
                    api_key=api_key,
                    api_secret=api_secret,
                    base_url=base_url,
                    timeout=timeout,
                )
                _PROVIDERS[key] = provider
    return provider.get_token(force_refresh=force_refresh)


def reset_zumrails_token_cache() -> None:
    """Clear the module-level provider cache (test hook; not used in app code)."""
    with _PROVIDERS_LOCK:
        _PROVIDERS.clear()
