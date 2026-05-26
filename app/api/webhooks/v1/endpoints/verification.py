"""Vendor verification webhook receiver (P6.6).

Flow (see kickoff §3 #8): read raw body → HMAC verify (signature + timestamp +
nonce) → on replay 202, on rejection emit ``webhook_rejected`` + 401 → parse JSON
(400 if malformed) → emit ``webhook_received`` → call
``orchestrator.handle_verification_result``. The router holds no application-state
logic and never writes ``PlatformCreditApplication.status`` (the orchestrator does).
The ``webhook_received`` / ``webhook_rejected`` events are webhook-infra records,
not application state (kickoff §11).
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.webhooks.v1.deps import (
    SUPPORTED_VENDORS,
    get_orchestrator,
    get_signature_verifier,
)
from app.api.webhooks.v1.schemas import WebhookAcceptedResponse, WebhookVerificationBody
from app.core.logging import get_logger
from app.db.base import get_db
from app.models.platform.event import PlatformEvent
from app.services.flow_orchestrator import (
    ApplicationNotFoundError,
    FlowOrchestrator,
    OrchestratorError,
)
from app.services.webhooks.signature_verifier import (
    NonceReplayed,
    SignatureVerificationError,
    SignatureVerifier,
)

logger = get_logger(__name__)
router = APIRouter(tags=["vendor-webhooks"])


def _emit_received(db: Session, vendor: str, nonce: str, timestamp: str, body: WebhookVerificationBody) -> None:
    db.add(
        PlatformEvent(
            event_type="webhook_received",
            actor="vendor",
            payload={
                "v": 1,
                "actor": {"type": "vendor", "id": vendor},
                "vendor": vendor,
                "nonce": nonce,
                "vendor_event_id": nonce,  # replay-protection anchor
                "timestamp": timestamp,
                "application_id": str(body.application_id),
                "verification_type": body.verification_type,
            },
        )
    )
    db.commit()


def _emit_rejected(db: Session, vendor: str, nonce: str, timestamp: str, reason: str) -> None:
    db.add(
        PlatformEvent(
            event_type="webhook_rejected",
            actor="vendor",
            payload={
                "v": 1,
                "actor": {"type": "vendor", "id": vendor},
                "vendor": vendor,
                "nonce": nonce,
                "timestamp": timestamp,
                "reason": reason,
            },
        )
    )
    db.commit()


@router.post("/{vendor}/verification", response_model=WebhookAcceptedResponse)
async def receive_verification(
    vendor: str,
    request: Request,
    db: Session = Depends(get_db),
    verifier: SignatureVerifier = Depends(get_signature_verifier),
    orchestrator: FlowOrchestrator = Depends(get_orchestrator),
):
    if vendor not in SUPPORTED_VENDORS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown vendor '{vendor}'")

    raw_body = await request.body()
    x_signature = request.headers.get("X-Signature", "")
    x_timestamp = request.headers.get("X-Timestamp", "")
    x_nonce = request.headers.get("X-Nonce", "")

    # 1. HMAC + timestamp + nonce.
    try:
        verifier.verify(vendor, raw_body, x_signature, x_timestamp, x_nonce)
    except NonceReplayed:
        # Valid signature, already-seen nonce → idempotent replay (vendor retry).
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content={"status": "already_processed"})
    except SignatureVerificationError as exc:
        _emit_rejected(db, vendor, x_nonce, x_timestamp, reason=str(exc))
        logger.warning("webhook_rejected", vendor=vendor, reason=str(exc))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Webhook verification failed"
        )

    # 2. Parse body only after the signature is trusted.
    try:
        body = WebhookVerificationBody(**json.loads(raw_body))
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Malformed webhook body: {exc}"
        )

    # 3. Record the accepted delivery (replay anchor), then hand off to the orchestrator.
    _emit_received(db, vendor, x_nonce, x_timestamp, body)
    try:
        result = orchestrator.handle_verification_result(
            body.application_id,
            body.verification_id,
            vendor_event_id=x_nonce,
            result=body.result,
            rich_payload=body.rich_payload,
        )
    except ApplicationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except OrchestratorError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    return WebhookAcceptedResponse(
        status="accepted", application_status=result.application_status, decided=result.decided
    )
