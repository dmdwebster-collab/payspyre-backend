"""Real Didit identity-verification adapter — P7.2 (outbound / initiate only).

``initiate()`` makes a live HTTP POST to Didit's create-session endpoint
(``POST {api_base_url}/v3/session/``) to start a hosted KYC flow. The
verification *result* arrives asynchronously via the Didit webhook — receiving
and normalizing that payload is **P7.2b** and outside this PR's scope.

The ABC method ``verify_identity()`` raises ``NotImplementedError`` because the
real path is webhook-delivered; it is not synchronously called by ``run_flow``.
Replay adapters reconstruct ``VerificationResult`` from the stored
``verification_completed`` event payload at decision time.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.services.adapters.base import (
    PatientProfile,
    VerificationAdapter,
    VerificationResult,
)


class DiditAPIError(Exception):
    """Raised on a non-2xx response from Didit's create-session endpoint."""


@dataclass(frozen=True)
class DiditInitiationResult:
    session_id: str   # Didit's UUID — used as the orchestrator's vendor_session_ref
    url: str          # Hosted verification URL the patient is redirected to
    cost_cents: int = 0


class DiditVerificationAdapter(VerificationAdapter):
    """Wraps Didit's ``POST /v3/session/`` (x-api-key auth) to start a session."""

    _SESSION_PATH = "/v3/session/"

    def __init__(self, api_key: str, api_base_url: str, workflow_id: str) -> None:
        self._api_key = api_key
        self._api_base_url = api_base_url.rstrip("/")
        self._workflow_id = workflow_id

    def initiate(
        self,
        application_id: str,
        patient: PatientProfile,
        cost_cents: int = 0,
    ) -> DiditInitiationResult:
        """Create a hosted Didit session for ``application_id`` and return its id/URL."""
        body: dict[str, Any] = {
            "workflow_id": self._workflow_id,
            "vendor_data": application_id,                  # echoed back in the webhook
            "metadata": {"application_id": application_id}, # also echoed; redundant but explicit
        }
        if patient.email:
            # Optional UX prefill. Email never reaches our logs (Hard Rule #6 / no-PII-in-logs).
            body["contact_details"] = {"email": patient.email}

        # No retries inside the adapter — orchestrator-level idempotency handles
        # that; retrying here risks double-charging the vendor. Routed through the
        # shared HTTP helper for consistent connect/read timeouts + status/latency
        # logging (no PII/keys logged).
        from app.core import http_client

        response = http_client.request(
            "POST",
            f"{self._api_base_url}{self._SESSION_PATH}",
            provider="didit",
            op="create_session",
            headers={
                "x-api-key": self._api_key,
                "Content-Type": "application/json",
            },
            json=body,
        )
        if response.status_code // 100 != 2:
            raise DiditAPIError(
                f"Didit create-session failed: HTTP {response.status_code} {response.text[:200]}"
            )
        payload = response.json()
        return DiditInitiationResult(
            session_id=str(payload["session_id"]),
            url=str(payload["url"]),
            cost_cents=cost_cents,
        )

    async def verify_identity(self, patient: PatientProfile, method: str) -> VerificationResult:
        # The real path is webhook-delivered. The replay adapter reconstructs the
        # VerificationResult from the stored verification_completed event payload
        # at decision time. This stub satisfies the ABC contract.
        raise NotImplementedError(
            "DiditVerificationAdapter.verify_identity is not called in the real path; "
            "results arrive via the Didit webhook (P7.2b)."
        )
