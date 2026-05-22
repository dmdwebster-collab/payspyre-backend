# PaySpyre v2 MVP — Claude Code kickoff (the build-not-spec version)

**Paste this into Claude Code in the `payspyre-backend` repo.**

---

You are working in the `payspyre-backend` repo. The existing KYC orchestration (15 PRs already shipped: Didit/Persona vendor abstraction, `kyc_sessions`, `kyc_events` append-only with REVOKE UPDATE/DELETE, webhook HMAC + idempotency, 11-rule YAML risk engine, FINTRAC audit export) stays as-is. We are **extending** it, not replacing it.

**Spec file:** `docs/payspyre_v2_spec.md` — this is the authoritative reference. Treat it as canonical for data model, hard rules, and PR boundaries.

**Build scope for this sprint = MVP only.** The full spec contains ~30 PRs. We are building **8 of them** in this sprint. Stop after each PR for human review before starting the next.

## MVP scope (8 PRs)

1. **PR P0** — Rename `clinic_id` → `vendor_id` everywhere (DB columns, models, services, tests, docs). One PR, no logic changes. **Do this before P1.**
2. **PR P1** — Migrations for §2.1–2.7: `platform_patients`, `platform_patient_fields`, `platform_credit_products`, `platform_credit_applications`, `platform_verifications`, `platform_consents`, `platform_events`. Includes `REVOKE UPDATE, DELETE ON platform_events`. Add test that revoke took effect.
3. **PR P2** — Patient profile service. CRUD with source-tagged field writes (`self_reported` | `practice_prefill` | `id_doc`). No UI yet.
4. **PR P3** — Credit product service. Admin CRUD + JSON Schema validation for `verification_matrix`. Seed one product: `dental_full_arch_v1` with the §3.3 config.
5. **PR P3.5** — Co-applicant linkage primitives only (migration + invite table). **No flow-engine work.** This is just data-model so we don't have to migrate later.
6. **PR P4** — Flow engine (§4). Pure functional core. Uses `MockVerificationAdapter` only — no real vendors. Synthetic test cases for all verification types and co-applicant group decisioning (`combination_strategy: average`).
7. **PR P5** — Consent service (§8.1, §8.2). Filesystem-loaded immutable consent text with version tags. No UI; service-only.
8. **PR P6** — Applicant API endpoints (§9.2) wired to flow engine + mock adapters. Quick-start endpoint enforces patient self-attested name + email per §8.4. Discrepancy detection runs at quick-start submit.

**End state of this sprint:** a patient can be created via API, fill in quick-start, sign consents, run through a fully-mocked flow (ID via existing Didit integration is fine; bank + bureau are pure mocks), and reach a decision. Full audit trail in `platform_events`. End-to-end integration test covers the happy path + one decline path + one co-applicant group path.

## Out of scope for this sprint (DO NOT BUILD)

- Real bank integration (Flinks)
- Real bureau integration (Equifax)
- SafeScan, FICO, ERS 2.0 scoring products
- Credit Health subscription / SSO
- Application fees
- Marketplace lead engine (tables defined, no service code)
- Cross-sell scaffold (tables defined, no service code)
- Adverse-action notice job
- Admin UI / RBAC / workplace nav
- Loan servicing (loans table, schedules, transactions, delinquency)
- SIN encryption key rotation hooks (basic encryption only)
- Common-name misidentification detection
- Generalized province management (keep QC hardcoded block for MVP)
- Decision rule tri-state (use existing pass/fail YAML engine; tri-state is post-MVP)

If you find yourself reaching for any of the above, **stop and ask** before adding scope.

## Hard rules (carried from prior KYC build, enforced at DB + CI level)

1. `platform_events` is append-only. Migration must `REVOKE UPDATE, DELETE`. Test must verify.
2. All webhook handlers: HMAC verify FIRST, idempotency check SECOND, then process. Return 200 on duplicate.
3. State transitions on `platform_credit_applications` happen ONLY via the flow engine. Add a grep-based CI check that no other code path writes to `status`.
4. Vendor abstraction is real: `MockVerificationAdapter` and the existing `DiditAdapter` must be swappable via config alone. CI runs the same end-to-end test with both adapters.
5. Consent text is immutable. `consent_text_shown` and `consent_text_version` are never UPDATEd — new consents reference new versions.
6. No PII in logs. No SIN in logs ever. No raw bureau responses in any structured field.
7. All thresholds (verification matrix, decision rules, lead pricing) come from YAML / DB config — never hardcoded in service code.
8. Product version snapshot: `platform_credit_applications.credit_product_version` captures the product version at application creation. Subsequent product edits must not affect in-flight applications.
9. Quebec province check returns "coming soon" — applications cannot be created with `province = 'QC'`.
10. The `MockBureauAdapter` returns deterministic synthetic data. It does NOT call any real Equifax/TransUnion endpoint. Mock must include synthetic SafeScan-style fraud signals so downstream rule integration can be tested later.
11. The `MockBankAdapter` returns deterministic synthetic data including synthetic income, NSF events, account age. Must produce data shaped like real Flinks responses.
12. Existing `kyc_*` tables and orchestration stay untouched. New tables are `platform_*`. The `platform_verifications` row links to `kyc_sessions.id` via `vendor_session_ref` for ID verifications.

## Working rhythm

- One PR at a time. Stop at the end of each PR. Wait for review.
- Each PR must include: migration (if applicable), service code, unit tests, integration test, updated docs in `docs/`.
- Each PR's description must explicitly call out: (a) what's in scope for THIS PR, (b) what's deferred to a later PR, (c) any hard-rule compliance evidence (e.g. "REVOKE UPDATE confirmed in test_platform_events_revoke").
- If a question comes up that requires Mike's input (8 open questions listed in §13 of the spec), **stop and ask** — do not invent an answer.

## Open questions requiring Mike's answer BEFORE PR P1

1. Existing `applications` table — migrate or extend?
2. Auth / identity — Supabase Auth shared with Alvero, or PaySpyre-local?
3. SIN encryption key management — pgcrypto + Workers secret + rotation, or Supabase Vault, or KMS?

The remaining open questions (billing rails, Equifax timeline, funding source, Quebec re-enable, co-applicant default strategy) can be deferred until later PRs.

## After the MVP ships

Demo to Dave via screen share. No more spec docs. Iterate from a working app, not from another design doc.

The full backlog (Credit Health, Equinox real integration, admin UI, servicing, etc.) lives in `docs/future/`. Do not read those files during this sprint.
