"""Shared outbound-HTTP helper for vendor adapters.

Gives every adapter a consistent connect/read timeout and structured
status + latency logging — WITHOUT logging request/response bodies, headers, query
strings, or any secret material (only ``provider``, ``op``, ``method``, host+path,
status, and latency_ms). Adapters historically rolled their own ``httpx.Client``
with ad-hoc single-float timeouts and (Didit/Flinks) zero logging.

This does NOT retry — outbound *create*/charge calls must not be blindly retried.
A caller that wants bounded retries for an idempotent GET/auth call should layer
that explicitly.
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
