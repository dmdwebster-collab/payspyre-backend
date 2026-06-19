# Build notes — autonomous session 2026-06-19

Assumptions made while building (documented instead of asking, per the autonomy brief).

## Applicant product catalogue + dev helpers (toward a clickable patient flow)

**Goal:** make the patient application flow runnable end-to-end in mock mode so it
can be demoed/beta-tested without real vendors.

**What was added:**
- `GET /api/applicant/v1/products` — unauthenticated list of `active` credit products
  (the patient app needs to show available financing + amount bounds). Product ids are
  `uuid4` per-DB, so a discovery endpoint is required (can't hardcode).
- Dev-only helpers, **mounted only when `ENVIRONMENT != "production"`** (see
  `app/api/applicant/v1/router.py`):
  - `GET /api/applicant/v1/dev/magic-link-code?application_id=` — surfaces the plaintext
    sign-in code that mock mode otherwise stores only as a SHA-256 hash.
  - `POST /api/applicant/v1/dev/applications/{id}/verifications/{purpose}/complete?score=720`
    — simulates a passed vendor result (mirrors `tests/test_applicant_journey.py`'s
    `_handle_result`, reusing `MockVerificationDispatcher.simulate_callback`).

**Assumptions:**
1. **Dev helpers are acceptable** as a non-prod convenience. They have no auth and are
   never mounted in production. The plaintext-code stash (`_DEV_PLAINTEXT_CODES` in
   `mock_notification_dispatcher.py`) is an in-process dict; the mock dispatcher is only
   selected in non-prod, so it never holds production codes.
2. **`score=720` default → approved** (per the seed product's bands; 550→declined,
   640→under_review), matching the journey test.
3. Verification "purpose" path param uses the consent-purpose vocabulary
   (`id_verification`, `soft_bureau_pull`, `bank_verification`, `hard_bureau_pull`),
   mapped to DB enum types by `CONSENT_TO_VERIFICATION_TYPE`.

## Verification

Ran against the project's configured test DB (`TEST_DATABASE_URL` in `.env.local` →
Supabase pooler — what CI uses; the harness truncates test-data tables, never mutates
schema). Docker was unavailable, so no local Postgres.

- `tests/test_applicant_dev_tools.py` (new) — full journey via the public API + dev
  endpoints: **3 passed**, stable across repeated runs.
- `tests/test_applicant_journey.py` — **3 passed** (flow unaffected by the security work).
- Full suite (excl. `tests/migrations`, which time out on the remote pooler — known):
  **431 passed, 2 failed in ~3m20s**. The 2 failures
  (`test_vendor_webhooks.py::...idempotent_replay`, `..._flinks...accounts_detail`) are
  **flaky — they pass in isolation** and live in webhook code untouched by this work. They
  are the pre-existing read-then-write idempotency race the audit flagged (H-5: no DB
  unique constraint on the webhook nonce), surfaced by the high-latency pooler + per-test
  truncation. Not a regression introduced here.

## How to run the full stack locally

```bash
# backend (mock mode, against your Supabase test DB)
cd payspyre-backend
export DATABASE_URL=$(grep '^TEST_DATABASE_URL=' .env.local | cut -d= -f2-)
uvicorn app.main:app --port 8000

# frontend (separate terminal)
cd payspyre-frontend && npm run dev   # http://localhost:3000  (proxies /api -> :8000)
```
Then: apply → "Auto-fill it" (dev) on the code screen → grant consents → Start + "Complete
(dev)" each verification → Submit → approved.
