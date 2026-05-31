"""Unit tests for the Twilio ``X-Twilio-Signature`` scheme (P7.4b).

The algorithm is delegated to ``twilio.request_validator.RequestValidator``;
these tests confirm the wiring and the URL/form/header pass-through. The
endpoint integration tests (``test_inbound_notifications_twilio.py``) cover
the full happy / unhappy paths against the live stack.
"""
import pytest
from twilio.request_validator import RequestValidator

from app.core.config import settings
from app.services.webhooks.signature_verifier import (
    SignatureInvalid,
    SignatureVerifier,
)

_AUTH_TOKEN = "twilio_test_auth_token_123"
_URL = "https://app.payspyre.test/api/webhooks/v1/notifications/twilio"
_FORM = {"MessageSid": "SM" + "f" * 32, "MessageStatus": "delivered",
         "AccountSid": "AC" + "a" * 32}


@pytest.fixture(autouse=True)
def _patch_token(monkeypatch):
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", _AUTH_TOKEN)


def _sign(url=_URL, form=_FORM, token=_AUTH_TOKEN) -> str:
    return RequestValidator(token).compute_signature(url, form)


class TestTwilio:
    def test_valid_signature_passes(self):
        sig = _sign()
        SignatureVerifier().verify_twilio_form(
            url=_URL, form_params=_FORM,
            headers={"X-Twilio-Signature": sig},
        )

    def test_tampered_url_fails(self):
        sig = _sign(url=_URL)
        with pytest.raises(SignatureInvalid):
            SignatureVerifier().verify_twilio_form(
                url=_URL + "?attacker=1", form_params=_FORM,
                headers={"X-Twilio-Signature": sig},
            )

    def test_tampered_form_param_fails(self):
        sig = _sign(form=_FORM)
        tampered = dict(_FORM, MessageStatus="failed")
        with pytest.raises(SignatureInvalid):
            SignatureVerifier().verify_twilio_form(
                url=_URL, form_params=tampered,
                headers={"X-Twilio-Signature": sig},
            )

    def test_missing_signature_header_fails(self):
        with pytest.raises(SignatureInvalid, match="Missing X-Twilio-Signature"):
            SignatureVerifier().verify_twilio_form(
                url=_URL, form_params=_FORM, headers={},
            )
