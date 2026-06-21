"""Verification dispatcher — P7.2.

Selects between real and mock verification adapters based on the
``USE_REAL_ADAPTERS`` feature flag, and returns a uniform :class:`DispatchResult`
with the ``.vendor`` + ``.vendor_session_ref`` attributes that
``FlowOrchestrator.initiate_verification`` consumes. This means the existing
orchestrator works with the new dispatcher unchanged — Mock and Real paths look
identical from its perspective.

Bureau (Equifax) always uses the mock path until the subscriber agreement is in
place. Any verification type other than ``kyc_id`` / ``bank_link`` also goes
through the mock path even when ``USE_REAL_ADAPTERS=True``.

**Wiring note (UPDATED 2026-06):** the live ``get_orchestrator`` dependencies (both
``app/api/applicant/v1/deps.py`` and ``app/api/webhooks/v1/deps.py``) ALREADY
construct ``VerificationDispatcher``. So flipping ``USE_REAL_ADAPTERS=True`` (with
Didit/Flinks creds present) routes the live applicant flow to the real adapters
immediately — the flag is the only remaining gate for kyc_id/bank_link. Bureau
(``bureau_soft``/``bureau_hard``) still always uses the mock path regardless of the
flag (no real-bureau switch is wired yet — gated on the Equifax agreement).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from app.core.config import settings
from app.services.adapters.base import PatientProfile
from app.services.adapters.didit_verification import DiditVerificationAdapter
from app.services.adapters.flinks_bank import FlinksBankAdapter
from app.services.verifications.mock_dispatcher import MockVerificationDispatcher


@dataclass(frozen=True)
class DispatchResult:
    """Uniform shape consumed by ``FlowOrchestrator.initiate_verification``."""

    vendor: str
    vendor_session_ref: str
    redirect_url: Optional[str] = None
    cost_cents: Optional[int] = None


class VerificationDispatcher:
    def __init__(self) -> None:
        self._use_real = settings.USE_REAL_ADAPTERS
        # Mock is always constructed — it's the flag-off path AND the bureau path.
        self._mock = MockVerificationDispatcher()
        if self._use_real:
            self._didit = DiditVerificationAdapter(
                api_key=settings.DIDIT_API_KEY,
                api_base_url=settings.DIDIT_API_BASE_URL,
                workflow_id=settings.DIDIT_WORKFLOW_ID,
            )
            self._flinks = FlinksBankAdapter(
                api_key=settings.FLINKS_API_KEY,
                api_base_url=settings.FLINKS_API_BASE_URL,
                customer_id=settings.FLINKS_CUSTOMER_ID,
            )

    def initiate(
        self,
        verification_type: str,
        application_id: UUID,
        patient_id: UUID,
        payload: dict,
    ) -> DispatchResult:
        """Route to the real or mock adapter and return a uniform DispatchResult."""
        # Flag off, or any verification_type without a real adapter wired
        # (bureau_soft / bureau_hard always mock; any unknown type also mock).
        if not self._use_real or verification_type not in ("kyc_id", "bank_link"):
            m = self._mock.initiate(verification_type, application_id, patient_id, payload)
            return DispatchResult(vendor=m.vendor, vendor_session_ref=m.vendor_session_ref)

        # The orchestrator surfaces only patient_id (not email) to the dispatcher,
        # so the real Didit session won't prefill the patient's email in this
        # iteration. Acceptable — contact_details is an optional UX nicety.
        patient = PatientProfile(patient_id=patient_id)
        app_id_str = str(application_id)

        if verification_type == "kyc_id":
            r = self._didit.initiate(application_id=app_id_str, patient=patient)
            return DispatchResult(
                vendor="didit",
                vendor_session_ref=r.session_id,
                redirect_url=r.url,
                cost_cents=r.cost_cents,
            )

        # verification_type == "bank_link"
        r = self._flinks.initiate(application_id=app_id_str, patient=patient)
        return DispatchResult(
            vendor="flinks",
            vendor_session_ref=r.connect_url,
            redirect_url=r.connect_url,
            cost_cents=r.cost_cents,
        )
