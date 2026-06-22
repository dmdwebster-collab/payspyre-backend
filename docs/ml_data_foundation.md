# AI/ML Phase 0 — Data Foundation

`scripts/ml/export_training_data.py` turns the live platform loan schema into a
single labeled feature/outcome CSV for **offline** risk-model training. It is a
plain stdlib script (`csv`, `hashlib`) — no pandas/sklearn enters the API repo;
model *training* happens in a separate workspace against the CSV this emits.

This one dataset serves all four planned model targets: **early-warning,
collections prioritization, underwriting, pricing**.

## Run

```bash
python -m scripts.ml.export_training_data --out training_data.csv
python -m scripts.ml.export_training_data --out td.csv --as-of 2026-06-01
python -m scripts.ml.export_training_data --out td.csv \
    --database-url postgresql+psycopg2://user:pw@host:5432/db
```

Connects via the app's `SessionLocal` (`DATABASE_URL`) by default, or an explicit
`--database-url`. `--as-of` (default today) is the snapshot date for all
performance/outcome features and labels. `--salt` (or `ML_EXPORT_SALT`) salts the
opaque id hashes.

## What's exported (one row per loan)

**Identifiers (no PII):** `loan_id_hash`, `application_id_hash` — salted SHA-256
surrogates, non-reversible without the salt; `as_of_date`. No name, email, phone,
SIN, DOB, or address columns ever — enforced by `assert_no_pii_columns()` at
startup and in tests.

**Origination features:** `principal_cents`, `annual_rate_bps`, `term_months`,
`requested_amount_cents`, `loan_source`, `is_migrated`, `decision`,
`decision_reason_codes` (pipe-joined stable codes), `decision_score` /
`decision_score_band_min` / `decision_score_band_max` (see gap #1),
`self_reported_income_cents`, `self_reported_liabilities_cents`,
`verification_depth`, `applicant_role`, `is_co_applicant`.

**Performance / outcome features (as-of):** `status`,
`principal_balance_cents`, installment status counts
(`installments_total/paid/partial/late/waived/due_count`),
`on_time_payment_ratio`, `nsf_count`, `late_payment_count`, `payments_count`,
`total_paid_cents` (NSF/reversal entries excluded), `max_days_past_due` (earliest
unpaid past-due installment vs as-of).

**Labels:** `outcome_default` (status `charged_off` OR max DPD ≥ 90),
`outcome_delinquent` (status `delinquent` OR max DPD ≥ 30), `outcome_paid_off`
(status `paid_off`), `days_past_due_bucket` (`current`/`1-29`/`30-59`/`60-89`/`90+`).

## Data gaps (read before training)

1. **No raw bureau attributes.** Per the platform audit, the bureau pull is
   consumed at decision time and discarded — only the decision + stable
   `decision_reasons` codes persist in `platform_credit_applications.decision`.
   Raw score, tradelines, utilization, inquiries, bankruptcies are unavailable.
   The `decision_score*` columns are emitted **null** (true for ~all current
   rows). The script already reads a score/band if a future privacy-safe snapshot
   adds one. Closing this gap is a prerequisite for a real underwriting model and
   is owned outside this script.

2. **Low volume until the legacy book is imported.** Native origination only
   began at the July beta. Historical performance lives in the legacy Turnkey
   book and must be imported via `scripts/migration/turnkey_import.py`
   (`source='turnkey_migration'`, `application_id IS NULL`) **before** this export
   has enough matured, labeled loans to train on. Migrated loans have no
   application, so their origination-feature columns are null by design.

3. **Label maturity / survivorship.** Young active loans haven't had time to
   default. Treat `outcome_*` as point-in-time at `--as-of`; apply an observation
   window / maturity cutoff in the training workspace. This script exports every
   loan and records the as-of date rather than filtering.

## Payment-history ETL (performance signal for migrated loans)

The legacy Turnkey **payment ledger** is loaded separately from the loan book by
`scripts/migration/turnkey_payments_import.py` (mapping/persist in
`app/services/migration/turnkey_payments.py`). This backfills real per-payment
performance signal (amounts, dates, cadence) onto migrated loans for training.

**Ledger-only, by design.** Historical payments are inserted as raw
`PlatformLoanPayment` rows **only** — they do NOT go through
`loan_servicing.record_payment`, and they never touch the schedule or
`principal_balance_cents`. Migrated loans already have their *current* outstanding
balance set by the loan import, so re-applying historical payments would
double-count and corrupt balances. Import order: loans first
(`turnkey_import.py`), then payments.

Idempotent on the Turnkey transaction id (`external_ref = 'turnkey:<id>'`) via the
unique `(loan_id, external_ref)` index (migration 033). Payments whose account
number matches no migrated loan are reported as `unmatched_account`, not inserted.
