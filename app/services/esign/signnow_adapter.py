"""SignNow e-signature adapter — outbound send + status + webhook verify.

PayPath uses SignNow (https://www.signnow.com — existing business account) as the
e-sign provider for loan agreements / loan documents. This adapter wraps the
three operations the servicing flow needs:

1. ``send_for_signature(...)`` — create a signing invite for a document or a
   reusable template (a loan-agreement template lives in the SignNow account)
   and return a signing URL + the SignNow document id.
2. ``get_signing_status(document_id)`` — poll the document's signing state
   (pending / signed / declined / ...) for reconciliation when a webhook is
   missed.
3. ``verify_webhook(...)`` — HMAC-verify SignNow's inbound completion callback
   so we can advance the loan to "executed" event-driven instead of polling.

Style mirrors ``app.services.adapters.didit_verification``: httpx.Client,
auth header, typed frozen dataclass results, no retries inside the adapter
(orchestrator-level idempotency owns retries to avoid duplicate invites), and
**no PII in logs / exception messages** (signer email + name never appear in
raised errors — only HTTP status + a truncated, non-PII response snippet).

Credentials are constructor-injected (NOT read from ``integration_settings``
here) so this class stays unit-testable and the wiring layer owns cred sourcing.

=============================================================================
SignNow API ASSUMPTIONS  (verify against real docs; designed to be easy to fix)
=============================================================================
Docs: https://docs.signnow.com/

AUTH MODEL — OAuth2 bearer token (NOT a static api key).
  SignNow's REST API authenticates with an OAuth2 access token sent as
  ``Authorization: Bearer <access_token>``. Access tokens are obtained from
  ``POST {base_url}/oauth2/token`` using a Basic-auth client credential
  (``Authorization: Basic <base64(client_id:client_secret)>``) plus a
  password or refresh-token grant. Token acquisition / refresh is an
  *orchestration concern* and is intentionally OUT of this adapter: the caller
  passes a ready ``access_token`` (see ``api_key`` param — named ``api_key`` to
  match the Didit adapter's param convention, but it carries the bearer token).
  This keeps the adapter side-effect free and lets the wiring layer cache /
  refresh tokens centrally. If your account uses a long-lived API key instead,
  this same param still works — only the header scheme below would change.

BASE URL
  Production: ``https://api.signnow.com``
  Sandbox:    ``https://api-eval.signnow.com``
  Injected via ``base_url`` so env selection lives in the wiring layer.

ENDPOINTS (assumed — confirm paths/field names against docs):
  - Send an existing document for signature (invite):
      ``POST {base_url}/document/{document_id}/invite``
  - Create a document from a template (returns a new document id), then invite:
      ``POST {base_url}/template/{template_id}/copy``  -> {"id": "<document_id>"}
  - Read document status (recipients + field/sign state):
      ``GET  {base_url}/document/{document_id}``
  The signing URL returned to the borrower is assumed to be an *embedded*
  signing link of the shape
      ``{base_url-less app host}/dispatch/...`` — in practice SignNow returns it
  in the invite response (``url`` / ``signing_link``). We surface whatever the
  invite response carries; if your account emails the invite instead of
  returning an embedded link, ``signing_url`` may be ``None`` and signers click
  through email — that is a valid, documented outcome here.

WEBHOOK SIGNING (assumed):
  SignNow event subscriptions POST a JSON body and sign it with HMAC-SHA256 of
  the **raw request body** using a per-subscription secret, delivered in the
  ``X-SignNow-Signature`` header, **base64-encoded** (matching the Flinks /
  Svix style already in this repo). We do a constant-time compare. There is no
  timestamp header assumed, so replay protection must live in the webhook
  endpoint's nonce check against ``platform_events`` (same pattern as
  ``signature_verifier.py``); this adapter only proves authenticity.
  ⚠ If real SignNow uses hex encoding or a different header name, change
  ``_WEBHOOK_SIGNATURE_HEADER`` and ``_WEBHOOK_ENCODING`` below — the two knobs
  are isolated for exactly this reason.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from app.core import http_client

logger = logging.getLogger(__name__)

# Knobs isolated so the documented webhook assumption is trivial to correct.
_WEBHOOK_SIGNATURE_HEADER = "X-SignNow-Signature"
_WEBHOOK_ENCODING = "base64"  # or "hex"


# ---------------------------------------------------------------------------
# Errors — transient (retryable) vs permanent (do-not-retry) classification
# ---------------------------------------------------------------------------


class SignNowAPIError(Exception):
    """Base class for SignNow REST failures (never contains signer PII)."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SignNowTransientError(SignNowAPIError):
    """Retryable failure — 5xx, 429, or a network/timeout error.

    Safe for the orchestrator to retry with backoff (the request likely did not
    take effect, or the vendor is briefly unavailable)."""


