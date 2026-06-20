# Didit production-readiness notes

Scope: confirm `app/services/adapters/didit_verification.py` reads creds cleanly
and document the gaps to flip Didit to prod. This is a review only — the existing
adapter is unchanged.

## What works today

- `DiditVerificationAdapter.__init__` takes `api_key`, `api_base_url`,
  `workflow_id` as **constructor-injected** values — the adapter itself does not
  read `settings` or the DB. Good: it's pure and testable (see
  `tests/test_didit_adapter.py`, respx-mocked).
- `initiate()` POSTs `{api_base_url}/v3/session/` with `x-api-key` auth, echoes
  `application_id` via `vendor_data` + `metadata` (webhook correlation), and
  raises `DiditAPIError` on any non-2xx. 10s timeout, no internal retries (correct
  — orchestrator idempotency owns retries to avoid double-charging the vendor).
- No PII in logs: email only goes into the request body as an optional prefill;
  it is never logged.

## Where creds come from now

- `VerificationDispatcher` (verifications/dispatcher.py) constructs the adapter
  from `settings.DIDIT_API_KEY`, `settings.DIDIT_API_BASE_URL`,
  `settings.DIDIT_WORKFLOW_ID` — i.e. **env/config**, not integration_settings.
- `config.py` already gates `USE_REAL_ADAPTERS=True` on `DIDIT_API_KEY` /
  `DIDIT_API_BASE_URL` / `DIDIT_WORKFLOW_ID` being non-empty (startup check).

## Gaps to flip Didit to prod

1. **Cred source — env vs integration_settings.** The admin area now manages a
   `didit` provider via `integration_settings` (P8.2), but the dispatcher still
   reads Didit creds from env `settings`. To make creds admin-rotatable like the
   other providers, build the adapter from
   `integration_settings.get(db, "didit")` (`secrets["api_key"]`,
   `config["api_base_url"]`, `config["workflow_id"]`), falling back to env
   `settings` when the row is absent. This mirrors the pattern used by the new
   `RealBureauAdapter` (constructor-injected settings row).
2. **Webhook result path (P7.2b).** `initiate()` is outbound-only; the KYC result
   arrives via the Didit webhook and `verify_identity()` intentionally raises
   `NotImplementedError`. Production needs the inbound webhook handler +
   signature verification (`signature_verifier_didit` exists) writing the
   `verification_completed` event the replay adapter reads. Confirm that path is
   live before flipping the flag.
3. **Live wiring.** Per dispatcher.py's docstring, the live `get_orchestrator`
   dependencies still construct `MockVerificationDispatcher` directly. Rewire
   them to `VerificationDispatcher` as the rollout step paired with
   `USE_REAL_ADAPTERS=True`.
4. **Workflow id / base url per environment.** Ensure the prod `DIDIT_WORKFLOW_ID`
   and `DIDIT_API_BASE_URL` (sandbox vs prod host) are set for the deployed env.

## Recommendation

The adapter itself is prod-ready and does NOT need changes. The remaining work is
wiring/config: (a) optionally source creds from `integration_settings` for
admin-managed rotation, (b) confirm the inbound webhook + signature path is live,
(c) swap the live dependency from `MockVerificationDispatcher` to
`VerificationDispatcher` when `USE_REAL_ADAPTERS=True`.
