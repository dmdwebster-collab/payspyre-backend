# PaySpyre v2 — Backlog

## Phase / feature backlog

- **2026-05-26 (P6.5 → updated P6.6)** — **HMAC vendor webhook infrastructure: SHIPPED in P6.6** at `POST /api/webhooks/v1/{vendor}/verification` (didit/flinks/equifax; HMAC-SHA256 + timestamp window + nonce replay via `platform_events`). **Remaining for P7:**
  - ~~**Remove the applicant-callable callback** `POST /api/applicant/v1/applications/{id}/verifications/{type}/callback` once vendor webhooks are the canonical path — it is now **deprecated** (emits a WARNING log + `Deprecation: true` / `Sunset: 2026-08-01` response headers) but kept functional for continuity.~~ **RESOLVED in P7.3 (2026-05-30):** route + handler removed; `VerificationCallbackBody` / `VerificationCallbackResponse` schemas dropped; `tests/test_applicant_callback_deprecation.py` deleted; `tests/test_applicant_journey.py` `_drive()` + steps 12-13 rewired to call `FlowOrchestrator.handle_verification_result(...)` directly (Option C — vendor wire transport is exhaustively covered in the `test_vendor_webhooks_*.py` suite, so the journey test stays focused on the state-machine contract).
  - ~~Real vendor-specific payload parsing (beyond the common MVP envelope) for Didit/Flinks/Equifax.~~ **RESOLVED in P7.2b (2026-05-30)** — see the P7.2b entry below; Equifax stays MVP-envelope until subscriber agreement closes.
  - ~~Real Twilio/SendGrid wiring for magic-link sends (currently `MockNotificationDispatcher`).~~ **RESOLVED in P7.4 (2026-05-31)** — note the kickoff used "SendGrid" as loose shorthand; the implementation uses **Resend** (matching the pre-existing `RESEND_API_KEY` / `RESEND_FROM_EMAIL` config slots) + Twilio for SMS. See the P7.4 entry below.

- **2026-05-31 (P7.4 outbound shipped)** — **Real notification adapters — send_magic_link only.**
  `RealNotificationDispatcher` (Resend for email, Twilio for SMS), flag-gated by
  `USE_REAL_NOTIFICATIONS=False`; prod startup validator fails loud when the flag is True
  but `RESEND_API_KEY` / `RESEND_FROM_EMAIL` / `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` /
  `TWILIO_FROM_NUMBER` are empty. Two-step write: `magic_link_issued` event flushed for
  bigint id → vendor call (no internal retries) → `notification_sent` event with
  `recipient_redacted = sha256(canonical)`. On vendor failure, raise → auth-service
  rolls back the session → no orphan `magic_link_issued`. `get_notification_dispatcher`
  selects mock vs real per the flag.

- **2026-05-31 (P7.4b inbound shipped)** — **Inbound notification webhooks + suppression.**
  `POST /api/webhooks/v1/notifications/twilio` (Messaging StatusCallback;
  `X-Twilio-Signature` via `twilio.request_validator.RequestValidator`) and
  `POST /api/webhooks/v1/notifications/resend` (Svix events; stdlib HMAC-SHA256 over
  `{svix-id}.{svix-timestamp}.{body}` against a base64-decoded `whsec_*` secret).
  New `notification_suppression` table (migration 023, sub-ms `is_suppressed` lookup
  via index on `recipient_redacted`); `RealNotificationDispatcher` consults it
  pre-send (gated `USE_SUPPRESSION_CHECK=True` default). Canonical status alphabet
  in payload (`delivered` / `sent` / `failed_transient` / `failed_permanent` /
  `complained`) — no DB enum. Twilio carrier-level Advanced Opt-Out (error 21610)
  → automatic `opt_out` suppression. Resend `email.bounced` permanence carried in
  `data.bounce.type` ("Permanent" → hard_bounce; "Transient" → no suppression).
  Orchestrator unchanged (observability-only — kickoff §6.8). Orphans (unknown
  `vendor_message_id`) → `webhook_orphaned` event + 202.
  **Open follow-ups:**
  - ~~**P7.4c — V1 NotificationQueue un-mount.** Mirrors P7.1 pattern: delete
    `app/services/notifications.py`, `app/api/v1/endpoints/notifications.py`,
    `app/models/notification.py`, and the `app/main.py:150` background-loop
    instantiation. Closes the `relation "notifications" does not exist` runtime
    spam that has been present in every test run since pre-P0. P7.4 and P7.4b
    deliberately do NOT touch this — it's a separate clean diff with its own
    bisection value.~~ **RESOLVED in P7.4c (2026-05-31)** — un-mounted (commit A)
    + 1,400 lines of V1 notification surface deleted across four files (commit B);
    `app/schemas/notification.py` swept too (was only imported by the deleted V1
    endpoint). Pre-flight confirmed V1 tables (`notifications`,
    `notification_preferences`, `notification_templates`, `deliveries`,
    `webhook_deliveries`) never existed on Supabase — no DB cleanup needed,
    unlike the P7.1 V1 table cleanup follow-up. Runtime log spam is gone.
  - **Inbound-SMS receiver (Twilio /messages/twilio).** Skipped in P7.4b —
    carrier-level Advanced Opt-Out handles STOP keywords and surfaces error
    21610 in StatusCallback (which P7.4b records as `opt_out`). If we ever
    need richer keyword handling (HELP, START, custom) we'll add the
    incoming-SMS receiver as a separate phase.
  - **Resend typed exceptions (track SDK upstream).** The `resend>=0.8.0` SDK
    raises bare `Exception`; we heuristically classify via
    `_classify_resend_error` (HTTP-status attrs first, then message-string hints
    against `_RESEND_PERMANENT_HINTS`). Swap to typed exception handling once
    `resend.exceptions.*` ships.
  - **Resend `Idempotency-Key` plumbing.** Resend supports the header
    (24-hour TTL, ≤256 chars) but the Python SDK does not surface it. Either
    bypass the SDK (call the API via `httpx` directly) or contribute upstream.
    Combined with P7.4's no-internal-retries rule this is bounded today; revisit
    if internal retries ever land.

