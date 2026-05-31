"""Inbound notification webhook receivers (P7.4b).

Two endpoints — one per vendor — both follow the per-vendor signature
+ translate + nonce + (optional) suppression-upsert pattern. The orchestrator
is NOT involved: these events are observability + suppression-list inputs,
not application-state writes.

Endpoint flow (shared shape):

1. Read body + headers (Twilio: form; Resend: raw JSON bytes).
2. Verify signature.
3. Parse → vendor body schema.
4. Translate → canonical status + optional suppression reason.
5. ``canonical == "ignored"`` → 202 ``"ignored"`` (no event, no suppression).
6. Nonce check (replay → 202 ``"replay"``).
7. Locate the original ``notification_sent`` event by
   (vendor, vendor_message_id). Missing → 202 ``"orphaned"`` + ``webhook_orphaned``
   event so ops can audit (vendor delivery retries are silently swallowed).
8. Emit ``notification_status_updated`` event linking to the original send
   and the magic-link event (both bigint event ids).
9. If the translator supplied a ``suppression_reason``, upsert into
   ``notification_suppression`` (idempotent via ``ON CONFLICT DO NOTHING``).
10. Commit + 202 ``"accepted"``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.webhooks.v1.deps import get_signature_verifier
from app.api.webhooks.v1.schemas import (
    ResendWebhookEnvelope,
    TwilioStatusCallbackBody,
)
from app.core.config import settings
from app.core.logging import get_logger
from app.db.base import get_db
from app.models.platform.event import PlatformEvent
from app.repositories.notification_suppression import NotificationSuppressionRepo
from app.services.webhooks.notification_translators import (
    TranslatedStatus,
    translate_resend_status,
    translate_twilio_status,
)
from app.services.webhooks.signature_verifier import (
    NonceReplayed,
    SignatureVerificationError,
    SignatureVerifier,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/notifications", tags=["vendor-webhooks"])


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _find_notification_sent_event(
    db: Session, vendor: str, vendor_message_id: str
) -> Optional[tuple[int, dict]]:
    """Locate the original ``notification_sent`` row by (vendor, vendor_message_id).

    Returns (event_id, payload) or None. The dispatcher writes vendor +
    vendor_message_id into the payload; we filter on the JSONB ``@>`` containment
    operator to use the GIN index.
    """
    row = db.execute(
        text(
            """
            SELECT id, payload FROM platform_events
            WHERE event_type = 'notification_sent'
              AND payload @> :key
            ORDER BY id DESC LIMIT 1
            """
        ),
        {"key": json.dumps({
            "vendor": vendor, "vendor_message_id": vendor_message_id,
        })},
    ).first()
    if row is None:
        return None
    return int(row[0]), dict(row[1])


def _emit_rejected(db: Session, vendor: str, reason: str) -> None:
    db.add(PlatformEvent(
        event_type="webhook_rejected",
        actor="vendor",
        payload={
            "v": 1,
            "actor": {"type": "vendor", "id": vendor},
            "vendor": vendor,
            "reason": reason,
        },
    ))
    db.commit()


def _emit_received(db: Session, vendor: str, vendor_event_id: str) -> None:
    """Anchor the nonce in the event log — ``SignatureVerifier._check_nonce``
    looks for this row to detect replays. Mirrors the verification endpoint
    pattern at ``app/api/webhooks/v1/endpoints/verification.py``.
    """
    db.add(PlatformEvent(
        event_type="webhook_received",
        actor="vendor",
        payload={
            "v": 1,
            "actor": {"type": "vendor", "id": vendor},
            "vendor": vendor,
            "vendor_event_id": vendor_event_id,
        },
    ))
    db.flush()  # the endpoint commits its full unit-of-work at the end


def _emit_orphaned(
    db: Session, *, vendor: str, vendor_message_id: str,
    vendor_event_id: str, received_at: str,
) -> None:
    db.add(PlatformEvent(
        event_type="webhook_orphaned",
        actor="vendor",
        payload={
            "v": 1,
            "actor": {"type": "vendor", "id": vendor},
            "vendor": vendor,
            "vendor_message_id": vendor_message_id,
            "vendor_event_id": vendor_event_id,
            "received_at": received_at,
        },
    ))
    db.commit()


def _emit_status_updated(
    db: Session,
    *,
    vendor: str,
    translated: TranslatedStatus,
    vendor_event_id: str,
    notification_sent_event_id: int,
    notification_sent_payload: dict,
    received_at: str,
) -> None:
    payload = {
        "v": 1,
        "actor": {"type": "vendor", "id": vendor},
        "vendor": vendor,
        "vendor_message_id": translated.vendor_message_id,
        "vendor_event_id": vendor_event_id,
        "status": translated.canonical,
        "vendor_raw_status": translated.vendor_raw_status,
        "vendor_error_code": translated.vendor_error_code,
        "notification_sent_event_id": notification_sent_event_id,
        "magic_link_event_id": notification_sent_payload.get("magic_link_event_id"),
        "received_at": received_at,
    }
    db.add(PlatformEvent(
        event_type="notification_status_updated",
        actor="vendor",
        # The original notification_sent event carries patient + app ids; copy
        # them onto the status row so ops queries by application_id work.
        patient_id=_uuid_or_none(notification_sent_payload.get("patient_id")),
        application_id=_uuid_or_none(notification_sent_payload.get("application_id")),
        payload=payload,
    ))


def _uuid_or_none(s: Any) -> Any:
    if not s:
        return None
    try:
        from uuid import UUID
        return UUID(str(s))
    except (ValueError, TypeError):
        return None


def _apply_suppression(
    db: Session,
    *,
    translated: TranslatedStatus,
    vendor: str,
    notification_sent_payload: dict,
    vendor_event_id: str,
) -> None:
    if not translated.suppression_reason:
        return
    recipient_redacted = notification_sent_payload.get("recipient_redacted")
    contact_method = notification_sent_payload.get("channel")
    if not recipient_redacted or not contact_method:
        # The original send didn't have the redaction field — unusual but
        # tolerable; skip suppression rather than fail the webhook.
        return
    NotificationSuppressionRepo(db).upsert(
        contact_method=contact_method,
        recipient_redacted=recipient_redacted,
        reason=translated.suppression_reason,
        vendor=vendor,
        vendor_event_id=vendor_event_id,
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_twilio_url(request: Request) -> str:
    """Reconstruct the public URL Twilio actually signed.

    Order of precedence:
    1. ``WEBHOOK_PUBLIC_BASE_URL`` config override — pin the scheme + host in prod.
    2. ``X-Forwarded-Proto`` header — upgrade scheme to https behind a reverse proxy.
    3. ``request.url`` as-is.
    """
    if settings.WEBHOOK_PUBLIC_BASE_URL:
        base = settings.WEBHOOK_PUBLIC_BASE_URL.rstrip("/")
        path = request.url.path
        query = f"?{request.url.query}" if request.url.query else ""
        return f"{base}{path}{query}"
    raw = str(request.url)
    forwarded_proto = (
        request.headers.get("x-forwarded-proto")
        or request.headers.get("X-Forwarded-Proto")
        or ""
    ).lower()
    if forwarded_proto == "https" and raw.startswith("http://"):
        raw = "https://" + raw[len("http://"):]
    return raw


# ---------------------------------------------------------------------------
# Twilio StatusCallback receiver
# ---------------------------------------------------------------------------


@router.post("/twilio", status_code=status.HTTP_202_ACCEPTED)
async def receive_twilio_status(
    request: Request,
    db: Session = Depends(get_db),
    verifier: SignatureVerifier = Depends(get_signature_verifier),
):
    form = await request.form()
    form_dict = {k: v for k, v in form.items()}

    # 1. Signature — Twilio signs over the form dict, not the raw body.
    url = _resolve_twilio_url(request)
    try:
        verifier.verify_twilio_form(
            url=url, form_params=form_dict, headers=dict(request.headers),
        )
    except SignatureVerificationError as exc:
        _emit_rejected(db, "twilio", str(exc))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Signature verification failed",
        )

    # 2. Parse.
    try:
        body = TwilioStatusCallbackBody(**form_dict)
    except (ValidationError, TypeError) as exc:
        _emit_rejected(db, "twilio", f"schema: {exc}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid payload: {exc}",
        )

    # 3. Translate.
    translated = translate_twilio_status(body)
    if translated.canonical == "ignored":
        return JSONResponse(status_code=202, content={"status": "ignored"})

    # 4. Nonce / replay.
    nonce = f"twilio:{body.MessageSid}:{body.MessageStatus}"
    try:
        verifier.check_nonce(nonce)
    except NonceReplayed:
        return JSONResponse(status_code=202, content={"status": "replay"})

    # 5. Anchor the nonce so a second delivery with the same SID + status no-ops.
    _emit_received(db, "twilio", nonce)

    # 6. Resolve original send.
    received_at = _now_iso()
    found = _find_notification_sent_event(db, "twilio", body.MessageSid)
    if found is None:
        _emit_orphaned(
            db, vendor="twilio", vendor_message_id=body.MessageSid,
            vendor_event_id=nonce, received_at=received_at,
        )
        return JSONResponse(status_code=202, content={"status": "orphaned"})
    sent_event_id, sent_payload = found

    # 7. Status update + suppression.
    _emit_status_updated(
        db, vendor="twilio", translated=translated, vendor_event_id=nonce,
        notification_sent_event_id=sent_event_id,
        notification_sent_payload=sent_payload, received_at=received_at,
    )
    _apply_suppression(
        db, translated=translated, vendor="twilio",
        notification_sent_payload=sent_payload, vendor_event_id=nonce,
    )
    db.commit()
    return JSONResponse(status_code=202, content={"status": "accepted"})


# ---------------------------------------------------------------------------
# Resend webhook receiver
# ---------------------------------------------------------------------------


@router.post("/resend", status_code=status.HTTP_202_ACCEPTED)
async def receive_resend_event(
    request: Request,
    db: Session = Depends(get_db),
    verifier: SignatureVerifier = Depends(get_signature_verifier),
):
    raw_body = await request.body()
    headers = dict(request.headers)

    # 1. Signature — raw body required for Svix HMAC-SHA256.
    try:
        verifier.verify_signature("resend", raw_body, headers)
    except SignatureVerificationError as exc:
        _emit_rejected(db, "resend", str(exc))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Signature verification failed",
        )

    # 2. Parse.
    try:
        envelope = ResendWebhookEnvelope(**json.loads(raw_body))
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        _emit_rejected(db, "resend", f"schema: {exc}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid payload: {exc}",
        )

    # 3. Translate.
    translated = translate_resend_status(envelope)
    if translated.canonical == "ignored":
        return JSONResponse(status_code=202, content={"status": "ignored"})

    if not translated.vendor_message_id:
        _emit_rejected(db, "resend", "missing data.email_id")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing data.email_id",
        )

    # 4. Nonce — svix-id is globally unique per delivery.
    svix_id = (
        headers.get("svix-id") or headers.get("Svix-Id") or ""
    )
    if not svix_id:
        _emit_rejected(db, "resend", "missing svix-id post-signature")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing svix-id",
        )
    try:
        verifier.check_nonce(svix_id)
    except NonceReplayed:
        return JSONResponse(status_code=202, content={"status": "replay"})

    # Anchor the nonce for replay protection.
    _emit_received(db, "resend", svix_id)

    received_at = _now_iso()
    found = _find_notification_sent_event(db, "resend", translated.vendor_message_id)
    if found is None:
        _emit_orphaned(
            db, vendor="resend", vendor_message_id=translated.vendor_message_id,
            vendor_event_id=svix_id, received_at=received_at,
        )
        return JSONResponse(status_code=202, content={"status": "orphaned"})
    sent_event_id, sent_payload = found

    _emit_status_updated(
        db, vendor="resend", translated=translated, vendor_event_id=svix_id,
        notification_sent_event_id=sent_event_id,
        notification_sent_payload=sent_payload, received_at=received_at,
    )
    _apply_suppression(
        db, translated=translated, vendor="resend",
        notification_sent_payload=sent_payload, vendor_event_id=svix_id,
    )
    db.commit()
    return JSONResponse(status_code=202, content={"status": "accepted"})
