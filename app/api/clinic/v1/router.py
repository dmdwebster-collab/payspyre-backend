"""Clinic API v1 router assembly. Mounted at /api/clinic/v1 in app/main.py.

Endpoints (all staff-authenticated via the platform JWT):
    GET  /clinic/v1/products
    GET  /clinic/v1/applications
    GET  /clinic/v1/dashboard/summary
    POST /clinic/v1/financing-links
"""
from fastapi import APIRouter

from app.api.clinic.v1.endpoints import applications, financing_links, marketplace, products
from app.core.config import settings

clinic_router = APIRouter()
clinic_router.include_router(products.router)
clinic_router.include_router(applications.router)
clinic_router.include_router(financing_links.router)
clinic_router.include_router(marketplace.router)

# UNAUTHENTICATED dev helper: seed a clinic (vendor + staff user + membership + JWT).
# Auto-on in development/test; elsewhere requires an EXPLICIT ENABLE_DEV_TOOLS (e.g.
# mock-mode staging). NEVER in production, and never where real PII lives.
if settings.ENVIRONMENT in ("development", "test") or settings.ENABLE_DEV_TOOLS:
    if settings.ENVIRONMENT != "production":
        from app.api.clinic.v1.endpoints import dev_tools

        clinic_router.include_router(dev_tools.router)
