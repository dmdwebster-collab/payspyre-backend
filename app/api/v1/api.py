from fastapi import APIRouter

from app.core.config import settings

from app.api.v1.endpoints import (
    admin_actions,
    admin_analytics,
    admin_analytics_depth,
    admin_application_process,
    admin_application_actions,
    admin_applications,
    admin_archive,
    admin_audit,
    admin_blacklist,
    admin_bureau_reporting,
    admin_borrower_security,
    admin_province_compliance,
    admin_collections,
    admin_communications,
    admin_collections_work,
    admin_config,
    admin_crm_customers,
    admin_crm_vendors,
    admin_dashboard,
    admin_decision_reasons,
    admin_customer_profiles,
    admin_document_templates,
    admin_flags,
    admin_hardship,
    admin_import,
    admin_loan_documents,
    admin_loans,
    admin_messages,
    admin_offers,
    admin_originations,
    admin_report_builder,
    admin_report_exports,
    admin_risk_scores,
    admin_scorecards,
    admin_settings,
    admin_staff_comments,
    admin_status_model,
    admin_system,
    admin_vendor_changes,
    admin_vendor_disbursements,
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
# WS-E originations-admin depth: field-level editing w/ change log, staff offer
# editing (PricingConfig bounds), header + profile photo, co-borrower linking.
api_router.include_router(
    admin_originations.router, prefix="/admin/applications", tags=["admin-originations"]
)
# P0 T3 (2026-07-21 review): the six controls that rendered DISABLED —
# Add Bank Account / Bank Verification / Send Email / Send SMS / Hard + Soft
# Pull. Staff-only; NEVER mounted on the clinic (vendor) API.
api_router.include_router(
    admin_application_actions.router,
    prefix="/admin/applications",
    tags=["admin-application-actions"],
)
# WS-E customer/loan flags: directory CRUD + raise/clear; suppress-notifications
# flags gate the notification processor's vendor sends.
api_router.include_router(admin_flags.router, prefix="/admin", tags=["admin-flags"])
api_router.include_router(admin_loans.router, prefix="/admin/loans", tags=["admin-cockpit"])
# WS-J — Hardship v1 (deferment / due-date change; e-sign gated). Deliberately
# NOT plain-staff: requires the dedicated hardship/create permission (admin
# implicitly allowed) — Dave's "user-defined availability" mandate.
api_router.include_router(admin_hardship.router, prefix="/admin/loans", tags=["admin-hardship"])
# WS-B — per-loan generated documents (agreement/PAD/schedules/statements) +
# versioned system-document templates w/ merge-field engine (Turnkey parity).
api_router.include_router(admin_loan_documents.router, prefix="/admin/loans", tags=["admin-documents"])
api_router.include_router(
    admin_document_templates.router, prefix="/admin/document-templates", tags=["admin-documents"]
)
api_router.include_router(admin_collections.router, prefix="/admin/collections", tags=["admin-cockpit"])
# WS-C — collections work-surface: collector assignment (bulk + tiers), action
# plans, promise-to-pay, header math, segregated insolvency portfolio.
api_router.include_router(
    admin_collections_work.router, prefix="/admin/collections", tags=["admin-collections"]
)
api_router.include_router(
    admin_collections_work.admin_router,
    prefix="/admin/collections",
    tags=["admin-collections"],
)
# Vendor⇄PaySpyre application messaging (in-app Slack replacement), whole-book.
api_router.include_router(admin_messages.router, prefix="/admin", tags=["admin-messages"])
# Dave's internal Comments tab (application + loan). STAFF-ONLY: admin mount
# only, never exposed on the clinic or borrower-portal routers.
api_router.include_router(
    admin_staff_comments.router, prefix="/admin", tags=["admin-staff-comments"]
)
# WS-A — communications hub: append-only legal comms log (full message bodies,
# Dave mandate #4) + staff templated email/SMS send + offline contact log.
api_router.include_router(
    admin_communications.router,
    prefix="/admin/communications",
    tags=["admin-communications"],
)
api_router.include_router(admin_audit.router, prefix="/admin/audit", tags=["admin-cockpit"])
api_router.include_router(admin_archive.router, prefix="/admin/archive", tags=["admin-archive"])
api_router.include_router(admin_blacklist.router, prefix="/admin/blacklist", tags=["admin-blacklist"])
api_router.include_router(
    admin_province_compliance.router,
    prefix="/admin/compliance",
    tags=["admin-province-compliance"],
)
api_router.include_router(
    admin_bureau_reporting.router, prefix="/admin/bureau-reporting", tags=["admin-bureau-reporting"]
)
# Phase 2 — write actions (decision/payment/payoff) + maker-checker (charge-off/disburse).
api_router.include_router(admin_actions.router, prefix="/admin", tags=["admin-actions"])
# Turnkey cutover import (P0 WS-D) — CSV upload -> preview -> confirm, admin-only.
api_router.include_router(admin_import.router, prefix="/admin/import", tags=["admin-import"])
# Phase 4 — advanced portfolio analytics (vintage / originations / CEI). Read-only.
api_router.include_router(admin_analytics.router, prefix="/admin/analytics", tags=["admin-analytics"])
# WS-H reports depth — profit split / buckets+debt-roll / overrides / AI-decisioning / geo.
api_router.include_router(admin_analytics_depth.router, prefix="/admin/analytics", tags=["admin-analytics"])
# Turnkey-parity XLSX report downloads (Dave's TL Smart Marker templates). Read-only.
api_router.include_router(admin_report_exports.router, prefix="/admin/reports", tags=["admin-reports"])
# WS-H — Excel report builder v1 + scheduled reports engine (definitions / schedules).
api_router.include_router(admin_report_builder.router, prefix="/admin/reports", tags=["admin-reports"])
# Phase 3 — config surfaces (RBAC visibility). Products reuse /credit-products. Read-only, admin.
api_router.include_router(admin_config.router, prefix="/admin/config", tags=["admin-config"])
# WS-F — settings suite (decision rules / company info / business calendar /
# notification matrix). Admin-only, audited writes.
api_router.include_router(admin_settings.router, prefix="/admin/settings", tags=["admin-settings"])
# Application-process config (WS W2-APPCONFIG): flow/offer/dictionaries/disclaimer/
# co-applicant + per-product policy read.
api_router.include_router(
    admin_application_process.router, prefix="/admin/settings", tags=["admin-settings"]
)
# WS-E — reject/cancel decision-reason directory (admin CRUD, soft-deactivate only).
api_router.include_router(
    admin_decision_reasons.router, prefix="/admin/decision-reasons", tags=["admin-config"]
)
# WS-D — multi-offer approvals ("Create Loan Offers") + AI bank-statement analysis.
# Books NO loan on create; the borrower's acceptance (applicant API) books it.
api_router.include_router(admin_offers.router, prefix="/admin", tags=["admin-offers"])
# WS-D — editable 5-band verified-data scorecards + per-vendor assignment (mandate #3).
api_router.include_router(admin_scorecards.router, prefix="/admin/scorecards", tags=["admin-scorecards"])
# Risk-score persistence model (migration 073) — the Risk score TAB per
# application, its append-only scoring history, and the honest backfill. The
# loans-by-risk-band + Scoring-section aggregations live under /admin/analytics.
api_router.include_router(admin_risk_scores.router, prefix="/admin/risk-scores", tags=["admin-risk-scores"])
# Dave's Application Status Flow v1.00 registry (2026-07-21 review §A) — the
# data the UI renders workplace queues + per-status action buttons from.
api_router.include_router(admin_status_model.router, prefix="/admin", tags=["admin-config"])
# Dave's Credit Application v1.0 — the Customer Profile as a first-class entity:
# the field registry (GET /admin/profile-schema) the manual form + applicant
# journey render from, profile CRUD (create/edit/lock/soft-delete, versioned),
# and "create an application from an EXISTING profile" with the profile state
# frozen onto the application.
api_router.include_router(
    admin_customer_profiles.router, prefix="/admin", tags=["admin-customer-profiles"]
)
# System mode (Simulation vs Live) — read-only, admin/staff, for the cockpit banner.
api_router.include_router(admin_system.router, prefix="/admin/system", tags=["admin-system"])
# WS-G — Vendor + Customer CRM. Vendor CRM: industry categories, contacts, bank
# accounts (masked), MSA docs w/ expiry alerts, onboarding, 9-role matrix,
# chain + portfolio. Customer CRM: cross-loan view, lock/block, changelog.
api_router.include_router(admin_crm_vendors.router, prefix="/admin/crm", tags=["admin-crm-vendors"])
api_router.include_router(
    admin_crm_customers.router, prefix="/admin/crm/customers", tags=["admin-crm-customers"]
)
# WS-J borrower-portal depth, staff-only halves: audited ID-document reads,
# bank-account add/remove, per-patient 2FA enforcement, payout-request queue.
api_router.include_router(
    admin_borrower_security.router, prefix="/admin", tags=["admin-borrower-security"]
)
# W2-DISB vendor self-serve disbursements oversight: any vendor's derived wallet,
# the cross-vendor payout ledger, and the (flag-gated) monthly-sweep trigger.
api_router.include_router(
    admin_vendor_disbursements.router,
    prefix="/admin/disbursements",
    tags=["admin-disbursements"],
)
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