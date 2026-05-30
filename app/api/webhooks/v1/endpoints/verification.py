"""Vendor verification webhook receiver (P6.6 + P7.2b).

Flow per vendor:
1. Read raw body + headers.
2. ``verifier.verify_signature(vendor, raw_body, headers)`` — vendor-specific
   sig + timestamp guards. Body is parsed only after this passes (an unsigned
   payload is never trusted).
3. Parse JSON with the vendor's schema (didit → ``DiditWebhookPayload``,
   flinks → ``FlinksWebhookPayload``, equifax → ``WebhookVerificationBody``
   MVP envelope).
4. Translate to the orchestrator handoff (didit / flinks via
   ``app.services.webhooks.translators``; equifax uses the body fields
   directly). ``TranslateResult.skip=True`` → 202 ``ignored`` (non-terminal
   status updates).
5. ``verifier.check_nonce(vendor_event_id)`` — replay check against
   ``platform_events`` using the vendor-derived nonce (Didit: ``event_id``;
   Flinks: ``flinks:{Login.Id}:{ResponseType}``; Equifax: ``X-Nonce``).
6. Resolve the ``PlatformVerification`` row from
   ``(application_id, verification_type)`` and (Flinks) upgrade
   ``vendor_session_ref`` from the placeholder Connect URL to ``Login.Id``.
7. Emit ``webhook_received``, then hand off to
   ``orchestrator.handle_verification_result``.

The router holds no application-state logic and never writes
``PlatformCreditApplication.status`` (the orchestrator does). The
``webhook_received`` / ``webhook_rejected`` events are webhook-infra records,
not application state (P6.6 kickoff §11).
"""
from __future__ import annotations

import json
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.webhooks.v1.deps import (
    SUPPORTED_VENDORS,
    get_orchestrator,
    get_signature_verifier,
)
from app.api.webhooks.v1.schemas import (
    DiditWebhookPayload,
    FlinksWebhookPayload,
    WebhookAcceptedResponse,
    WebhookVerificationBody,
)
from app.core.logging import get_logger
from app.db.base import get_db
from app.models.platform.event import PlatformEvent
from app.models.platform.verification import PlatformVerification
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
from app.services.webhooks.translators import (
    TranslateResult,
    TranslatorError,
    translate_didit_payload,
    translate_flinks_payload,
)

logger = get_logger(__name__)
router = APIRouter(tags=["vendor-webhooks"])


def _emit_received(
    db: Session,
    vendor: str,
    vendor_event_id: str,
    application_id: UUID,
    verification_type: str,
) -> None:
    db.add(
        PlatformEvent(
            event_type="webhook_received",
            actor="vendor",
            payload={
                "v": 1,
                "actor": {"type": "vendor", "id": vendor},
                "vendor": vendor,
                "vendor_event_id": vendor_event_id,  # replay-protection anchor
                "application_id": str(application_id),
                "verification_type": verification_type,
            },
        )
    )
    db.commit()


def _emit_rejected(db: Session, vendor: str, reason: str) -> None:
    db.add(
        PlatformEvent(
            event_type="webhook_rejected",
            actor="vendor",
            payload={
                "v": 1,
                "actor": {"type": "vendor", "id": vendor},
                "vendor": vendor,
                "reason": reason,
            },
        )
    )
    db.commit()


def _build_translate_result_from_mvp(body: WebhookVerificationBody, x_nonce: str) -> TranslateResult:
    """Adapt the equifax MVP envelope to the TranslateResult shape used downstream."""
    return TranslateResult(
        application_id=body.application_id,
        verification_type=body.verification_type,
        vendor_event_id=x_nonce,
        result=body.result,
        rich_payload=body.rich_payload,
        vendor_session_ref=None,
        skip=False,
    )


def _parse_and_translate(
    vendor: str, raw_body: bytes, headers: dict[str, str]
) -> TranslateResult:
    """Parse + translate per vendor. Raises HTTPException(400) on bad body."""
    try:
        parsed = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Malformed webhook body: {exc}",
        )

    try:
        if vendor == "didit":
            payload = DiditWebhookPayload(**parsed)
            return translate_didit_payload(payload)
        if vendor == "flinks":
            payload_f = FlinksWebhookPayload(**parsed)
            return translate_flinks_payload(payload_f)
        # equifax — MVP envelope.
        body = WebhookVerificationBody(**parsed)
        x_nonce = headers.get("x-nonce") or headers.get("X-Nonce") or ""
        if not x_nonce:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing X-Nonce header (equifax MVP envelope)",
            )
        return _build_translate_result_from_mvp(body, x_nonce)
    except (ValidationError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Malformed webhook body: {exc}",
        )
    except TranslatorError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )


