"""Unit tests for ZumrailsAdapter — respx mocks httpx (no live HTTP), no DB.

Covers: create_disbursement success, 5xx→transient, 4xx→permanent, and webhook
verification pass/fail. The adapter's assumed Zumrails API shape (see its module
docstring) is encoded in the mock responses here — if the real API differs,
these mocks are the place to correct alongside the adapter constants.
"""
import hashlib
import hmac
import json

import httpx
import pytest
import respx

from app.services.payments.zumrails_adapter import (
    PermanentZumrailsError,
    TransactionResult,
    TransactionStatus,
    TransientZumrailsError,
    ZumrailsAdapter,
)

_API_KEY = "test-zum-key"
_API_SECRET = "test-zum-secret"
_BASE_URL = "https://api.zumrails.com"
_WEBHOOK_SECRET = "whsec-test-zumrails"
_FUNDING_SOURCE = "fund-src-0001"

_AUTHORIZE_URL = f"{_BASE_URL}/api/authorize"
_TRANSACTION_URL = f"{_BASE_URL}/api/transaction"


def _adapter() -> ZumrailsAdapter:
    return ZumrailsAdapter(
        api_key=_API_KEY,
        api_secret=_API_SECRET,
        base_url=_BASE_URL,
        webhook_secret=_WEBHOOK_SECRET,
        funding_source_id=_FUNDING_SOURCE,
    )


def _mock_authorize() -> None:
    respx.post(_AUTHORIZE_URL).mock(
        return_value=httpx.Response(200, json={"result": {"Token": "jwt-abc"}})
    )


def _txn_response(status_code: int = 200, **overrides) -> httpx.Response:
    result = {
        "Id": "txn-9999",
        "TransactionStatus": "Pending",
        "Currency": "CAD",
        "ClientTransactionId": "loan-42",
        **overrides,
    }
    return httpx.Response(status_code, json={"isError": False, "result": result})


class TestCreateDisbursementSuccess:
    @respx.mock
    def test_returns_typed_result(self):
        _mock_authorize()
        respx.post(_TRANSACTION_URL).mock(return_value=_txn_response())
        result = _adapter().create_disbursement(
            recipient_id="user-recipient-1",
            amount_cents=10050,
            client_transaction_id="loan-42",
        )
        assert isinstance(result, TransactionResult)
        assert result.transaction_id == "txn-9999"
        assert result.status is TransactionStatus.PENDING
        assert result.direction == "disbursement"
        assert result.amount_cents == 10050
        assert result.currency == "CAD"
        assert result.client_transaction_id == "loan-42"

    @respx.mock
    def test_authorizes_then_posts_transaction(self):
        auth = respx.post(_AUTHORIZE_URL).mock(
            return_value=httpx.Response(200, json={"result": {"Token": "jwt-abc"}})
        )
        txn = respx.post(_TRANSACTION_URL).mock(return_value=_txn_response())
        _adapter().create_disbursement(
            recipient_id="user-recipient-1",
            amount_cents=10050,
            client_transaction_id="loan-42",
        )
        assert auth.called
        assert txn.called
        # Bearer token from /authorize is attached to the transaction call.
        assert txn.calls.last.request.headers["Authorization"] == "Bearer jwt-abc"

    @respx.mock
    def test_request_body_shape(self):
        _mock_authorize()
        txn = respx.post(_TRANSACTION_URL).mock(return_value=_txn_response())
        _adapter().create_disbursement(
            recipient_id="user-recipient-1",
            amount_cents=10050,
            client_transaction_id="loan-42",
            memo="loan disbursement",
        )
        body = json.loads(txn.calls.last.request.content)
        assert body["ZumRailsType"] == "WithdrawFromAccount"
        assert body["UserId"] == "user-recipient-1"
        assert body["FundingSourceId"] == _FUNDING_SOURCE
        assert body["ClientTransactionId"] == "loan-42"
        assert body["Memo"] == "loan disbursement"
        # Default assumption: decimal dollars, not cents.
        assert body["Amount"] == 100.50

    @respx.mock
    def test_collection_uses_addtoaccount_type(self):
        _mock_authorize()
        txn = respx.post(_TRANSACTION_URL).mock(
            return_value=_txn_response(ZumRailsType="AddToAccount")
        )
        result = _adapter().create_collection(
            payer_id="user-payer-1",
            amount_cents=5000,
            client_transaction_id="repay-7",
        )
        body = json.loads(txn.calls.last.request.content)
        assert body["ZumRailsType"] == "AddToAccount"
        assert result.direction == "collection"


