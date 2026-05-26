"""Pure tests for the P6 mock verification dispatcher (no DB)."""
from uuid import uuid4

from app.services.verifications.mock_dispatcher import (
    MockInitiationResult,
    MockVerificationDispatcher,
)


def test_initiate_returns_mock_vendor_and_unique_session_ref():
    d = MockVerificationDispatcher()
    r1 = d.initiate("kyc_id", uuid4(), uuid4(), {})
    r2 = d.initiate("kyc_id", uuid4(), uuid4(), {})
    assert isinstance(r1, MockInitiationResult)
    assert r1.vendor == "mock"
    assert r1.vendor_session_ref.startswith("mock_")
    assert r1.vendor_session_ref != r2.vendor_session_ref  # unique
    assert r1.started_at.tzinfo is not None


def test_simulate_callback_returns_default_rich_payload_shape():
    d = MockVerificationDispatcher()
    cb = d.simulate_callback("bureau_soft", result="passed")
    assert cb["vendor"] == "mock"
    assert cb["verification_type"] == "bureau_soft"
    assert cb["result"] == "passed"
    assert cb["rich_payload"]["credit_score"] == 720
    assert cb["rich_payload"]["bankruptcy"] is False
    # fraud_signals must be a dict (BureauResult.fraud_signals is dict[str, object])
    assert isinstance(cb["rich_payload"]["fraud_signals"], dict)


def test_simulate_callback_accepts_overrides():
    d = MockVerificationDispatcher()
    cb = d.simulate_callback(
        "bureau_soft", result="passed", rich_payload={"credit_score": 550, "bankruptcy": True}
    )
    assert cb["rich_payload"]["credit_score"] == 550
    assert cb["rich_payload"]["bankruptcy"] is True
    # untouched defaults remain
    assert cb["rich_payload"]["inquiries_6mo"] == 1
