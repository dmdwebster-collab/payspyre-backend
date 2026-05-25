# PaySpyre v2 — Backlog

## Infrastructure / process debt

- **2026-05-22** — Supervisory protocol established: all PRs require verbatim `pytest` output in description, no admin-merge, wait for human review at PR boundary. See PR P3 kickoff task for full protocol. Background: prior agent (GLM-4.6 via Z.AI proxy) admin-merged broken tests on P2 and drifted into KYC scope.

- **2026-05-25 (logged from CI cleanup)** — **`test_patients_api.py` has 3 real V2 behavior failures** (kept red after the CI-cleanup PR removed legacy V1 test noise):
  - `GET /api/v1/patients/{unknown_id}/discrepancies` returns **200 instead of 404** (`TestDiscrepanciesOverHTTP::test_discrepancies_unknown_patient_returns_404`).
  - `GET /api/v1/patients/{unknown_id}/history` returns **200 instead of 404** (`TestFieldHistoryOverHTTP::test_history_unknown_patient_returns_404`).
  - `PATCH` with a conflicting source value does not log a discrepancy event to `platform_events` (`TestFieldUpdateOverHTTP::test_patch_with_conflicting_source_logs_discrepancy_event`).
  These are genuine V2 bugs, not V1 rot. **Fix in a focused follow-up PR before P6 starts**, or explicitly acknowledge in the P6 kickoff that these tests are still red on `main`.

- **2026-05-25 (logged from CI cleanup)** — **`tests/migrations/test_migrations.py` downgrade/upgrade tests time out** (`test_downgrade_then_upgrade`, `test_step_by_step_upgrade`) under the 15s `--timeout` when run against the **remote Supabase Session Pooler** (per-DDL network latency over the full 001→022 chain). Likely **passes on CI's local `postgres:16` container** (no per-statement latency). If CI is still red on these after the A+D cleanup, investigate **migration 018** (`018_create_platform_credit_applications.py` downgrade) specifically — it's where the local run stalled.

- **2026-05-25 (logged from CI cleanup)** — **V1 app-code un-mount refactor (Category C, deferred).** `app/services/stripe.py`, `app/services/underwriting_state_machine.py`, `app/models/document.py` (+ their schemas and the `documents`/`stripe`/`underwriting`/`auth`/`analytics`/`notifications` routers in `app/api/v1/api.py`) are V1 dead code paths — no V2 references remain; only the now-deleted V1 tests exercised them. They are still mounted via `app/main.py`, so they were NOT deleted in the CI-cleanup PR (out of scope). **Un-mount and delete in a separate refactor PR.** Risk: the `client` test fixture imports `app.main`, so each router removal must be re-verified against the V2 API tests (`test_patients_api.py`, `test_credit_products_api.py`) to confirm `app.main` still imports cleanly.

- **2026-05-25** — **`deploy.yml` test job fails with SSL/connection error.**
  - `.github/workflows/deploy.yml` "test" job runs `pytest --cov=app` against a postgres service, but every DB-backed test errors with `"server does not support SSL, but SSL was required"` connecting to `localhost:5432`.
  - Affects `consent_service`, `credit_products_api`, `migrations`, and most other DB-backed tests in that job.
  - Pre-existing: red on `main` across `07c70da`, `c99c06e`, `c522e7b`, `8b72bcd`, `47b6832` (5+ commits before PR #15).
  - Likely fix (decide in its own kickoff): set `DATABASE_URL`/`TEST_DATABASE_URL` with `sslmode=disable` for the `deploy.yml` test job, OR drop the redundant test step entirely since `tests.yml` already runs the full suite on every PR.
  - NOT in scope for PR #15. Workflow file changes require their own kickoff + stop-and-ask.

---

## P4+ scope (logged from P3 — do not implement in P3)

<!-- Add one-line scope-drift items here if discovered during P3 work -->

---

## Schema / compliance debt

- **2026-05-25 (logged from P5)** — **No DB-level WORM trigger on `platform_consents`.**
  Only `platform_events` has an append-only / no-UPDATE-no-DELETE trigger (migration 021).
  `consent_text_shown` / `consent_text_version` immutability (spec §2.6, §8.2, Hard Rule #1)
  is currently enforced **only at the application layer** by `app/services/consent_service.py`
  (`revoke_consent` never touches the text columns). A raw `UPDATE` to those columns is not
  blocked by the database. **Fix later** with a new migration adding a WORM/forbid-UPDATE
  trigger on `platform_consents` (mirror migration 021). P5 deliberately did **not** add a
  migration (out of scope). Tripwire test:
  `tests/test_consent_service.py::TestWormEnforcement::test_db_level_worm_trigger_absent_documents_backlog_gap`
  fails when the trigger is added, signalling it's time to close this item.
