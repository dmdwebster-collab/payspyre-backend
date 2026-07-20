from fastapi import APIRouter

from app.core.config import settings

from app.api.v1.endpoints import (
    admin_actions,
    admin_analytics,
    admin_applications,
    admin_audit,
    admin_collections,
    admin_config,
    admin_dashboard,
    admin_decision_reasons,
    admin_import,
    admin_loans,
    admin_messages,
    admin_system,
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
# Lender/admin operations portal — Phase 1 read cockpit (docs/lender_admin_portal_spec.md).
# Whole-book reads, admin/staff gated (audit is admin-only). Mounted under /admin/*.
api_router.include_router(admin_dashboard.router, prefix="/admin/dashboard", tags=["admin-cockpit"])
api_router.include_router(admin_applications.router, prefix="/admin/applications", tags=["admin-cockpit"])
api_router.include_router(admin_loans.router, prefix="/admin/loans", tags=["admin-cockpit"])
api_router.include_router(admin_collections.router, prefix="/admin/collections", tags=["admin-cockpit"])
# Vendor⇄PaySpyre application messaging (in-app Slack replacement), whole-book.
api_router.include_router(admin_messages.router, prefix="/admin", tags=["admin-messages"])
api_router.include_router(admin_audit.router, prefix="/admin/audit", tags=["admin-cockpit"])
# Phase 2 — write actions (decision/payment/payoff) + maker-checker (charge-off/disburse).
api_router.include_router(admin_actions.router, prefix="/admin", tags=["admin-actions"])
# Turnkey cutover import (P0 WS-D) — CSV upload -> preview -> confirm, admin-only.
api_router.include_router(admin_import.router, prefix="/admin/import", tags=["admin-import"])
# Phase 4 — advanced portfolio analytics (vintage / originations / CEI). Read-only.
api_router.include_router(admin_analytics.router, prefix="/admin/analytics", tags=["admin-analytics"])
# Phase 3 — config surfaces (RBAC visibility). Products reuse /credit-products. Read-only, admin.
api_router.include_router(admin_config.router, prefix="/admin/config", tags=["admin-config"])
# WS-E — reject/cancel decision-reason directory (admin CRUD, soft-deactivate only).
api_router.include_router(
    admin_decision_reasons.router, prefix="/admin/decision-reasons", tags=["admin-config"]
)
# System mode (Simulation vs Live) — read-only, admin/staff, for the cockpit banner.
api_router.include_router(admin_system.router, prefix="/admin/system", tags=["admin-system"])
# Embedded pre-qual widget intake (server-to-server, X-Widget-Key gated; inert until
# WIDGET_API_KEY is set). Turns the widget's pre-qual into a real application.
from app.api.v1.endpoints import widget_intake  # noqa: E402

api_router.include_router(widget_intake.router, tags=["widget-intake"])
# UNAUTHENTICATED dev helper: seed an admin/staff RBAC user so the cockpit can be
# signed into on a fresh env (the clinic dev-seed only makes a clinic STAFF user).
# Same gate as the clinic dev-seed: auto-on in dev/test, else explicit ENABLE_DEV_TOOLS
# (mock-mode staging); NEVER in production.
if settings.ENVIRONMENT in ("development", "test") or settings.ENABLE_DEV_TOOLS:
    if settings.ENVIRONMENT != "production":
        from app.api.v1.endpoints import admin_dev_tools

        api_router.include_router(admin_dev_tools.router, prefix="/admin", tags=["admin-dev"])
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