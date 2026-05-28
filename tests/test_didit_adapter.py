"""Unit tests for DiditVerificationAdapter — uses respx to mock httpx (no live HTTP)."""
import asyncio
import uuid

import httpx
import pytest
import respx

from app.services.adapters.base import PatientProfile
from app.services.adapters.didit_verification import (
    DiditAPIError,
    DiditInitiationResult,
    DiditVerificationAdapter,
)

_API_KEY = "test-didit-key"
_BASE_URL = "https://verification.didit.me"
_WORKFLOW_ID = "wf-00000000-0000-0000-0000-000000000001"
_SESSION_URL = f"{_BASE_URL}/v3/session/"


def _patient(email: str | None = None) -> PatientProfile:
    return PatientProfile(patient_id=uuid.uuid4(), email=email)


def _adapter() -> DiditVerificationAdapter:
    return DiditVerificationAdapter(
        api_key=_API_KEY, api_base_url=_BASE_URL, workflow_id=_WORKFLOW_ID
    )


def _ok_response() -> httpx.Response:
    return httpx.Response(
        201,
        json={"session_id": "abc-session-1234", "url": "https://verify.didit.me/abc"},
    )


class TestInitiateRequest:
    @respx.mock
    def test_posts_correct_url(self):
        route = respx.post(_SESSION_URL).mock(return_value=_ok_response())
        _adapter().initiate(application_id=str(uuid.uuid4()), patient=_patient())
        assert route.called
        assert route.call_count == 1
        assert str(route.calls.last.request.url) == _SESSION_URL

    @respx.mock
    def test_sends_x_api_key_header(self):
        route = respx.post(_SESSION_URL).mock(return_value=_ok_response())
        _adapter().initiate(application_id=str(uuid.uuid4()), patient=_patient())
        assert route.calls.last.request.headers["x-api-key"] == _API_KEY

    @respx.mock
    def test_body_has_workflow_id_and_vendor_data(self):
        import json
        route = respx.post(_SESSION_URL).mock(return_value=_ok_response())
        app_id = str(uuid.uuid4())
        _adapter().initiate(application_id=app_id, patient=_patient())
        body = json.loads(route.calls.last.request.content)
        assert body["workflow_id"] == _WORKFLOW_ID
        assert body["vendor_data"] == app_id
        assert body["metadata"] == {"application_id": app_id}

    @respx.mock
    def test_includes_contact_details_when_email_present(self):
        import json
        route = respx.post(_SESSION_URL).mock(return_value=_ok_response())
        _adapter().initiate(application_id="a", patient=_patient(email="pat@example.com"))
        body = json.loads(route.calls.last.request.content)
        assert body["contact_details"] == {"email": "pat@example.com"}

    @respx.mock
    def test_omits_contact_details_when_no_email(self):
        import json
        route = respx.post(_SESSION_URL).mock(return_value=_ok_response())
        _adapter().initiate(application_id="a", patient=_patient())
        body = json.loads(route.calls.last.request.content)
        assert "contact_details" not in body


class TestInitiateResponse:
    @respx.mock
    def test_returns_session_id_and_url(self):
        respx.post(_SESSION_URL).mock(return_value=_ok_response())
        result = _adapter().initiate(application_id="a", patient=_patient(), cost_cents=42)
        assert isinstance(result, DiditInitiationResult)
        assert result.session_id == "abc-session-1234"
        assert result.url == "https://verify.didit.me/abc"
        assert result.cost_cents == 42

    @respx.mock
    def test_raises_on_4xx(self):
        respx.post(_SESSION_URL).mock(
            return_value=httpx.Response(401, json={"detail": "unauthorized"})
        )
        with pytest.raises(DiditAPIError, match="401"):
            _adapter().initiate(application_id="a", patient=_patient())

    @respx.mock
    def test_raises_on_5xx(self):
        respx.post(_SESSION_URL).mock(return_value=httpx.Response(500))
        with pytest.raises(DiditAPIError, match="500"):
            _adapter().initiate(application_id="a", patient=_patient())


class TestVerifyIdentityNotImplemented:
    def test_async_method_raises_not_implemented(self):
        with pytest.raises(NotImplementedError, match="webhook"):
            asyncio.run(_adapter().verify_identity(_patient(), "id_doc_scan"))
