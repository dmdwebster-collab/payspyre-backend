# WS-B — Document / merge-field engine (Turnkey "System documents" parity)

Covers GAP_ANALYSIS executive gap #4 (video 08 §2.11 System documents; video 11
borrower documents tab). Turnkey generates loan agreements, PAD agreements,
amortization + fee schedules from per-product/per-vendor VERSIONED templates
with merge fields, feeding SignNow. This is our native equivalent.

## Data model (migration `056_document_templates.py`)

| Table | Purpose |
|---|---|
| `platform_document_templates` | One row per template **version**. New version = new row; old rows are immutable history (no delete, no body update). `active` is the only mutable flag (deactivate/restore = Turnkey's history/restore). Scopes: `global`, `product` (`product_id`), `vendor` (`vendor_id`) with a coherence CHECK + partial unique indexes on (kind, scope-key, version). |
| `platform_loan_documents` | Generated snapshots per loan: rendered HTML frozen at generation time + template id/version + the scalar merge context (`merge_data`, for audit). `generated_via`: `booking` \| `on_demand` \| `borrower`. Regeneration = new row, never a rewrite. |

Seeded v1 GLOBAL defaults for all 7 kinds: `loan_agreement`, `pad_agreement`,
`amortization_schedule`, `fee_schedule`, `terms_and_conditions`,
`privacy_policy`, `account_statement` (the last extends the spec's six so
statements render through the same engine). Dave's ~20 real TL templates land
later as new versions / vendor-scoped overrides — non-destructively.

## Resolution precedence

For a loan's (product, vendor): **vendor-scoped > product-scoped > global**;
highest **active** version wins within the winning scope. Non-matching scoped
templates never apply.

## Placeholder syntax (closed substitution language — deliberately NOT Jinja;
admin-authored bodies cannot execute expressions)

* Scalar: `{{FieldName}}` → HTML-escaped value
* Table: `{{Table:TableName}}` → fully rendered `<table>`
* Unknown placeholders render **empty** and are reported by the admin preview
  endpoint (a typo never ships literal `{{...}}` to a borrower).

## Canonical merge-field dictionary

Served live by `GET /api/v1/admin/document-templates/merge-fields` (the
source of truth is `MERGE_FIELDS` / `TABLE_FIELDS` in
`app/services/document_engine.py`; a test keeps dictionary ↔ context builder in
lockstep).

### Scalar fields

| Group | Field | Meaning |
|---|---|---|
| Borrower | `BorrowerFullName` | Full legal name |
| | `BorrowerFirstName` / `BorrowerLastName` | Legal name parts |
| | `BorrowerEmail` / `BorrowerPhone` | Contact (phone E.164) |
| | `BorrowerDateOfBirth` | YYYY-MM-DD |
| Loan | `LoanId` / `LoanStatus` | Identifier, current status |
| | `PrincipalAmount` | Formatted, e.g. `$2,400.00` |
| | `AnnualInterestRate` | Formatted bps, e.g. `9.90%` |
| | `TermMonths` / `InstallmentCount` | Term + scheduled installment count |
| | `FirstDueDate` / `MaturityDate` | First/last installment due dates |
| | `TotalOfPayments` / `TotalInterest` | Scheduled totals, formatted |
| | `Currency` / `DisbursedDate` | CAD; empty until disbursed |
| Product | `ProductName` / `ProductCode` / `ProductVertical` | Credit product facts |
| Vendor | `VendorName` / `VendorDbaName` | Clinic business / DBA name |
| | `VendorEmail` / `VendorPhone` | Contact |
| | `VendorAddress` / `VendorCity` / `VendorProvince` / `VendorPostalCode` | Address |
| Company | `CompanyName` / `SupportEmail` / `CompanyPhone` | Same source as notification merge fields |
| | `WebsiteUrl` / `TermsUrl` / `PrivacyUrl` | Public URLs |
| General | `GeneratedDate` | Document generation date |
| Statement | `StatementPeriodStart` / `StatementPeriodEnd` | Statement window (statement renders only) |
| | `StatementOpeningBalance` / `StatementClosingBalance` | Formatted principal balances |
| | `StatementPrincipalPaid` / `StatementInterestPaid` | Formatted period splits |

### Table fields

| Table | Columns | Source |
|---|---|---|
| `AmortizationSchedule` | # / Due date / Principal / Interest / Total | `platform_loan_schedule` rows, installment order |
| `FeeSchedule` | Fee / Amount / When charged / Add-on | Enabled `FeeConfig`s from the product's `pricing_config` (malformed config → empty table, logged, never a crash) |

## Generation flows

* **Booking** — `loan_lifecycle.book_loan` freezes `loan_agreement` +
  `pad_agreement` snapshots (idempotent per loan+kind; best-effort — a missing
  template can never fail the money-path booking).
* **Admin on demand** — `POST /api/v1/admin/loans/{loan_id}/documents/generate`
  (any kind; `account_statement` takes `period_start`/`period_end` and reuses
  `loan_servicing.generate_statement` figures). List/detail alongside.
  `POST …/documents/send-for-signature` delegates to the hardened
  `loan_lifecycle.send_agreement` (SignNow) — graceful no-op while creds are
  absent (simulator/test mode, expected).
* **Borrower documents tab** — `/api/applicant/v1/loans/{loan_id}/documents`
  (booking agreement + own statements; download as HTML),
  `/api/applicant/v1/documents/terms-and-conditions` and `…/privacy-policy`
  (current active global version, rendered), and
  `POST /loans/{loan_id}/statements/generate` for on-demand statements
  (download now; email when SendGrid creds land). Patient-scoped, 404-not-403.

## Admin template API (versioned, never destructive)

`/api/v1/admin/document-templates` (admin role): list w/ history, detail,
`POST` new version (auto version = max+1 per kind+scope key; 409 on concurrent
race), `POST /{id}/deactivate` + `/{id}/activate`, `POST /{id}/preview`
(against a real loan or blank sample; reports unresolved fields),
`GET /merge-fields`.

## Descoped (follow-ups)

* Word/PDF export of templates and generated documents (HTML-only today).
* Rendering generated HTML into the SignNow document upload path — today the
  send flow uses the account's configured SignNow template id
  (`integration_settings.signnow.config.template_id`), matching the pre-WS-B
  wiring; the generated snapshot is the borrower-visible record.
* Per-loan-document e-sign refs (agreement status stays on the loan row).
* Admin UI (waves 3–4 frontend).