def _resolve_verification_id(
    db: Session, application_id: UUID, verification_type: str
) -> Optional[UUID]:
    """Look up the (most recent) PlatformVerification row for this application + type.

    Used by Didit + Flinks paths — the equifax MVP envelope carries
    verification_id directly so this is bypassed there.
    """
    row = (
        db.query(PlatformVerification)
        .filter(
            PlatformVerification.application_id == application_id,
            PlatformVerification.verification_type == verification_type,
        )
        .order_by(PlatformVerification.started_at.desc())
        .first()
    )
    return row.id if row else None


def _upgrade_vendor_session_ref(
    db: Session, application_id: UUID, verification_type: str, new_ref: str
) -> None:
    """Replace the Flinks placeholder Connect URL with the real Login.Id.

    The Connect URL was stored at initiate (P7.2) because Flinks doesn't return
    a LoginId until post-Connect redirect. This is the one-time upgrade once
    the webhook arrives with the real Login.Id. Idempotent across replays
    (running it twice is a no-op).
    """
    row = (
        db.query(PlatformVerification)
        .filter(
            PlatformVerification.application_id == application_id,
            PlatformVerification.verification_type == verification_type,
        )
        .order_by(PlatformVerification.started_at.desc())
        .first()
    )
    if row is not None and row.vendor_session_ref != new_ref:
        row.vendor_session_ref = new_ref
        db.add(row)
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
    headers = {k: v for k, v in request.headers.items()}  # case-insensitive copy

    # 1. Signature + timestamp (no DB; never trust the body until this passes).
    try:
        verifier.verify_signature(vendor, raw_body, headers)
    except SignatureVerificationError as exc:
        _emit_rejected(db, vendor, reason=str(exc))
        logger.warning("webhook_rejected", vendor=vendor, reason=str(exc))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Webhook verification failed"
        )

    # 2. Parse + translate per vendor.
    translation = _parse_and_translate(vendor, raw_body, headers)

    # 3. Nonce / replay check (uses vendor-derived event id).
    assert translation.vendor_event_id is not None  # invariants enforced above
    try:
        verifier.check_nonce(translation.vendor_event_id)
    except NonceReplayed:
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED, content={"status": "already_processed"}
        )

    # 4. Non-terminal status (Didit "In Review" / "In Progress" / Flinks KYC etc.)
    #    — acknowledge + record the receipt for ops audit, but do NOT call the
    #    orchestrator (verification stays pending until the terminal payload).
    if translation.skip:
        _emit_received(
            db,
            vendor,
            translation.vendor_event_id,
            translation.application_id,
            translation.verification_type or "unknown",
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={"status": "ignored", "application_status": None, "decided": False},
        )

    assert translation.application_id is not None
    assert translation.verification_type is not None
    assert translation.result is not None
    assert translation.rich_payload is not None

    # 5. Resolve verification_id (equifax MVP envelope passes it directly).
    if vendor == "equifax":
        # Re-parse to fetch verification_id (only equifax carries it in the body).
        equifax_body = WebhookVerificationBody(**json.loads(raw_body))
        verification_id = equifax_body.verification_id
    else:
        verification_id = _resolve_verification_id(
            db, translation.application_id, translation.verification_type
        )
        if verification_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"No {translation.verification_type} verification found for "
                    f"application {translation.application_id}"
                ),
            )

    # 6. Flinks-only: upgrade the placeholder Connect URL to the real Login.Id.
    if vendor == "flinks" and translation.vendor_session_ref:
        _upgrade_vendor_session_ref(
            db,
            translation.application_id,
            translation.verification_type,
            translation.vendor_session_ref,
        )

    # 7. Record the accepted delivery (replay anchor), then hand off.
    _emit_received(
        db,
        vendor,
        translation.vendor_event_id,
        translation.application_id,
        translation.verification_type,
    )
    try:
        result = orchestrator.handle_verification_result(
            translation.application_id,
            verification_id,
            vendor_event_id=translation.vendor_event_id,
            result=translation.result,
            rich_payload=translation.rich_payload,
        )
    except ApplicationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except OrchestratorError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    return WebhookAcceptedResponse(
        status="accepted",
        application_status=result.application_status,
        decided=result.decided,
    )
