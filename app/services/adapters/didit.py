"""Didit identity-verification adapter.

The existing ``DiditClient`` (app/services/kyc_vendor.py) is *session-oriented*:
``create_verification_session()`` returns a verification URL the patient completes
out of band, and the terminal pass/fail is delivered later via webhook. This
wrapper exposes that behind the engine's single-call ``verify_identity`` interface
(kickoff "Didit adapter integration", Step 2 — session-oriented branch).

Because the pure engine is synchronous with respect to the patient, it can only
*initiate* a session here, so a freshly created session maps to result="unknown"
(which the engine surfaces as manual_review). The real terminal result is recorded
by P6 orchestration via the webhook handler. We never modify ``kyc_vendor.py`` and
never mutate KYC tables (Hard Rule #12).
"""
from __future__ import annotations

from typing import Optional
from uuid import uuid4

from app.services.adapters.base import PatientProfile, VerificationAdapter, VerificationResult
from app.services.kyc_vendor import DiditClient


class DiditVerificationAdapter(VerificationAdapter):
    def __init__(self, client: Optional[DiditClient] = None, *, vendor: str = "didit") -> None:
        self._client = client
        self._vendor = vendor

    def _get_client(self) -> DiditClient:
        if self._client is None:
            self._client = DiditClient()
        return self._client

    async def verify_identity(self, patient: PatientProfile, method: str) -> VerificationResult:
        # The engine's adapter interface does not pass an application id; use a
        # fresh correlation id for the session. Wiring the real application id is
        # a P6 orchestration concern.
        correlation_id = uuid4()
        session = await self._get_client().create_verification_session(
            borrower_id=patient.patient_id,
            loan_application_id=correlation_id,
            external_id=correlation_id,
        )
        session_ref = str(getattr(session, "kyc_session_id", correlation_id))

        return VerificationResult(
            verification_type="identity",
            method=method,
            result="unknown",  # session initiated; terminal result arrives via webhook (P6)
            confidence=0.0,
            vendor=self._vendor,
            vendor_session_ref=session_ref,
        )
