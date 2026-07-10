# P0 Build Plan — Turnkey Parity (autonomous fan-out, 2026-07-10)

Executes the P0 roadmap from `GAP_ANALYSIS.md` in two dependency-ordered phases.
Each workstream = one agent in an isolated worktree → branch → PR → CI-gated merge train
(per the established fan-out protocol: DB-free tests in agents, CI full suite is the regression gate,
update-branch + merge on green, never `--admin`).

**Safety invariants (all workstreams):**
- Money behavior is config-gated and OFF by default (no live behavior change until enabled).
- Business policy values (fees, retry counts, tolerances) are config with documented defaults — flagged for Dave, never hardcoded decisions.
- Integer cents everywhere; SIN never plaintext; audit via `platform_events`; no new dependencies (pyproject-only rule — flag needs in PR).
- Migrations use string revision ids `0XX_name`; Phase 1 all chain from `043_canonical_credit_application`; re-chained sequentially during the merge train.

## Phase 1 — foundations (parallel)

| WS | Branch | Migration | Scope |
|----|--------|-----------|-------|
| A | `feat/p0-actuals-ledger` | 044 | Actuals-based daily-simple-interest engine + immutable loan ledger (`platform_loan_transactions`: effective/processing dates, payment types, allocation columns, add-on balance category, reference numbers, backfill from existing payments); interest-due/payoff math from actual payment dates; `record_payment` allocation (interest→principal→fees) |
| B | `feat/p0-pricing-schema` | (none exp.) | Typed `PricingConfig` schema (rate + bounds, role-gated edit, frequencies, TL's fee taxonomy, per-frequency APR validation vs criminal rate + province-cap hook); fixes pricing_config↔quote mismatch; product CRUD validates |
| C | `feat/p0-schema-pack` | 046 | SIN optional end-to-end; 3-yr address + employment history; employment types (FT/PT/seasonal); income-type policy (EI/Student excluded from new intake); co-borrower separate-file linking on applicant groups |
| D | `feat/p0-import` | 047 | CSV import service (customers/loans/payments/disbursements) with preview→confirm, idempotent rows, `platform_import_batches`; builds on migration-035 turnkey loan-import path |
| E | `feat/p0-underwriting` | 048 | Cancel decision + admin-editable reject/cancel reason directories (compliant seeds); application assignment + queue filters; bankruptcy 2-yr-discharge rule (config) + vendor-override→human-review; verification-mismatch-never-auto-declines invariant tests |

Merge order: A → B → C → D → E (re-chain 046/047/048 down_revisions as needed).

## Phase 2 — layers (parallel, after Phase 1 merges)

| WS | Branch | Migration | Scope |
|----|--------|-----------|-------|
| F | `feat/p0-repayment-modes` | 049 | Repayment modes (regular/add-on/special/payoff) in payments + borrower Pay Now; scheduled-transaction surgery (suspend/add w/ mandatory comment + history) |
| G | `feat/p0-auto-collection` | 050 | Scheduled Zumrails PAD pulls on due dates (job), NSF fee→add-on + configurable retry, per-loan disable-auto-charge, dead-account return codes, PAD pre-notification; **flag-gated OFF** |
| H | `feat/p0-delinquency-buckets` | 051 | Dave's month-end snapshot bucket machine: CML → Pot 30/60/90 → Default (+ insolvency status stub); feeds collections queue + aging |
| I | `feat/p0-vendor-origination` | 052 | Vendor intake form (checklist model: treatment cost − insurance − down payment), live payment preview, combined verification request trigger, API-level tab scoping (mask bank, exclude risk/bureau/statements), "request reprocessing" action |
| J | `feat/p0-hardship-v1` | 053 | Hardship module v1: deferment + due-date change, permission-gated, borrower e-sign required before effect, auto snap-back scaffolding |

## Status log
- 2026-07-10: Phase 1 launched.

## External dependencies (unchanged, tracked in GAP_ANALYSIS)
Dave's 2 Excel files ("account due as of", "amount to move") — WS-A/G use config-gated approximations until received. Equifax agreement. Zumrails prod creds.
