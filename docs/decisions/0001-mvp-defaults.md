---
name: mvp-pre-p1-decisions
description: Default decisions for the 3 pre-P1 open questions in the PaySpyre v2 MVP kickoff
metadata:
  type: project
---

# ADR 0001: MVP Pre-P1 Default Decisions

**Context:** The v2 spec lists 8 open questions (§13). Three of these block PR P1. This ADR records the default decisions used to unblock the MVP sprint, per Mike's instruction to use reasonable defaults and proceed autonomously.

**Date:** 2026-05-21

---

## Decision 1: Existing `loan_applications` table — CREATE new, don't extend

**Question:** Does one already exist in `payspyre-backend`? If so, do we migrate to `platform_credit_applications` or extend the existing one?

**Finding:** Yes, `loan_applications` exists with schema: `borrower_id`, `vendor_id`, `co_borrower_id`, `requested_amount`, `status`, `credit_product_code`, etc. This is the v1 lending schema.

**Decision:** Create `platform_credit_applications` as a NEW table per v2 spec. Do NOT extend `loan_applications`.

**Rationale:**
- v2 is a platform-first rearchitecture with different data model (patients, not borrowers; credit products with verification matrices, not fixed product codes)
- v1 and v2 applications will coexist during migration period
- `loan_applications` references `borrowers` table; `platform_credit_applications` references `platform_patients` table
- Post-MVP: build migration script to port active v1 applications to v2 if needed, but that's out of scope for this sprint

**Implementation:**
- PR P1 migration creates `platform_credit_applications` fresh with exact schema from v2 spec §2.4
- No ALTER TABLE on `loan_applications`
- No data migration in PR P1

---

## Decision 2: Auth/identity — JWKS-based JWT validation (Supabase-ready but not required)

**Question:** Does PaySpyre have its own user auth, or share with Alvero's Supabase Auth? Where do clinic users authenticate from?

**Finding:** Current `payspyre-backend` has JWT auth using python-jose with symmetric HS256 (`JWT_SECRET_KEY` env var). No Supabase Auth integration exists.

**Decision:** Implement JWKS-based JWT validation middleware that supports BOTH:
- Supabase Auth JWTs (RS256 with JWKS endpoint) — ready for Alvero SSO
- Internal JWTs (HS256 with `JWT_SECRET_KEY`) — fallback for existing tests/dev

**Rationale:**
- Mike's memory indicates Supabase Auth with Alvero SSO is the target state
- JWKS validation is vendor-agnostic and works with both Supabase and future IdPs
- Keeps existing tests working (HS256) while enabling Supabase RS256 via config
- No hard dependency on Supabase Auth being live on day one

**Implementation:**
- New `app/core/auth_jwks.py` with:
  - `JWKS_URL` config env var (optional, null = use legacy HS256)
  - `jwks_fetch()` to cache public keys from JWKS endpoint
  - `validate_jwt()` that tries JWKS first, falls back to HS256 if JWKS_URL not set
- Update `app/core/auth.py::get_current_user()` to use new validator
- Tests: mock JWKS endpoint for Supabase path, keep existing HS256 tests

---

## Decision 3: SIN encryption — pgcrypto with env var key (rotation is post-MVP)

**Question:** Which approach for `sin_encrypted` — Supabase Vault, AWS KMS, pgcrypto with rotating column-key, or external HSM?

**Decision:** pgcrypto with `pgp_sym_encrypt()` using column-level symmetric key sourced from `PAYSPYRE_SIN_ENCRYPTION_KEY` env var.

**Rationale:**
- pgcrypto is already available in Supabase PostgreSQL
- Env var sourcing is simple and works with existing deployment (Cloudflare Workers / Docker)
- Key rotation hooks are deferred to post-MVP per spec ("basic encryption only" in MVP scope)
- Supabase Vault / KMS add infrastructure complexity not justified for MVP

**Implementation:**
- PR P1 migration: `sin_encrypted BYTEA` column with COMMENT explaining encryption
- Model accessor methods in `PlatformPatient`:
  - `set_sin(sin: str)` — encrypts with pgcrypto before write
  - `get_sin_decrypted()` — decrypts on read (used ONLY in bureau adapter)
- Service layer guarantees: never log `sin_encrypted`, never return via API
- Tests: verify `sin_last3` is populated, full value never returns in API responses

**Post-MVP path (deferred):**
- Add key rotation job (reads new key, re-encrypts all rows, writes event)
- Consider KMS/VaaS if key management becomes operational burden

---

## Summary

These three decisions unblock PR P1. All other open questions from spec §13 are deferred to later PRs as indicated in the MVP kickoff doc.

**Next step:** Proceed to PR P0 (rename clinic_id → vendor_id), then PR P1 migrations.
