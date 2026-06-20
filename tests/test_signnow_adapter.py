"""Unit tests for SignNowAdapter — respx mocks httpx (NO network), NO database.

Covers: send-for-signature (document + template paths), status check, 5xx
-> transient, 4xx -> permanent, and webhook verify pass/fail.
"""
import base64
import hashlib
import hmac
import json

import httpx
import pytest
import respx

from app.services.esign import (
    FieldValue,
    SignerInput,
    SignNowAdapter,
    SignNowPermanentError,
    SignNowSendResult,
    SignNowStatusResult,
    SignNowTransientError,
    SignNowWebhookError,
    SignNowWebhookEvent,
)

_TOKEN = "test-bearer-token"
_BASE_URL = "https://api-eval.signnow.com"
_WEBHOOK_SECRET = "wh-secret-123"
_DOC_ID = "doc-abc-123"
_TEMPLATE_ID = "tpl-xyz-789"


def _adapter() -> SignNowAdapter:
    return SignNowAdapter(
        api_key=_TOKEN, base_url=_BASE_URL, webhook_secret=_WEBHOOK_SECRET
    )


def _signer() -> SignerInput:
    return SignerInput(email="borrower@example.com", name="Pat Borrower")


def _invite_url(document_id: str) -> str:
    return f"{_BASE_URL}/document/{document_id}/invite"


def _document_url(document_id: str) -> str:
    return f"{_BASE_URL}/document/{document_id}"


def _template_copy_url(template_id: str) -> str:
    return f"{_BASE_URL}/template/{template_id}/copy"


# ---------------------------------------------------------------------------
# send_for_signature — existing document
# ---------------------------------------------------------------------------


class TestSendForSignatureDocument:
    @respx.mock
    def test_success_returns_document_id_and_signing_url(self):
        route = respx.post(_invite_url(_DOC_ID)).mock(
            return_value=httpx.Response(
                200, json={"id": "invite-1", "url": "https://app.signnow.com/sign/abc"}
            )
        )
        result = _adapter().send_for_signature(signer=_signer(), document_id=_DOC_ID)
        assert isinstance(result, SignNowSendResult)
        assert result.document_id == _DOC_ID
        assert result.signing_url == "https://app.signnow.com/sign/abc"
        assert result.invite_id == "invite-1"
        assert route.called

    @respx.mock
    def test_sends_bearer_auth_header(self):
        route = respx.post(_invite_url(_DOC_ID)).mock(
            return_value=httpx.Response(200, json={"id": "invite-1"})
        )
        _adapter().send_for_signature(signer=_signer(), document_id=_DOC_ID)
        assert route.calls.last.request.headers["authorization"] == f"Bearer {_TOKEN}"

    @respx.mock
    def test_invite_body_carries_signer_email_and_role(self):
        route = respx.post(_invite_url(_DOC_ID)).mock(
            return_value=httpx.Response(200, json={"id": "i"})
        )
        _adapter().send_for_signature(
            signer=SignerInput(email="b@example.com", name="B", role="Borrower"),
            document_id=_DOC_ID,
        )
        body = json.loads(route.calls.last.request.content)
        assert body["to"][0]["email"] == "b@example.com"
        assert body["to"][0]["role"] == "Borrower"

    @respx.mock
    def test_signing_url_none_when_account_emails_invite(self):
        respx.post(_invite_url(_DOC_ID)).mock(
            return_value=httpx.Response(200, json={"id": "invite-1"})
        )
        result = _adapter().send_for_signature(signer=_signer(), document_id=_DOC_ID)
        assert result.signing_url is None

    def test_requires_exactly_one_of_document_or_template(self):
        with pytest.raises(ValueError, match="exactly one"):
            _adapter().send_for_signature(signer=_signer())
        with pytest.raises(ValueError, match="exactly one"):
            _adapter().send_for_signature(
                signer=_signer(), document_id=_DOC_ID, template_id=_TEMPLATE_ID
            )


# ---------------------------------------------------------------------------
# send_for_signature — from template (copy -> prefill -> invite)
# ---------------------------------------------------------------------------


