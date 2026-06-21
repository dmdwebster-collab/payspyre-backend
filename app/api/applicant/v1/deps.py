"""Applicant API dependencies — patient JWT enforcement (P6.5).

``get_current_applicant`` validates the ``Authorization: Bearer <jwt>`` header
and returns the decoded claims. It does NOT check per-application scope — that is
an endpoint-level concern (``application_id in claims.app_ids`` → else 403).
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import get_db
from app.services.auth.patient_auth_service import JWT_ALGORITHM, PatientAuthService
from app.services.mock_notification_dispatcher import MockNotificationDispatcher


@dataclass
class ApplicantClaims:
    patient_id: UUID
    app_ids: list[UUID]


def get_current_applicant(authorization: str = Header(...)) -> ApplicantClaims:
    """Decode + validate the patient JWT. Raises 401 on any problem."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token"
        )
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(
            token, settings.PATIENT_JWT_SECRET, algorithms=[JWT_ALGORITHM]
        )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token"
        )
    try:
        patient_id = UUID(payload["sub"])
        app_ids = [UUID(a) for a in payload.get("app_ids", [])]
    except (KeyError, ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed token claims"
        )
    return ApplicantClaims(patient_id=patient_id, app_ids=app_ids)


def require_app_scope(application_id: UUID, claims: ApplicantClaims) -> None:
    """Endpoint-level authorization: 403 if the JWT doesn't cover this application."""
    if application_id not in claims.app_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This token is not authorized for the requested application",
        )


# --- injectable collaborators (overridable in tests) -----------------------


def get_notification_dispatcher(db: Session = Depends(get_db)):
    """Select between mock and real notification dispatcher per the flag.

    P7.4: flips to ``RealNotificationDispatcher`` when
    ``settings.USE_REAL_NOTIFICATIONS=True`` (prod startup validator in
    ``app/core/config.py`` fails loud if the flag is True without the Resend
    + Twilio credentials). Default False preserves the P6.5 mock behavior;
    tests that override the dep with their own mock are unaffected.

    Return type is duck-typed: both dispatchers expose
    ``send_magic_link(patient_id, application_id, contact_method, token,
    ttl_seconds=900) -> int``.
    """
    from app.core.config import settings

    if settings.USE_REAL_NOTIFICATIONS:
        from app.services.real_notification_dispatcher import RealNotificationDispatcher
        return RealNotificationDispatcher(db)
    return MockNotificationDispatcher(db)


def get_patient_auth_service(
    db: Session = Depends(get_db),
    dispatcher: MockNotificationDispatcher = Depends(get_notification_dispatcher),
) -> PatientAuthService:
    return PatientAuthService(db, dispatcher)


def get_orchestrator(db: Session = Depends(get_db)):
    """Construct a FlowOrchestrator for the request.

    P7.2b: uses ``VerificationDispatcher`` instead of ``MockVerificationDispatcher``
    directly. The dispatcher reads ``settings.USE_REAL_ADAPTERS`` and routes
    per verification type. Default (flag off) preserves mock behavior.
    """
    import app.services.consent_service as consent_service
    from app.services.flow_orchestrator import FlowOrchestrator
    from app.services.verifications.dispatcher import VerificationDispatcher

    return FlowOrchestrator(db, consent_service, VerificationDispatcher(db))