- **2026-05-28 (P7.2 outbound shipped)** — **Real Didit + Flinks adapters — initiate path only.**
  `DiditVerificationAdapter` (real `POST /v3/session/` with `x-api-key`), `FlinksBankAdapter`
  (generates the Connect iframe URL; no HTTP at initiate), and `VerificationDispatcher`
  (flag-based real-vs-mock selector returning a uniform `DispatchResult`). All gated behind
  `USE_REAL_ADAPTERS=False`; the prod startup validator fails loud if the flag is True but
  `DIDIT_API_KEY` / `DIDIT_WORKFLOW_ID` / `FLINKS_API_KEY` / `FLINKS_CUSTOMER_ID` are empty.
  Equifax bureau stays mocked.

- **2026-05-30 (P7.2b inbound shipped)** — **Real Didit + Flinks webhook RESULT path.**
  Per-vendor schemas (`DiditWebhookPayload`, `FlinksWebhookPayload`) + translators
  (`app/services/webhooks/translators.py`) mapping real vendor JSON → `rich_payload` the
  shipped replay adapters consume. `SignatureVerifier` extended to dispatch per vendor:
  Didit `X-Signature-V2` (canonical-JSON HMAC-SHA256-hex) + `X-Timestamp`; Flinks
  `flinks-authenticity-key` (base64 HMAC-SHA256 of raw body); Equifax keeps the original
  MVP envelope until a real adapter ships. Flinks Tag feature carries `application_id`
  through Connect; `vendor_session_ref` is upgraded from the placeholder Connect URL to
  `Login.Id` on first webhook. `get_orchestrator` (applicant + webhooks deps) now uses
  `VerificationDispatcher()` so flipping `USE_REAL_ADAPTERS=True` enables real outbound
  + real inbound in one switch.
  **Open follow-ups:**
  - **Flinks Enrich/Attributes API for precision income/NSF.** P7.2b derives
    `monthly_income_cents` / `nsf_count_90d` / `account_age_months` by walking
    `Accounts[].Transactions[]` arithmetically in `translators._derive_bank_metrics`.
    Replace with a follow-up Flinks Enrich call once subscription is confirmed
    (TODO logged in-translator).
  - **`USE_REAL_ADAPTERS` prod flip.** Flag still defaults False; flip in prod once
    Didit + Flinks creds are configured.
  - **`platform_verification_status` "manual_review" enum value.** Didit's "In Review"
    status currently 202-skips (verification stays `pending`) because the enum has no
    manual-review state. Add via a future migration if Didit's manual-review path needs
    explicit ops handling.
  - **Equifax bureau adapter (real).** Still pending the subscriber agreement; `MockBureauAdapter`
    remains the active bureau adapter even when `USE_REAL_ADAPTERS=True`. The
    `/equifax/verification` route keeps the MVP envelope until then.

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
