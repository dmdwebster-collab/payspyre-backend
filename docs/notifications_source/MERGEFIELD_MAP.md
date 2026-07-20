# Merge-field → template-context mapping

Dave's notification docs (this folder) are Turnkey Lender exports using
`*|MergeField|*` tokens. Our templates are Jinja2 (`{{ snake_case }}`) rendered
with `StrictUndefined` — a missing key fails the render test, never ships blank.

Fields marked **global** are injected automatically by
`app/services/notification_render.py::_global_context()` — callers never pass
them; explicit context keys with the same name win.

## Global (auto-injected)

| Turnkey field | Context key | Value |
|---|---|---|
| `*\|CompanyName\|*` | `company_name` | "PaySpyre Financial Inc." |
| `*\|SupportEmail\|*` | `support_email` | `settings.SUPPORT_EMAIL` |
| `*\|CompanyPhone\|*` | `company_phone` | `settings.COMPANY_PHONE` (omit when empty) |
| `*\|CompanyWebsiteUrl\|*` | `website_url` / `dashboard_url` | payspyre.com / portal `/account` |
| — | `terms_url`, `privacy_url`, `nominate_url` | payspyre.com legal/nominate pages |

## Person / customer

| Turnkey field | Context key | Notes |
|---|---|---|
| `*\|FullName\|*` | `full_name` | falls back to `borrower_name` in `_base.html` greeting |
| `*\|CustomerName\|*` | `customer_name` | same person; Dave's docs use both |
| `*\|EmailLogin\|*` | `email` | borrower's login email |
| `*\|CoApplicantFullName\|*` | `co_applicant_name` | |
| `*\|RecipientFullName\|*` | `recipient_name` | back-office recipient |

## Loan

| Turnkey field | Context key | Notes |
|---|---|---|
| `*\|LoanId\|*` | `loan_id` | short public id (first 8 of UUID today) |
| `*\|Vendor\|*` | `vendor_name` | |
| `*\|LoanCurrentStatus\|*` | `loan_status` | |
| `*\|LoanAmount\|*` | `amount` | formatted currency string |
| `*\|FirstInstallment\|*` | `first_payment_amount` | formatted |
| `*\|FirstInstallmentDate\|*` | `first_payment_date` | formatted |
| `*\|NextPaymentAmount\|*` | `payment_amount` | in dunning contexts (the reminder is ABOUT the next payment) |
| `*\|NextPaymentAmount\|*` | `next_payment_amount` | in payment_received, where `payment_amount` is the payment just made |
| `*\|NextPaymentDate\|*` | `due_date` | |
| `*\|OutstandingInterest\|*` | `outstanding_interest` | internal deferment-request notice |
| `*\|ContractDate\|*` | `contract_date` | internal agreement-signed notice |
| `*\|OutstandingPrincipal\|*` | `outstanding_principal` | formatted |
| `*\|OutstandingBalance\|*` | `outstanding_balance` | formatted |
| `*\|PastDueAmount\|*` | `past_due_amount` | formatted |
| `*\|AccountDueAsOf\|*` | `due_as_of` | date account became due |
| `*\|DPD\|*` | `days_overdue` | matches existing dunning context |
| `*\|Debt\|*` | `past_due_amount` | Dave uses both; one key |
| `*\|DueDate\|*` | `due_date` | |
| `*\|CancelReason\|*` | `cancel_reason` | |
| `*\|RejectionReason\|*` | `rejection_reason` | |
| `*\|WrittenOffAmount\|*` | `written_off_amount` | formatted |
| `*\|CurrentDate\|*` | `current_date` | formatted; passed by caller (no `now()` in templates) |

## Payments

| Turnkey field | Context key |
|---|---|
| `*\|PaymentAmount\|*` | `payment_amount` |
| `*\|PaymentDate\|*` | `payment_date` |
| `*\|NSFFee\|*` | `nsf_fee` |
| `*\|PTPPaymentAmount\|*` | `ptp_amount` |
| `*\|PTPPaymentDate\|*` | `ptp_date` |

## Verification / documents

| Turnkey field | Context key |
|---|---|
| `*\|BankAccountVerificationSentDate\|*` | `verification_sent_date` |
| `*\|BankAccountVerificationExpiryDate\|*` | `verification_expiry_date` |
| `*\|DocSignatureSentDate\|*` | `signature_sent_date` |
| `*\|InitialLoginUrl\|*` / `*\|PasswordResetUrl\|*` | `login_url` — **adapted**: our portal is magic-link, not password |

## Deliberate adaptations from Dave's copy

1. **Passwords → magic link.** Welcome/registration/password-reset copy
   references passwords and "Forgot Password" links. Our borrower portal uses
   email magic-link login. Templates keep Dave's structure/tone but describe
   magic-link sign-in. Flagged for Dave's review.
2. **`«BorrowerSSN»`, bank account/routing merge fields** exist in Turnkey's
   dictionary but are NOT used in any notification template we render — we do
   not put PII of that class in email. (Dave's docs don't use them in email
   bodies either; they are document-creation fields.)
3. **Turnkey table sections** (`TableStart:Schedule` etc.) are for document
   generation (loan agreements), not notifications — out of scope here.
4. **Investor/Investment fields** — no investor module on our platform; not
   mapped.
