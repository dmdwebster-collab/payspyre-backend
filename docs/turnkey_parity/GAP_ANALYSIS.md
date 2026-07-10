# Turnkey Lender → PaySpyre Parity Gap Analysis

**Source:** Dave's 11 narrated walkthrough videos of the live Turnkey Lender platform (filmed 2026-07-08/09), transcribed and frame-analyzed in full. Per-workplace exhaustive inventories live in this directory (`01__…` – `11__…`); our build baseline is `00__OUR_BUILD_INVENTORY.md`. This document is the synthesis: what we already have, what's partial, what's missing, and what Dave explicitly redefined.

**How to read the verdicts**

| Verdict | Meaning |
|---|---|
| ✅ HAVE | Built on `main`, materially equivalent (or better) |
| 🟡 PARTIAL | Core exists; gaps in depth, UI, or configurability |
| ❌ MISSING | Not built |
| 🔵 REDESIGNED | Dave explicitly wants it different from Turnkey — build his version, not parity |
| ⚪ SKIP | Dave explicitly said we don't need it |

**Priorities** — P0: can't run the loan book without it (blocks Aug test / Sept cutover). P1: needed within weeks of cutover. P2: parity/modernization backlog.

---

## Executive summary

Our build covers the **origination → decision → e-sign → disbursement skeleton** well (in several places better than Turnkey: event-sourced audit, maker-checker, consent WORM, immutable decision snapshots). The five structural gaps that would actually stop us from replacing Turnkey are:

