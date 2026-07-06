"""Applicant API v1 router assembly. Mounted at /api/applicant/v1 in app/main.py."""
from fastapi import APIRouter

from app.api.applicant.v1.endpoints import (
    applications,
    auth,
    borrower_auth,
    dashboard,
    disclosure,
    documents,
    finalize,
    loans,
    manual_application,
    marketplace,
    products,
)
from app.core.config import settings

applicant_router = APIRouter()
applicant_router.include_router(auth.router)
# Consolidated disclosure (accept-all) + manual application + finalize MUST
# register BEFORE applications.router: applications has a catch-all POST
# /consents/{purpose} and a GET /{application_id} that would otherwise shadow the
# literal /detail + /finalize paths (FastAPI matches in registration order).
applicant_router.include_router(disclosure.router)
applicant_router.include_router(manual_application.router)
applicant_router.include_router(finalize.router)
applicant_router.include_router(documents.router)
applicant_router.include_router(applications.router)
applicant_router.include_router(products.router)
applicant_router.include_router(marketplace.router)
# Borrower portal (docs/borrower_portal_spec.md): email login + loan servicing (reads + Pay Now).
applicant_router.include_router(borrower_auth.router)
applicant_router.include_router(loans.router)
applicant_router.include_router(dashboard.router)

# UNAUTHENTICATED dev helpers (surface the mock magic-link code; simulate verification
# results). Auto-on in development/test; elsewhere requires an EXPLICIT ENABLE_DEV_TOOLS
# (e.g. mock-mode staging). NEVER in production, and never where real PII lives.
if settings.ENVIRONMENT in ("development", "test") or settings.ENABLE_DEV_TOOLS:
    if settings.ENVIRONMENT != "production":
        from app.api.applicant.v1.endpoints import dev_tools

        applicant_router.include_router(dev_tools.router)
