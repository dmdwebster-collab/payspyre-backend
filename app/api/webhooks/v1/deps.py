"""Dependencies for the vendor webhook API (P6.6)."""
from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.services.webhooks.signature_verifier import SignatureVerifier

# Vendors with a configured webhook secret (path-param allowlist; unknown → 404).
# P7.4b adds twilio + resend but they route through their own /notifications/{vendor}
# endpoints (different body shape + signature scheme), not the
# /{vendor}/verification path-param route.
SUPPORTED_VENDORS = ("didit", "flinks", "equifax")


def get_signature_verifier(db: Session = Depends(get_db)) -> SignatureVerifier:
    return SignatureVerifier(db)


def get_orchestrator(db: Session = Depends(get_db)):
    """FlowOrchestrator for the request.

    P7.2b: uses ``VerificationDispatcher`` instead of ``MockVerificationDispatcher``
    directly. The dispatcher itself reads ``settings.USE_REAL_ADAPTERS`` and
    picks real-vs-mock per verification type (bureau always mock — Equifax
    real path is gated on a separate subscriber agreement). When the flag is
    False (the default), mock behavior is preserved.
    """
    import app.services.consent_service as consent_service
    from app.services.flow_orchestrator import FlowOrchestrator
    from app.services.verifications.dispatcher import VerificationDispatcher

    return FlowOrchestrator(db, consent_service, VerificationDispatcher())