1. **No automatic payment collection engine (P0).** Turnkey auto-charges scheduled PAD payments on due dates and handles NSF returns. We only have borrower-initiated Pay Now and staff manual entry; a failed collection just logs an event. We need: scheduled auto-charge job (Zumrails pull on due date), NSF fee + configurable retry ("amount to move" logic), disable-auto-charge flag per loan, and dead-account return-code handling.
2. **Interest engine is schedule-based, not actuals-based (P0).** Dave's product is daily simple interest recomputed from *actual* payment dates (late payment = extra per-diem, early = less). Our `record_payment` applies fixed installments. This changes payoff math, delinquency math, and the ledger. It is the money-math heart of the platform and Dave has flagged it repeatedly (his "account due as of" + "amount to move" Excel files are the spec — **get these from Dave**).
3. **No hardship / rescheduling module (P0-lite / P1).** Turnkey scatters this across "payment holiday", "rescheduling", "change due dates"; Dave wants a single permission-gated **Hardship** section: deferments, frequency/due-date changes, temporary Adjustment of Terms with auto snap-back — every change requiring borrower e-sign first. Minimum for launch: deferment + due-date change with e-sign; full AoT can follow.
4. **No document/merge-field template engine (P1).** Turnkey generates loan agreements, PAD agreements, amortization + fee schedules from per-product/per-vendor versioned templates with merge fields, feeding SignNow. We send e-sign but have no template library or merge-field generation. Also the borrower documents tab (agreement, T&Cs, privacy policy, on-demand statements) depends on it.
5. **Migration tooling (P0 for cutover).** We have `turnkey_migration` loan import hooks, but Dave explicitly wants Excel/CSV upload migration for customers, loans, payments, disbursements (Turnkey's own Import section is his blueprint). Without it there is no cutover of the ~560-loan live book.

Everything else is incremental: admin UI depth (application editing, offer editing, comms hub, flags, assignment), the collections work-surface (queues, PTP, action plans), reporting filters (vendor multi-select + profit split), settings UIs (decision rules, scorecards, notification matrix), and two big net-new features Dave spec'd that Turnkey doesn't even have (vendor self-serve disbursements; province compliance engine).

---

## Dave's cross-cutting redesign mandates 🔵

These override "copy Turnkey" everywhere they apply:

1. **Co-borrowers = completely separate applicant files**, linked by ID/email, each with identical tabs/docs/photo — not one shared record. (Our applicant-group model is close; the *file* separation and linking UX is the gap.)
2. **Consent-first, always.** "We can never subvert or get around the applicant's approval" — bureau pulls, bank pulls, everything routed through borrower consent. ✅ Already our architecture.
3. **Score only verified data.** Application scorecard on self-reported data is worthless to him; scoring runs after ID + bank + bureau verification. Additive bin-based scoring, natural min/max (no clamping), 5 risk bands (Excellent/Good/Average/Weak/Poor-Fail) with per-band credit limits and rates; per-vendor scorecard assignment.
4. **Communications log = legal evidence.** Every email/SMS/dashboard notification stored with the exact message body, per account, forever — used in collections and court.
5. **SIN optional** (legally cannot be required); 3 years of address AND employment history; FT/PT/seasonal employment types; EI and Student removed as income types.
6. **Ledger, not "transactions".** Running category balances; reference numbers auto-generated as `vendor-loan-txn#`; **two dates per transaction (effective vs processing)** with permission-bounded backdating.
7. **Write-off is not a header button.** Lives in the permission-gated hardship/collections area, dual-controlled. ✅ Our maker-checker already does this.
8. **Delinquency = month-end snapshot buckets**: current-month-late → Pot 30 → Pot 60 (bureau-reported) → Pot 90 → Default, plus a segregated insolvency portfolio. Turnkey's 1-30/31-60/61-90/91+ DPD buckets are "not accurate" for his model.
9. **Vendors originate; PaySpyre decides.** Vendor sets amount/product (they arranged the treatment); underwriting is ours; vendors get exactly one underwriting action: "Request reprocessing". Auto-rejections are silently escalated to a human before the vendor is told.
10. **Bank-level borrower security**: 2FA; no self-serve bank-account add/delete; ID images write-only after upload; address/employment edits versioned (current/former), never overwritten.

---

## Per-workplace gap tables

### 1. Originations (video 01)

| Capability | Verdict | Priority | Notes |
|---|---|---|---|
| Pipeline queue w/ filters (time/status/branch/product/assignee/vendor/province) | 🟡 | P1 | We list applications; need filter set + waiting-time + assignment columns |
| "Assign to me" + assignment gating & audit | ❌ | P1 | No assignment model on applications |
| Full credit-application data model | ✅ | — | Migration 043 canonical schema |
| Admin edit of application w/ change log | ❌ | P1 | Read-only today; edits must audit to `platform_events` |
| Loan header w/ key facts + contact info + photo | 🟡 | P1 | Detail exists; Dave wants email/phone under name + profile photo (ID-match, ideally automated) |
| Offer/terms editing w/ product min/max enforcement | 🟡 | P0 | Quote engine + APR ✅; staff offer-editing endpoint missing |
| Canadian-method APR + amortization preview | ✅ | — | SOR/2001-104 + criminal-rate check |
| Flinks bank tab (accounts, txns, income/expense/fraud attributes) | 🟡 | P1 | Adapter real; admin surface missing. Dave: rebuild expense categorization (learned from descriptions); micro-lender usage as explicit risk factor |
| Credit bureau soft/hard pull + stored reports + tradelines | 🟡 | P0* | Mock-only pending Equifax agreement (Dave item). Store full reports; tradelines clickable/editable; show reported minimum monthly payment |
| Merge-field document generation → SignNow | ❌ | P1 | See cross-cutting gap #4 |
| Documents: 3 visibility-scoped stores (staff/vendor/borrower) | 🟡 | P1 | Upload + RBAC exists; visibility scoping model missing |
| Communications hub (templated email/SMS from admin, offline contact log) | ❌ | P1 | Cross-cutting mandate #4 |
| Customer/loan flags w/ notification suppression | ❌ | P2 | |
| Vendor-visible comments | ❌ | P1 | The vendor↔underwriting channel (checklist lives here) |
| Workflow transition audit | ✅ | — | `platform_events` |
| Co-borrower as separate linked file | 🔵 | P1 | Mandate #1 |
| SIN optional / address+employment history / income types | 🔵 | P0 | Schema-level; cheap now, expensive later |

### 2. Underwriting (video 02)

| Capability | Verdict | Priority | Notes |
|---|---|---|---|
| Decision queue (waiting-for-approval) | 🟡 | P0 | `under_review` sink exists; needs queue view + assignment |
| Decision actions: Approve / Reject / Cancel / Send for reprocessing | 🟡 | P0 | Have approve/decline/refer + override. Missing: **Cancel** (non-credit closure, own reason directory) and **reprocessing** (back to vendor) |
| Multi-offer approval (n offers → borrower picks on dashboard) | ❌ | P1 | Max 3 offers; 30-day expiry; offer-acceptance confirmation stays (e-sign cost control) |
| 8-check risk pipeline w/ per-check results | 🟡 | P1 | Have identity/bank/bureau/fraud. Geolocation, web-activity, cyber checks: Dave treats as noise — don't clone; keep our check model |
| System decision → auto-approve / manual review | ✅ | — | Flow engine bands |
| Verification mismatch ⇒ mandatory human review, never auto-reject | 🔵 | P0 | Verify flow engine honors this everywhere (soft conditions → manual_review ✅; confirm no auto-decline paths on mismatch) |
| 5-band scorecard w/ per-band limits+rates, editable bands | 🔵 | P1 | Replace 680/600 two-cut with banded model (mandate #3) |
| AI bank-statement analysis (income/expense classification) | 🔵 | P1 | Dave does this manually today; "should be something an AI can do" — flow-engine input, human-visible |
| Bankruptcy policy: hard-fail w/ 2-yr-discharge rule; vendor-override → human review | 🟡 | P0 | Active bankruptcy decline ✅; discharge-age rule + override path missing |
| Standardized, compliant rejection reasons directory | 🟡 | P0 | `decision_reasons` codes ✅ + adverse action ✅; need admin-editable directory w/ borrower-facing wording |
| Workflow + comments audit w/ system auto-comments | ✅ | — | |
| Full comms log w/ message bodies | ❌ | P1 | Mandate #4 |
| Credit bureau tab redesign (sortable/editable tradelines, inquiries, public records) | ❌ | P1* | Blocked on Equifax |

### 3. Servicing (video 03)

| Capability | Verdict | Priority | Notes |
|---|---|---|---|
| Approved-pending-activation + Active queues | 🟡 | P1 | Statuses exist; queue/filter surface thin |
| Actuals-based daily simple interest engine | ❌ | **P0** | Executive gap #2. Get Dave's two Excel files |
| Payments tab (as-agreed schedule) vs Transactions (ledger) | 🟡 | P0 | Schedule ✅, payments list ✅; true ledger w/ running category balances missing (mandate #6) |
| Scheduled-transaction surgery (suspend one payment, add custom txn, mandatory comment, change history) | ❌ | P0 | "A very, very important section" |
| Auto-charge on due dates + Disable-auto-charges flag | ❌ | **P0** | Executive gap #1 |
| Repayment modes: Regular / Add-on (non-accruing fees) / Special (100% principal) / Payoff | 🟡 | P0 | record_payment is regular-only; payoff quote ✅; add-on fee bucket + special mode missing |
| Payment types (cash/check/EFT/card/adjustment) for reconciliation | ❌ | P1 | Add `payment_type` to ledger |
| Payout calculator ≤30 days forward (per-diem, staff-facing) | 🟡 | P1 | As-of payoff quote ✅; forward-dated + fee detail + "payments not suspended by payoff inquiry" rule |
| Hardship module (deferment, freq/due-date change, temp AoT w/ snap-back, e-sign gated) | ❌ | P0/P1 | Executive gap #3 |
| Effective vs processing dates + permissioned backdating | ❌ | P1 | Mandate #6 |
| Immutable decision snapshot on loan (risk + bureau frozen) | ✅ | — | Snapshots + events; new pulls diffable = P2 |
| PTP (promise to pay) | ❌ | P1 | Shared with Collections |
| Statements | ✅ | — | `generate_statement` |

### 4. Collections (video 04)

| Capability | Verdict | Priority | Notes |
|---|---|---|---|
| Delinquency queue w/ DPD, outstanding, probability-to-collect | 🟡 | P1 | Aging + queue endpoints exist; Dave's sort = principal + DPD; collectability score = explicitly nice-to-have |
| Dave's bucket state machine (month-end snapshots, Pot 30/60/90, Default) | 🔵 | P0 | Mandate #8 — our nightly aging flips statuses but isn't his snapshot model |
| Insolvency portfolio (consumer proposal/bankruptcy/credit counseling, trustee payments, maintenance fee) | ❌ | P2 | Segregated queue; post-launch |
| Bulk collector assignment (by vendor / alpha / bucket) + per-user queues + junior/senior tiers | ❌ | P1 | "Assign to me one-at-a-time is just not viable" |
| Action plans w/ settings-definable action types | ❌ | P1 | |
| Promise to pay (amount/date/comment, optional no-late-fee) | ❌ | P1 | |
| Automated dunning ladder 7/14/21/30/60/90 | ✅ | — | Identical day offsets already in `DunningPolicy` 🎯 |
| Header math: interest due + fees due + add-on balance = payout | ❌ | P0 | Depends on actuals engine + add-on bucket |
| "Account due as of" + "amount to move" (50% tolerance) algorithms | ❌ | P0 | **Dave to supply 2 Excel files**; tolerance rule lives in product/hardship settings |
| Credit bureau reporting of Pot 60+ | ❌ | P1* | With monthly Equifax reporting (Tools) |
| Write-off permission-gated | ✅ | — | Maker-checker; Dave's ask already exceeded |
| Expiring cards | ⚪ | — | "Doesn't make a lot of sense — no credit cards" |

### 5. Reports (video 05)

| Capability | Verdict | Priority | Notes |
|---|---|---|---|
| Report suite (portfolio/operational/risk/scoring/underwriting + dashboard) | ✅ | — | Our analytics suite covers the categories |
| **Vendor multi-select filter on every report** + multi-location rollup | ❌ | P1 | Dave's #1 repeated ask |
| Profit split: gross / vendor / PaySpyre | ❌ | P1 | Per-vendor profitability |
| Delinquency-bucket reporting (CML → Pot30/60/90 → default/insolvency/written-off) + debt-roll % | ❌ | P1 | Replaces "bad debt"; buckets in time series |
| Time-series page (metric dropdown, date brush, chart/table toggle, export) | 🟡 | P2 | Data endpoints ✅; presentation features frontend |
| Scoring analytics (PSI stability, scorecard accuracy, delinquency by band) | 🟡 | P2 | Have scoring report; PSI-style depth later |
| Underwriter monitoring + overrides report | 🟡 | P1 | Have underwriting report; add per-officer overrides + **vendor-overrides report** (overrides → outcomes) |
| AI-decisioning tracking | ❌ | P1 | Which decisions automated, performance vs human |
| Geographic heat map | ❌ | P2 | Nice-to-have |
| Early-payment / bad-debt / at-risk metrics re-specified for daily-simple-interest | 🔵 | P1 | Current TL definitions "wrong"; respec with actuals engine |

### 6. Archive (video 06)

| Capability | Verdict | Priority | Notes |
|---|---|---|---|
| Terminal-record archive w/ close-reason filters (cancelled/rejected/repaid/expired/written-off/bank-verification-expired) | 🟡 | P1 | Statuses + immutability ✅ (event-sourced); needs archive list view + close-reason filter + full frozen detail |
| Frozen decision snapshot on terminal records | ✅ | — | "Historical record of why and how we made the decision" — our snapshots do this |
| Polymorphic detail (repaid shows servicing data; rejected shows origination only) | 🟡 | P2 | Frontend concern |

### 7. Settings (videos 07–08)

| Capability | Verdict | Priority | Notes |
|---|---|---|---|
| Users + 19-perm matrix; add hardship / backdate / override perms | 🟡 | P1 | RBAC model ✅; granular perm set + admin UI |
| Initial passwords set by admin | 🔵 | P1 | Dave: never; invite/reset flow instead (our magic-link/invite pattern already aligns) |
| Company info (names/logo/favicon, multiple contacts) feeding docs+notifications | ❌ | P1 | Small config table |
| Branch offices | ⚪ | — | Single-branch reality; keep `branch` as a field, skip the module |
| **Province compliance engine** (per-province APR caps incl. high-cost-credit thresholds, licensing flags, disclosure + comms-frequency rules, QC language; blocks product creation on breach) | 🔵 | P1 | Net-new beyond Turnkey (TL has a checkbox list). We have the Quebec gate + criminal-rate check as seeds. Legal inputs from counsel (Dave item) |
| Integrations page (toggle, creds, test, simulator modes, provider-swappable) | ✅ | — | `integration_settings` + test endpoint; matches Dave's ask |
| Credit product editor — full pricing model (daily simple interest, rate bounds + role-gated edit, amount/term constraints, 14 fee types, accrual/repayment priority, APR auto-calc per frequency) | 🟡 | P0 | `pricing_config` JSONB holds this; needs schema formalization (pricing_config ↔ quote mismatch already a known blocker) + fee engine + frequency support |
| Repayment frequencies as checkboxes in ONE product (weekly/biweekly/semi-monthly/monthly), fees variable by frequency | 🔵 | P0 | Kills TL's "4 products per bracket is completely ridiculous"; one product per bracket |
| Grace period / due dates / due-date seasons / payoff config / disbursement options / approval / repayment modes tabs | 🟡 | P1 | Payoff = 100% principal + fees, 0% future interest; loans close at zero total debt; level installments kept by reducing count |
| Payment holiday → rebuild as Hardship tab | 🔵 | P0/P1 | Executive gap #3 |
| Delinquency buckets config | 🔵 | P0 | Dave's model (mandate #8), not TL's |
| Business calendar w/ auto Canadian stat holidays → auto payment-delay notices | ❌ | P1 | Currently manual emails by Dave |
| Decision rules (~50 toggleable rules, editable thresholds, outcome dropdown) | 🟡 | P1 | `verification_matrix` + YAML rulesets ✅ engine-side; admin UI + rule granularity missing. Values captured in `08__` file (DTI 50, PTI 20, min income $1500, min age 19, bureau 600/660…) |
| Scorecards (editable bands: ranges, PD, odds, per-band decision) | 🔵 | P1 | Mandate #3 verified-data model |
| Notification matrix (4 audiences × 3 channels × ~40 events, per-event editor w/ merge fields, attachments, branding) | 🟡 | P1 | Outbox + rules table + 15 templates ✅; audience/event coverage + admin editor missing. Dashboard channel always-on |
| Due-date reminders −7/−2, +7/14/21/30/60/90 + PAD pre-notification waiver for weekly | ✅/🟡 | P0 | Offsets identical ✅; PAD pre-notification compliance nuance to verify |
| Application process config (form editor per product, flow confirmations, offer expiry 30d, max 3 offers, dictionaries, disclaimers, co-app config) | 🟡 | P1 | We have consent/disclosure flow; per-product form assignment + dictionaries admin missing |
| **System documents: per-product/per-vendor versioned agreement templates + merge-field engine (scalar+table fields)** | ❌ | P1 | Executive gap #4; ~20 dental templates exist in TL to migrate |
| Security: audit trail everything, human-readable diffs, archival backup | ✅/🟡 | P2 | Ours is append-only & unlimited (beats TL's 90-day); diffs/PDF export later |
| API clients | ⚪ | — | Deprioritized by Dave for launch |
| Appearance/themes/field renaming | ⚪ | P2 | Our own brand system instead |

### 8. Tools (video 09)

| Capability | Verdict | Priority | Notes |
|---|---|---|---|
| Customer management CRM (lock/block w/ reason, credit-app editor, changelog, cross-loan view) | 🟡 | P1 | Patient model + docs ✅; admin CRM surface missing. NO admin-set passwords (Dave) |
| **Excel/CSV import: customers, loans, payments, disbursements (preview-then-confirm)** | 🟡 | **P0** | Executive gap #5. Migration-035 hooks exist for loans; build the rest + preview flow |
| Excel report builder (smart-tag templates, filters) | ❌ | P2 | |
| **Scheduled reports — auto monthly vendor statement emailed to vendor** | ❌ | P1 | Dave explicitly wants |
| **Equifax batch reporting (first-of-month auto-send, Equifax format)** | ❌ | P1* | Compliance-relevant; needs Equifax agreement (Dave item) |
| Blacklists (suspicious phones/emails/etc.) | ❌ | P2 | Notification suppression ≠ credit blacklist |
| Vendor CRM (vendor→provider→loan chaining, industry categories, bank accounts, contacts, MSA docs w/ expiry alerts, 9-role permission matrix, portfolio report) | 🟡 | P1 | Vendor/provider model + change-requests ✅; MSA mgmt, onboarding automation, expiry notifications missing |
| Soft-delete vendors (hidden, preserved for history) | ✅ | — | Soft-delete shipped in hardening PRs |
| "I don't have a vendor" default vendor → marketplace flow | ✅ | — | Marketplace built |
| Loan status list + add "verification" status | ✅ | — | Our status enums already richer |

### 9. Vendor Access (video 10 — this one is a requirements spec, not a demo)

| Capability | Verdict | Priority | Notes |
|---|---|---|---|
| Vendor portal = scoped backend (own portfolio only) | ✅ | — | `/api/clinic/v1` vendor_id-scoped |
| Vendor-originated application (province/product/amount/term/rate/provider/start-date/custom-first-due-date + live P+I+commission schedule preview) | 🟡 | **P0** | Financing links ✅; full intake form + live preview missing. Core flow: "PaySpyre can't know if a patient should get $500 or $5,000" |
| Application Submission Checklist data model (treatment cost − insurance − down payment = amount financed; preferred payment amount/frequency/date) | ❌ | P0 | Structured fields, not a comment |
| SMS/email-triggered borrower verification (ID + bank + bureau consent in one combined request) | 🟡 | P0 | Magic-link + consent flow ✅; combined-request UX + vendor trigger missing |
| Tab-level scoping: allow Documents/PTP/Workflow/Flags/Map/Comments; mask bank to last digits; NEVER risk score/bureau/bank statements/hardship/scheduled txns/Contacts | 🔵 | P0 | Enforce in clinic API responses (API-level, not just frontend) |
| One underwriting action: "Request reprocessing" + decision notifications; auto-rejects silently escalate to human first | 🔵 | P0 | Mandate #9 |
| **Self-serve vendor disbursements: MTD collected/due/available, 4-business-day clearing holdback, free monthly auto-payout, fee-charged extra payouts, processor→vendor direct (intermediary-status avoidance)** | ❌ | P1 | Biggest net-new ask; interacts with funding model + Zumrails wallets |
| Vendor performance dashboard ("big sexy numbers") | ✅ | — | Clinic dashboard suite |
| Vendors see Archive; never Settings/Tools | ✅/🔵 | P0 | Scoping rule |

### 10. Borrower Portal (video 11)

| Capability | Verdict | Priority | Notes |
|---|---|---|---|
| Login + password mgmt | 🔵 | — | Ours is magic-link (better UX, no password DB); Dave wants "bank-level" + 2FA → add step-up/2FA option | 
| Personal details self-edit | 🟡 | P1 | Edits must version (current/former), not overwrite; profile photo capture |
| ID upload (borrower + co-borrower, front/back) | 🟡 | P1 | Upload ✅; write-only-after-upload restriction |
| Bank details: Flinks-linked accounts, default-for-payments toggle; NO self-serve add/delete | 🟡 | P0 | Flinks link ✅; account list + default designation + lockdown rules |
| New-loan wizard in portal (terms calculator entry) | 🟡 | P1 | Terms-calculator journey ✅ (Dave's journey spec); in-portal re-origination + renewal welcomed |
| Loan detail: schedule (initial vs current, closed-payments toggle), transactions, next-payment widget + countdown | 🟡 | P1 | Endpoints ✅; initial-vs-current schedule views need actuals engine |
| **Make payment: Regular (editable) / Add-on (fee bucket) / Payoff (system-calc, non-editable, today-only)** | 🟡 | P0 | Pay Now ✅ regular-only; modes depend on servicing gaps |
| Payment method locked to default verified account | 🔵 | P0 | "Should not be modifiable at the moment" |
| Documents tab: loan agreement (static, generated at booking), T&Cs, privacy policy | ❌ | P1 | Depends on doc engine |
| **On-demand account statements (tax time; download or email)** | 🟡 | P1 | Statement generation ✅; on-demand borrower endpoint + template |
| Payout request (not self-serve figure) ≤30 days; payments not suspended by inquiry | 🔵 | P1 | Borrowers *request*; staff calculator |
| Notification bell + stage-driven status banner | ✅/🟡 | P2 | Feed ✅; banner frontend |
| Multi-language | ⚪ | P2 | Not raised as a need |

---

## Priority roadmap

### P0 — required to run the book (Aug test → Sept cutover)
1. **Auto-collection engine**: scheduled Zumrails PAD pulls on due dates; NSF handling (fee → add-on bucket, retry policy, disable-auto-charge flag, dead-account return codes); PAD pre-notification compliance.
2. **Actuals-based daily-simple-interest engine**: per-diem accrual from actual payment dates; ledger w/ running balances + effective/processing dates; header math (interest due + fees due + add-on = payout). ⟵ *Dave's two Excel files are the spec.*
3. **Repayment modes**: regular / add-on / special / payoff in `record_payment` + borrower Pay Now.
4. **Scheduled-transaction surgery**: suspend/add individual scheduled payments w/ mandatory comment + history.
5. **Vendor origination flow**: full intake form (checklist data model) + live payment preview + combined verification request + tab-scoped visibility + "request reprocessing".
6. **Underwriting completeness**: cancel action + reason directories (reject/cancel), queue+assignment, bankruptcy discharge rule, verification-mismatch-never-auto-rejects invariant check.
7. **Credit product formalization**: pricing_config schema (fees by type & frequency, rate bounds, role-gated edit), one-product-per-bracket w/ frequency checkboxes; fixes known pricing_config↔quote mismatch blocker.
8. **Delinquency bucket state machine** (Dave's month-end snapshot model) feeding collections + reports.
9. **Migration import**: Excel/CSV upload w/ preview-confirm for customers/loans/payments/disbursements.
10. **Hardship v1**: deferment + due-date change, permission-gated, borrower e-sign required.
11. **Schema now, cheap now**: SIN optional, address/employment history (3 yrs), employment/income type lists, co-borrower file linking.

### P1 — launch window (weeks around cutover)
Comms hub + full message-body log • merge-field document engine + borrower documents tab + on-demand statements • multi-offer approvals • 5-band verified-data scorecards + per-vendor assignment • decision-rules & scorecard & notification-matrix admin UIs • collections work-surface (bulk assignment, queues, PTP, action plans) • vendor self-serve disbursements w/ holdback • vendor CRM (MSA expiry, onboarding automation) + customer CRM • monthly vendor statements (scheduled reports) • Equifax month-end batch reporting • report vendor-filter + profit split + bucket/debt-roll reporting + overrides reports • province compliance engine v1 • business calendar/stat holidays • archive view • borrower 2FA + bank-account lockdown + versioned profile edits • payout calculator (forward-dated) • AI bank-statement analysis v1.

### P2 — post-launch backlog
Insolvency portfolio • Excel report builder • blacklists • geographic heat map • PSI scoring analytics depth • flags w/ notification suppression • audit diffs/PDF archival • polymorphic archive detail • multi-language • appearance/theming.

### ⚪ Explicitly skipped (Dave)
Branch offices module • expiring cards • TL's DPD buckets & 120+ bucket • collectability score (nice-to-have only) • API clients (for launch) • admin-set passwords • change-due-dates as a standalone tool (folds into hardship) • geolocation/web-activity/cyber-security checks as decision inputs.

---

## External dependencies (Dave)
- **Equifax subscriber agreement** — unblocks live pulls (P0*), bureau tab, month-end reporting.
- **Dave's two Excel files** — "account due as of" + "amount to move" algorithms (P0 interest/collections math).
- Provincial counsel inputs — APR caps / licensing for compliance engine (already on Dave's open-items list).
- Zumrails production creds + wallet setup (incl. vendor payout wallets for self-serve disbursements).
- ~20 TL loan-agreement templates (v10.x) for the document engine migration.
- Dunning policy sign-off (docs/dunning_policy_questions.md — pending).

## Open questions for Dave
1. Auto-charge default: PAD pull on every due date for all borrowers, or opt-in per loan? Retry policy after first NSF (TL "amount to move" 50% tolerance — confirm exact algorithm in the Excel).
2. Delinquency snapshot timing: calendar month-end confirmed? What cures a bucket (full catch-up vs current-month only)?
3. Vendor disbursement: confirm 4-business-day holdback, monthly free payout date, extra-payout fee amount; Zumrails wallet-to-wallet mechanics.
4. Offer expiry (30 days) and max offers (3) — keep TL values?
5. Which of the 14 TL fee types do we actually charge at launch (admin $1/pmt, NSF $45, origination $25 were demoed)?
