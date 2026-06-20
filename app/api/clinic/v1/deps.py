"""Clinic API dependencies.

The clinic console authenticates with the existing platform staff JWT, so we
reuse ``app.core.auth.get_current_user`` directly. ``get_orchestrator`` is
re-exported (a thin construction helper mirroring the applicant deps) so the
financing-link endpoint can build pre-filled applications, and so tests can
override it with a mock.

FOLLOW-UP (clinic scoping): there is no clinic principal modeled today. When a
``clinic`` / ``practice`` table + a ``user -> clinic`` membership (and an
``application.clinic_id`` or ``vendor_id -> clinic`` link) exist, replace
``get_current_user`` here with a ``get_current_clinic_user`` that resolves the
caller's clinic and pass that scope down to the list/summary queries.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.auth import get_current_user  # noqa: F401  (re-exported for endpoints)
from app.db.base import get_db
from fastapi import Depends


def get_orchestrator(db: Session = Depends(get_db)):
    """Construct a FlowOrchestrator for the request (mirrors applicant deps)."""
    import app.services.consent_service as consent_service
    from app.services.flow_orchestrator import FlowOrchestrator
    from app.services.verifications.dispatcher import VerificationDispatcher

    return FlowOrchestrator(db, consent_service, VerificationDispatcher())
