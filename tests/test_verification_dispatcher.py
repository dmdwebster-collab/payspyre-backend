"""Tests for VerificationDispatcher — flag-based real/mock selection with respx."""
import uuid

import httpx
import pytest
import respx

from app.core.config import settings


def _new_dispatcher():
    # Import inside the helper so we get a fresh instance after monkeypatching
    # settings.USE_REAL_ADAPTERS (the dispatcher reads the flag in __init__).
    from app.services.verifications.dispatcher import VerificationDispatcher
    return VerificationDispatcher()


def _patch_real(monkeypatch):
    monkeypatch.setattr(settings, "USE_REAL_ADAPTERS", True)
    monkeypatch.setattr(settings, "DIDIT_API_KEY", "test-didit-key")
    monkeypatch.setattr(settings, "DIDIT_WORKFLOW_ID", "wf-test")
    monkeypatch.setattr(settings, "FLINKS_API_KEY", "test-flinks-key")
    monkeypatch.setattr(settings, "FLINKS_CUSTOMER_ID", "cust-test")


class TestMockMode:
    def test_flag_off_routes_kyc_to_mock(self):
        d = _new_dispatcher()
        r = d.initiate("kyc_id", uuid.uuid4(), uuid.uuid4(), {})
        assert r.vendor == "mock"
        assert r.vendor_session_ref.startswith("mock_")

    def test_flag_off_routes_bank_link_to_mock(self):
        d = _new_dispatcher()
        r = d.initiate("bank_link", uuid.uuid4(), uuid.uuid4(), {})
        assert r.vendor == "mock"

    def test_bureau_always_mock_even_when_flag_on(self, monkeypatch):
        _patch_real(monkeypatch)
        d = _new_dispatcher()
        r = d.initiate("bureau_soft", uuid.uuid4(), uuid.uuid4(), {})
        assert r.vendor == "mock"
        r2 = d.initiate("bureau_hard", uuid.uuid4(), uuid.uuid4(), {})
        assert r2.vendor == "mock"


class TestRealMode:
    @respx.mock
    def test_flag_on_kyc_routes_to_didit_http(self, monkeypatch):
        _patch_real(monkeypatch)
        route = respx.post("https://verification.didit.me/v3/session/").mock(
            return_value=httpx.Response(
                201, json={"session_id": "didit-sess-1", "url": "https://verify.didit.me/x"}
            )
        )
        d = _new_dispatcher()
        r = d.initiate("kyc_id", uuid.uuid4(), uuid.uuid4(), {})
        assert route.called
        assert r.vendor == "didit"
        assert r.vendor_session_ref == "didit-sess-1"
        assert r.redirect_url == "https://verify.didit.me/x"

    def test_flag_on_bank_link_routes_to_flinks_connect_url(self, monkeypatch):
        _patch_real(monkeypatch)
        d = _new_dispatcher()
        app_id = uuid.uuid4()
        r = d.initiate("bank_link", app_id, uuid.uuid4(), {})
        assert r.vendor == "flinks"
        # vendor_session_ref is the Connect URL until the LoginId arrives post-redirect (P7.2b).
        assert "toolbox-iframe.private.fin.ag" in r.vendor_session_ref
        assert "customerId=cust-test" in r.vendor_session_ref
        assert str(app_id) in r.vendor_session_ref  # encoded in redirectUrl for correlation


class TestFlowOrchestratorConsumes:
    """§5 Step 5: confirm FlowOrchestrator can consume the new dispatcher
    (duck-typed; no orchestrator changes needed)."""

    def test_dispatch_result_satisfies_orchestrator_contract(self):
        from app.services.verifications.dispatcher import DispatchResult
        # FlowOrchestrator.initiate_verification reads .vendor + .vendor_session_ref
        # from the dispatcher result. DispatchResult exposes both.
        r = DispatchResult(vendor="x", vendor_session_ref="y")
        assert r.vendor == "x"
        assert r.vendor_session_ref == "y"
