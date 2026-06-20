"""Applicant API v1 router assembly. Mounted at /api/applicant/v1 in app/main.py."""
from fastapi import APIRouter

from app.api.applicant.v1.endpoints import applications, auth, marketplace, products
from app.core.config import settings

applicant_router = APIRouter()
applicant_router.include_router(auth.router)
applicant_router.include_router(applications.router)
applicant_router.include_router(products.router)
applicant_router.include_router(marketplace.router)

# Dev/staging-only helpers (surface the mock magic-link code; simulate verification
# results) so the patient flow can be demoed end-to-end without real vendors. NEVER
# mounted in production.
if settings.ENVIRONMENT != "production":
    from app.api.applicant.v1.endpoints import dev_tools

    applicant_router.include_router(dev_tools.router)
