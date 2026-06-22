# Borrower Portal — API Contract & Spec

**Status:** Draft for review
**Author:** (drafted with Claude Code)
**Scope:** A logged-in **borrower** (patient) portal where an approved/funded borrower returns to **see and manage their loan(s)** — balance, schedule, payments, statements. Backend API contract + screen list. Frontend lives in `payspyre-frontend` and builds against this contract.

---

## 1. Context & what exists

Today the patient-facing app is an **application/onboarding flow** (`/api/applicant/v1`): apply → verify → consent → SIN → decision → marketplace. **After approval there is no borrower experience** — no way to log back in and view a loan, balance, schedule, or payment.

**The good news — the spine already exists:**
- **Loan-servicing models are built + tested** ([loan.py](../app/models/platform/loan.py)): `PlatformLoan` (principal, balance, rate, term, status, disbursement/agreement state), `PlatformLoanScheduleItem` (amortization: due dates, principal/interest/total, paid, status), `PlatformLoanPayment` (received payments), `PlatformLoanStatement` (periodic statements). Money is integer cents throughout.
- **A patient-scoped JWT already exists** ([deps.py](../app/api/applicant/v1/deps.py)): the applicant token's claims are `{sub: patient_id, app_ids: [application_ids]}`. So tokens are already **patient-level**.
- **Loan → borrower linkage:** `platform_loans.application_id → platform_credit_applications.patient_id`. (Migrated loans have `application_id = NULL`, `source='turnkey_migration'` — they have no PaySpyre patient and so never surface in any borrower's portal.)

So this is largely a **new read API + UI**, not new data modeling.

This portal mounts under the existing applicant surface — **`/api/applicant/v1/loans/*`** — reusing the patient JWT and `get_current_applicant`. Every query is scoped to `claims.patient_id`.

---

## 2. Auth — the one real decision (needs Dave / product)

Patients currently get a token via a **magic link tied to a specific application** (request magic-link for `application_id` → exchange → JWT). A **returning borrower** arrives with no application in hand and needs to **log in by email and see *all* their loans**.

The token *shape* already supports this (`sub = patient_id`). The decision is the **login entry point**:

- **DECISION (LOCKED): extend magic-link to a patient-level "borrower login."** New email → magic-link → JWT carrying `patient_id` + **all** of that patient's `app_ids`. Reuses `PatientAuthService` and the existing notification dispatcher; no passwords; consistent with the rest of the product.
- ~~Alternative: password accounts~~ — rejected (heavier; more compliance surface).

**New auth endpoints (Phase 0):**
- `POST /api/applicant/v1/auth/borrower-login/request` — body `{ email }` → sends a magic link to the patient's email. Always returns 200 (don't reveal whether the email exists).
- `POST /api/applicant/v1/auth/borrower-login/exchange` — body `{ email, token }` → returns `{ jwt, patient_id }`; the JWT's `app_ids` = every application belonging to that patient (so it covers all their loans).

> Open question for Dave: is magic-link-by-email an acceptable borrower login, or is a password account required? (recommend magic-link.)

---

## 3. Data availability — what's backable today

| Screen need | Source | Backable now? |
|---|---|---|
| My loans (list, status, balance) | `platform_loans` (scoped via application→patient) | ✅ |
| Loan detail (principal, APR from `annual_rate_bps`, term, next payment, disbursement/agreement state) | `platform_loans` + earliest unpaid `platform_loan_schedule` row | ✅ |
| Amortization schedule | `platform_loan_schedule` | ✅ |
| Payment history | `platform_loan_payments` | ✅ |
| Statements | `platform_loan_statements` | ✅ |
| **Make a payment** | needs a Zumrails debit rail + idempotency | ⚠️ **Phase 2** (read-only first) |

---

## 4. Endpoint contract

