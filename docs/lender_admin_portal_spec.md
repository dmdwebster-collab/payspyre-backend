# Lender / Admin Operations Portal — Spec & Roadmap

**Status:** Draft for review
**Author:** (drafted with Claude Code)
**Scope:** PaySpyre's **internal operations cockpit** — the surface where PaySpyre *runs the lending business*: underwriting, servicing, collections, disbursement, product & pricing config, clinic management, compliance, and platform analytics. This is the admin counterpart to the customer-facing portals.

> **This is the heart of the platform.** The patient app *originates* loans; the clinic console is a *partner* surface; the borrower portal is *self-service*. This portal is where the **lender** operates — it's the functional replacement for the Turnkey Lender admin we're retiring.

---

## 1. The key principle — the engine already exists

Critically, **the lending engine is already built** in the backend. This portal is mostly **surfacing existing services + a handful of new admin write-actions + a UI** — not building the machine from scratch:

| Capability | Existing backend |
|---|---|
| Origination / decisioning | `flow_orchestrator.py`, `flow_engine.py`, `PlatformCreditApplication` (status, `decision`, `flow_state`) |
| Verifications | `services/verifications/`, `PlatformVerification`, `credit_bureau.py`, `kyc_vendor.py`, `bank/` |
| Loans / servicing | `loan_servicing.py` (amortization, `record_payment`, `get_loan_status`, `generate_statement`), `loan_lifecycle.py`, `PlatformLoan` + schedule/payments/statements |
| Collections / delinquency | `loan_servicing.run_delinquency_aging()` |
| Disbursement | `services/payments/zumrails_adapter.py` |
| Agreements (e-sign) | `services/esign/signnow_adapter.py` |
| Adverse action (FCRA) | `adverse_action.py` (build/render/send notices) |
| Credit products & pricing | `credit_products.py` (CRUD, versioning, verification matrix) |
| Clinics/vendors | `Vendor` model, `clinic_membership.py`, the admin vendor-change-request endpoint |
| Marketplace | `services/marketplace/`, `lead_metrics.py` |
| Integrations / creds | `integration_settings.py`, `integration_creds.py`, `connection_test.py` |
| Metrics | `services/metrics/platform_metrics.py` (Prometheus) |
| **Access control** | full RBAC — `UserRole` (admin/staff/patient/vendor), `Permission`/`Role`/`RolePermission`/`UserRoleLink` |
| **Audit trail** | `PlatformEvent` — append-only, WORM-triggered event log |

**What's missing is the admin API surface and the UI to drive all of it.** Today the only admin endpoint is `/api/v1/admin/vendor-profile-change-requests` (approve/reject) — one seed of a surface that needs ~10 modules.

---

## 2. Actors & access

- **Audience:** PaySpyre internal staff — operators, underwriters, servicing/collections, finance/admin. NOT clinics, NOT borrowers.
- **Auth:** the existing platform staff JWT + **RBAC**. All endpoints gated by `require_roles("admin")` / `require_roles("staff","admin")`, and finer `require_permission(resource, action)` for sensitive actions (decision override, disbursement, charge-off).
- **Audit:** every state-changing admin action **must** append a `PlatformEvent` (actor = the staff user) so the WORM log is the system of record for "who did what." This is a hard requirement, not optional — it's the compliance backbone.
- **Roles to define:** at minimum `underwriter`, `servicing`, `admin` (super) as `Role` rows with scoped `Permission`s, so a collections agent can't, say, change product pricing.

---

## 3. Functional modules

Each module: **purpose · what already exists · what's missing · key endpoints** (`/api/v1/admin/...`). All money in integer cents; all writes audited.

### M1 — Operations dashboard (portfolio at a glance)
- **Purpose:** the lender's home screen — portfolio KPIs across ALL clinics/borrowers: origination volume & approval rate, outstanding principal, active/delinquent/charged-off counts, delinquency rate, disbursed vs pending, marketplace revenue, today's work (apps awaiting review, disbursements to release, delinquencies to action).
- **Exists:** the data (applications, loans, schedule, payments, marketplace) + `platform_metrics`. This is the *vendor dashboard, god-mode* (no `vendor_id` scoping).
- **Missing:** the aggregate-across-all-vendors endpoints + UI.
- **Endpoints:** `GET /admin/dashboard/overview`, `/admin/dashboard/portfolio` (vintage/aging), `/admin/work-queue` (counts).