class TestSendForSignatureTemplate:
    @respx.mock
    def test_copies_template_then_invites_new_document(self):
        new_doc = "doc-from-tpl-1"
        copy_route = respx.post(_template_copy_url(_TEMPLATE_ID)).mock(
            return_value=httpx.Response(200, json={"id": new_doc})
        )
        invite_route = respx.post(_invite_url(new_doc)).mock(
            return_value=httpx.Response(200, json={"id": "invite-9", "url": "https://s/x"})
        )
        result = _adapter().send_for_signature(
            signer=_signer(), template_id=_TEMPLATE_ID
        )
        assert copy_route.called
        assert invite_route.called
        assert result.document_id == new_doc

    @respx.mock
    def test_prefills_fields_before_inviting(self):
        new_doc = "doc-from-tpl-2"
        respx.post(_template_copy_url(_TEMPLATE_ID)).mock(
            return_value=httpx.Response(200, json={"id": new_doc})
        )
        prefill_route = respx.put(_document_url(new_doc)).mock(
            return_value=httpx.Response(200, json={})
        )
        respx.post(_invite_url(new_doc)).mock(
            return_value=httpx.Response(200, json={"id": "i"})
        )
        _adapter().send_for_signature(
            signer=_signer(),
            template_id=_TEMPLATE_ID,
            fields=[FieldValue(name="loan_amount", value="5000")],
        )
        assert prefill_route.called
        body = json.loads(prefill_route.calls.last.request.content)
        assert body["fields"][0]["name"] == "loan_amount"
        assert body["fields"][0]["prefilled_text"] == "5000"

    @respx.mock
    def test_raises_permanent_when_copy_missing_document_id(self):
        respx.post(_template_copy_url(_TEMPLATE_ID)).mock(
            return_value=httpx.Response(200, json={"foo": "bar"})
        )
        with pytest.raises(SignNowPermanentError, match="missing document id"):
            _adapter().send_for_signature(signer=_signer(), template_id=_TEMPLATE_ID)


# ---------------------------------------------------------------------------
# get_signing_status
# ---------------------------------------------------------------------------


class TestGetSigningStatus:
    @respx.mock
    def test_signed_status(self):
        respx.get(_document_url(_DOC_ID)).mock(
            return_value=httpx.Response(
                200,
                json={"field_invites": [{"role": "Borrower", "status": "fulfilled"}]},
            )
        )
        result = _adapter().get_signing_status(_DOC_ID)
        assert isinstance(result, SignNowStatusResult)
        assert result.status == "signed"
        assert result.signed is True
        assert result.declined is False

    @respx.mock
    def test_pending_status(self):
        respx.get(_document_url(_DOC_ID)).mock(
            return_value=httpx.Response(
                200,
                json={"field_invites": [{"role": "Borrower", "status": "pending"}]},
            )
        )
        result = _adapter().get_signing_status(_DOC_ID)
        assert result.status == "pending"
        assert result.signed is False

    @respx.mock
    def test_declined_status(self):
        respx.get(_document_url(_DOC_ID)).mock(
            return_value=httpx.Response(
                200,
                json={"field_invites": [{"role": "Borrower", "status": "declined"}]},
            )
        )
        result = _adapter().get_signing_status(_DOC_ID)
        assert result.status == "declined"
        assert result.declined is True

    @respx.mock
    def test_signer_states_drop_pii(self):
        respx.get(_document_url(_DOC_ID)).mock(
            return_value=httpx.Response(
                200,
                json={
                    "field_invites": [
                        {
                            "role": "Borrower",
                            "status": "fulfilled",
                            "email": "secret@example.com",
                        }
                    ]
                },
            )
        )
        result = _adapter().get_signing_status(_DOC_ID)
        assert result.signer_states == [{"role": "Borrower", "status": "signed"}]
        # PII must not survive into the normalized signer states.
        assert all("email" not in s for s in result.signer_states)


# ---------------------------------------------------------------------------
# error classification
# ---------------------------------------------------------------------------


