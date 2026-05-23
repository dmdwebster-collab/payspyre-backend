"""Deterministic mock credit-bureau adapter.

Synthetic scores seeded from the patient email (same email -> same score), plus
SafeScan-style synthetic fraud signals (Hard Rule #10). Optional overrides drive
the engine's decision bands in tests without breaking determinism.
"""
from __future__ import annotations

from typing import Literal, Optional

from app.services.adapters._synthetic import scaled, seed_from_email
from app.services.adapters.base import (
    BureauAdapter,
    BureauResult,
    PatientProfile,
    VerificationOutcome,
)


class MockBureauAdapter(BureauAdapter):
    def __init__(
        self,
        *,
        forced_score: Optional[int] = None,
        forced_bankruptcy: Optional[bool] = None,
        forced_fraud_high_risk: Optional[bool] = None,
        forced_result: Optional[VerificationOutcome] = None,
        vendor: str = "mock_bureau",
    ) -> None:
        self._forced_score = forced_score
        self._forced_bankruptcy = forced_bankruptcy
        self._forced_fraud_high_risk = forced_fraud_high_risk
        self._forced_result = forced_result
        self._vendor = vendor

    def _build(self, patient: PatientProfile, pull_type: Literal["soft", "hard"]) -> BureauResult:
        seed = seed_from_email(patient.email)

        score = self._forced_score if self._forced_score is not None else scaled(seed, "bureau_score", 520, 800)

        safescan_score = scaled(seed, "safescan_score", 0, 999)
        if self._forced_fraud_high_risk is not None:
            identity_high_risk = self._forced_fraud_high_risk
        else:
            identity_high_risk = safescan_score >= 900

        if self._forced_bankruptcy is not None:
            bankruptcy = self._forced_bankruptcy
        else:
            bankruptcy = scaled(seed, "bankruptcy", 0, 99) >= 97

        fraud_signals: dict[str, object] = {
            "safescan_score": safescan_score,
            "identity_high_risk": identity_high_risk,
            "velocity_alert": scaled(seed, "velocity", 0, 99) >= 95,
            "address_mismatch": scaled(seed, "address_mismatch", 0, 99) >= 90,
        }

        result: VerificationOutcome = self._forced_result if self._forced_result is not None else "passed"

        return BureauResult(
            pull_type=pull_type,
            score=score,
            result=result,
            bankruptcy=bankruptcy,
            fraud_signals=fraud_signals,
            confidence=1.0,
            vendor=self._vendor,
        )

    async def soft_pull(self, patient: PatientProfile) -> BureauResult:
        return self._build(patient, "soft")

    async def hard_pull(self, patient: PatientProfile) -> BureauResult:
        return self._build(patient, "hard")
