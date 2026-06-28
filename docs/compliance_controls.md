# Compliance Controls Registry

Auditor-facing evidence map. Each regulatory rule PaySpyre enforces is mapped to
the module that enforces it and the test(s) that prove it. This is the control
registry; the runnable smoke counterpart is `tests/compliance/test_controls_present.py`
(marked `@pytest.mark.compliance`, run via `pytest -m compliance`). The heavy DB/API
integration tests listed below remain the authoritative per-push enforcement gate
(main `tests.yml`); the smoke suite is the fast, DB-free green/red over the registry.

Status legend: **ENFORCED** = invariant is wired in code and covered by passing tests.

| # | Control | Regulation / source | Enforcing module(s) | Enforcing test(s) | Smoke (`-m compliance`) | Status |
|---|---------|---------------------|---------------------|-------------------|--------------------------|--------|
| C1 | Canadian regulatory APR calculation (APR = C/(T·P)·100; fees=0 ⇒ APR = nominal rate) | Cost of Borrowing (Banks) Regulations **SOR/2001-104 s.3–4** | `app/services/loan_quote.py::compute_apr_bps` (and `quote_loan`) | `tests/test_loan_apr.py` (`TestAprNoFeesEqualsContractRate`, `TestAprWithFees`, `TestAprRegulatoryWorkedExample::test_regulatory_apr_with_fee`) | `TestRegulatoryAPR` | ENFORCED |
| C2 | Criminal-rate cap — APR may not reach/exceed 35% | **Criminal Code (Canada) s.347**, lowered to 35% APR by Bill C-47 (in force 2026-01-01) | Constant + helper `loan_quote.CRIMINAL_RATE_CAP_BPS` (3500) / `exceeds_criminal_rate`; enforced at **quote** (`quote_loan` sets `exceeds_criminal_rate`), at **product config** (`app/api/v1/endpoints/credit_products.py::_reject_criminal_rate_config` via `product_worst_case_apr_bps`), and at **booking** (`app/services/loan_servicing.py::create_loan_from_application`) | `tests/test_loan_apr.py::TestCriminalRateCap`; `tests/test_credit_products_api.py::test_create_rejects_criminal_rate_pricing`; `tests/test_booking_criminal_rate.py` (`test_booking_refuses_criminal_rate`, `test_booking_allows_normal_rate`) | `TestCriminalRateCap` | ENFORCED |
| C3 | SIN encryption-at-rest (full SIN never stored plaintext, never logged/returned) | **PIPEDA** safeguards principle (Principle 7) | `app/core/sin_crypto.py` (Fernet envelope: AES-128-CBC + HMAC-SHA256, dedicated `SIN_ENCRYPTION_KEY`) | `tests/test_manual_application.py::test_sin_is_encrypted_not_stored_plaintext` | `TestSinEncryption` | ENFORCED |
| C4 | Marketing consent is separate, versioned, and OPTIONAL (anti-bundling; never gates underwriting) | **CASL / CRTC opt-in** + **PIPEDA** anti-bundling | `config/consent_text/` registry (active.json + `marketing_communications/`); `app/services/flow_orchestrator.py::_CONSENT_ORDER` (excludes marketing) | `tests/test_marketing_consent.py` (`test_marketing_consent_is_registered_and_versioned`, `test_marketing_consent_is_not_a_required_underwriting_consent`) | `TestMarketingConsentSeparate` | ENFORCED |
| C5 | `application.status` write guardrail — status transitions owned solely by the orchestrator | Internal control / spec §4.3 (decision integrity; supports auditable lending decisions) | `app/services/flow_orchestrator.py` (sole writer of `PlatformCreditApplication.status`) | `tests/test_application_status_writes.py::test_application_status_written_only_by_orchestrator` | (static-scan test; covered by heavy suite) | ENFORCED |
| C6 | Consent audit trail (purpose + version + IP + UA recorded per grant) | **PIPEDA** accountability / consent record-keeping | `app/services/flow_orchestrator.py::record_consent_grant` (emits `consent_granted` with version + IP + UA); consent text registry `config/consent_text/active.json` | `tests/test_flow_orchestrator.py`, `tests/test_consent_service.py` | (DB-bound; covered by heavy suite) | ENFORCED |
| C7 | Pre-qualification decision single-source-of-truth (widget pre-qual uses the SAME band as the full decision) | Internal control — consistent/auditable automated decisioning (PIPEDA automated-decision transparency) | `app/services/flow_engine.py::prequalify_score` (reads `verification_matrix.bureau.manual_review_band`) | `tests/test_flow_engine.py` (`test_low_score_declined`, `test_mid_band_manual_review`, `test_manual_review_band_product_override`) | `TestPrequalSingleSourceOfTruth` | ENFORCED |

## How to run

```bash
# Fast, DB-free registry smoke (this control suite only):
pytest -m compliance -q

# Full authoritative enforcement gate (the heavy DB/API tests above):
pytest
```

CI: `.github/workflows/compliance-controls.yml` runs `pytest -m compliance` on every
push/PR and reports the result (non-blocking for now).

## Notes for auditors

- The smoke suite calls the enforcing functions **directly** (pure / DB-free) so it
  is a fast green/red over the registry. It intentionally does **not** re-prove the
  DB-bound enforcement paths (booking refusal, status-write scan, consent-grant
  persistence) — those are owned by the heavy integration tests in the same column,
  run by the main `tests.yml` job.
- The s.347 cap (`CRIMINAL_RATE_CAP_BPS = 3500`) is centralized in one constant so all
  three enforcement points (quote / product-config / booking) share a single source.
- The APR figures and the 35% cap value should receive a final legal/QA pass against a
  known worked example before go-live (see the COMPLIANCE NOTE in `loan_quote.py`).
