# Vendor Performance Dashboard — API Contract & Spec

**Status:** Draft for review
**Author:** (drafted with Claude Code)
**Scope:** Backend API contract for a vendor-facing "see everything about my account / overall performance" dashboard. Frontend lives in `payspyre-frontend` and builds against this contract.

---

## 1. Context & what already exists

In this platform **clinics ARE vendors** (`vendors` table, v2 spec §13). A vendor-scoped portal already exists at **`/api/clinic/v1`** with the hard parts solved:

- **Auth + tenant scoping** — `get_current_clinic_user` ([deps.py:42](../app/api/clinic/v1/deps.py)) resolves the caller's `vendor_id` via `platform_clinic_memberships` and returns a `ClinicPrincipal`. Every endpoint filters on `principal.vendor_id`; no cross-vendor leakage.
- **Existing endpoints:** `GET /products`, `GET /applications`, `GET /dashboard/summary` (4 status buckets only), marketplace leads/interest/booking, per-vendor billing, `POST /financing-links`.
- **Schema convention** — response shapes mirror the frontend TS interfaces in `payspyre-frontend/src/lib/clinicApi.ts`. Money is **always integer cents**. Field names match the TS contract exactly so the console can drop mocks unchanged. ([schemas.py](../app/api/clinic/v1/schemas.py))

**The gap:** the current dashboard is 4 counts. There is no performance/analytics layer (rates, trends, funnel, payment performance, revenue) and no account self-service. This spec defines that layer.

All new endpoints live under the existing prefix → **`/api/clinic/v1/dashboard/*`** and **`/api/clinic/v1/account/*`**, reuse `get_current_clinic_user`, and follow the cents/`from_attributes`/TS-mirror conventions.

---

## 2. Data availability — what's backable today

| Domain | Source | Vendor scoping | Backable now? |
|---|---|---|---|
| Application volume / status / decisions | `platform_credit_applications` (`status`, `decision`, `decision_at`, `requested_amount_cents`, `created_at`, `vendor_id`) | direct `vendor_id` FK | ✅ Yes |
| Approval rate, avg amount, trends | same | direct | ✅ Yes |
| Loan book (active, paid off, delinquent), disbursement | `platform_loans` (`status`, `principal_cents`, `principal_balance_cents`, `disbursed_at`, `disbursement_status`) | **via join** `platform_loans.application_id → platform_credit_applications.vendor_id` (no direct vendor_id) | ✅ Yes (join) |
| Payment performance (on-time / late / delinquent) | `platform_loan_schedule` (`due_date`, `status` paid/late, `paid_cents`), `platform_loan_payments` | same join chain | ✅ Yes (join) |
| Marketplace funnel & revenue / lead charges | `platform_marketplace_*` + `vendor_billing()` ([listings.py:444](../app/services/marketplace/listings.py)) | direct vendor scoping | ✅ Yes |
| Verification cost attribution | `platform_verifications` + metrics | per-application | ⚠️ Partial — aggregate only |

**Gotchas to settle before coding:**
1. **Two application worlds.** `Vendor.applications` (ORM relationship, [loan.py:170](../app/models/loan.py)) points at the **legacy** `LoanApplication`, *not* `PlatformCreditApplication`. The clinic API and this spec use `PlatformCreditApplication.vendor_id`. Do **not** use the ORM `vendor.applications` accessor — it's the wrong table.
2. **Loans have no `vendor_id`.** Every loan-book / payment-performance query must join through `application_id`. Index check: confirm `platform_credit_applications.vendor_id` and `platform_loans.application_id` are indexed (the latter is unique-constrained, so yes).
3. **Status vocab mismatch.** Platform app status has 9 values; the console collapses to 4 via `status_map.to_clinic_status`. Performance KPIs need the *full* 9-value vocab (e.g. distinguish `under_review` vs `pre_qualified`), so KPI endpoints should expose raw platform statuses, not the collapsed 4.

---

## 3. Endpoint contract

All endpoints: `GET`, staff-authenticated (platform JWT), scoped to `principal.vendor_id`. Time params are ISO-8601; default window = last 30 days. All money in integer cents.

### 3.1 `GET /dashboard/overview`
Single "everything at a glance" payload for the landing screen (one round-trip).