class TestErrorClassification:
    @respx.mock
    def test_5xx_is_transient(self):
        _mock_authorize()
        respx.post(_TRANSACTION_URL).mock(return_value=httpx.Response(503))
        with pytest.raises(TransientZumrailsError, match="503"):
            _adapter().create_disbursement(
                recipient_id="user-recipient-1",
                amount_cents=10050,
                client_transaction_id="loan-42",
            )

    @respx.mock
    def test_4xx_is_permanent(self):
        _mock_authorize()
        respx.post(_TRANSACTION_URL).mock(
            return_value=httpx.Response(400, json={"message": "bad funding source"})
        )
        with pytest.raises(PermanentZumrailsError, match="400"):
            _adapter().create_disbursement(
                recipient_id="user-recipient-1",
                amount_cents=10050,
                client_transaction_id="loan-42",
            )

    @respx.mock
    def test_application_error_in_2xx_is_permanent(self):
        _mock_authorize()
        respx.post(_TRANSACTION_URL).mock(
            return_value=httpx.Response(
                200, json={"isError": True, "message": "insufficient funds"}
            )
        )
        with pytest.raises(PermanentZumrailsError, match="insufficient funds"):
            _adapter().create_disbursement(
                recipient_id="user-recipient-1",
                amount_cents=10050,
                client_transaction_id="loan-42",
            )

    @respx.mock
    def test_network_error_is_transient(self):
        _mock_authorize()
        respx.post(_TRANSACTION_URL).mock(
            side_effect=httpx.ConnectError("boom")
        )
        with pytest.raises(TransientZumrailsError):
            _adapter().create_disbursement(
                recipient_id="user-recipient-1",
                amount_cents=10050,
                client_transaction_id="loan-42",
            )

    def test_zero_amount_rejected_before_http(self):
        # No respx routes registered → any HTTP call would error; this must
        # raise before touching the network.
        with pytest.raises(PermanentZumrailsError, match="positive"):
            _adapter().create_disbursement(
                recipient_id="user-recipient-1",
                amount_cents=0,
                client_transaction_id="loan-42",
            )


class TestGetTransactionStatus:
    @respx.mock
    def test_polls_and_normalizes_terminal_status(self):
        _mock_authorize()
        respx.get(f"{_BASE_URL}/api/transaction/txn-9999").mock(
            return_value=httpx.Response(
                200,
                json={"result": {"Id": "txn-9999", "TransactionStatus": "Completed"}},
            )
        )
        result = _adapter().get_transaction_status("txn-9999")
        assert result.status is TransactionStatus.COMPLETED
        assert result.status.is_terminal is True


class TestWebhookVerification:
    def test_verify_pass(self):
        adapter = _adapter()
        body = b'{"event":"transaction.completed","id":"txn-9999"}'
        sig = hmac.new(
            _WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        assert adapter.verify_webhook(body, sig) is True

    def test_verify_pass_with_sha256_prefix(self):
        adapter = _adapter()
        body = b'{"event":"transaction.completed"}'
        sig = hmac.new(
            _WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        assert adapter.verify_webhook(body, f"sha256={sig}") is True

    def test_verify_fail_bad_signature(self):
        adapter = _adapter()
        body = b'{"event":"transaction.completed"}'
        assert adapter.verify_webhook(body, "deadbeef") is False

    def test_verify_fail_tampered_body(self):
        adapter = _adapter()
        body = b'{"amount":100}'
        sig = hmac.new(
            _WEBHOOK_SECRET.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        assert adapter.verify_webhook(b'{"amount":999}', sig) is False

    def test_verify_blank_signature_is_false(self):
        assert _adapter().verify_webhook(b"{}", "") is False

    def test_verify_without_secret_raises(self):
        adapter = ZumrailsAdapter(
            api_key=_API_KEY,
            api_secret=_API_SECRET,
            base_url=_BASE_URL,
        )
        with pytest.raises(PermanentZumrailsError, match="webhook_secret"):
            adapter.verify_webhook(b"{}", "abc")
