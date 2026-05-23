"""Deterministic mock identity-verification adapter.

Synthetic IDV results seeded from the patient email: the same email always
produces the same result. Optional explicit overrides exist purely for test
ergonomics (driving the engine's decision bands) and do not break determinism —
given the same constructor args and the same patient, the output is fixed.
"""
from __future__ import annotations

from typing import Optional

from app.services.adapters._synthetic import scaled, seed_from_email
from app.services.adapters.base import (
    PatientProfile,
    VerificationAdapter,
    VerificationOutcome,
    VerificationResult,
)


class MockVerificationAdapter(VerificationAdapter):
    def __init__(
        self,
        *,
        forced_result: Optional[VerificationOutcome] = None,
        forced_confidence: Optional[float] = None,
        vendor: str = "mock_idv",
    ) -> None:
        self._forced_result = forced_result
        self._forced_confidence = forced_confidence
        self._vendor = vendor

    async def verify_identity(self, patient: PatientProfile, method: str) -> VerificationResult:
        seed = seed_from_email(patient.email)
        # Default band 0.85-0.99 so the happy path passes the matrix min_confidence
        # (0.85) without forcing; tests force a low confidence to exercise failure.
        if self._forced_confidence is not None:
            confidence = self._forced_confidence
        else:
            confidence = scaled(seed, "idv_confidence", 85, 99) / 100.0

        result: VerificationOutcome = self._forced_result if self._forced_result is not None else "passed"

        return VerificationResult(
            verification_type="identity",
            method=method,
            result=result,
            confidence=round(confidence, 2),
            vendor=self._vendor,
            vendor_session_ref=f"mock-idv-{seed % 10**10:010d}",
        )