```ts
interface VendorOverview {
  vendor: { id: string; business_name: string; status: VendorStatus };
  window: { from: string; to: string };               // echo of effective window
  applications: {
    total: number;
    approved: number; declined: number; in_review: number; started: number;
    approval_rate: number;                              // approved / decided, 0..1, null if no decisions
    avg_requested_amount_cents: number;
    total_requested_amount_cents: number;
  };
  loan_book: {
    active_count: number; active_principal_balance_cents: number;
    disbursed_count: number; disbursed_amount_cents: number;
    paid_off_count: number; delinquent_count: number;
  };
  payments: {
    on_time_rate: number;                              // 0..1, schedule items paid on/before due
    late_count: number; delinquent_count: number;
    collected_cents: number;                            // payments received in window
  };
  marketplace: {
    leads_available: number; interests_expressed: number;
    appointments_booked: number; charges_cents: number;
  };
}
```

### 3.2 `GET /dashboard/applications/timeseries?from&to&granularity=day|week|month`
Volume + outcome trend for charts.
```ts
interface AppTimeseriesPoint {
  bucket: string;              // ISO date at bucket start
  started: number; approved: number; declined: number; in_review: number;
  requested_amount_cents: number;
}
type AppTimeseries = { granularity: "day"|"week"|"month"; points: AppTimeseriesPoint[] };
```

### 3.3 `GET /dashboard/funnel?from&to`
Conversion funnel (application origination → marketplace).
```ts
interface VendorFunnel {
  // application funnel
  started: number; verifying: number; pre_qualified: number;
  approved: number; disbursed: number;
  // marketplace funnel (parallel track)
  leads_viewed: number; interest_expressed: number;
  appointment_booked: number; charged: number;
}
```

### 3.4 `GET /dashboard/loan-book?status=&limit=&cursor=`
Paginated loan list for the vendor, with servicing state. Cursor pagination (created_at, id).
```ts
interface VendorLoanRow {
  loan_id: string; application_id: string;
  patient_name: string;
  principal_cents: number; principal_balance_cents: number;
  status: PlatformLoanStatus;            // pending_disbursement|active|paid_off|...
  disbursement_status: string; disbursed_at: string | null;
  next_due_date: string | null; next_due_cents: number | null;
  days_past_due: number;                 // 0 if current
}
type VendorLoanBook = { rows: VendorLoanRow[]; next_cursor: string | null };
```

### 3.5 `GET /dashboard/revenue?from&to`
Marketplace earnings / lead-charge history (wraps existing `vendor_billing`).
```ts
interface RevenuePoint { bucket: string; charges_cents: number; charge_count: number; }
interface VendorRevenue {
  total_charges_cents: number; charge_count: number;
  by_trigger: Record<string, number>;   // charge_trigger -> cents
  timeseries: RevenuePoint[];
}
```

### 3.6 `GET /account/profile` (read-only) & `POST /account/profile/change-requests`
**DECISION (locked):** vendors **cannot** edit their profile directly. The profile is **read-only**; any change is submitted as an **admin-routed change request** that an admin must approve before it mutates the `vendors` row. There is **no** vendor-facing PATCH/PUT that writes to `vendors`.

**`GET /account/profile`** returns the full `Vendor` business/contact/address + `status`, `compliance_score`, `license_*` (all read-only to the vendor).
```ts
interface VendorProfile {
  id: string; business_name: string; dba_name: string | null; business_type: string;
  contact_name: string; email: string; phone: string;
  address: { line1: string; line2: string|null; city: string; province: string; postal_code: string };
  status: VendorStatus; compliance_score: number | null;
  license_number: string | null; license_expiry: string | null;  // read-only
}
```

**`POST /account/profile/change-requests`** — vendor submits *requested* changes (does NOT mutate `vendors`). Persists a `platform_vendor_profile_change_requests` row (status `pending`) for an admin to approve/reject out-of-band. Returns the recorded request.
```ts
interface ProfileChangeRequestBody {
  // any subset of vendor-editable fields; whitelist enforced server-side
  contact_name?: string; email?: string; phone?: string;
  address_line1?: string; address_line2?: string | null;
  city?: string; province?: string; postal_code?: string;
  note?: string;                       // free-text reason for the change
}
interface ProfileChangeRequest {
  id: string; vendor_id: string;
  requested_changes: Record<string, string>;   // only whitelisted keys
  status: "pending" | "approved" | "rejected";
  note: string | null;
  created_at: string;
}
```
**Whitelist (vendor-requestable):** `contact_name`, `email`, `phone`, `address_line1`, `address_line2`, `city`, `province`, `postal_code`. **Never requestable:** `business_name`, `dba_name`, `business_type`, `status`, `compliance_score`, `license_*` — these are admin-only and any such key in the body is rejected (422).

