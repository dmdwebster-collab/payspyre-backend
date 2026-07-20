# Turnkey-Parity Report Exports

**Source spec:** Dave's seven custom PaySpyre-branded report templates built inside
Turnkey Lender with the Smart Marker system (template export dated 2025-08-18, plus
the TL "List of Smart Markers" KB reference). These are the operational artifacts
vendors and ops receive today; after cutover the platform must produce the same
files natively.

**Implementation:**

| Piece | Path |
|---|---|
| Report builders + XLSX renderer | `app/services/report_exports.py` |
| Admin endpoints (whole book, `vendor_id`/date filters) | `GET /api/v1/admin/reports/exports[/{key}]` (`app/api/v1/endpoints/admin_report_exports.py`) |
| Clinic endpoints (hard-scoped to caller's vendor) | `GET /api/clinic/v1/reports/exports[/{key}]` (`app/api/clinic/v1/endpoints/report_exports.py`) |
| Tests | `tests/test_report_exports.py` |

Report keys: `borrower-data`, `loan-originations`, `payments-received`,
`payments-failed`, `payments-reversed`, `loan-transactions`, `scheduled-payments`.
Every sheet carries the TL template's branding block (PaySpyre Financial Inc. /
100-2033 Gordon Dr. / Kelowna …), the same column headers, a totals row
(`SUBTOTAL(109, …)` so it respects Excel filtering, like TL's), an auto-filter and
frozen header row.

## Data sources

The immutable **money ledger** (`platform_loan_transactions`, WS-A / migration
049) is the source of truth for payment and transaction rows: per-row
principal/interest/fees allocation, Dave's `{vendor}-{loan}-{seq}` reference,
dual effective/processing dates, and compensating `reversal` rows. Loans with
no ledger rows (a pre-cutover book that never received its reconciliation row)
fall back to replaying `platform_loan_payments` against the schedule with the
pre-ledger `record_payment` allocation order (oldest installment first,
principal before interest) — `allocate_payment_splits()`.

## Column → source mapping

| TL Smart Marker | Platform source | Notes |
|---|---|---|
| `Loans_Vendor` | `vendors.business_name` via `platform_loans.application_id → platform_credit_applications.vendor_id` | |
| `Loans_Store` (Provider) | `vendors.dba_name` (fallback `business_name`) | **Gap 1** below |
| `Loans_LoanID` | `legacy_account_number` if migrated, else loan UUID | keeps TL account numbers recognizable post-migration |
| `Customers_FullName/Email/Phone/Street/City/State/ZipCode` | application PII snapshot (`first_name`…`residence_postal_code`), patient fallback for name/email/phone | |
| `Loans_Status` | `platform_loans.status`, humanized | |
| `Loans_DisbursementDate` (Activation) | `disbursed_at` | |
| `Loans_CloseDate` | `updated_at` when status ∈ {paid_off, charged_off, cancelled} | **Gap 3** — no dedicated close timestamp |
| `Loans_LoanAmount` | `principal_cents` | |
| `Loans_AverageInstallment` | Σ schedule `total_cents` / n installments | same definition as the TL footnote |
| `Loans_InterestRate` | `annual_rate_bps` / 10 000, percent-formatted | |
| `Loans_LoanTermFull` | `term_months` + " months" | |
| `Loans_OutstandingPrincipal` | `principal_balance_cents` | as-of-generation, matching the TL footnote |
| `Transactions_TransactionDate` | ledger `effective_date` (Actual Time = ledger `created_at` — the dual-date mandate) | fallback: `received_at` |
| `Transactions_Type` | ledger `txn_type` (Payment / Disbursement / Fee / Adjustment / Reversal) | |
| `Transactions_Amount/Interest/Principal` | ledger `amount_cents` / `interest_cents` / `principal_cents` allocation buckets | fallback: replay-derived split |
| `Transactions_Fee` / Admin Fees Pd | ledger `fees_cents` | one bucket — no admin/penalty split yet (**Gap 2**) |
| `Transactions_PaymentType` (Method) | ledger `payment_type` → TL vocabulary (Cash / Check / Bank Transfer / Card) | |
| `Transactions_Reference` | ledger `reference` (`{vendor}-{loan}-{seq}`) | fallback: `external_ref` / `disbursement_ref` |
| Reversed payments | `reversal` rows joined to the original payment row; windowed on the reversal's `effective_date`, displaying the original payment's date/amounts | |
| `ExpectedPayments_NextPaymentDate/ExpectedAmount/Status` | open `platform_loan_schedule` items (`total_cents − paid_cents`; Scheduled / Grace / Past Due / Partial from due date vs `GRACE_DAYS`) **plus** `platform_loan_custom_transactions` with status `scheduled` ("Scheduled (Custom)") | matches TL's scheduled/grace/past-due vocabulary; custom txns are WS-F |

## Known gaps (rows that can't be populated yet)

1. **Provider/Store entity.** TL models Vendor → Store; the platform has a single
   `vendors` table. Provider column = `dba_name` for now. When a provider/store
   entity lands (same note as `admin_analytics.py`'s provider drill), swap
   `_vendor_names()`.
2. **Failed payments are headers-only, and fees have one bucket.** The ledger
   records money that *moved* — a failed pull moves nothing, and no
   payment-attempt table exists yet, so `payments-failed` ships with correct
   headers and no rows until attempt tracking lands (auto-collection WS). The
   ledger's single `fees_cents` bucket also can't split admin vs penalty fees,
   so the penalty column is 0 and fees ride in Admin Fees Pd.
3. **Close Date is approximated** by the loan's last `updated_at` for terminal
   statuses. A dedicated `closed_at` column would make it exact.
4. **Statuses in `scheduled-payments`** are derived live from due date + grace
   window rather than the aging cron's stored status, so the report is correct
   even between cron runs.

## Verification

`tests/test_report_exports.py` (14 tests, live test DB): ledger-sourced payment
allocations + reversal routing (reversed payments leave the Received report and
appear on the Reversed report), full ledger transaction dump, fallback
split-replay math (principal-before-interest, waived installments,
overpayment), sheet content, admin vendor filter, clinic hard-scoping (vendor A
never sees vendor B), custom scheduled transactions, 404/422 handling, catalog
endpoint.
