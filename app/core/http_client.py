"""Shared outbound-HTTP helper for vendor adapters.

Gives every adapter a consistent connect/read timeout and structured
status + latency logging — WITHOUT logging request/response bodies, headers, query
strings, or any secret material (only ``provider``, ``op``, ``method``, host+path,
status, and latency_ms). Adapters historically rolled their own ``httpx.Client``
with ad-hoc single-float timeouts and (Didit/Flinks) zero logging.

``request()`` does NOT retry — outbound *create*/charge calls must not be blindly
retried. For an idempotent GET/status call, ``request_with_retry()`` layers a
small, bounded retry-with-backoff on top (transport errors + 5xx only); callers
must opt in and must never use it for non-idempotent POSTs.
"""
from __future__ import annotations

import time

import httpx

from app.core.logging import get_logger

logger = get_logger(__name__)

# Split connect vs read so a slow vendor read doesn't share the (short) connect
# budget, and neither hangs a worker indefinitely.
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0)


def _safe_target(url: str) -> str:
    """host+path only — never the query string (it can carry tokens/customer ids)."""
    try:
        u = httpx.URL(url)
        return f"{u.host}{u.path}"
    except Exception:  # noqa: BLE001
        return "?"


def request(
    method: str,
    url: str,
    *,
    provider: str,
    op: str = "",
    client: httpx.Client | None = None,
    **kwargs,
) -> httpx.Response:
    """Issue an outbound request with the standard timeout + status/latency logging.

    Pass an existing ``client`` to reuse a connection pool, else a one-shot client
    is used. Returns the ``httpx.Response`` (callers handle non-2xx). Logs on both
    success and transport error; never logs bodies/headers/query/secrets.
    """
    kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
    target = _safe_target(url)
    start = time.monotonic()
    try:
        if client is not None:
            resp = client.request(method, url, **kwargs)
        else:
            with httpx.Client(timeout=kwargs.pop("timeout")) as c:
                resp = c.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        logger.warning(
            "outbound_http_error",
            provider=provider,
            op=op,
            method=method,
            target=target,
            latency_ms=round((time.monotonic() - start) * 1000, 1),
            error_type=type(exc).__name__,
        )
        raise
    logger.info(
        "outbound_http",
        provider=provider,
        op=op,
        method=method,
        target=target,
        status=resp.status_code,
        latency_ms=round((time.monotonic() - start) * 1000, 1),
    )
    return resp


# Small + bounded so a flaky vendor doesn't pin a worker: 3 total attempts with
# 0.2s, 0.4s backoff between them => at most ~0.6s of sleeping on top of the
# per-attempt timeouts. Tune via args, not by raising these defaults blindly.
_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_BACKOFF_BASE = 0.2


def request_with_retry(
    method: str,
    url: str,
    *,
    provider: str,
    op: str = "",
    client: httpx.Client | None = None,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    backoff_base: float = _DEFAULT_BACKOFF_BASE,
    sleep=time.sleep,
    **kwargs,
) -> httpx.Response:
    """Like :func:`request`, but with a bounded retry for *idempotent* calls only.

    ONLY use this for safe-to-repeat reads (GET/status). Retrying a non-idempotent
    POST could double-create at the vendor — use plain :func:`request` for those.

    Retries on transport errors (timeouts / connection resets, surfaced as
    ``httpx.HTTPError``) and on 5xx responses, with linear backoff
    (``backoff_base * attempt``). 4xx responses are returned immediately (they
    won't get better on retry). After ``max_attempts``, the last error is raised
    or the last 5xx response is returned so the caller handles it as before.
    """
    last_exc: httpx.HTTPError | None = None
    resp: httpx.Response | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = request(
                method, url, provider=provider, op=op, client=client, **kwargs
            )
        except httpx.HTTPError as exc:
            last_exc = exc
            resp = None
            if attempt == max_attempts:
                raise
        else:
            if resp.status_code < 500 or attempt == max_attempts:
                return resp
        # transient failure with attempts left — back off then retry
        sleep(backoff_base * attempt)
    # Unreachable in practice (loop returns/raises on the final attempt), but keep
    # mypy + the type checker happy.
    if resp is not None:
        return resp
    assert last_exc is not None
    raise last_exc
