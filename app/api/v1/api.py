from fastapi import APIRouter

from app.api.v1.endpoints import (
    admin_vendor_changes,
    auth,
    credit_products,
    health,
    integration_settings,
)

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(credit_products.router, prefix="/credit-products", tags=["credit-products"])
api_router.include_router(integration_settings.router, prefix="/integration-settings", tags=["integration-settings"])
# Admin review of vendor profile change requests (spec §3.6). Admin-only; the
# approve route is the only path that writes vendor-requested changes onto vendors.
api_router.include_router(
    admin_vendor_changes.router,
    prefix="/admin/vendor-profile-change-requests",
    tags=["admin-vendor-changes"],
)
# V1 `patients` router UN-MOUNTED 2026-06-20 (audit): its endpoints were a broken
# access-control surface — GET/PATCH /patients/{id} were gated only by a valid staff
# JWT (no role, no object scope) → any authenticated user could read/write ANY
# patient's identity (IDOR), and POST /patients/quickstart was fully unauthenticated.
# The endpoints are dead in V2 (the frontend never called them; patient records are
# created/updated via the applicant API + flow orchestrator, which use
# PatientProfileService directly). The service is retained + tested; only the unsafe
# HTTP surface is removed.
# V1 notifications router removed in P7.4c (un-mounted; files deleted in commit B).
# P8.1 (2026-06-19): un-mounted the unauthenticated legacy V1 lending surface —
#   loan, credit, underwriting, funding, vendors, analytics. Every one of these
#   mutated decision/funding/vendor state (or streamed the loan book as CSV / pulled
#   bureau credit reports) on legacy LoanApplication-era models with NO authentication
#   (no get_current_user, no role check, no object scope). They are dead in V2 — the
#   platform flow runs entirely on platform_* models via the applicant API + flow
#   orchestrator. Files/models/schemas/services deleted in a follow-up commit, mirroring
#   the P7.1 / P7.4c un-mount-then-delete split. Live V2 surface retained:
#   auth, credit_products, patients, health.