### M2 — Applications & underwriting
- **Purpose:** the review queue — list/filter applications (esp. `under_review` / manual-review), open one to see the full decision context (verifications, consents, self-reported vs verified, bureau pull, the `decision` payload + reasons), and **act**: approve, decline, request more info, or override the automated decision.
- **Exists:** `PlatformCreditApplication` (status, decision, flow_state), `flow_orchestrator`, verifications/consents, `adverse_action` for declines.
- **Missing:** admin list/detail endpoints + **manual decision/override action** (new write path that records a staff decision + reasons, triggers adverse-action on decline) + UI.
- **Endpoints:** `GET /admin/applications?status=&vendor=&q=`, `GET /admin/applications/{id}` (full underwriting view), `POST /admin/applications/{id}/decision` (`{outcome, reasons, note}` → updates status, books loan on approve, fires adverse-action on decline; audited).

### M3 — Loan servicing
- **Purpose:** the whole loan book (across all vendors) + per-loan servicing: balance, schedule, payments, statements; record a manual payment/adjustment; generate a statement.
- **Exists:** `loan_servicing.record_payment`, `get_loan_status`, `generate_statement`, `generate_amortization_schedule`; the borrower/clinic loan-book read patterns (un-scope them for admin).
- **Missing:** admin loan list/detail + `record_payment`/adjustment + statement-gen endpoints + UI.
- **Endpoints:** `GET /admin/loans?status=&vendor=&dpd=`, `GET /admin/loans/{id}`, `POST /admin/loans/{id}/payments` (manual/adjustment, audited), `POST /admin/loans/{id}/statements` (generate).

### M4 — Disbursements (funding)
- **Purpose:** release funding for approved+signed loans, retry failed payouts, reconcile against Zumrails, track disbursement state.
- **Exists:** `PlatformLoan.disbursement_status` (not_started→in_progress→completed/failed) + `disbursement_ref`, `payments/zumrails_adapter.py`, the inbound payment webhook.
- **Missing:** an admin disbursement queue + trigger/retry actions (idempotent) + reconciliation view + UI. Also surfaces agreement status (`agreement_status`/SignNow) since funding gates on a signed agreement.
- **Endpoints:** `GET /admin/disbursements?status=`, `POST /admin/loans/{id}/disburse` (idempotent, audited), `POST /admin/loans/{id}/disburse/retry`.

### M5 — Collections & delinquency
- **Purpose:** delinquency aging across the book (30/60/90/120 buckets — parity with the dashboard tiers), run/inspect the aging job, action delinquencies, charge-off.
- **Exists:** `loan_servicing.run_delinquency_aging()`, the loan/schedule late states, the delinquency cron.
- **Missing:** admin aging views + a **charge-off action** (status → `charged_off`, audited) + collections worklist + UI.
- **Endpoints:** `GET /admin/collections/aging`, `GET /admin/collections/queue?bucket=`, `POST /admin/loans/{id}/charge-off` (audited), `POST /admin/jobs/delinquency-aging/run` (manual trigger).

### M6 — Compliance, adverse action & audit
- **Purpose:** the compliance surface — adverse-action notices (generated/sent/failed), consent records, the SIN handling posture, and the **audit log** (search the `PlatformEvent` WORM trail by actor/entity/type).
- **Exists:** `adverse_action.py` (FCRA notices), `PlatformConsent`, `PlatformEvent` (the audit spine), `consent_service.py`.
- **Missing:** an admin audit-log search endpoint + adverse-action status/resend + a compliance view + UI.
- **Endpoints:** `GET /admin/audit?actor=&entity=&type=&from=&to=`, `GET /admin/applications/{id}/adverse-action`, `POST /admin/applications/{id}/adverse-action/resend`.

