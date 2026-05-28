# PaySpyre v2 — Backlog

## Phase / feature backlog

- **2026-05-26 (P6.5 → updated P6.6)** — **HMAC vendor webhook infrastructure: SHIPPED in P6.6** at `POST /api/webhooks/v1/{vendor}/verification` (didit/flinks/equifax; HMAC-SHA256 + timestamp window + nonce replay via `platform_events`). **Remaining for P7:**
  - **Remove the applicant-callable callback** `POST /api/applicant/v1/applications/{id}/verifications/{type}/callback` once vendor webhooks are the canonical path — it is now **deprecated** (emits a WARNING log + `Deprecation: true` / `Sunset: 2026-08-01` response headers) but kept functional for continuity.
  - Real vendor-specific payload parsing (beyond the common MVP envelope) for Didit/Flinks/Equifax.
  - Real Twilio/SendGrid wiring for magic-link sends (currently `MockNotificationDispatcher`).

- **2026-05-28 (P7.2 outbound shipped)** — **Real Didit + Flinks adapters — initiate path only.**
  `DiditVerificationAdapter` (real `POST /v3/session/` with `x-api-key`), `FlinksBankAdapter`
  (generates the Connect iframe URL; no HTTP at initiate), and `VerificationDispatcher`
  (flag-based real-vs-mock selector returning a uniform `DispatchResult`). All gated behind
  `USE_REAL_ADAPTERS=False`; the prod startup validator fails loud if the flag is True but
  `DIDIT_API_KEY` / `DIDIT_WORKFLOW_ID` / `FLINKS_API_KEY` / `FLINKS_CUSTOMER_ID` are empty.
  Equifax bureau stays mocked. **Open follow-ups:**
  - **P7.2b — real-webhook RESULT path.** Per-vendor `DiditWebhookPayload` / `FlinksWebhookPayload`
    schemas, a normalizer (real → `rich_payload`), reworking `app/api/webhooks/v1/endpoints/verification.py`
    to select the per-vendor schema + resolve `verification_id`/`application_id` from the vendor
    session ref, and per-vendor signature verification (Didit's own header + Flinks's
    `flinks-authenticity-key`). Out of P7.2 because it requires modifying the shipped P6.6
    endpoint + HMAC verifier.
  - **`USE_REAL_ADAPTERS` prod flip + dispatcher wiring.** P7.2 builds and tests
    `VerificationDispatcher` but does NOT rewire the live `get_orchestrator` deps (applicant
    + webhooks) to use it — those still construct `MockVerificationDispatcher` directly. When
    flipping the flag in production, also swap that construction to `VerificationDispatcher()`.
  - **Flinks income extraction via Enrich API.** Real income / NSF extraction needs Flinks's
    Enrich endpoint (`/GetAccountsSummary` / Attributes); P7.2b will use it for normalization.
  - **Equifax bureau adapter (real).** Still pending the subscriber agreement; `MockBureauAdapter`
    remains the active bureau adapter even when `USE_REAL_ADAPTERS=True`.

## Infrastructure / process debt

- **2026-05-22** — Supervisory protocol established: all PRs require verbatim `pytest` output in description, no admin-merge, wait for human review at PR boundary. See PR P3 kickoff task for full protocol. Background: prior agent (GLM-4.6 via Z.AI proxy) admin-merged broken tests on P2 and drifted into KYC scope.

- **2026-05-25 → RESOLVED in P6.7 (PR #20, branch `feature/p6.7-patients-api-cleanup`)** — ~~`test_patients_api.py` has 3 real V2 behavior failures~~:
  - `GET /api/v1/patients/{unknown_id}/discrepancies` returned 200 instead of 404 → fixed: existence check in `PatientProfileService.detect_discrepancies`.
  - `GET /api/v1/patients/{unknown_id}/history` returned 200 instead of 404 → fixed: existence check in `get_field_history` (both via new `_get_patient_or_404` helper).
  - `PATCH` discrepancy-event test failed on `column "created_at" does not exist` → fixed: test SQL used `created_at`; `platform_events` timestamp column is `occurred_at` (the discrepancy logging itself was already correct).

- **2026-05-25 (logged from CI cleanup)** — **`tests/migrations/test_migrations.py` downgrade/upgrade tests time out** (`test_downgrade_then_upgrade`, `test_step_by_step_upgrade`) under the 15s `--timeout` when run against the **remote Supabase Session Pooler** (per-DDL network latency over the full 001→022 chain). Likely **passes on CI's local `postgres:16` container** (no per-statement latency). If CI is still red on these after the A+D cleanup, investigate **migration 018** (`018_create_platform_credit_applications.py` downgrade) specifically — it's where the local run stalled.

- **2026-05-25 → RESOLVED in PR #26 (P7.1, branch `feature/p7.1-v1-unmount`)** — ~~V1 app-code un-mount refactor (Category C)~~. Two-commit split (A=un-mount routers from `app/api/v1/api.py`, B=delete files + clean `__init__`s). Removed: `app/api/v1/endpoints/{documents,stripe,kyc}.py`, `app/services/{stripe,underwriting_state_machine}.py`, `app/models/{document,stripe}.py`. Pre-flight grep found `kyc.py` also depended on `document`/`underwriting_state_machine`, so `kyc` was included (Mike confirmed it's dead V1). Suite unchanged at 230 passed (only the known local-pooler artifacts red). **Deferred:** `app/schemas/` left intact (`schemas/kyc` is imported by the live `risk_engine`; unused `schemas/document`/`schemas/stripe` swept later); V1 DB tables (documents, stripe_*) untouched — separate PR.

- **2026-05-25 → RESOLVED 2026-05-27** — ~~`deploy.yml` test job fails (no DB / connection error)~~. Root cause: the `test` job ran `pytest --cov=app` with **no Postgres service and no DB env**, so every DB-backed test errored on connection (pre-existing, red on `main` since well before PR #15).
  - **Fixed in PR #22:** added a `postgres:16` service + `DATABASE_URL`/`TEST_DATABASE_URL` (+ JWT/Stripe test env) + an alembic upgrade-to-head step to the `test` job, mirroring `tests.yml`. The job now passes.
  - **Followed by PR #23:** fixing the `test` gate unblocked `build-and-push`, which failed at DO registry login (`DO_ACCESS_TOKEN` not set) and would have auto-deployed to production on every `main` merge. So `build-and-push` + `deploy-*` were gated to a manual `workflow_dispatch` — a normal push runs only the `test` gate.
  - As of `6b58ec2`, all three workflows (`Tests`, `Migrations`, `Build and Deploy`) are **green on `main`**; no auto-deploy occurs.
  - **Remaining (separate item below):** DO continuous deployment is not wired — deploys are manual-only until `DO_ACCESS_TOKEN` (+ `DO_APP_ID`) secrets are set.

- **2026-05-27 → wired in PR #25 (P7.0)** — **DO continuous deployment.** `deploy-production` now uses `digitalocean/app_action/deploy@cc55bc9b` (v2.0.11) — deploys the `.do/app.yaml` spec (`app_spec_location`, app "payspyre-api"; not `app_name`, which expects the app name not the `DO_APP_ID` UUID), polls until live, and runs a `GET /health` smoke test (fails the job on non-200). Manual `workflow_dispatch` gate preserved; `deploy-staging` left as the doctl stub.
  - **Action still required from Mike before a deploy will work:** populate the repo secrets `DO_ACCESS_TOKEN` and `DO_APP_ID` (production app UUID). Until then a manual run will fail at the DO auth/lookup step.
  - **Deferred:** `deploy-staging` CD wiring; auto-deploy on push (gate stays manual).

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
