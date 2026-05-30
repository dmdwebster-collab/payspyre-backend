"""Unit tests for FlinksBankAdapter — initiate() is URL-only (no outbound HTTP)."""
import asyncio
import uuid
from urllib.parse import parse_qs, urlparse

import pytest

from app.services.adapters.base import PatientProfile
from app.services.adapters.flinks_bank import (
    FlinksBankAdapter,
    FlinksInitiationResult,
)

_API_KEY = "test-flinks-key"
_API_BASE_URL = "https://toolbox-api.private.fin.ag"
_CUSTOMER_ID = "cust-00000000-0000-0000-0000-000000000001"


def _patient() -> PatientProfile:
    return PatientProfile(patient_id=uuid.uuid4())


def _adapter() -> FlinksBankAdapter:
    return FlinksBankAdapter(
        api_key=_API_KEY, api_base_url=_API_BASE_URL, customer_id=_CUSTOMER_ID
    )


class TestInitiateConnectUrl:
    def test_returns_flinks_initiation_result(self):
        result = _adapter().initiate(application_id="a", patient=_patient(), cost_cents=99)
        assert isinstance(result, FlinksInitiationResult)
        assert result.cost_cents == 99

    def test_login_id_is_none_at_initiate(self):
        result = _adapter().initiate(application_id="a", patient=_patient())
        assert result.login_id is None  # populated only after Flinks Connect redirects back

    def test_connect_url_uses_iframe_origin(self):
        result = _adapter().initiate(application_id="a", patient=_patient())
        parsed = urlparse(result.connect_url)
        assert parsed.scheme == "https"
        assert parsed.netloc == "toolbox-iframe.private.fin.ag"
        assert parsed.path == "/v2/"

    def test_connect_url_carries_customer_id(self):
        result = _adapter().initiate(application_id="a", patient=_patient())
        qs = parse_qs(urlparse(result.connect_url).query)
        assert qs["customerId"] == [_CUSTOMER_ID]

    def test_connect_url_has_redirect_with_application_id(self):
        app_id = str(uuid.uuid4())
        result = _adapter().initiate(application_id=app_id, patient=_patient())
        qs = parse_qs(urlparse(result.connect_url).query)
        assert "redirectUrl" in qs
        redirect = qs["redirectUrl"][0]
        # application_id encoded in the redirect for post-Connect correlation
        redirect_qs = parse_qs(urlparse(redirect).query)
        assert redirect_qs["application_id"] == [app_id]

    def test_connect_url_carries_tag_for_webhook_correlation(self):
        # P7.2b: Flinks Tag is the webhook-correlation bridge — Flinks echoes the
        # tag back in the webhook body, letting us look up PlatformVerification by
        # application_id when the webhook arrives.
        app_id = str(uuid.uuid4())
        result = _adapter().initiate(application_id=app_id, patient=_patient())
        qs = parse_qs(urlparse(result.connect_url).query)
        assert qs["tag"] == [app_id]


class TestLinkAccountNotImplemented:
    def test_async_method_raises_not_implemented(self):
        with pytest.raises(NotImplementedError, match="webhook"):
            asyncio.run(_adapter().link_account(_patient()))
