"""Prometheus ``/metrics`` scrape endpoint — spec §11 (audit H-8).

A tiny standalone router so ``app/main.py`` can mount it at the app root
(``app.include_router(metrics_router)``) without a ``/api/...`` prefix — the
standard Prometheus scrape path is ``/metrics``.

Importing this module imports ``platform_metrics``, which registers the §11
metrics on the default ``prometheus_client`` registry. ``generate_latest()``
serialises that registry in the Prometheus text exposition format.
"""
from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

# Importing the metrics module registers all §11 collectors on the default
# REGISTRY so they appear in generate_latest() output even before first use.
from app.services.metrics import platform_metrics  # noqa: F401

metrics_router = APIRouter()


@metrics_router.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    """Expose the default Prometheus registry in text exposition format."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