All endpoints: patient-JWT authed (`get_current_applicant`), **scoped to `claims.patient_id`** (join `loan → credit_application → patient_id`). A loan whose patient ≠ the caller → **404** (not 403 — don't reveal existence). Money in integer cents; `annual_rate_bps` is basis points (1% = 100).

### 4.1 `GET /loans`
```ts
interface BorrowerLoanSummary {
  loan_id: string
  status: 'pending_disbursement' | 'active' | 'paid_off' | 'delinquent' | 'charged_off' | 'cancelled'
  principal_cents: number
  principal_balance_cents: number
  currency: string
  disbursed_at: string | null
  next_due_date: string | null      // earliest unpaid installment
  next_due_cents: number | null
  days_past_due: number             // 0 if current
}
type BorrowerLoans = { loans: BorrowerLoanSummary[] }
```

### 4.2 `GET /loans/{loan_id}`
```ts
interface BorrowerLoanDetail extends BorrowerLoanSummary {
  annual_rate_bps: number
  apr_percent: number               // annual_rate_bps / 100, for display
  term_months: number
  agreement_status: 'not_sent' | 'sent' | 'signed' | 'declined'
  disbursement_status: 'not_started' | 'in_progress' | 'completed' | 'failed'
  amount_paid_cents: number         // sum of payments
  installments_total: number
  installments_paid: number
  product_name: string | null
}
```

### 4.3 `GET /loans/{loan_id}/schedule`
```ts
interface ScheduleRow {
  installment_number: number
  due_date: string
  principal_cents: number
  interest_cents: number
  total_cents: number
  paid_cents: number
  status: 'scheduled' | 'paid' | 'partial' | 'late' | 'waived'
}
type LoanSchedule = { rows: ScheduleRow[] }
```

### 4.4 `GET /loans/{loan_id}/payments`
```ts
interface PaymentRow { amount_cents: number; received_at: string; method: string }
type LoanPayments = { rows: PaymentRow[] }   // most-recent first
```

### 4.5 `GET /loans/{loan_id}/statements`
```ts
interface StatementRow {
  period_start: string; period_end: string
  opening_balance_cents: number; closing_balance_cents: number
  principal_paid_cents: number; interest_paid_cents: number
  generated_at: string
}
type LoanStatements = { rows: StatementRow[] }
```

### 4.6 (Phase 2) `POST /loans/{loan_id}/payments`
Make a one-off payment via Zumrails debit. Idempotency-keyed; records a `PlatformLoanPayment` on success and reduces the balance. **Out of scope for Phase 1** (the portal ships read-only first). Body/return TBD with the Zumrails integration.

---

## 5. Screens (frontend, `payspyre-frontend`)
1. **Borrower login** — email → "check your inbox" → magic-link landing → into the portal. (Mock-mode dev helper auto-fills the code, like the application flow.)
2. **My loans** — card/list per loan: status badge, balance, next payment due, a progress bar (`installments_paid / installments_total`).
3. **Loan detail** — headline balance + next payment; APR / term / disbursement state; tabs or sections for **Schedule**, **Payments**, **Statements**.
4. **Schedule** — the amortization table (paid / upcoming / late per row).
5. *(Phase 2)* **Make a payment** — pay-now CTA.

Reuses the existing brand system (teal/Paper, Cormorant/Work Sans) and `ui` primitives; likely a new `BorrowerLayout` (mirrors `FlowLayout`/`ClinicLayout`) and a patient/borrower session distinct from the clinic session.

---

## 6. Field definitions (avoid drift)
- **days_past_due** = `today − earliest unpaid schedule due_date` (0 if none overdue) — same definition as the clinic loan-book (spec parity).
- **next payment** = earliest `platform_loan_schedule` row with `status != 'paid'` (and not `waived`), by `installment_number`.
- **apr_percent** = `annual_rate_bps / 100` (display only).
- **amount_paid_cents** = `Σ platform_loan_payments.amount_cents` for the loan.
- A loan is shown to a borrower iff `loan.application_id` resolves to a `credit_application` whose `patient_id == claims.patient_id`. Migrated loans (`application_id IS NULL`) never appear.

---

## 7. Phasing
1. **Phase 0 — Borrower auth:** the two `borrower-login` endpoints (patient-level magic link). Gated on the auth decision (§2).
2. **Phase 1 — Read-only servicing API + UI:** `/loans`, `/loans/{id}`, `/schedule`, `/payments`, `/statements` + the login/my-loans/detail/schedule screens. **This is the bulk of the value** — borrowers can see and understand what they owe.
3. **Phase 2 — Pay now:** `POST /loans/{id}/payments` via Zumrails (idempotent) + the pay UI. Needs the disbursement/debit rail wired.

---

## 8. Open questions (Dave / product)
1. **Borrower login model** — magic-link-by-email (recommended) vs password accounts? (Phase 0 blocker.)
2. **Pay-now in scope, and on which rail?** Zumrails debit assumed; confirm + idempotency/refund policy. (Phase 2.)
3. **Statements** — generated on a schedule already, or does this portal need to trigger/lazily generate them? (confirm a statement-generation job exists or is needed.)
4. **Co-applicants** — if a loan has a co-applicant, do both borrowers see it in their portals? (scoping nuance.)

---

## 9. Non-goals (this spec)
- The frontend implementation (separate `payspyre-frontend` effort; this is the contract).
- Disbursement/origination changes — the portal is downstream, read-only over the existing LMS spine.
- Collections workflows / lender-side servicing tools (that's the separate **lender/admin portal**, not this).
