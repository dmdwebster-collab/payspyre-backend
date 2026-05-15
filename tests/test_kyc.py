import pytest
from uuid import uuid4

from app.services.risk_engine import RiskRulesEngine
from app.services.underwriting_state_machine import state_machine, KycState


@pytest.fixture
def risk_engine():
    return RiskRulesEngine()


@pytest.mark.asyncio
async def test_risk_engine_approve_clean_record(risk_engine):
    kyc_result = {
        "checks": [
            {"type": "identity", "details": {"score": 0.98}},
            {"type": "liveness", "details": {"score": 0.99}},
            {"type": "aml", "details": {"hits": []}},
        ]
    }

    loan_app = {
        "address": {"country": "CA"},
        "credit_history_months": 24,
        "loan_amount": 3000,
    }

    result = await risk_engine.evaluate(kyc_result, loan_app)

    assert result.decision == "approve"
    assert result.risk_score >= 0.85


@pytest.mark.asyncio
async def test_risk_engine_reject_liveness_failed(risk_engine):
    kyc_result = {
        "checks": [
            {"type": "liveness", "details": {"score": 0.80}},
        ]
    }

    loan_app = {
        "address": {"country": "CA"},
    }

    result = await risk_engine.evaluate(kyc_result, loan_app)

    assert result.decision == "rejected"
    assert "liveness_failed" in result.flags_applied


@pytest.mark.asyncio
async def test_risk_engine_reject_non_ca_address(risk_engine):
    kyc_result = {
        "checks": [],
    }

    loan_app = {
        "address": {"country": "US"},
    }

    result = await risk_engine.evaluate(kyc_result, loan_app)

    assert result.decision == "rejected"
    assert "non_ca_address" in result.flags_applied


@pytest.mark.asyncio
async def test_risk_engine_manual_review_low_identity_score(risk_engine):
    kyc_result = {
        "checks": [
            {"type": "identity", "details": {"score": 0.85}},
            {"type": "liveness", "details": {"score": 0.99}},
            {"type": "aml", "details": {"hits": []}},
        ]
    }

    loan_app = {
        "address": {"country": "CA"},
    }

    result = await risk_engine.evaluate(kyc_result, loan_app)

    assert result.decision == "manual_review"
    assert "identity_mismatch" in result.flags_applied


@pytest.mark.asyncio
async def test_state_machine_kyc_session_created():
    transition = await state_machine.handle_event(
        KycState.PENDING_KYC,
        "kyc_session_created",
    )

    assert transition is not None
    assert transition.from_state == KycState.PENDING_KYC
    assert transition.to_state == KycState.KYC_IN_PROGRESS


@pytest.mark.asyncio
async def test_state_machine_kyc_result_received():
    transition = await state_machine.handle_event(
        KycState.KYC_IN_PROGRESS,
        "kyc_result_received",
    )

    assert transition is not None
    assert transition.to_state == KycState.KYC_COMPLETED


@pytest.mark.asyncio
async def test_state_machine_invalid_event():
    transition = await state_machine.handle_event(
        KycState.PENDING_KYC,
        "invalid_event",
    )

    assert transition is None


@pytest.mark.asyncio
async def test_state_machine_terminal_state():
    assert state_machine.is_terminal(KycState.REJECTED)
    assert not state_machine.is_terminal(KycState.KYC_IN_PROGRESS)


@pytest.mark.asyncio
async def test_state_machine_valid_events():
    events = state_machine.get_valid_events(KycState.PENDING_KYC)
    assert "kyc_session_created" in events

    events = state_machine.get_valid_events(KycState.KYC_IN_PROGRESS)
    assert "kyc_result_received" in events