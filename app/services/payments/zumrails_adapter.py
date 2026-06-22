"""Zumrails payments adapter — fund disbursement + payment collection.

Zumrails (https://www.zumrails.com) is the platform's payments / funding
provider. This adapter wraps its REST API so the servicing layer can:

* **disburse** funds to a recipient when a loan is funded
  (``create_disbursement``) — a "WithdrawFromAccount" / push transaction in
  Zumrails terms (money leaves the business's funding source → recipient);
* **collect** a payment from a payer (``create_collection``) — a
  "AddToAccount" / pull transaction (money is debited from the customer →
  business funding source);
* poll a transaction's lifecycle (``get_transaction_status``); and
* verify inbound webhook signatures (``verify_webhook``).

Style mirrors ``app/services/adapters/didit_verification.py``: a thin httpx
client, typed frozen-dataclass results, credentials injected via the
constructor, and **no internal retries** (the caller / servicing layer owns
idempotency — retrying a push here risks double-disbursing). Errors are
classified transient-vs-permanent the way
``app/services/real_notification_dispatcher.py`` does, so callers can decide
whether a retry is plausible.

================================================================================
ZUMRAILS API ASSUMPTIONS  (documented because we cannot hit the live API here —
every value below is centralized so it is trivial to correct against the real
Zumrails docs / a sandbox capture)
================================================================================

AUTH — Zumrails uses an OAuth-style token exchange:
``POST {base_url}/api/authorize`` with JSON ``{"username": <api_key>,
"password": <api_secret>}`` returns ``{"result": {"Token": "<jwt>"}}``. That
bearer token is then sent as ``Authorization: Bearer <jwt>`` on every
subsequent call. We map ``api_key`` → username and ``api_secret`` → password.
ASSUMPTION: token lifetime is long enough that we fetch one per adapter call
chain and do not refresh mid-call. If the real integration issues static API
keys instead, set ``auth_mode="static_bearer"`` and the api_key is sent
directly as the bearer token (no /authorize round-trip).

ENDPOINTS (paths are ``_*_PATH`` constants below — correct in one place):
* ``POST /api/authorize``         → token exchange
* ``POST /api/transaction``       → create a transaction (push or pull)
* ``GET  /api/transaction/{id}``  → fetch one transaction

REQUEST SHAPE for ``POST /api/transaction`` (assumed Zumrails field names):
``{"ZumRailsType": "WithdrawFromAccount" | "AddToAccount",
   "TransactionMethod": "Eft",
   "Amount": <decimal dollars>,
   "Currency": "CAD",
   "FundingSourceId": <our funding account id>,
   "UserId": <Zumrails user/recipient id>,
   "Memo": <free text>,
   "ClientTransactionId": <our idempotency ref / loan id>}``
ASSUMPTION: amounts are sent as decimal **dollars** (e.g. 100.50), NOT cents.
We accept ``amount_cents`` at the boundary (the codebase's money unit) and
convert. Flip ``AMOUNT_IN_CENTS = True`` if Zumrails actually wants minor units.

RESPONSE SHAPE: ``{"isError": false, "result": {"Id": "<uuid>",
"TransactionStatus": "Pending" | "InProgress" | "Completed" | "Failed" |
"Cancelled", ...}}``. We normalize ``TransactionStatus`` to our own
``TransactionStatus`` enum below.

WEBHOOK SIGNING: Zumrails' public docs do not pin an HMAC scheme, so this is a
**reasonable assumed scheme** — correct it against their real signing once
known. We assume the webhook secret (a shared signing key, configured
separately from api_key/api_secret) signs the raw request body:
``signature = hex( HMAC-SHA256(webhook_secret, raw_body) )`` delivered in the
``X-Zumrails-Signature`` header. Comparison is constant-time. If Zumrails
prefixes a timestamp or uses base64, adjust ``verify_webhook`` (single method).
================================================================================
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any, Literal, Optional

import httpx

from app.core import http_client
from app.core.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Tunable assumptions (centralized — see module docstring)
# ---------------------------------------------------------------------------

#: If True, send ``Amount`` as integer minor units (cents) instead of decimal
#: dollars. Default False per the assumed Zumrails contract.
AMOUNT_IN_CENTS = False

_AUTHORIZE_PATH = "/api/authorize"
_TRANSACTION_PATH = "/api/transaction"            # POST to create
_TRANSACTION_BY_ID_PATH = "/api/transaction/{id}"  # GET one

#: Zumrails transaction "type" for a push (disburse, money out to recipient).
_TYPE_DISBURSEMENT = "WithdrawFromAccount"
#: Zumrails transaction "type" for a pull (collect, money in from payer).
_TYPE_COLLECTION = "AddToAccount"

_SIGNATURE_HEADER = "X-Zumrails-Signature"

#: Standardized on the shared split connect/read timeout (app.core.http_client)
#: rather than the old ad-hoc single 15.0s float, so a slow read can't share the
#: short connect budget and neither hangs a worker indefinitely.
_DEFAULT_TIMEOUT = http_client.DEFAULT_TIMEOUT


# ---------------------------------------------------------------------------
# Error hierarchy (transient vs permanent — mirrors real_notification_dispatcher)
# ---------------------------------------------------------------------------


class ZumrailsError(Exception):
    """Base class for Zumrails adapter failures."""


class TransientZumrailsError(ZumrailsError):
    """Vendor 5xx / 429 / network / timeout — a caller-level retry is plausible."""


class PermanentZumrailsError(ZumrailsError):
    """Vendor 4xx / validation / rejected — retry will not help."""


# ---------------------------------------------------------------------------
# Typed results
# ---------------------------------------------------------------------------


class TransactionStatus(str, Enum):
    """Our normalized view of a Zumrails transaction lifecycle.

    ``UNKNOWN`` covers any vendor status string we don't recognize yet — the
    caller should treat it as non-terminal and keep polling rather than
    assuming success/failure.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        return self in (
            TransactionStatus.COMPLETED,
            TransactionStatus.FAILED,
            TransactionStatus.CANCELLED,
        )