class SignNowPermanentError(SignNowAPIError):
    """Non-retryable failure — 4xx other than 429 (bad creds, bad template id,
    malformed request). Retrying will not help; surface for human/flow action."""


class SignNowWebhookError(Exception):
    """Raised when the inbound webhook signature cannot be verified."""


# ---------------------------------------------------------------------------
# Typed inputs / results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignerInput:
    """One recipient of a signature invite. ``email``/``name`` are PII — they
    are sent to SignNow but must never be logged or put into error messages."""

    email: str
    name: str
    role: str = "Signer"  # SignNow template role label, when sending from a template
    order: int = 1         # signing order for sequential signing


@dataclass(frozen=True)
class FieldValue:
    """A prefilled field to stamp onto the document before sending (e.g. loan
    amount, APR, term) keyed by the field/tab name configured in SignNow."""

    name: str
    value: str


@dataclass(frozen=True)
class SignNowSendResult:
    """Outcome of ``send_for_signature``."""

    document_id: str             # SignNow document id — our durable vendor ref
    signing_url: Optional[str]   # embedded signing link, if the account returns one
    invite_id: Optional[str] = None
    status: str = "sent"


@dataclass(frozen=True)
class SignNowStatusResult:
    """Normalized signing status for a document."""

    document_id: str
    status: str                       # normalized: pending|signed|declined|unknown
    raw_status: Optional[str] = None  # vendor's own string, for debugging
    signed: bool = False
    declined: bool = False
    signer_states: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class SignNowWebhookEvent:
    """Verified, normalized SignNow webhook payload (post-signature-check)."""

    event_type: str            # e.g. "document.complete"
    document_id: str
    status: str                # normalized status (see _normalize_status)
    raw: dict[str, Any] = field(default_factory=dict)


# Map SignNow's document/recipient states to our normalized vocabulary. Extend
# as real states are observed; unknown strings fall through to "unknown".
_STATUS_MAP = {
    "pending": "pending",
    "created": "pending",
    "sent": "pending",
    "waiting": "pending",
    "fulfilled": "signed",
    "signed": "signed",
    "completed": "signed",
    "complete": "signed",
    "declined": "declined",
    "cancelled": "declined",
    "canceled": "declined",
}


def _normalize_status(raw: Optional[str]) -> str:
    if not raw:
        return "unknown"
    return _STATUS_MAP.get(str(raw).strip().lower(), "unknown")


