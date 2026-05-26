"""Dependencies for the vendor webhook API (P6.6)."""
from fastapi import Depends
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.services.webhooks.signature_verifier import SignatureVerifier

# Vendors with a configured webhook secret (path-param allowlist; unknown → 404).
SUPPORTED_VENDORS = ("didit", "flinks", "equifax")


def get_signature_verifier(db: Session = Depends(get_db)) -> SignatureVerifier:
    return SignatureVerifier(db)


def get_orchestrator(db: Session = Depends(get_db)):
    """FlowOrchestrator for the request (real consent service + mock dispatcher)."""
    import app.services.consent_service as consent_service
    from app.services.flow_orchestrator import FlowOrchestrator
    from app.services.verifications.mock_dispatcher import MockVerificationDispatcher

    return FlowOrchestrator(db, consent_service, MockVerificationDispatcher())
