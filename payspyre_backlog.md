# PaySpyre v2 — Backlog

## Infrastructure / process debt

- **2026-05-22** — Supervisory protocol established: all PRs require verbatim `pytest` output in description, no admin-merge, wait for human review at PR boundary. See PR P3 kickoff task for full protocol. Background: prior agent (GLM-4.6 via Z.AI proxy) admin-merged broken tests on P2 and drifted into KYC scope.

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
