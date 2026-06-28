"""Auditor-facing compliance control SMOKE suite.

This is the runnable counterpart to ``docs/compliance_controls.md``: a fast,
DB-free assertion that each regulatory invariant is still wired and behaving.
It deliberately does NOT duplicate the heavy DB / API integration tests (those
remain the authoritative enforcement gate, run by the main ``tests.yml`` job).
Here we call the underlying pure functions directly so the whole suite stays
pure and runs in milliseconds, and so a single ``pytest -m compliance`` invocation
gives an auditor a green/red on the controls registry.

Run with:
  pytest -m compliance -q

Every test is marked ``@pytest.mark.compliance``.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.compliance


# ---------------------------------------------------------------------------
# Control C1 — Canadian regulatory APR (Cost of Borrowing Regs SOR/2001-104 s.3-4)
# Enforcing module: app/services/loan_quote.py::compute_apr_bps
# ---------------------------------------------------------------------------
class TestRegulatoryAPR:
    def test_s4_no_fees_apr_is_the_nominal_rate(self):
        """s.4: with no cost of borrowing other than interest (fees=0), the APR
        IS the annual interest rate. compute_apr_bps must return it unchanged."""
        from app.services.loan_quote import compute_apr_bps

        assert compute_apr_bps(1_000_000, 1290, 12, "monthly", fees_cents=0) == 1290
        assert compute_apr_bps(2_500_000, 1999, 36, "bi_weekly", fees_cents=0) == 1999

    def test_s3_fees_push_apr_above_the_nominal_rate(self):
        """s.3(1) APR = (C/(T*P))*100: a fee is part of C, so APR > nominal rate."""
        from app.services.loan_quote import compute_apr_bps

        base = compute_apr_bps(1_000_000, 1290, 12, "monthly", fees_cents=0)
        with_fee = compute_apr_bps(1_000_000, 1290, 12, "monthly", fees_cents=20_000)
        assert with_fee > base

    def test_s3_known_worked_example(self):
        """Hand-computed against s.3(1): $10,000 @ 12.00% nominal, 12 monthly
        payments, $300 fee -> 1744 bps (17.44%). Pins the regulatory formula."""
        from app.services.loan_quote import compute_apr_bps

        assert compute_apr_bps(1_000_000, 1200, 12, "monthly", fees_cents=30_000) == 1744


# ---------------------------------------------------------------------------
# Control C2 — Criminal Code (Canada) s.347 35% APR cap (Bill C-47, 2026-01-01)
# Enforcing module: app/services/loan_quote.py::exceeds_criminal_rate + cap const
# Enforced at quote, at product-config, and at booking (see registry).
# ---------------------------------------------------------------------------
class TestCriminalRateCap:
    def test_cap_is_3500_bps(self):
        from app.services.loan_quote import CRIMINAL_RATE_CAP_BPS

        assert CRIMINAL_RATE_CAP_BPS == 3500

    def test_boundary_is_inclusive(self):
        """A loan AT the cap is already criminal (>= cap)."""
        from app.services.loan_quote import exceeds_criminal_rate

        assert exceeds_criminal_rate(3500) is True
        assert exceeds_criminal_rate(3499) is False

    def test_product_worst_case_apr_catches_criminal_config(self):
        """Config-time guard: a small advance + heavy fee + short term can exceed
        the cap; product_worst_case_apr_bps must surface that worst case."""
        from app.services.loan_quote import (
            exceeds_criminal_rate,
            product_worst_case_apr_bps,
        )

        pricing = {
            "annual_rate_bps": 3000,
            "fees_cents": 80_000,
            "term_min": 6,
            "term_max": 12,
        }
        worst = product_worst_case_apr_bps(min_amount_cents=100_000, pricing_config=pricing)
        assert exceeds_criminal_rate(worst) is True


# ---------------------------------------------------------------------------
# Control C3 — SIN encryption-at-rest (PIPEDA safeguards)
# Enforcing module: app/core/sin_crypto.py
# ---------------------------------------------------------------------------
class TestSinEncryption:
    def test_encrypt_roundtrips_under_a_configured_key(self, monkeypatch):
        """With a key configured, the stored value is a marked ciphertext (NOT the
        plaintext SIN) and decrypts back to the original."""
        from cryptography.fernet import Fernet

        from app.core import sin_crypto

        key = Fernet.generate_key().decode("utf-8")
        monkeypatch.setattr(sin_crypto, "_configured_key", lambda: key)

        stored = sin_crypto.encrypt_sin("123456789")
        assert stored is not None
        assert stored.startswith("sinv1:")
        assert "123456789" not in stored
        assert sin_crypto.decrypt_sin(stored) == "123456789"

    def test_marker_prevents_double_encryption(self, monkeypatch):
        from cryptography.fernet import Fernet

        from app.core import sin_crypto

        key = Fernet.generate_key().decode("utf-8")
        monkeypatch.setattr(sin_crypto, "_configured_key", lambda: key)

        once = sin_crypto.encrypt_sin("987654321")
        twice = sin_crypto.encrypt_sin(once)
        assert twice == once  # already-marked value is returned as-is


# ---------------------------------------------------------------------------
# Control C4 — CASL/PIPEDA: marketing consent is registered, versioned & OPTIONAL
# Enforcing module: config/consent_text + flow_orchestrator._CONSENT_ORDER
# ---------------------------------------------------------------------------
class TestMarketingConsentSeparate:
    def test_marketing_consent_registered_and_versioned(self):
        from app.services import consent_service

        ct = consent_service.get_active_consent_text("marketing_communications")
        assert ct.purpose == "marketing_communications"
        assert ct.version

    def test_marketing_consent_is_not_a_required_underwriting_consent(self):
        """Anti-bundling: marketing is never part of the required consent ordering,
        so the underwriting flow can never gate on it."""
        from app.services.flow_orchestrator import _CONSENT_ORDER

        assert "marketing_communications" not in _CONSENT_ORDER


# ---------------------------------------------------------------------------
# Control C5 — Pre-qualification decision single-source-of-truth
# Enforcing module: app/services/flow_engine.py::prequalify_score
# ---------------------------------------------------------------------------
class TestPrequalSingleSourceOfTruth:
    def test_band_logic_uses_the_manual_review_band(self):
        """Below band -> declined; inside band -> manual_review; above -> approved;
        no score -> unknown. Same band the full decision uses."""
        from app.services.flow_engine import DEFAULT_MANUAL_REVIEW_BAND, prequalify_score

        lo = DEFAULT_MANUAL_REVIEW_BAND["min"]
        hi = DEFAULT_MANUAL_REVIEW_BAND["max"]

        assert prequalify_score(lo - 1, {}) == "declined"
        assert prequalify_score(lo, {}) == "manual_review"
        assert prequalify_score(hi, {}) == "manual_review"
        assert prequalify_score(hi + 1, {}) == "approved"
        assert prequalify_score(None, {}) == "unknown"

    def test_band_is_product_overridable(self):
        """The band is read from verification_matrix.bureau.manual_review_band so the
        widget pre-qual and the full decision can never diverge."""
        from app.services.flow_engine import prequalify_score

        cfg = {"manual_review_band": {"min": 500, "max": 600}}
        assert prequalify_score(499, cfg) == "declined"
        assert prequalify_score(550, cfg) == "manual_review"
        assert prequalify_score(601, cfg) == "approved"
