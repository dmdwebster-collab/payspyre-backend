# PaySpyre v2 — Roadmap to Launch

**Date:** 2026-06-19 · **Target:** testing in **August 2026**, finished product by **mid-September 2026**
**Companion docs:** [audit_2026-06-19.md](audit_2026-06-19.md) (findings + evidence) · [payspyre_v2_spec.md](payspyre_v2_spec.md) (canonical spec)

> **How to read this:** Section A = what only **Dave** can do (business/product/vendor — external lead time). Section B = what **Mike** (+ the AI) owns (code/infra/compliance). Section C = the timeline that stitches them together. The August/September dates **only hold if Section A clears by mid-July** — those items have external lead time nobody on the build side controls.

---

## A. Required from DAVE (external relationships + review)

**Role clarified 2026-06-19:** Mike builds everything (backend **and** the patient-facing frontend) and decides product set, launch scope, and scoring/scorecard config. **Dave drives only the 4 external relationships below** (agreements/accounts only he can move) and **reviews each build milestone for adequacy + gives final sign-off.** Dave is the QA/approver, not a co-builder.

| # | Item | Why it blocks launch | Needed by |
|---|------|----------------------|-----------|
| **D-1** | **Equifax CA subscriber agreement** | Blocks all real credit-bureau pulls (hard + soft). Code/client exists; it's a mock until the contract closes. **Single biggest external dependency.** | **Mid-July** (to test on real bureau in Aug) |
| **D-2** | **Flinks subscription tier — confirm Enrich/Attributes API** | Without it, income/NSF are derived by naive transaction arithmetic (audit H-7) — unfit for real underwriting decisions. | **Mid-July** |
| **D-3** | **Funding source / Gord** — `payspyre_capital` vs `partner_lender` vs `hybrid` | Determines which credit products can be `active` on day one. **Ties directly to the Gord credit-limit conversation.** | **Early July** |
| **D-4** | **Production vendor accounts** — Didit, Flinks, Resend, Twilio | Integrations are built but flag-gated off until real production creds are loaded. | **Early July** |

**Dave's review role (ongoing):** check each milestone for adequacy (flow, decisioning, market fit) and give final launch-checklist sign-off. Surface the spec / a mocked-flow demo for early comment.

**Now owned by Mike (Dave reviews, does not drive)** — moved out of Dave's column per the role clarification: launch product set + amount bands, verification-matrix config, scorecard metrics, co-applicant strategy (`average`, already decided), Quebec block decision, marketplace scope, and **the frontend build**. See Section B.5.

---

## B. Required from MIKE (code / infra / compliance — AI-assisted)

Grouped by priority. Items map to audit finding IDs.

### B.1 — Security & compliance hardening (must precede real-data testing)
- [x] **Un-mount unauthenticated legacy V1 lending surface** (C-1, C-2) — **DONE 2026-06-19**, branch `feature/p8.1-security-hardening`.
- [x] **Delete commit B** — **DONE 2026-06-19** (`81aecce`). Deleted the 6 dead endpoint files, 5 legacy schemas, and `risk_engine.py`. `credit_bureau.py` preserved for B.3 re-homing; legacy ORM models (`loan`, `credit`, `kyc`) retained to avoid model/table drift — model+table cleanup is a separate DB-affecting follow-up.
- [ ] **Implement SIN encryption + fix `String`/`BYTEA` drift** (C-3) — only if SIN collection is in launch scope (Mike's scope call, B.5). Needs key-management decision (B.4).
- [ ] **Fix broken API-key auth** (H-4) — deterministic hash lookup; never store plaintext.
- [ ] **Implement RLS vendor/borrower context extractor** (H-3) — or remove RLS reliance if not used for tenant isolation.
- [ ] **DB-level webhook idempotency** (H-5) — unique constraint on the nonce + GIN index; dedupe on `IntegrityError`.
- [ ] **`platform_consents` WORM trigger** (H-6) + `platform_patient_fields` trigger (M-9).
- [ ] **Adverse-action (ECOA/FCRA) notice job + template** (audit §7) — required before real lending.
- [ ] Parameterize RLS `SET LOCAL` (M-6); fix `PATIENT_JWT_SECRET` non-prod guard (M-2); atomic magic-link consume (M-1).

### B.2 — Scoring & decision wiring (gated on Mike's scorecard finalization, B.5)
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
- [x] Fix null `platform_event_id` on orchestrator PostHog events (M-7) — **DONE 2026-06-19** (`3d9f5a2`).
- [ ] **§11 KPI / Prometheus metrics** (H-8) — no `platform_metrics.py` / `/metrics` endpoint yet.
- [x] CI grep-guard for flow-engine-only status writes (L-1) — **already exists** (`tests/test_application_status_writes.py`); the audit finding was a false positive.

### B.5 — Product, scope & frontend (Mike builds/decides; Dave reviews)
- [ ] **Build the patient-facing frontend** — in active build, on track for August testing.
- [ ] **Launch product set + amount bands** — leaning narrow (small dental + full-arch); add more post-launch.
- [ ] **Marketplace scope** — leaning post-launch (large build; spec Phases C). If pulled into launch: lead pricing + billing rails.
- [ ] **Quebec** — leaning "coming soon" block at launch (re-enable needs French + Law 25 disclosures); also fix the inert live-path block (audit §7).
- [ ] **Scorecard finalize + verification-matrix config** — Mike's call; wires into B.2.

---

## C. Timeline to completion

| Window | Dave (Section A) | Mike (Section B) | Milestone |
|--------|------------------|------------------|-----------|
| **Now → early July** | D-3, D-4 (funding/Gord + vendor accounts) | **B.1 security/compliance hardening** (un-mount done; idempotency, auth, RLS, WORM, adverse-action) + **B.5 start frontend** | Surface hardened; scope locked |
| **Early → mid July** | D-1, D-2 (**Equifax + Flinks contracts close**) | **B.2 scoring/config wiring** + frontend build; begin B.3 | Scorecard integrated; config path real |
| **Mid → late July** | Creds delivered (D-4); review staging | **B.3 real Didit + Flinks in staging**; bureau if D-1 closed; Enrich replaces stopgap; frontend in progress | Real-vendor end-to-end in staging |
| **August** | Adequacy review / UAT sign-off | Full staging UAT on the real frontend — decline paths, co-applicant paths, edge cases, fixes | **"Testing in August" milestone** |
| **Early–mid Sept** | Final launch-checklist sign-off | Prod deploy (B.4 DO secrets), final hardening | **Finished product** |
| **Post-launch** | Review | Marketplace / cross-sell / admin console / reporting (spec Phases C–E) | per Mike's scope call |

### Critical-path caveats (state these to Dave explicitly)
1. **The dates hold only if D-1 (Equifax) and D-2 (Flinks tier) close by mid-July.** They have external lead time nobody on the build side controls — they are the long poles, and they are the part that's on Dave.
2. **The audit surfaced a security/compliance tranche (B.1) that wasn't visible at the doc level.** Some of it (unauth surface — now fixed; SIN encryption; adverse-action notice) is launch-blocking for a regulated lender *independent of vendor contracts*. It's slotted before August testing rather than discovered during UAT.
