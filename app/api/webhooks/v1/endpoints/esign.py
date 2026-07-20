"""Inbound e-signature webhook receiver — SignNow (P8.x).

One endpoint — ``POST /api/webhooks/v1/signnow/agreement`` — receives SignNow's
document-completion callback and advances the loan lifecycle. It mirrors the
verification + notification webhook surface exactly:

1. Read the raw body + headers (HMAC is over the raw bytes — never parse first).
2. Build a ``SignNowAdapter`` from the enabled ``signnow`` integration_settings
   row (secrets carry the per-subscription ``webhook_secret``). A missing /
   disabled / unconfigured provider rejects (401) rather than trusting an
   unverifiable body.
3. ``adapter.verify_webhook(raw_body, headers)`` — HMAC-SHA256 of the raw body
   against the webhook secret. On failure → emit ``webhook_rejected`` + 401.
4. Idempotency / replay: ``verifier.check_nonce(nonce)`` claims the
   ``processed_webhooks`` PRIMARY KEY atomically (same mechanism as
   ``verification.py`` / ``notifications.py``). The nonce is
   ``"signnow:<document_id>:<status>"`` — each (document, terminal status) is
   delivered once; a re-delivery conflicts (rowcount 0) → 202 ``"replay"``.
5. Resolve the loan by ``agreement_ref == event.document_id``. Unknown ref →
   202 ``"orphaned"`` (SignNow keeps retrying otherwise; an unknown document is
   not an error we want to surface as a 5xx).
6. Dispatch on the normalized status:
     * ``signed``   → ``loan_lifecycle.on_agreement_signed``
     * ``declined`` → ``loan_lifecycle.on_agreement_declined``
     * anything else (``pending`` / ``unknown``) → 202 ``"ignored"`` (no
       lifecycle write — the loan stays at its current state until terminal).
7. Return 200 ``"accepted"``.

The endpoint holds no loan-state logic of its own — every transition is owned by
``loan_lifecycle`` (forward-only + idempotent there too, so a replay that slips
past the nonce is still safe).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.webhooks.v1.deps import get_signature_verifier
from app.core.logging import get_logger
from app.db.base import get_db
from app.models.platform.event import PlatformEvent
from app.models.platform.loan import PlatformLoan
from app.services import hardship as hardship_service
from app.services import integration_settings, loan_lifecycle
from app.services.esign.signnow_adapter import (
    SignNowAdapter,
    SignNowWebhookError,
)
from app.services.webhooks.signature_verifier import (
    NonceReplayed,
    SignatureVerifier,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/signnow", tags=["vendor-webhooks"])

_PROVIDER = "signnow"


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


def _build_adapter(db: Session) -> SignNowAdapter | None:
    """Construct a ``SignNowAdapter`` from the enabled ``signnow`` settings row.

    Returns ``None`` if the provider is missing / disabled / has no
    webhook_secret — the caller turns that into a 401 (an unverifiable body is
    never trusted). Credential sourcing lives here at the wiring layer; the
    adapter never reads integration_settings itself.
    """
    setting = integration_settings.get(db, _PROVIDER)
    if setting is None or not setting.enabled:
        return None
    config = setting.config or {}
    secrets = setting.secrets or {}
    webhook_secret = secrets.get("webhook_secret")
    if not webhook_secret:
        return None
    api_key = secrets.get("api_key") or secrets.get("access_token") or ""
    base_url = config.get("base_url") or ""
    return SignNowAdapter(
        api_key=api_key,
        base_url=base_url,
        webhook_secret=webhook_secret,
    )


@router.post("/agreement", status_code=status.HTTP_200_OK)
async def receive_signnow_agreement(
    request: Request,
    db: Session = Depends(get_db),
    verifier: SignatureVerifier = Depends(get_signature_verifier),
):
    raw_body = await request.body()
    headers = {k: v for k, v in request.headers.items()}

    # 1. Build the adapter (cred sourcing) — no adapter ⇒ cannot verify ⇒ 401.
    adapter = _build_adapter(db)
    if adapter is None:
        _emit_rejected(db, reason="signnow provider disabled or unconfigured")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Webhook verification failed",
        )

    # 2. Verify signature (HMAC over raw body) BEFORE trusting/parsing it.
    try:
        event = adapter.verify_webhook(raw_body, headers)
    except SignNowWebhookError as exc:
        _emit_rejected(db, reason=str(exc))
        logger.warning("webhook_rejected", vendor=_PROVIDER, reason=str(exc))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Webhook verification failed",
        )

    if not event.document_id:
        _emit_rejected(db, reason="missing document_id")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing document_id",
        )

    # 3. Idempotency / replay — atomic claim on processed_webhooks (same nonce
    #    mechanism as verification.py). Distinct status keeps signed vs declined
    #    deliveries for the same doc independent.
    nonce = f"signnow:{event.document_id}:{event.status}"
    try:
        verifier.check_nonce(nonce)
    except NonceReplayed:
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"status": "replay"},
        )

    # 4. Resolve the loan by agreement_ref == SignNow document id.
    loan = (
        db.query(PlatformLoan)
        .filter(PlatformLoan.agreement_ref == event.document_id)
        .first()
    )
    if loan is None:
        # 4b. Not a loan agreement — a hardship AMENDMENT document? (WS-J: the
        #     borrower-signed amendment is the legal gate before any schedule
        #     change; on "signed" the hardship service applies the change.)
        outcome = hardship_service.handle_esign_event(
            db, document_id=event.document_id, status=event.status
        )
        if outcome is not None:
            db.commit()  # hardship service adds+flushes; the webhook owns the commit
            logger.info(
                "signnow_webhook_hardship_processed",
                document_id=event.document_id,
                status=event.status,
                outcome=outcome,
            )
            return {"status": "accepted"}

        # Unknown document — ack so SignNow stops retrying; record for ops.
        logger.info(
            "signnow_webhook_orphaned",
            document_id=event.document_id,
            status=event.status,
        )
        db.commit()  # persist the nonce claim so a retry of the same unknown no-ops
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"status": "orphaned"},
        )

    # 5. Dispatch on terminal status. Non-terminal → ack without a lifecycle write.
    if event.status == "signed":
        loan_lifecycle.on_agreement_signed(db, loan)
    elif event.status == "declined":
        loan_lifecycle.on_agreement_declined(db, loan)
    else:
        db.commit()  # persist the nonce claim; loan untouched
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"status": "ignored"},
        )

    # loan_lifecycle commits its own unit-of-work (which carries the nonce claim).
    logger.info(
        "signnow_webhook_processed",
        loan_id=str(loan.id),
        document_id=event.document_id,
        status=event.status,
    )
    return {"status": "accepted"}
