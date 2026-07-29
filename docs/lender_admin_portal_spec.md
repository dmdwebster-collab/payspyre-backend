# Lender / Admin Operations Portal — Spec & Roadmap

**Status:** Draft v2 — upgraded with (a) full parity review of the 190-screen Turnkey incumbent, (b) deep-research benchmark of best-in-class lender cockpits (22 verified claims), (c) locked modernization principles.
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

**What's missing is the admin API surface and the UI to drive all of it.** Today the only admin endpoint is `/api/v1/admin/vendor-profile-change-requests` (approve/reject) — one seed of a surface that needs ~12 modules.

---

## 1.5 Three inputs shaped this spec

1. **Parity baseline — the Turnkey Lender admin we're retiring** (190 current-platform screenshots reviewed). The incumbent is a deep LOS+LMS: a **6-stage pipeline** `Origination → Risk Evaluation → Underwriting → Servicing → Collection → Archive`, plus Reports, Settings, and Tools. **Nothing here may be lost in the migration** — it's the floor.
2. **Best-in-class — deep research** (22 adversarially-verified claims; LoanPro, Peach, Bridgeforce, Oracle FLLL, FFIEC/CFPB, MBA, Experian, et al.). Defines where the bar *should* be. Folded into the modules below as **[BIC]** items.
3. **Modernization principles (locked with Dave):** keep the incumbent's depth, rebuild the experience. The incumbent is feature-rich but dense/dated — our edge is the *same power, modernized*.