class SignNowAdapter:
    """Wraps SignNow's REST API (Bearer auth) for loan-agreement e-signature.

    Construct with an OAuth2 ``api_key`` (bearer access token; see module
    docstring AUTH MODEL) and a ``base_url``. Optional ``timeout`` overrides the
    per-request httpx timeout.
    """

    _INVITE_PATH = "/document/{document_id}/invite"
    _TEMPLATE_COPY_PATH = "/template/{template_id}/copy"
    _DOCUMENT_PATH = "/document/{document_id}"

    def __init__(
        self,
        api_key: str,
        base_url: str,
        webhook_secret: Optional[str] = None,
        # Defaults to the shared split connect/read timeout (app.core.http_client)
        # rather than a single 10.0s float, so a slow vendor read can't share the
        # short connect budget and neither hangs a worker indefinitely.
        timeout: httpx.Timeout | float = http_client.DEFAULT_TIMEOUT,
    ) -> None:
        self._access_token = api_key
        self._base_url = base_url.rstrip("/")
        self._webhook_secret = webhook_secret
        self._timeout = timeout

    # -- public API ---------------------------------------------------------

    def send_for_signature(
        self,
        signer: SignerInput,
        document_id: Optional[str] = None,
        template_id: Optional[str] = None,
        fields: Optional[list[FieldValue]] = None,
        subject: Optional[str] = None,
        message: Optional[str] = None,
    ) -> SignNowSendResult:
        """Send a loan agreement to ``signer`` for signature.

        Provide exactly one of:
          - ``document_id`` — an already-uploaded SignNow document, OR
          - ``template_id`` — a reusable loan-agreement template; a fresh
            document is created from it first (``/template/{id}/copy``).

        ``fields`` prefills named tabs (loan amount, APR, term, etc.) before the
        invite. Returns the SignNow ``document_id`` (durable vendor ref) and an
        embedded ``signing_url`` when the account returns one.
        """
        if bool(document_id) == bool(template_id):
            # Programmer error, not vendor PII — safe to raise verbatim.
            raise ValueError(
                "send_for_signature requires exactly one of document_id or template_id"
            )

        with httpx.Client(timeout=self._timeout) as client:
            if template_id:
                document_id = self._copy_template(client, template_id)
            assert document_id is not None  # narrowed by the xor check above

            if fields:
                self._prefill_fields(client, document_id, fields)

            invite_body: dict[str, Any] = {
                # Document-group / role invite shape. Field names are an
                # assumption (see module docstring) — adjust to match docs.
                "to": [
                    {
                        "email": signer.email,
                        "role": signer.role,
                        "order": signer.order,
                    }
                ],
            }
            if subject:
                invite_body["subject"] = subject
            if message:
                invite_body["message"] = message

            response = self._request(
                client,
                "POST",
                self._INVITE_PATH.format(document_id=document_id),
                json=invite_body,
            )

        payload = self._json(response)
        signing_url = (
            payload.get("url")
            or payload.get("signing_link")
            or payload.get("link")
        )
        invite_id = payload.get("id") or payload.get("invite_id")
        return SignNowSendResult(
            document_id=str(document_id),
            signing_url=str(signing_url) if signing_url else None,
            invite_id=str(invite_id) if invite_id else None,
            status="sent",
        )

    def get_signing_status(self, document_id: str) -> SignNowStatusResult:
        """Fetch the current signing status for ``document_id``.

        Used to reconcile when a completion webhook is missed. Reads
        ``GET /document/{id}`` and normalizes the recipient/field state into our
        pending|signed|declined|unknown vocabulary.
        """
        with httpx.Client(timeout=self._timeout) as client:
            response = self._request(
                client,
                "GET",
                self._DOCUMENT_PATH.format(document_id=document_id),
            )
        payload = self._json(response)
        raw_status = self._extract_document_status(payload)
        normalized = _normalize_status(raw_status)
        return SignNowStatusResult(
            document_id=str(document_id),
            status=normalized,
            raw_status=raw_status,
            signed=normalized == "signed",
            declined=normalized == "declined",
            signer_states=self._extract_signer_states(payload),
        )

    def verify_webhook(
        self,
        raw_body: bytes,
        headers: dict[str, str],
    ) -> SignNowWebhookEvent:
        """Verify + parse SignNow's inbound webhook (completion callback).

        HMAC-SHA256 of the raw body against the per-subscription
        ``webhook_secret`` (constructor-injected), compared constant-time
        against the ``X-SignNow-Signature`` header (base64 by assumption — see
        module docstring WEBHOOK SIGNING). Raises ``SignNowWebhookError`` on any
        failure. Replay/nonce protection is the *endpoint's* job (no timestamp
        header is assumed), mirroring ``signature_verifier.py``.
        """
        if not self._webhook_secret:
            raise SignNowWebhookError("No webhook_secret configured on adapter")

        provided = _header(headers, _WEBHOOK_SIGNATURE_HEADER)
        if not provided:
            raise SignNowWebhookError(f"Missing {_WEBHOOK_SIGNATURE_HEADER}")

        digest = hmac.new(
            self._webhook_secret.encode("utf-8"), raw_body, hashlib.sha256
        ).digest()
        if _WEBHOOK_ENCODING == "hex":
            expected = digest.hex()
        else:
            expected = base64.b64encode(digest).decode("ascii")

        if not hmac.compare_digest(expected, provided):
            raise SignNowWebhookError("Signature mismatch")

        import json

        try:
            parsed = json.loads(raw_body)
        except (ValueError, TypeError):
            raise SignNowWebhookError("Body is not valid JSON")

        event_type = str(parsed.get("event") or parsed.get("event_type") or "")
        document_id = str(
            parsed.get("document_id")
            or parsed.get("documentId")
            or (parsed.get("content") or {}).get("document_id")
            or ""
        )
        raw_status = (
            parsed.get("status")
            or event_type  # e.g. "document.complete" -> normalizes to signed
            or None
        )
        # Allow "document.complete"/"document.declined" event names to normalize.
        normalized = _normalize_status(
            raw_status.split(".")[-1] if isinstance(raw_status, str) else raw_status
        )
        return SignNowWebhookEvent(
            event_type=event_type,
            document_id=document_id,
            status=normalized,
            raw=parsed if isinstance(parsed, dict) else {},
        )

    # -- internal helpers ---------------------------------------------------

    def _copy_template(self, client: httpx.Client, template_id: str) -> str:
        """Create a document from a template; return the new document id."""
        response = self._request(
            client,
            "POST",
            self._TEMPLATE_COPY_PATH.format(template_id=template_id),
            json={},
        )
        payload = self._json(response)
        document_id = payload.get("id") or payload.get("document_id")
        if not document_id:
            # Vendor contract violation — no PII in this message.
            raise SignNowPermanentError(
                "Template copy response missing document id", status_code=response.status_code
            )
        return str(document_id)

    def _prefill_fields(
        self, client: httpx.Client, document_id: str, fields: list[FieldValue]
    ) -> None:
        """Stamp prefilled field values onto the document before inviting.

        Assumed endpoint: ``PUT /document/{id}`` with a ``fields`` array. Field
        names are an assumption — adjust the body shape to match real docs.
        """
        body = {"fields": [{"name": f.name, "prefilled_text": f.value} for f in fields]}
        self._request(
            client,
            "PUT",
            self._DOCUMENT_PATH.format(document_id=document_id),
            json=body,
        )

    def _request(
        self,
        client: httpx.Client,
        method: str,
        path: str,
        json: Optional[dict[str, Any]] = None,
    ) -> httpx.Response:
        """Issue a request with Bearer auth and classify failures.

        No PII ever enters the raised message — only HTTP status and a short,
        non-PII slice of the response text. Routed through the shared HTTP helper
        for consistent connect/read timeouts + status/latency logging (no
        PII/keys logged).
        """
        url = f"{self._base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            response = http_client.request(
                method,
                url,
                provider="signnow",
                op="request",
                client=client,
                headers=headers,
                json=json,
            )
        except httpx.TimeoutException as exc:
            raise SignNowTransientError(f"SignNow request timed out: {method} {path}") from exc
        except httpx.HTTPError as exc:
            # Network-level error (DNS, connection reset, ...) — retryable.
            raise SignNowTransientError(
                f"SignNow request failed at transport: {type(exc).__name__}"
            ) from exc

        self._raise_for_status(response)
        return response

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        if status // 100 == 2:
            return
        # Truncate response text — SignNow error bodies are vendor-side and
        # generally PII-free, but cap length so nothing large leaks to logs.
        snippet = (response.text or "")[:200]
        if status == 429 or status // 100 == 5:
            raise SignNowTransientError(
                f"SignNow transient error: HTTP {status} {snippet}", status_code=status
            )
        raise SignNowPermanentError(
            f"SignNow permanent error: HTTP {status} {snippet}", status_code=status
        )

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _extract_document_status(payload: dict[str, Any]) -> Optional[str]:
        """Best-effort status extraction from ``GET /document``.

        SignNow exposes signing state across ``field_invites`` /
        ``requests``/``status``; we check a few likely shapes. Returns the raw
        vendor string (or None) — normalization happens in the caller.
        """
        if payload.get("status"):
            return str(payload["status"])

        invites = payload.get("field_invites") or payload.get("requests") or []
        if isinstance(invites, list) and invites:
            statuses = {
                str(i.get("status", "")).lower() for i in invites if isinstance(i, dict)
            }
            if statuses and statuses <= {"fulfilled", "signed", "completed"}:
                return "fulfilled"
            if "declined" in statuses or "cancelled" in statuses:
                return "declined"
            return "pending"
        return None

    @staticmethod
    def _extract_signer_states(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Return per-signer state WITHOUT email/name (no PII).

        Only the role + status are kept; recipient email/name are dropped so the
        result can be logged safely.
        """
        invites = payload.get("field_invites") or payload.get("requests") or []
        out: list[dict[str, Any]] = []
        if isinstance(invites, list):
            for i in invites:
                if isinstance(i, dict):
                    out.append(
                        {
                            "role": i.get("role"),
                            "status": _normalize_status(i.get("status")),
                        }
                    )
        return out


def _header(headers: dict[str, str], name: str) -> str:
    """Case-insensitive header lookup (mirrors signature_verifier._header)."""
    if name in headers:
        return headers[name]
    lowered = name.lower()
    if lowered in headers:
        return headers[lowered]
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return ""
