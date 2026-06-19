# PaySpyre v2 — Roadmap to Launch

**Date:** 2026-06-19 · **Target:** testing in **August 2026**, finished product by **mid-September 2026**
**Companion docs:** [audit_2026-06-19.md](audit_2026-06-19.md) (findings + evidence) · [payspyre_v2_spec.md](payspyre_v2_spec.md) (canonical spec)

> **How to read this:** Section A = what only **Dave** can do (business/product/vendor — external lead time). Section B = what **Mike** (+ the AI) owns (code/infra/compliance). Section C = the timeline that stitches them together. The August/September dates **only hold if Section A clears by mid-July** — those items have external lead time nobody on the build side controls.

---

## A. Required from DAVE (business / product / vendor)

Nothing in this section can be built around — each is a decision or an external relationship only Dave owns.

| # | Item | Why it blocks launch | Needed by |
|---|------|----------------------|-----------|
| **D-1** | **Equifax CA subscriber agreement** | Blocks all real credit-bureau pulls (hard + soft). Code/client exists; it's a mock until the contract closes. **Single biggest external dependency.** | **Mid-July** (to test on real bureau in Aug) |
| **D-2** | **Flinks subscription tier — confirm Enrich/Attributes API** | Without it, income/NSF are derived by naive transaction arithmetic (audit H-7) — unfit for real underwriting decisions. | **Mid-July** |
| **D-3** | **Production vendor accounts + who holds credentials** — Didit, Flinks, Resend, Twilio | Integrations are built but flag-gated off until real creds are loaded. Confirm ownership (Dave sets up vendor accounts, Mike configures). | **Early July** |
| **D-4** | **Funding source at launch** — `payspyre_capital` vs `partner_lender` vs `hybrid` | Determines which credit products can be `active` on day one. **Ties directly to the Gord credit-limit conversation.** | **Early July** |
| **D-5** | **Launch product set + amount bands** (e.g. dental small + full-arch only?) | Each product needs its own verification matrix + scorecard config before it can go live. | **Early July** |
| **D-6** | **Verification matrix sign-off per product** (Dave's toggle config) | The per-product checks/thresholds. Note: the config wiring itself has a bug (audit M-8) Mike must fix in parallel. | **Mid-July** |
| **D-7** | **Scorecard scoring metrics — finalize & hand over** *(Dave's current workstream)* | The scoring model is **not in the codebase yet**. Once locked, Mike wires it into the decision engine. This is the gating item for the scoring module. The clamping concern is a *design-doc* issue, not a code issue — see audit §1. | **Mid-July** |
| **D-8** | **Co-applicant strategy** — confirm `average` default per product | Already decided (average of score outcomes — correct). Just needs recording per product. | Low effort |
| **D-9** | **Quebec** — keep hard block for launch, or commit to a re-enable date (French + Law 25)? | Decides whether QC ships as a hard block or a config flag. (Note: the block is currently inert in the live path — audit §7 — Mike fixes regardless.) | **Mid-July** |
| **D-10** | **Marketplace** — in launch scope or post-launch? If in: lead pricing + does lead-billing plug into an existing system? | Marketplace (Phases C of the spec) is **not built** (tables only). Decides a large chunk of remaining build. | **Late June** (affects timeline) |
| **D-11** | **Frontend — where is it / who builds it?** | **No PaySpyre frontend exists on the build machine** (audit F-0). A "finished product" needs one. This may be the largest unscoped item. | **Late June** (urgent) |
| **D-12** | **Sign-offs** — review the spec / take a demo of the mocked flow; later, joint launch-checklist sign-off | His "is there a doc to comment on?" → the spec. | Ongoing |

---

## B. Required from MIKE (code / infra / compliance — AI-assisted)

Grouped by priority. Items map to audit finding IDs.

### B.1 — Security & compliance hardening (must precede real-data testing)
- [x] **Un-mount unauthenticated legacy V1 lending surface** (C-1, C-2) — **DONE 2026-06-19**, branch `feature/p8.1-security-hardening`.
- [ ] **Delete commit B** — remove the un-mounted files/models/schemas/dead services; re-home the real Equifax/TransUnion client before deleting `credit_bureau.py`.
- [ ] **Implement SIN encryption + fix `String`/`BYTEA` drift** (C-3) — only if SIN collection is in launch scope (depends on D-7/D-5). Needs key-management decision (B.4).
- [ ] **Fix broken API-key auth** (H-4) — deterministic hash lookup; never store plaintext.
- [ ] **Implement RLS vendor/borrower context extractor** (H-3) — or remove RLS reliance if not used for tenant isolation.
- [ ] **DB-level webhook idempotency** (H-5) — unique constraint on the nonce + GIN index; dedupe on `IntegrityError`.
- [ ] **`platform_consents` WORM trigger** (H-6) + `platform_patient_fields` trigger (M-9).
- [ ] **Adverse-action (ECOA/FCRA) notice job + template** (audit §7) — required before real lending.
- [ ] Parameterize RLS `SET LOCAL` (M-6); fix `PATIENT_JWT_SECRET` non-prod guard (M-2); atomic magic-link consume (M-1).

### B.2 — Scoring & decision wiring (gated on Dave D-6/D-7)
- [ ] **Wire the finalized scorecard into the decision engine**; create the `config/decision_rules/` + `config/credit_products/` config path the spec intends.
- [ ] **Fix `manual_review_band`/`min_score`/`affordability` config wiring** (M-8) so per-product thresholds are genuinely config-driven, not hardcoded.
- [ ] **Identity discrepancy reconciliation at decision** (H-1) — pass name to the engine, run `detect_discrepancies` in-flow.
- [ ] Enforce self-attested name/email at the applicant entry point (H-2).

### B.3 — Real vendor integration (gated on Dave D-1/D-2/D-3)
- [ ] Flip `USE_REAL_ADAPTERS` / `USE_REAL_NOTIFICATIONS` in staging as creds land.
- [ ] Replace Flinks transaction-arithmetic income/NSF with the Enrich/Attributes API (H-7).
- [ ] Re-wire the real Equifax/TransUnion bureau client into the flow as a config-only swap.
- [ ] Move `credit_bureau` cache + rate-limiter out of process (M-4); make Flinks URLs config-driven (M-5).

### B.4 — Infra / decisions Mike owns (spec §13 open questions)
- [ ] **Auth/identity model** (Q3 — PaySpyre-local vs shared Supabase). **SIN key management** (Q9). **Existing `applications` table** migrate/extend (Q1).
- [ ] **DO deploy secrets** — populate `DO_ACCESS_TOKEN` + `DO_APP_ID` (CD is wired but fails without them).
- [ ] **§11 KPI / Prometheus metrics** (H-8) + fix null `platform_event_id` on orchestrator PostHog events (M-7).
- [ ] **CI grep-guard** for flow-engine-only status writes (L-1).

---

## C. Timeline to completion

| Window | Dave (Section A) | Mike (Section B) | Milestone |
|--------|------------------|------------------|-----------|
| **Now → early July** | D-3, D-4, D-5, D-10, D-11 (scope + accounts + **frontend decision**) | **B.1 security/compliance hardening** (un-mount done; idempotency, auth, RLS, WORM, adverse-action) | Surface hardened; launch scope locked |
| **Early → mid July** | D-1, D-2, D-6, D-7, D-9 (**contracts close + scorecard handed over**) | **B.2 scoring/config wiring** once D-6/D-7 land; begin B.3 | Scorecard integrated; config path real |
| **Mid → late July** | Creds delivered (D-3) | **B.3 real Didit + Flinks in staging**; bureau if D-1 closed; Enrich replaces stopgap | Real-vendor end-to-end in staging |
| **August** | UAT participation / sign-off | Full staging UAT — decline paths, co-applicant paths, edge cases, fixes; **frontend integration** | **"Testing in August" milestone** |
| **Early–mid Sept** | Joint launch-checklist sign-off (D-12) | Prod deploy (B.4 DO secrets), final hardening | **Finished product** |
| **Post-launch** | — | Marketplace / cross-sell / admin console / reporting (spec Phases C–E) | unless pulled into scope by **D-10** |

### Critical-path caveats (state these to Dave explicitly)
1. **The dates hold only if D-1 (Equifax) and D-2 (Flinks tier) close by mid-July.** They have external lead time nobody on the build side controls — they are the long poles.
2. **The frontend (D-11) is the biggest unknown.** A finished *product* by mid-September is not credible if a frontend doesn't exist or isn't staffed. Resolve scope/ownership in late June.
3. **The audit surfaced a security/compliance tranche (B.1) that wasn't visible at the doc level.** Some of it (unauth surface — now fixed; SIN encryption; adverse-action notice) is launch-blocking for a regulated lender *independent of vendor contracts*. It's slotted before August testing rather than discovered during UAT.
