"""Pure tests for the P6 replay adapters (no DB).

Asserts the adapters reconstruct exactly the typed result objects defined in
``app/services/adapters/base.py``.
"""
import asyncio

import pytest

from app.services.adapters.base import (
    BankAccountSummary,
    BureauResult,
    PatientProfile,
    VerificationResult,
)
from app.services.verifications.replay_adapters import (
    ReplayBankAdapter,
    ReplayBureauAdapter,
    ReplayMissingResultError,
    ReplayVerificationAdapter,
)
from uuid import uuid4


def _patient():
    return PatientProfile(patient_id=uuid4(), province=None, email="r@example.com")


def test_replay_bureau_reconstructs_bureau_result():
    stored = {
        "bureau_soft": {
            "result": "passed",
            "credit_score": 712,
            "bankruptcy": False,
            "fraud_signals": {"identity_high_risk": False},
            "confidence": 0.9,
        }
    }
    out = asyncio.run(ReplayBureauAdapter(stored).soft_pull(_patient()))
    assert isinstance(out, BureauResult)
    assert out.pull_type == "soft"
    assert out.score == 712
    assert out.result == "passed"
    assert out.bankruptcy is False
    assert out.fraud_signals == {"identity_high_risk": False}
    assert out.confidence == 0.9


def test_replay_bureau_coerces_non_dict_fraud_signals():
    # mock default sometimes carries an empty list; BureauResult needs a dict.
    stored = {"bureau_hard": {"result": "passed", "credit_score": 700, "fraud_signals": []}}
    out = asyncio.run(ReplayBureauAdapter(stored).hard_pull(_patient()))
    assert out.fraud_signals == {}
    assert out.pull_type == "hard"


def test_replay_bureau_missing_result_raises():
    with pytest.raises(ReplayMissingResultError, match="bureau_soft"):
        asyncio.run(ReplayBureauAdapter({}).soft_pull(_patient()))


def test_replay_verification_reconstructs_with_confidence():
    stored = {"kyc_id": {"result": "passed", "confidence": 0.97, "method": "id_doc_scan"}}
    out = asyncio.run(ReplayVerificationAdapter(stored).verify_identity(_patient(), "email_otp"))
    assert isinstance(out, VerificationResult)
    assert out.result == "passed"
    assert out.confidence == 0.97
    assert out.method == "id_doc_scan"  # stored method wins over the requested one


def test_replay_bank_maps_payload_keys_to_summary_fields():
    stored = {
        "bank_link": {
            "result": "passed",
            "monthly_income_cents": 825000,
            "nsf_count_90d": 2,
            "avg_balance_cents": 130000,
            "account_age_months": 24,
        }
    }
    out = asyncio.run(ReplayBankAdapter(stored).link_account(_patient()))
    assert isinstance(out, BankAccountSummary)
    assert out.result == "passed"
    assert out.monthly_income_after_tax_cents == 825000  # mapped key
    assert out.balance_current_cents == 130000  # mapped key
    assert out.nsf_count_90d == 2
    assert out.account_age_months == 24
