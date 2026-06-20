"""Dev-only applicant helpers — mounted ONLY when ENVIRONMENT != 'production'
(see router.py). These exist so a local/staging demo of the patient flow can run
end-to-end without real vendors: in mock mode no SMS/email is sent and verification
results normally arrive via signed vendor webhooks, neither of which a browser demo
can drive. They replicate exactly what tests/test_applicant_journey.py does.

NEVER mounted in production. No auth (local/staging convenience only).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

import app.services.consent_service as consent_service
from app.db.base import get_db
from app.models.platform.verification import PlatformVerification
from app.services.flow_orchestrator import CONSENT_TO_VERIFICATION_TYPE, FlowOrchestrator
from app.services.mock_notification_dispatcher import peek_dev_code
from app.services.verifications.mock_dispatcher import MockVerificationDispatcher

router = APIRouter(prefix="/dev", tags=["applicant-dev"])

_PENDING_STATUSES = ("pending", "in_progress")
_BUREAU_TYPES = ("bureau_soft", "bureau_hard")


@router.get("/magic-link-code")
def magic_link_code(application_id: uuid.UUID):
    """Surface the plaintext magic-link code that mock mode only stored as a hash."""
    code = peek_dev_code(str(application_id))
    if code is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No magic-link code on record for this application (mock mode only).",
        )
    return {"application_id": str(application_id), "code": code}


@router.post("/applications/{application_id}/verifications/{purpose}/complete")
def complete_verification(
    application_id: uuid.UUID,
    purpose: str,
    score: int = Query(720, ge=300, le=900, description="Synthetic bureau score (720=approve)"),
    db: Session = Depends(get_db),
):
    """Simulate a 'passed' vendor result so the verification completes (mock mode)."""
    mapped = CONSENT_TO_VERIFICATION_TYPE.get(purpose)
    if mapped is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown verification purpose '{purpose}'. Expected one of "
            f"{list(CONSENT_TO_VERIFICATION_TYPE)}.",
        )

    verification = (
        db.query(PlatformVerification)
        .filter(
            PlatformVerification.application_id == application_id,
            PlatformVerification.verification_type == mapped,
            PlatformVerification.status.in_(_PENDING_STATUSES),
        )
        .order_by(PlatformVerification.started_at.desc())
        .first()
    )
    if verification is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"No pending '{mapped}' verification to complete for this application.",
        )

    override = {"credit_score": score} if mapped in _BUREAU_TYPES else None
    rich_payload = MockVerificationDispatcher().simulate_callback(mapped, "passed", override)[
        "rich_payload"
    ]

    orchestrator = FlowOrchestrator(db, consent_service, MockVerificationDispatcher())
    result = orchestrator.handle_verification_result(
        application_id,
        verification.id,
        vendor_event_id=f"dev-{purpose}-{uuid.uuid4().hex[:8]}",
        result="passed",
        rich_payload=rich_payload,
    )
    return {
        "verification_type": mapped,
        "result": "passed",
        "application_status": result.application_status,
        "decided": result.decided,
        "decision": result.decision,
    }