class TestErrorClassification:
    @respx.mock
    def test_5xx_is_transient(self):
        respx.post(_invite_url(_DOC_ID)).mock(return_value=httpx.Response(503))
        with pytest.raises(SignNowTransientError, match="503"):
            _adapter().send_for_signature(signer=_signer(), document_id=_DOC_ID)

    @respx.mock
    def test_429_is_transient(self):
        respx.post(_invite_url(_DOC_ID)).mock(return_value=httpx.Response(429))
        with pytest.raises(SignNowTransientError, match="429"):
            _adapter().send_for_signature(signer=_signer(), document_id=_DOC_ID)

    @respx.mock
    def test_4xx_is_permanent(self):
        respx.post(_invite_url(_DOC_ID)).mock(
            return_value=httpx.Response(401, json={"error": "unauthorized"})
        )
        with pytest.raises(SignNowPermanentError, match="401"):
            _adapter().send_for_signature(signer=_signer(), document_id=_DOC_ID)

    @respx.mock
    def test_timeout_is_transient(self):
        respx.get(_document_url(_DOC_ID)).mock(
            side_effect=httpx.ConnectTimeout("timed out")
        )
        with pytest.raises(SignNowTransientError):
            _adapter().get_signing_status(_DOC_ID)

    @respx.mock
    def test_error_message_has_no_signer_pii(self):
        respx.post(_invite_url(_DOC_ID)).mock(return_value=httpx.Response(500))
        try:
            _adapter().send_for_signature(
                signer=SignerInput(email="leak@example.com", name="Leak Me"),
                document_id=_DOC_ID,
            )
        except SignNowTransientError as exc:
            assert "leak@example.com" not in str(exc)
            assert "Leak Me" not in str(exc)
        else:  # pragma: no cover
            pytest.fail("expected SignNowTransientError")


# ---------------------------------------------------------------------------
# webhook verification
# ---------------------------------------------------------------------------


def _sign(body: bytes, secret: str = _WEBHOOK_SECRET) -> str:
    return base64.b64encode(
        hmac.new(secret.encode(), body, hashlib.sha256).digest()
    ).decode("ascii")


class TestWebhookVerify:
    def test_verify_pass_returns_normalized_event(self):
        body = json.dumps(
            {"event": "document.complete", "document_id": _DOC_ID}
        ).encode()
        headers = {"X-SignNow-Signature": _sign(body)}
        event = _adapter().verify_webhook(raw_body=body, headers=headers)
        assert isinstance(event, SignNowWebhookEvent)
        assert event.event_type == "document.complete"
        assert event.document_id == _DOC_ID
        assert event.status == "signed"

    def test_verify_pass_case_insensitive_header(self):
        body = json.dumps({"event": "document.declined", "document_id": _DOC_ID}).encode()
        headers = {"x-signnow-signature": _sign(body)}
        event = _adapter().verify_webhook(raw_body=body, headers=headers)
        assert event.status == "declined"

    def test_verify_fail_on_bad_signature(self):
        body = json.dumps({"event": "document.complete", "document_id": _DOC_ID}).encode()
        headers = {"X-SignNow-Signature": _sign(body, secret="wrong-secret")}
        with pytest.raises(SignNowWebhookError, match="mismatch"):
            _adapter().verify_webhook(raw_body=body, headers=headers)

    def test_verify_fail_on_missing_header(self):
        body = b"{}"
        with pytest.raises(SignNowWebhookError, match="Missing"):
            _adapter().verify_webhook(raw_body=body, headers={})

    def test_verify_fail_without_secret_configured(self):
        adapter = SignNowAdapter(api_key=_TOKEN, base_url=_BASE_URL)
        body = b"{}"
        headers = {"X-SignNow-Signature": _sign(body)}
        with pytest.raises(SignNowWebhookError, match="No webhook_secret"):
            adapter.verify_webhook(raw_body=body, headers=headers)

    def test_verify_fail_on_non_json_body_after_valid_signature(self):
        body = b"not-json"
        headers = {"X-SignNow-Signature": _sign(body)}
        with pytest.raises(SignNowWebhookError, match="not valid JSON"):
            _adapter().verify_webhook(raw_body=body, headers=headers)
