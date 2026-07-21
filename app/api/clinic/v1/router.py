"""Clinic API v1 router assembly. Mounted at /api/clinic/v1 in app/main.py.

Endpoints (all staff-authenticated via the platform JWT):
    GET  /clinic/v1/products
    GET  /clinic/v1/applications
    POST /clinic/v1/applications                      (WS-I vendor intake)
    POST /clinic/v1/applications/preview              (WS-I live payment preview)
    POST /clinic/v1/applications/{id}/request-reprocessing  (WS-I)
    GET  /clinic/v1/dashboard/summary
    GET  /clinic/v1/dashboard/overview
    GET  /clinic/v1/dashboard/applications/timeseries
    GET  /clinic/v1/dashboard/loan-book
    GET  /clinic/v1/dashboard/funnel
    GET  /clinic/v1/dashboard/revenue
    GET  /clinic/v1/account/profile
    POST /clinic/v1/account/profile/change-requests
    POST /clinic/v1/financing-links
"""
from fastapi import APIRouter

from app.api.clinic.v1.endpoints import (
    account,
    applications,
    dashboard_applications,
    dashboard_loanbook,
    dashboard_marketplace,
    dashboard_overview,
    financing_links,
    marketplace,
    messages,
    products,
    report_exports,
    vendor_disbursements,
    vendor_origination,
)
from app.core.config import settings

clinic_router = APIRouter()
clinic_router.include_router(products.router)
clinic_router.include_router(applications.router)
# WS-I vendor origination: intake + preview + request-reprocessing. Mounted
# BEFORE the shared prefix-less applications GET is unaffected (distinct methods).
clinic_router.include_router(vendor_origination.router)
clinic_router.include_router(financing_links.router)
clinic_router.include_router(marketplace.router)
# Vendor⇄PaySpyre application messaging (in-app Slack replacement), vendor-scoped.
clinic_router.include_router(messages.router)
# Vendor performance dashboard (spec: docs/vendor_dashboard_spec.md).
clinic_router.include_router(dashboard_overview.router)
clinic_router.include_router(dashboard_applications.router)
clinic_router.include_router(dashboard_loanbook.router)
clinic_router.include_router(dashboard_marketplace.router)
clinic_router.include_router(account.router)
# Turnkey-parity XLSX report downloads, hard-scoped to the caller's vendor.
clinic_router.include_router(report_exports.router)
# Vendor self-serve disbursements (W2-DISB) — wallet reads + extra-payout
# request, hard-scoped to the caller's vendor. Money-out is flag-gated OFF.
clinic_router.include_router(vendor_disbursements.router)

# UNAUTHENTICATED dev helper: seed a clinic (vendor + staff user + membership + JWT).
# Auto-on in development/test; elsewhere requires an EXPLICIT ENABLE_DEV_TOOLS (e.g.
# mock-mode staging). NEVER in production, and never where real PII lives.
if settings.ENVIRONMENT in ("development", "test") or settings.ENABLE_DEV_TOOLS:
    if settings.ENVIRONMENT != "production":
        from app.api.clinic.v1.endpoints import dev_tools

        clinic_router.include_router(dev_tools.router)
