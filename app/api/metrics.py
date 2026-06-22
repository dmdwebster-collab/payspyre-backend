"""Prometheus ``/metrics`` scrape endpoint — spec §11 (audit H-8).

A tiny standalone router so ``app/main.py`` can mount it at the app root
(``app.include_router(metrics_router)``) without a ``/api/...`` prefix — the
standard Prometheus scrape path is ``/metrics``.

Importing this module imports ``platform_metrics``, which registers the §11
metrics on the default ``prometheus_client`` registry. ``generate_latest()``
serialises that registry in the Prometheus text exposition format.

Auth (hardening): the registry exposes internal KPIs (application counts, decision
outcomes, …), so the scrape is gated. With ``METRICS_AUTH_TOKEN`` set it requires
``Authorization: Bearer <token>``; with it unset it stays open in non-production for
convenience but is DENIED in production — /metrics is never unauthenticated in prod.
"""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Header, HTTPException, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.core.config import settings

# Importing the metrics module registers all §11 collectors on the default
# REGISTRY so they appear in generate_latest() output even before first use.
from app.services.metrics import platform_metrics  # noqa: F401

metrics_router = APIRouter()


def _authorize_metrics(authorization: str | None) -> None:
    token = settings.METRICS_AUTH_TOKEN
    if token:
        expected = f"Bearer {token}"
        # Constant-time compare so the token can't be guessed by timing.
        if not authorization or not hmac.compare_digest(authorization, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid metrics token.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return
    # No token configured: open in non-production, denied in production so the KPI
    # surface is never unauthenticated on a prod deployment.
    if settings.ENVIRONMENT == "production":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not found.",  # don't advertise an unconfigured endpoint exists
        )


@metrics_router.get("/metrics", include_in_schema=False)
def metrics(authorization: str | None = Header(default=None)) -> Response:
    """Expose the default Prometheus registry in text exposition format."""
    _authorize_metrics(authorization)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