# ASSUMPTION: Zumrails ``TransactionStatus`` string values → our enum.
_VENDOR_STATUS_MAP = {
    "pending": TransactionStatus.PENDING,
    "inprogress": TransactionStatus.IN_PROGRESS,
    "in_progress": TransactionStatus.IN_PROGRESS,
    "processing": TransactionStatus.IN_PROGRESS,
    "completed": TransactionStatus.COMPLETED,
    "complete": TransactionStatus.COMPLETED,
    "succeeded": TransactionStatus.COMPLETED,
    "failed": TransactionStatus.FAILED,
    "error": TransactionStatus.FAILED,
    "rejected": TransactionStatus.FAILED,
    "cancelled": TransactionStatus.CANCELLED,
    "canceled": TransactionStatus.CANCELLED,
}


def _normalize_status(raw: Any) -> TransactionStatus:
    if not isinstance(raw, str):
        return TransactionStatus.UNKNOWN
    return _VENDOR_STATUS_MAP.get(raw.strip().lower(), TransactionStatus.UNKNOWN)


@dataclass(frozen=True)
class TransactionResult:
    """Outcome of a create-transaction or status-poll call.

    ``transaction_id`` is Zumrails' own id (used as the servicing layer's
    vendor reference). ``raw_status`` keeps the original vendor string for
    audit; ``status`` is our normalized enum. No PII is carried here.
    """

    transaction_id: str
    status: TransactionStatus
    raw_status: str
    amount_cents: int
    currency: str
    direction: Literal["disbursement", "collection"]
    #: Our idempotency / correlation ref echoed back (loan id etc.), if any.
    client_transaction_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class ZumrailsAdapter:
    """Wraps the Zumrails REST API for disbursement + collection.

    Credentials are injected (never read from ``integration_settings`` here) so
    the adapter is unit-testable. ``webhook_secret`` is optional and only needed
    for ``verify_webhook``.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str,
        *,
        webhook_secret: Optional[str] = None,
        funding_source_id: Optional[str] = None,
        currency: str = "CAD",
        auth_mode: Literal["oauth", "static_bearer"] = "oauth",
        timeout: httpx.Timeout | float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._base_url = base_url.rstrip("/")
        self._webhook_secret = webhook_secret
        self._funding_source_id = funding_source_id
        self._currency = currency
        self._auth_mode = auth_mode
        self._timeout = timeout

    # -- public API ---------------------------------------------------------

    def create_disbursement(
        self,
        *,
        recipient_id: str,
        amount_cents: int,
        client_transaction_id: str,
        funding_source_id: Optional[str] = None,
        memo: Optional[str] = None,
    ) -> TransactionResult:
        """Push funds to ``recipient_id`` for a funded loan.

        ``recipient_id`` is the Zumrails user id of the payee (resolved upstream
        in the servicing layer — never a raw bank account number, so no PII
        reaches here). ``client_transaction_id`` is our idempotency / correlation
        reference (e.g. the loan disbursement id); Zumrails echoes it back.

        Single-shot — no internal retries. Raises ``TransientZumrailsError`` /
        ``PermanentZumrailsError`` on failure.
        """
        return self._create_transaction(
            direction="disbursement",
            zum_type=_TYPE_DISBURSEMENT,
            user_id=recipient_id,
            amount_cents=amount_cents,
            client_transaction_id=client_transaction_id,
            funding_source_id=funding_source_id,
            memo=memo,
        )

    def create_collection(
        self,
        *,
        payer_id: str,
        amount_cents: int,
        client_transaction_id: str,
        funding_source_id: Optional[str] = None,
        memo: Optional[str] = None,
    ) -> TransactionResult:
        """Pull a payment from ``payer_id`` (loan repayment / collection).

        Symmetric to ``create_disbursement`` but with the Zumrails "AddToAccount"
        type. Same idempotency + error-classification contract.
        """
        return self._create_transaction(
            direction="collection",
            zum_type=_TYPE_COLLECTION,
            user_id=payer_id,
            amount_cents=amount_cents,
            client_transaction_id=client_transaction_id,
            funding_source_id=funding_source_id,
            memo=memo,
        )

    def get_transaction_status(self, transaction_id: str) -> TransactionResult:
        """Fetch a single transaction and return its normalized status.

        Safe to call repeatedly (a plain GET); the servicing layer polls this
        until ``result.status.is_terminal``.
        """
        token = self._get_token()
        path = _TRANSACTION_BY_ID_PATH.format(id=transaction_id)
        url = f"{self._base_url}{path}"
        # Routed through the shared HTTP helper for consistent connect/read
        # timeouts + status/latency logging (no PII/keys logged).
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = http_client.request(
                    "GET",
                    url,
                    provider="zumrails",
                    op="get_transaction",
                    client=client,
                    headers=self._auth_headers(token),
                )
        except httpx.HTTPError as exc:
            raise TransientZumrailsError(
                f"Zumrails get-transaction network error: {type(exc).__name__}"
            ) from exc
        payload = self._parse_response(response, "get-transaction")
        return self._result_from_payload(
            payload,
            # direction/amount are echoed from the vendor payload; on a bare
            # status poll we don't know the original direction with certainty,
            # so trust the vendor type when present, else default disbursement.
            direction=self._direction_from_payload(payload),
            requested_amount_cents=None,
        )

    def verify_webhook(self, raw_body: bytes, signature_header: str) -> bool:
        """Verify a Zumrails webhook HMAC signature (assumed scheme).

        ASSUMED SCHEME (see module docstring): the configured ``webhook_secret``
        signs the raw request body with HMAC-SHA256, hex-encoded, delivered in
        ``X-Zumrails-Signature``. Returns True iff the signature matches
        (constant-time). Returns False on a missing/blank signature.

        Raises ``PermanentZumrailsError`` if no ``webhook_secret`` was
        configured — a config error, not a verification outcome.
        """
        if not self._webhook_secret:
            raise PermanentZumrailsError(
                "verify_webhook called but no webhook_secret configured"
            )
        if not signature_header:
            return False
        expected = hmac.new(
            self._webhook_secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        # Tolerate an optional "sha256=" prefix some providers prepend.
        provided = signature_header.strip()
        if "=" in provided and provided.lower().startswith("sha256="):
            provided = provided.split("=", 1)[1]
        return hmac.compare_digest(expected, provided)

    # -- internals ----------------------------------------------------------

    def _create_transaction(
        self,
        *,
        direction: Literal["disbursement", "collection"],
        zum_type: str,
        user_id: str,
        amount_cents: int,
        client_transaction_id: str,
        funding_source_id: Optional[str],
        memo: Optional[str],
    ) -> TransactionResult:
        if amount_cents <= 0:
            raise PermanentZumrailsError("amount_cents must be positive")
        source_id = funding_source_id or self._funding_source_id
        if not source_id:
            raise PermanentZumrailsError(
                "funding_source_id required (pass it or set it on the adapter)"
            )

        token = self._get_token()
        body: dict[str, Any] = {
            "ZumRailsType": zum_type,
            "TransactionMethod": "Eft",
            "Amount": self._format_amount(amount_cents),
            "Currency": self._currency,
            "FundingSourceId": source_id,
            "UserId": user_id,
            "ClientTransactionId": client_transaction_id,
        }
        if memo:
            body["Memo"] = memo

        url = f"{self._base_url}{_TRANSACTION_PATH}"
        # No retries: a retried push could double-disburse. The servicing layer
        # owns idempotency via ClientTransactionId. Routed through the shared HTTP
        # helper for consistent connect/read timeouts + status/latency logging.
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = http_client.request(
                    "POST",
                    url,
                    provider="zumrails",
                    op="create_transaction",
                    client=client,
                    headers=self._auth_headers(token),
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise TransientZumrailsError(
                f"Zumrails create-transaction network error: {type(exc).__name__}"
            ) from exc

        payload = self._parse_response(response, "create-transaction")
        # Log without PII: ids + status only, never recipient/account details.
        result = self._result_from_payload(
            payload,
            direction=direction,
            requested_amount_cents=amount_cents,
        )
        logger.info(
            "zumrails_transaction_created",
            direction=direction,
            transaction_id=result.transaction_id,
            status=result.status.value,
            client_transaction_id=client_transaction_id,
        )
        return result

    def _get_token(self) -> str:
        """Resolve the bearer token per ``auth_mode``.

        ``static_bearer`` returns ``api_key`` directly (no round-trip).
        ``oauth`` exchanges api_key/api_secret at ``/api/authorize``.
        """
        if self._auth_mode == "static_bearer":
            return self._api_key

        url = f"{self._base_url}{_AUTHORIZE_PATH}"
        # Routed through the shared HTTP helper for consistent connect/read
        # timeouts + status/latency logging (no PII/keys logged — the credential
        # body is never logged, only host+path/status/latency).
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = http_client.request(
                    "POST",
                    url,
                    provider="zumrails",
                    op="authorize",
                    client=client,
                    headers={"Content-Type": "application/json"},
                    json={"username": self._api_key, "password": self._api_secret},
                )
        except httpx.HTTPError as exc:
            raise TransientZumrailsError(
                f"Zumrails authorize network error: {type(exc).__name__}"
            ) from exc

        payload = self._parse_response(response, "authorize")
        # ASSUMPTION: token at result.Token (case-tolerant).
        result = payload.get("result") or payload.get("Result") or {}
        token = result.get("Token") or result.get("token") or payload.get("token")
        if not token:
            raise PermanentZumrailsError("Zumrails authorize response had no Token")
        return str(token)

    def _auth_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _format_amount(self, amount_cents: int) -> Any:
        if AMOUNT_IN_CENTS:
            return amount_cents
        # Decimal dollars, 2dp, no float artifacts.
        dollars = (Decimal(amount_cents) / Decimal(100)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        return float(dollars)

    def _parse_response(self, response: httpx.Response, op: str) -> dict[str, Any]:
        """Classify HTTP status → transient/permanent, then return parsed JSON.

        Zumrails also signals application errors with ``isError: true`` inside a
        2xx envelope; we surface those as permanent (validation/business
        rejection) unless an explicit retryable hint is present.
        """
        status = response.status_code
        if status in (429, 500, 502, 503, 504):
            raise TransientZumrailsError(
                f"Zumrails {op} HTTP {status}: {self._safe_snippet(response)}"
            )
        if 400 <= status < 500:
            raise PermanentZumrailsError(
                f"Zumrails {op} HTTP {status}: {self._safe_snippet(response)}"
            )
        if status // 100 != 2:
            # Any other non-2xx (1xx/3xx unexpected) — conservative transient.
            raise TransientZumrailsError(
                f"Zumrails {op} unexpected HTTP {status}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise PermanentZumrailsError(
                f"Zumrails {op} returned non-JSON body"
            ) from exc
        if not isinstance(payload, dict):
            raise PermanentZumrailsError(f"Zumrails {op} returned non-object JSON")

        if payload.get("isError") is True or payload.get("IsError") is True:
            # ASSUMPTION: application-level error in a 2xx envelope is a business
            # rejection (bad funding source, insufficient funds rule, etc.) —
            # permanent. message strings may contain free text but no PII fields.
            msg = payload.get("message") or payload.get("Message") or "unspecified"
            raise PermanentZumrailsError(f"Zumrails {op} application error: {msg}")
        return payload

    @staticmethod
    def _safe_snippet(response: httpx.Response) -> str:
        """First 200 chars of the error body — Zumrails error bodies carry
        status/validation text, not applicant PII, but we still truncate."""
        try:
            return response.text[:200]
        except Exception:  # pragma: no cover - defensive
            return "<unreadable body>"

    def _result_from_payload(
        self,
        payload: dict[str, Any],
        *,
        direction: Literal["disbursement", "collection"],
        requested_amount_cents: Optional[int],
    ) -> TransactionResult:
        result = payload.get("result") or payload.get("Result") or payload
        txn_id = (
            result.get("Id")
            or result.get("id")
            or result.get("TransactionId")
            or result.get("transactionId")
        )
        if not txn_id:
            raise PermanentZumrailsError("Zumrails response had no transaction Id")
        raw_status = (
            result.get("TransactionStatus")
            or result.get("transactionStatus")
            or result.get("Status")
            or result.get("status")
            or ""
        )
        amount_cents = requested_amount_cents
        if amount_cents is None:
            amount_cents = self._amount_to_cents(
                result.get("Amount") or result.get("amount")
            )
        currency = (
            result.get("Currency") or result.get("currency") or self._currency
        )
        client_txn_id = (
            result.get("ClientTransactionId") or result.get("clientTransactionId")
        )
        return TransactionResult(
            transaction_id=str(txn_id),
            status=_normalize_status(raw_status),
            raw_status=str(raw_status),
            amount_cents=int(amount_cents or 0),
            currency=str(currency),
            direction=direction,
            client_transaction_id=str(client_txn_id) if client_txn_id else None,
        )

    @staticmethod
    def _amount_to_cents(amount: Any) -> int:
        if amount is None:
            return 0
        if AMOUNT_IN_CENTS:
            try:
                return int(amount)
            except (TypeError, ValueError):
                return 0
        try:
            return int(
                (Decimal(str(amount)) * 100).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
        except (TypeError, ValueError, ArithmeticError):
            return 0

    @staticmethod
    def _direction_from_payload(
        payload: dict[str, Any],
    ) -> Literal["disbursement", "collection"]:
        result = payload.get("result") or payload.get("Result") or payload
        zum_type = str(
            result.get("ZumRailsType") or result.get("zumRailsType") or ""
        ).lower()
        if zum_type == _TYPE_COLLECTION.lower():
            return "collection"
        return "disbursement"