### M7 — Clinics / vendors management
- **Purpose:** onboard/approve/suspend/terminate clinics, view/set compliance scores & review dates, manage memberships, and approve **vendor profile change-requests** (already exists).
- **Exists:** `Vendor` (status, compliance_score, review dates), `clinic_membership.py`, `/admin/vendor-profile-change-requests` (the one built endpoint — folds in here).
- **Missing:** vendor list/detail/lifecycle actions (approve/suspend), membership management, onboarding + UI.
- **Endpoints:** `GET /admin/vendors`, `GET /admin/vendors/{id}`, `POST /admin/vendors` (onboard), `PATCH /admin/vendors/{id}` (status/compliance — the write `vendors` *can't* do from the clinic side), plus the existing change-request approve/reject.

### M8 — Credit products & pricing
- **Purpose:** define and version the lending products — rates, terms, amount bands, the **verification matrix** (which checks each product requires), activate/deactivate.
- **Exists:** `credit_products.py` (full CRUD + versioning + `_validate_verification_matrix`), `PlatformCreditProduct`.
- **Missing:** admin product CRUD endpoints + a pricing/verification-matrix editor UI. (Mostly a thin wrapper over the existing service.)
- **Endpoints:** `GET/POST /admin/credit-products`, `GET/PATCH /admin/credit-products/{id}`, `POST /admin/credit-products/{id}/deactivate`.

### M9 — Marketplace operations
- **Purpose:** oversee the financing marketplace — listings, lead pricing/qualification tiers, charge triggers, vendor billing/lead-charge reconciliation.
- **Exists:** `services/marketplace/` (listings, pricing, billing), `lead_metrics.py`.
- **Missing:** admin marketplace views (all listings/charges across vendors) + pricing config + UI.
- **Endpoints:** `GET /admin/marketplace/listings`, `GET /admin/marketplace/billing`, pricing-config endpoints.

### M10 — Integrations & settings
- **Purpose:** manage the real-integration credentials (SendGrid/Zumrails/SignNow/Twilio/Equifax/Flinks/GA — the settings-area mandate), run connection tests, view webhook health.
- **Exists:** `integration_settings.py`, `integration_creds.py`, `connection_test.py`, the `/api/v1/integration-settings` endpoints (admin-gated), webhook routers.
- **Missing:** mostly a UI over the existing endpoints + a webhook-health/event view. (This is the most-built module already.)
- **Endpoints:** existing `/integration-settings` + `/admin/webhooks/health`.

### M11 — People & access (RBAC admin)
- **Purpose:** manage internal staff users, assign roles/permissions, deactivate accounts.
- **Exists:** full RBAC models (`UserRole`, `Permission`, `Role`, `RolePermission`, `UserRoleLink`), `auth_service.py`.
- **Missing:** admin user/role management endpoints + UI.
- **Endpoints:** `GET/POST /admin/users`, `POST /admin/users/{id}/roles`, `GET /admin/roles`.

---

## 4. API & UI conventions
- **Mount:** all under `/api/v1/admin/*`, `require_roles`-gated, RBAC-permissioned for writes.
- **Audit:** every write appends a `PlatformEvent` with the staff actor. Non-negotiable.
- **No new engine:** prefer wrapping existing services (`loan_servicing`, `credit_products`, `adverse_action`, `flow_orchestrator`, the Zumrails/SignNow adapters) over new logic.
- **Frontend:** a **new admin app/area** (likely its own route tree `/admin/*` with its own session + `RequireAdminAuth`, or a separate deploy). Reuses the brand system. This is a large UI — the biggest of the three portals.

---

## 5. Phasing (surface-first, then actions, then config)

1. **Phase 1 — Read-only ops cockpit (fast, high value):** M1 dashboard, M2 application list/detail (read), M3 loan list/detail (read), M5 aging views, M6 audit search. Pure surfacing of existing data → immediate operational visibility. Low risk.
2. **Phase 2 — Core write actions (the operating loop):** M2 manual decision/override, M3 record payment, M4 disburse/retry, M5 charge-off. Each audited + permissioned. This is what lets PaySpyre actually *operate* the book.
3. **Phase 3 — Configuration & admin:** M7 vendor lifecycle, M8 product/pricing editor, M9 marketplace ops, M10 integrations UI, M11 RBAC admin.
4. **Phase 4 — Reporting & polish:** portfolio/vintage analytics, exports, saved views.

Each phase = admin API module(s) (wrapping existing services) + tests + the corresponding admin UI screens.

---

## 6. Open questions (Dave / product)
1. **Separate admin app vs a route tree in the existing frontend?** (recommend a separate `/admin` area or app — different audience, security posture, and deploy.)
2. **Role granularity** for launch — is `admin` + `staff` enough, or do we need `underwriter` / `servicing` / `finance` roles day one?
3. **Manual decisioning** — does an operator override the automated decision directly, or only action a manual-review queue the engine routes to them? (affects M2's write path)
4. **Maker-checker** on the riskiest actions (disbursement, charge-off, pricing changes) — require a second approver?
5. **Disbursement authority** — fully manual release, or auto-release on signed-agreement with admin only handling exceptions?

---

## 7. Non-goals (this spec)
- The clinic/vendor console and borrower portal (already built — this portal is the internal counterpart, not a replacement).
- Re-building the lending engine — it exists; this surfaces and operates it.
- Accounting/GL integration (QuickBooks etc.) — a later finance concern.

---

## 8. Reality check on size
This is **the largest of the four portals** by far — it's effectively the LOS + LMS admin (everything Turnkey Lender did). The good news is the engine is built, so Phase 1 (read-only cockpit) is achievable quickly and delivers real operational value; the write-actions and config build out from there. Recommend building it **phase by phase**, each shippable, rather than all at once.
