"""Inbound payments webhook receiver — Zumrails (P8.x).

One endpoint — ``POST /api/webhooks/v1/zumrails/transaction`` — receives
Zumrails' transaction-lifecycle callback and advances the loan's disbursement
state. It mirrors the verification / e-sign webhook surface exactly:

1. Read the raw body + the ``X-Zumrails-Signature`` header (HMAC is over the raw
   bytes — never parse first).
2. Build a ``ZumrailsAdapter`` from the enabled ``zumrails`` integration_settings
   row (secrets carry the ``webhook_secret``). Missing / disabled / unconfigured
   provider → 401 (an unverifiable body is never trusted).
3. ``adapter.verify_webhook(raw_body, signature_header)`` → bool. False → emit
   ``webhook_rejected`` + 401.
4. Parse the (now-trusted) body to extract the Zumrails transaction id + status,
   normalized to ``TransactionStatus`` via the adapter's own vocabulary.
5. Idempotency / replay: ``verifier.check_nonce(nonce)`` claims the
   ``processed_webhooks`` PRIMARY KEY atomically (same mechanism as
   ``verification.py``). The nonce is ``"zumrails:<txn_id>:<status>"``; a
   re-delivery conflicts → 202 ``"replay"``.
6. Resolve the loan by ``disbursement_ref == txn_id``. Unknown → 202
   ``"orphaned"``.
7. Dispatch on the terminal status:
     * ``COMPLETED`` → ``loan_lifecycle.on_disbursement_complete``
     * ``FAILED``    → ``loan_lifecycle.on_disbursement_failed``
     * non-terminal (``PENDING`` / ``IN_PROGRESS`` / ``CANCELLED`` / ``UNKNOWN``)
       → 202 ``"ignored"`` (no lifecycle write).
8. Return 200 ``"accepted"``.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.webhooks.v1.deps import get_signature_verifier
from app.core.logging import get_logger
from app.db.base import get_db
from app.models.platform.event import PlatformEvent
from app.models.platform.loan import PlatformLoan
from app.services import integration_settings, loan_lifecycle, loan_payments
from app.services.payments.zumrails_adapter import (
    PermanentZumrailsError,
    TransactionStatus,
    ZumrailsAdapter,
    _normalize_status,
)
from app.services.webhooks.signature_verifier import (
    NonceReplayed,
    SignatureVerifier,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/zumrails", tags=["vendor-webhooks"])

_PROVIDER = "zumrails"
_SIGNATURE_HEADER = "X-Zumrails-Signature"


def _emit_rejected(db: Session, reason: str) -> None:
    """Webhook-infra audit record for a rejected delivery (mirrors verification.py)."""
    event = PlatformEvent(
        event_type="webhook_rejected",
        actor="vendor",
        payload={
            "v": 1,
            "actor": {"type": "vendor", "id": _PROVIDER},
            "vendor": _PROVIDER,
            "reason": reason,
        },
    )
    db.add(event)
    db.commit()
    from app.services.observability.posthog_bridge import capture_event
    capture_event(event)


def _build_adapter(db: Session) -> ZumrailsAdapter | None:
    """Construct a ``ZumrailsAdapter`` from the enabled ``zumrails`` settings row.

    Returns ``None`` if the provider is missing / disabled / has no
    webhook_secret — the caller turns that into a 401. Cred sourcing lives here;
    the adapter never reads integration_settings itself.
    """
    setting = integration_settings.get(db, _PROVIDER)
    if setting is None or not setting.enabled:
        return None
    config = setting.config or {}
    secrets = setting.secrets or {}
    webhook_secret = secrets.get("webhook_secret")
    if not webhook_secret:
        return None
    return ZumrailsAdapter(
        api_key=secrets.get("api_key") or "",
        api_secret=secrets.get("api_secret") or "",
        base_url=config.get("base_url") or "",
        webhook_secret=webhook_secret,
        funding_source_id=config.get("funding_source_id"),
        currency=config.get("currency", "CAD"),
        auth_mode=config.get("auth_mode", "oauth"),
    )


def _extract_txn(parsed: dict) -> tuple[str, TransactionStatus]:
    """Pull (transaction_id, normalized status) from a verified webhook body.

    Tolerates the same case-variant / nesting shapes the adapter's
    ``_result_from_payload`` handles (``result``/``Result`` envelope or a flat
    body). Returns ``("", UNKNOWN)`` when the id is absent so the caller can 400.
    """
    result = parsed.get("result") or parsed.get("Result") or parsed
    if not isinstance(result, dict):
        result = parsed
    txn_id = (
        result.get("Id")
        or result.get("id")
        or result.get("TransactionId")
        or result.get("transactionId")
        or ""
    )
    raw_status = (
        result.get("TransactionStatus")
        or result.get("transactionStatus")
        or result.get("Status")
        or result.get("status")
        or ""
    )
    return str(txn_id), _normalize_status(raw_status)


@router.post("/transaction", status_code=status.HTTP_200_OK)
async def receive_zumrails_transaction(
    request: Request,
    db: Session = Depends(get_db),
    verifier: SignatureVerifier = Depends(get_signature_verifier),
):
    raw_body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    signature_header = headers.get(_SIGNATURE_HEADER.lower(), "")

    # 1. Build the adapter (cred sourcing) — no adapter ⇒ cannot verify ⇒ 401.
    adapter = _build_adapter(db)
    if adapter is None:
        _emit_rejected(db, reason="zumrails provider disabled or unconfigured")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Webhook verification failed",
        )

    # 2. Verify signature (HMAC over raw body) BEFORE trusting/parsing it.
    try:
        ok = adapter.verify_webhook(raw_body, signature_header)
    except PermanentZumrailsError as exc:
        # No webhook_secret on the adapter — a config error, treat as rejection.
        _emit_rejected(db, reason=str(exc))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Webhook verification failed",
        )
    if not ok:
        _emit_rejected(db, reason="signature mismatch")
        logger.warning("webhook_rejected", vendor=_PROVIDER, reason="signature mismatch")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Webhook verification failed",
        )

    # 3. Parse the now-trusted body.
    try:
        parsed = json.loads(raw_body)
    except (ValueError, TypeError) as exc:
        _emit_rejected(db, reason=f"malformed body: {exc}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed webhook body",
        )
    if not isinstance(parsed, dict):
        _emit_rejected(db, reason="body is not a JSON object")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed webhook body",
        )

    txn_id, txn_status = _extract_txn(parsed)
    if not txn_id:
        _emit_rejected(db, reason="missing transaction id")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing transaction id",
        )

    # 4. Idempotency / replay — atomic claim on processed_webhooks.
    nonce = f"zumrails:{txn_id}:{txn_status.value}"
    try:
        verifier.check_nonce(nonce)
    except NonceReplayed:
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"status": "replay"},
        )

    # 5. Resolve the loan by disbursement_ref == Zumrails txn id.
    loan = (
        db.query(PlatformLoan)
        .filter(PlatformLoan.disbursement_ref == txn_id)
        .first()
    )
    if loan is None:
        # Not a disbursement — it may be a borrower payment (collection), which is
        # resolved from the loan_payment_initiated event log rather than a column.
        if txn_status == TransactionStatus.COMPLETED:
            if loan_payments.on_collection_complete(db, txn_id):
                logger.info("zumrails_collection_settled", transaction_id=txn_id)
                return {"status": "accepted"}
        elif txn_status == TransactionStatus.FAILED:
            if loan_payments.on_collection_failed(db, txn_id):
                logger.info("zumrails_collection_failed", transaction_id=txn_id)
                return {"status": "accepted"}
        else:
            db.commit()  # non-terminal collection update — ack, nonce persisted
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content={"status": "ignored"},
            )
        logger.info("zumrails_webhook_orphaned", transaction_id=txn_id, status=txn_status.value)
        db.commit()  # persist the nonce claim so a retry of the same unknown no-ops
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"status": "orphaned"},
        )

    # 6. Dispatch on the terminal status. Non-terminal → ack without a write.
    if txn_status == TransactionStatus.COMPLETED:
        loan_lifecycle.on_disbursement_complete(db, loan, ref=txn_id)
    elif txn_status == TransactionStatus.FAILED:
        loan_lifecycle.on_disbursement_failed(db, loan, ref=txn_id)
    else:
        db.commit()  # persist the nonce claim; loan untouched
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"status": "ignored"},
        )

    # loan_lifecycle commits its own unit-of-work (which carries the nonce claim).
    logger.info(
        "zumrails_webhook_processed",
        loan_id=str(loan.id),
        transaction_id=txn_id,
        status=txn_status.value,
    )
    return {"status": "accepted"}