> **Refuted by the research (don't over-build):** "AI-powered auto-decisioning" as a must-have (vendor hype), a rigid 6-type exception taxonomy, and strict "KYC must finish before underwriting begins" sequencing — all killed 0-3. Keep decisioning rules-based + human-in-the-loop; surface KYC/sanctions as gates, not a hard pre-sequence.

## 1.6 Modernization principles (the design bar)
1. **One smart account-360, not 15-tab soup** — a persistent summary header + progressively-disclosed sections + contextual side panels.
2. **Work-queues that *drive* action** — surface **next-best-action**, saved filters, SLA/aging indicators, bulk actions. **[BIC]** prioritized **scored** worklists, not flat per-status lists.
3. **Decisioning shows its reasoning** — approve/decline/override panel with the *why* inline (rules fired, scorecard breakdown, verification/bureau results), reason codes, **[BIC] explicit Override-OK / Warning-OK confirm gates**, and a live offer-builder that previews the amortization schedule as terms change.
4. **Explorable analytics** — drill-down, period comparison, **[BIC] roll/cure rates, CEI, vintage cohort curves** — not static report cards.
5. **Fast, modern interaction** — global command-palette search, keyboard-driven, inline edits, real-time updates, responsive, accessible.
6. **On-brand** — teal / Cormorant / Work Sans, Paper-and-Ink, generous whitespace — the opposite of the dense gray incumbent.
7. **Audit + maker-checker baked into the chrome** — every action shows who/when; the riskiest (disburse, charge-off, pricing) require a second approver.
8. **Role-based cockpits** — an underwriter, a collections agent, and an admin each land on a surface tuned to *their* job.

## 1.7 Information architecture (the shell)
- **Pipeline shell** mirroring the operational lifecycle: **Dashboard · Applications (Origination→Risk→Underwriting, unified) · Loans (Servicing) · Disbursements · Collections · Marketplace · Reports · Settings · Tools** — collapsing the incumbent's split Origination/Risk-Evaluation/Underwriting into **one decisioning surface** with stage filters.
- **The canonical pattern** is **work-queue (left) + account-360 (right)**, reused across applications, loans, and collections: a rich header (score, requested/offered amount, APR, clinic, dates, status), **action buttons**, an **alert/flag banner**, a **"next action"** prompt, **comments/notes**, and well-organized detail (not the incumbent's ~15 cramped tabs).

---

## 2. Actors & access

- **Audience:** PaySpyre internal staff — operators, underwriters, servicing/collections, finance/admin. NOT clinics, NOT borrowers.
- **Auth:** the existing platform staff JWT + **RBAC**. All endpoints gated by `require_roles("admin")` / `require_roles("staff","admin")`, and finer `require_permission(resource, action)` for sensitive actions (decision override, disbursement, charge-off).
- **Audit:** every state-changing admin action **must** append a `PlatformEvent` (actor = the staff user) so the WORM log is the system of record for "who did what." This is a hard requirement, not optional — it's the compliance backbone.
- **Roles to define:** at minimum `underwriter`, `servicing`, `admin` (super) as `Role` rows with scoped `Permission`s, so a collections agent can't, say, change product pricing.

---

## 3. Functional modules

Each module: **purpose · what already exists · what's missing · key endpoints** (`/api/v1/admin/...`). All money in integer cents; all writes audited.

### M1 — Operations dashboard & portfolio analytics
- **Purpose:** the lender's home screen — portfolio KPIs across ALL clinics/borrowers, plus today's work (apps to review, disbursements to release, delinquencies to action).
- **KPIs — base:** origination volume & approval rate, outstanding principal, active/delinquent/charged-off counts, delinquency rate, disbursed vs pending, marketplace revenue.
- **[BIC] KPIs the incumbent + research say to add:** **roll rate** (accounts migrating to the next DPD bucket) and **cure rate**; **Collection Effectiveness Index (CEI)** with an ~85% target; **promise-to-pay / right-party-contact** rates; **vintage / cohort curves** (cumulative default by months-on-book, by origination cohort); profit & profit/portfolio; write-off-reason breakdown; geographic distribution. Real-time, with **period comparison** and drill-down (parity with the incumbent Reports: Business performance / Portfolio / Operational / Risks / Scoring / Underwriting).
- **Exists:** the data + `platform_metrics`. This is the *vendor dashboard, god-mode* (no `vendor_id` scoping) — but with the deeper risk/collections KPIs above.
- **Missing:** aggregate-across-all-vendors endpoints + the vintage/roll/CEI computations + UI.
- **Endpoints:** `GET /admin/dashboard/overview`, `/admin/dashboard/portfolio` (outstanding, by-status, by-vendor), `/admin/dashboard/vintage` (cohort curves), `/admin/dashboard/roll-rates`, `/admin/work-queue` (counts).

### M2 — Applications & underwriting (the decisioning surface)
- **Purpose:** the review queue (unifying Origination → Risk Evaluation → Underwriting with stage filters) — open an application to the full decision context and **act**: approve, decline, request info, or override.
- **Decision context to surface:** verifications (ID/bank/bureau), consents, self-reported vs verified, **the scorecard breakdown** (which characteristics scored what — see M12), the rules that fired, the bureau pull, and the `decision` payload + reasons.
- **[BIC] decisioning UX (researched + incumbent):** explicit **Override-OK / Warning-OK confirm gates** (you must consciously confirm an override of a rule/warning); **exception routing by severity & type** to specialist / senior underwriter / compliance; **sanctions/KYC hits surfaced as a gate** that escalates before funding (not a hard pre-sequence — refuted); a **live offer-builder** that previews the amortization schedule as amount/term/rate change, supporting **multiple offers** ("Create Loan Offers" — present several to the applicant); incumbent actions to preserve: **Reprocess** (re-run decision after new data), **Change terms**, **Bank verification**, **Cancel**. Every override/condition/manual decision is **audited** (M6).
- **Exists:** `PlatformCreditApplication`, `flow_orchestrator`, verifications/consents, `adverse_action` for declines.
- **Missing:** admin list/detail endpoints; the **manual decision/override write path** (records staff decision + reason codes, books loan on approve, fires adverse-action on decline); offer-builder; exception routing; reprocess action + UI.
- **Endpoints:** `GET /admin/applications?stage=&status=&vendor=&q=`, `GET /admin/applications/{id}`, `POST /admin/applications/{id}/decision` (`{outcome, reason_codes[], note, override?}` — audited), `POST /admin/applications/{id}/offers` (build/preview offers), `POST /admin/applications/{id}/reprocess`, `POST /admin/applications/{id}/assign` (route to a queue/user).

### M3 — Loan servicing
- **Purpose:** the whole loan book + per-loan servicing: balance, schedule, payments, statements, transactions; record payments/adjustments; modify the loan.
- **[BIC] servicing capabilities (researched + incumbent):** **payment waterfall** config + **payment orchestration** (autopay/PAD/ACH, scheduled transactions, **retries**); **rescheduling / loan maintenance / restructuring** (incumbent has explicit Rescheduling + Maintenance + Scheduled-transactions tabs); a **payout/early-settlement quote** (principal balance + accrued + future interest + fees → total payoff — incumbent "Payout calculation"); **immutable-ledger, replay-forward corrections** — Peach-style **"Loan Replay"**: a backdated correction recomputes the schedule forward while the original ledger is preserved (vs destructive edits). **Note:** replay-forward may need a ledger-model change — see open questions.
- **Exists:** `loan_servicing.record_payment`, `get_loan_status`, `generate_statement`, `generate_amortization_schedule`; `PlatformLoan` + schedule/payments/statements; the borrower/clinic loan-book read patterns (un-scope for admin).
- **Missing:** admin loan list/detail; payment/adjustment + statement-gen endpoints; reschedule/maintenance + payout-quote + (eventually) replay-correction + UI.
- **Endpoints:** `GET /admin/loans?status=&vendor=&dpd=`, `GET /admin/loans/{id}`, `POST /admin/loans/{id}/payments` (manual/adjustment, audited), `POST /admin/loans/{id}/statements`, `POST /admin/loans/{id}/reschedule`, `GET /admin/loans/{id}/payoff-quote?date=`, `POST /admin/loans/{id}/corrections` (replay-forward; phase-gated on the ledger decision).

### M4 — Disbursements (funding)
- **Purpose:** release funding for approved+signed loans, retry failed payouts, reconcile against Zumrails, track disbursement state.
- **Exists:** `PlatformLoan.disbursement_status` (not_started→in_progress→completed/failed) + `disbursement_ref`, `payments/zumrails_adapter.py`, the inbound payment webhook.
- **Missing:** an admin disbursement queue + trigger/retry actions (idempotent) + reconciliation view + UI. Also surfaces agreement status (`agreement_status`/SignNow) since funding gates on a signed agreement.
- **Endpoints:** `GET /admin/disbursements?status=`, `POST /admin/loans/{id}/disburse` (idempotent, audited), `POST /admin/loans/{id}/disburse/retry`.

### M5 — Collections & delinquency
- **Purpose:** work delinquent accounts across the book — aging, worklist, relief, charge-off.
- **[BIC] (researched + incumbent):** aging by **daily DPD** *and* the 30/60/90/120 buckets, **trended** (the buckets are the reporting standard, but track DPD daily); a **prioritized SCORED worklist** with rule-based automation (not a flat per-bucket list — assign/route by risk + balance + recency, surface **next action** per account); **relief tools** — temporary payment plans, deferrals, **modification/restructuring**, hardship; **promise-to-pay** + contact/outcome capture (timing TBD — see open questions); **charge-off workflow** with reason codes (bankruptcy/insolvency/etc., parity with the incumbent write-off reasons). KPIs (CEI, roll/cure, PTP-kept) surface in M1.
- **Exists:** `loan_servicing.run_delinquency_aging()`, loan/schedule late states, the delinquency cron, the dashboard delinquency tiers.
- **Missing:** scored-worklist endpoint + relief actions (payment-plan/modification) + promise-to-pay capture + **charge-off action** (status → `charged_off`, reason-coded, audited) + UI.
- **Endpoints:** `GET /admin/collections/aging` (DPD + buckets, trended), `GET /admin/collections/queue?priority=&bucket=&assignee=`, `POST /admin/loans/{id}/payment-plan`, `POST /admin/loans/{id}/promise-to-pay`, `POST /admin/loans/{id}/charge-off` (`{reason_code, note}`, audited), `POST /admin/jobs/delinquency-aging/run`.

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
- **Purpose:** define and version the lending products — and (parity with the incumbent's product builder) much more than rate/term/amount: **schedule-building rules, grace periods, payment holidays, due-date logic & reasons, payoff rules, disbursement options, repayment modes, required documents**, and the **verification matrix**. Activate/deactivate; version.
- **Exists:** `credit_products.py` (CRUD + versioning + `_validate_verification_matrix`), `PlatformCreditProduct`. (Some builder facets — payment holidays, repayment modes — may need new config fields.)
- **Missing:** admin product CRUD endpoints + the full product-builder UI.
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

### M12 — Decision engine & scorecards (the underwriting brain) — *parity gap my v1 missed*
- **Purpose:** configure how applications are scored & decided — the incumbent's most powerful module: **scorecards** (Application Check + Bank Account Check) with weighted **scoring characteristics** (marital status, dependents, education, citizenship, residential status, income, etc.), **risk segments**, and **decision rules** (auto-approve/refer/decline thresholds, policy rules). This drives M2's decision context.
- **Exists:** the product `verification_matrix` + `flow_engine`/`flow_orchestrator` (decisioning), but **a configurable scorecard + rules editor is likely NEW backend** (point weights, segments, rule thresholds as data, not code).
- **Missing:** scorecard/rules data model + CRUD + a config UI. **Caveat (research):** keep it **rules-based + human-in-the-loop**; "AI auto-decisioning" was a refuted must-have — don't over-build.
- **Endpoints:** `GET/PUT /admin/decision-engine/scorecards/{type}`, `GET/PUT /admin/decision-engine/rules`, `GET /admin/decision-engine/risk-segments`.

### M13 — Application-process & notifications config — *parity gap*
- **Purpose:** configure the customer journey & comms without code — application **flow/form/co-applicant** fields, **dictionaries**, **disclaimers**, required documents; and **notification templates** (customer / co-applicant / back-office / vendor), **due-date reminders**, email templates, custom messages.
- **Exists:** consent/document services; the **event-driven notification/dunning layer is being built in parallel** (see `notification_processor` / `notification_render`) — this module is its **config + template UI**.
- **Missing:** template/flow config endpoints + UI.
- **Endpoints:** `GET/PUT /admin/notifications/templates`, `GET/PUT /admin/application-process/*`.

### M14 — Operator tools — *parity gap*
- **Purpose:** the incumbent's Tools menu — **Customer Management**, bulk **Import/Export**, **Excel reports**, **Blacklists**, **Credit Bureau** lookups, **Loan Migration** (the Turnkey importer — already built as `services/migration/turnkey.py`!), and per-vendor tools.
- **Exists:** `services/migration/turnkey.py` (loan migration), `credit_bureau.py`, export patterns; blacklist is likely **new**.
- **Missing:** admin tool endpoints + UI; a blacklist model.
- **Endpoints:** `POST /admin/tools/import`, `GET /admin/tools/export`, `GET/POST /admin/blacklists`, `POST /admin/tools/migration/*`.

---

## 4. API & UI conventions
- **Mount:** all under `/api/v1/admin/*`, `require_roles`-gated, RBAC-permissioned for writes.
- **Audit:** every write appends a `PlatformEvent` with the staff actor. Non-negotiable.
- **No new engine:** prefer wrapping existing services (`loan_servicing`, `credit_products`, `adverse_action`, `flow_orchestrator`, the Zumrails/SignNow adapters) over new logic.
- **Frontend:** a **new admin app/area** (likely its own route tree `/admin/*` with its own session + `RequireAdminAuth`, or a separate deploy). Reuses the brand system. This is a large UI — the biggest of the three portals.

---

## 5. Phasing (surface-first, then actions, then config)

1. **Phase 1 — Read-only ops cockpit (fast, high value):** the app shell + work-queue/account-360 pattern; M1 dashboard (base KPIs), M2 application list/detail (read, with decision context + scorecard breakdown), M3 loan list/detail (read), M5 aging views (DPD + buckets), M6 audit search. Pure surfacing → immediate operational visibility. Low risk.
2. **Phase 2 — Core write actions (the operating loop):** M2 decision/override **with the Override-OK gates + offer-builder + reprocess**, M3 record payment + payoff-quote + reschedule, M4 disburse/retry, M5 charge-off + payment-plan/promise-to-pay capture. Each audited + permissioned + **maker-checker on the riskiest**.
3. **Phase 3 — Configuration & admin:** M12 decision-engine/scorecard editor, M8 product builder, M7 vendor lifecycle, M13 notifications/app-process config, M9 marketplace ops, M10 integrations UI, M11 RBAC admin, M14 tools (import/export/blacklists/migration).
4. **Phase 4 — Advanced analytics & polish:** **[BIC]** vintage/cohort curves, roll/cure rates, CEI; scored collections worklist; replay-forward ledger corrections; saved views, exports, command-palette.

Each phase = admin API module(s) (wrapping existing services) + tests + the corresponding admin UI screens.

---

## 6. Open questions (Dave / product)
1. **Separate admin app vs a route tree in the existing frontend?** (recommend a separate `/admin` area or app — different audience, security posture, and deploy.)
2. **Role granularity** for launch — `admin` + `staff` enough, or `underwriter` / `servicing/collections` / `finance` day one?
3. **Manual decisioning** — operator overrides the engine directly, or only actions a manual-review queue the engine routes to them? (affects M2)
4. **Maker-checker scope** *(research flagged)* — which actions require a second approver? (recommend: disbursement, charge-off, pricing/scorecard changes, manual payment adjustments.)
5. **Disbursement authority** — fully manual release, or auto-release on signed-agreement with admin handling only exceptions?
6. **Ledger replay-forward** *(research flagged)* — re-architect the ledger for Peach-style immutable replay corrections **before** launch, or ship destructive-edit + audit now and add replay later? (recommend: later; not a Phase-1 blocker.)
7. **Promise-to-pay & contact capture** *(research flagged)* — build into M5 now, or defer until a dialer/comms loop exists? (recommend: capture the data model now, full workflow later.)
8. **Collections worklist** *(research flagged)* — rule-based prioritization at launch with a *scored* model as a fast-follow? (recommend: yes — rules first.)
9. **Scorecard/decision-rules config (M12)** — build the configurable editor now, or hard-code the current policy and add the editor later?

---

## 7. Non-goals (this spec)
- The clinic/vendor console and borrower portal (already built — this portal is the internal counterpart, not a replacement).
- Re-building the lending engine — it exists; this surfaces and operates it.
- Accounting/GL integration (QuickBooks etc.) — a later finance concern.

---

## 8. Reality check on size
This is **the largest of the four portals** by far — it's effectively the LOS + LMS admin (everything Turnkey Lender did). The good news is the engine is built, so Phase 1 (read-only cockpit) is achievable quickly and delivers real operational value; the write-actions and config build out from there. Recommend building it **phase by phase**, each shippable, rather than all at once.