**Admin counterpart (BUILT — on the main `/api/v1`, admin-role-gated, NOT the clinic API):**
- `GET  /api/v1/admin/vendor-profile-change-requests?status=&vendor_id=&limit=` — review queue across all vendors
- `GET  /api/v1/admin/vendor-profile-change-requests/{id}` — detail
- `POST /api/v1/admin/vendor-profile-change-requests/{id}/approve` — applies the **whitelisted** `requested_changes` to the `vendors` row (the ONLY write path), stamps `reviewed_by`/`reviewed_at`/`review_note`. Whitelist re-enforced at apply time (admin-only keys skipped). Pending-only → 409 if already decided.
- `POST /api/v1/admin/vendor-profile-change-requests/{id}/reject` — marks rejected, stamps review audit, does NOT mutate `vendors`.

Guarded by `require_roles("admin")`. Review audit columns added in **migration 035**. The clinic-facing API itself still exposes no mutation of `vendors`.

---

## 4. KPI definitions (precise, to avoid frontend/backend drift)

- **approval_rate** = `approved / (approved + declined)` over apps whose `decision_at` ∈ window. `null` when denominator = 0. (Excludes in-review/withdrawn/expired from the denominator.)
- **on_time_rate** = schedule items with `status='paid'` AND `paid` on/before `due_date`, ÷ all due schedule items in window.
- **days_past_due (dpd)** = `today − earliest unpaid schedule item due_date` (0 if none overdue).
- **Delinquency tiers (LOCKED — industry standard, configurable grace):**
  - `current` — dpd = 0
  - `late` — `1 ≤ dpd ≤ GRACE_DAYS` (default `GRACE_DAYS = 15`; overdue, late fee may apply per contract, **not** bureau-reportable)
  - `delinquent` — `dpd ≥ 30`, sub-bucketed **30 / 60 / 90 / 120** to match Canadian credit-bureau categories
  - (`16 ≤ dpd < 30` is still counted as `late` for the late bucket; "delinquent" begins at 30 to align with bureau reporting)
  - `GRACE_DAYS` lives in settings (default 15) so collections policy can tune it without code change.
- **active_principal_balance_cents** = `Σ principal_balance_cents` where loan `status='active'`.
- **collected_cents** = `Σ platform_loan_payments.amount_cents` in window.
- Marketplace funnel counts come from `platform_marketplace_*` interest/charge rows; **charges** = interests with `lead_charged_at` ∈ window.

---

## 5. Phasing (recommended build order)

1. **Phase 1 — Applications analytics** (zero new joins, pure `platform_credit_applications`): `/dashboard/overview` (applications block only) + `/dashboard/applications/timeseries`. Ships value fast, low risk.
2. **Phase 2 — Loan book & payments** (join through application): `/dashboard/loan-book`, payments block of overview. Needs the join-perf check.
3. **Phase 3 — Marketplace funnel & revenue**: `/dashboard/funnel`, `/dashboard/revenue` (mostly wraps existing services).
4. **Phase 4 — Account self-service**: `/account/profile` GET + PATCH.

Each phase = response schemas in `schemas.py`, an endpoint module under `app/api/clinic/v1/endpoints/`, registered in `router.py`, scoped via `get_current_clinic_user`, plus tests mirroring the existing clinic test pattern.

---

## 6. Open questions (need Dave / product)

1. ~~Delinquency grace period~~ — **RESOLVED:** 15-day grace (`late`), delinquent at 30+ dpd bucketed 30/60/90/120 (§4). `GRACE_DAYS` configurable.
2. **Default dashboard window** — 30 days assumed; confirm. Lifetime totals also wanted on overview?
3. ~~Profile edit scope~~ — **RESOLVED:** read-only for vendors; all edits via admin-routed change request (§3.6).
4. **Verification cost visibility** — should vendors see what verifications cost them, or is that internal-only?
5. **Multiple users per clinic** — overview is clinic-wide; do we ever need per-staff-member attribution?

---

## 7. Non-goals (this spec)

- The frontend UI (separate `payspyre-frontend` effort; this is the contract it consumes).
- Admin-side vendor management / onboarding (separate admin surface).
- Real-time/streaming; all endpoints are request/response reads.
- Row-level-security wiring (the app-layer `vendor_id` filter is the enforcement boundary today; RLS hardening tracked separately).
