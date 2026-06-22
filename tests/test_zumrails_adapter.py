"""Unit tests for ZumrailsAdapter — respx mocks httpx (no live HTTP), no DB.

Covers: create_disbursement success, 5xx→transient, 4xx→permanent, and webhook
verification pass/fail. The adapter's assumed Zumrails API shape (see its module
docstring) is encoded in the mock responses here — if the real API differs,
these mocks are the place to correct alongside the adapter constants.
"""
import hashlib
import hmac
import json
from unittest.mock import patch

import httpx
import pytest
import respx

from app.core import http_client
from app.services.payments import zumrails_adapter as zum_mod
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


class TestAmountConversion:
    """Cents → wire-amount conversion must be exact (no float drift).

    These are pure-conversion tests (no HTTP) exercising ``_format_amount`` and
    the inbound ``_amount_to_cents`` so the money-out path is provably lossless.
    """

    def test_cents_to_dollars_exact(self):
        # 339500 cents == $3395.00 — the headline round-trip from the task.
        assert _adapter()._format_amount(339500) == 3395.00

    @pytest.mark.parametrize(
        "cents,dollars",
        [
            (1, 0.01),
            (10, 0.10),
            (99, 0.99),
            (100, 1.00),
            (10050, 100.50),
            (339500, 3395.00),
            (339501, 3395.01),
            (100099, 1000.99),
            (2000005, 20000.05),
            (99999999999, 999999999.99),
        ],
    )
    def test_format_amount_matches_decimal_dollars(self, cents, dollars):
        assert _adapter()._format_amount(cents) == dollars

    def test_format_amount_emits_json_clean_number(self):
        # The float we hand httpx must json-serialize back to the same 2dp value
        # (no 3395.0100000000001 artifacts) for every realistic amount.
        from decimal import Decimal

        for cents in (1, 339500, 339501, 100099, 2000005, 99999999999):
            wire = _adapter()._format_amount(cents)
            assert Decimal(json.dumps(wire)) == Decimal(cents) / Decimal(100)

    def test_round_trips_cents_dollars_cents(self):
        adapter = _adapter()
        for cents in (1, 99, 339500, 339501, 100099, 2000005):
            wire = adapter._format_amount(cents)
            assert adapter._amount_to_cents(wire) == cents

    def test_amount_to_cents_from_string_and_number(self):
        adapter = _adapter()
        # Vendor may echo the amount as a JSON number or a string; both → cents.
        assert adapter._amount_to_cents("3395.00") == 339500
        assert adapter._amount_to_cents(3395.0) == 339500
        assert adapter._amount_to_cents("100.50") == 10050
        assert adapter._amount_to_cents(None) == 0

    def test_non_int_amount_rejected(self):
        # A float/str sneaking past the boundary is a programming error, caught
        # before any money leaves: must raise, never silently coerce.
        adapter = _adapter()
        with pytest.raises(PermanentZumrailsError, match="must be an int"):
            adapter._format_amount(3395.00)  # type: ignore[arg-type]
        with pytest.raises(PermanentZumrailsError, match="must be an int"):
            adapter._format_amount("339500")  # type: ignore[arg-type]
        # bool is an int subclass but never a valid amount.
        with pytest.raises(PermanentZumrailsError, match="must be an int"):
            adapter._format_amount(True)  # type: ignore[arg-type]

    def test_amount_too_large_to_represent_losslessly_raises(self):
        # Above ~2**53 cents the float can no longer hold 2dp exactly; on the
        # money-out path we fail loudly rather than push a drifted amount. This
        # is far beyond any real loan but proves the guard fires.
        adapter = _adapter()
        with pytest.raises(PermanentZumrailsError, match="losslessly"):
            adapter._format_amount(9007199254740993)


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


class TestOutboundTimeoutsStandardized:
    """The adapter standardizes on the shared split connect/read timeout and
    routes transport through ``app.core.http_client`` (timeout + safe logging)."""

    def test_default_timeout_is_shared_split_timeout(self):
        adapter = _adapter()
        # No ad-hoc single-float default — it's the shared split httpx.Timeout.
        assert adapter._timeout is http_client.DEFAULT_TIMEOUT
        assert isinstance(adapter._timeout, httpx.Timeout)
        assert adapter._timeout.connect == 5.0
        assert adapter._timeout.read == 15.0

    def test_module_default_is_the_shared_timeout(self):
        assert zum_mod._DEFAULT_TIMEOUT is http_client.DEFAULT_TIMEOUT

    @respx.mock
    def test_create_routes_through_shared_http_client(self):
        # Patch the shared helper and assert the adapter calls it (so timeout +
        # status/latency logging are applied centrally) with the right provider
        # and a client carrying the split timeout.
        _mock_authorize()
        respx.post(_TRANSACTION_URL).mock(return_value=_txn_response())
        with patch.object(
            zum_mod.http_client, "request", wraps=http_client.request
        ) as spy:
            _adapter().create_disbursement(
                recipient_id="user-recipient-1",
                amount_cents=10050,
                client_transaction_id="loan-42",
            )
        providers = {c.kwargs["provider"] for c in spy.call_args_list}
        assert providers == {"zumrails"}
        # The authorize + create_transaction calls both went through the helper.
        ops = {c.kwargs["op"] for c in spy.call_args_list}
        assert {"authorize", "create_transaction"} <= ops
        for call in spy.call_args_list:
            client = call.kwargs["client"]
            assert client.timeout.connect == 5.0
            assert client.timeout.read == 15.0
