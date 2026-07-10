"""Replay adapters for the P6 flow orchestrator.

``run_flow()`` (P4) was designed to *call adapters that dispatch live vendor
requests* and read back typed result objects. In the orchestrator's
webhook-driven model the vendor calls have already happened — the results live
in the ``verification_completed`` ``platform_events`` payloads. These adapters
satisfy the P4 adapter ABCs but simply return the previously-collected results,
so the orchestrator can hand them to ``run_flow()`` for a pure decision.

Construction: each adapter is built with ``stored_results`` — a dict mapping the
``platform_verification_type`` enum value to the stored result dict
``{"result": "passed"|"failed", **rich_payload}``. Missing a required key raises
``ReplayMissingResultError`` (should never happen: the orchestrator only runs the
flow once every required verification is terminal).

The bank rich-payload keys (``monthly_income_cents`` / ``avg_balance_cents``) are
mapped to the actual ``BankAccountSummary`` field names
(``monthly_income_after_tax_cents`` / ``balance_current_cents``).
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.services.adapters.base import (
    BankAccountSummary,
    BankAdapter,
    BureauAdapter,
    BureauResult,
    PatientProfile,
    VerificationAdapter,
    VerificationResult,
)


class ReplayMissingResultError(Exception):
    """Raised when a replay adapter is asked for a verification type with no stored result."""


def _require(stored_results: dict[str, dict], key: str) -> dict:
    data = stored_results.get(key)
    if data is None:
        raise ReplayMissingResultError(
            f"No stored verification result for '{key}' — cannot replay it into run_flow()."
        )
    return data


class ReplayVerificationAdapter(VerificationAdapter):
    def __init__(self, stored_results: dict[str, dict]) -> None:
        self._stored = stored_results

    async def verify_identity(self, patient: PatientProfile, method: str) -> VerificationResult:
        data = _require(self._stored, "kyc_id")
        return VerificationResult(
            verification_type="identity",
            method=data.get("method", method),
            result=data["result"],
            confidence=float(data.get("confidence", 0.0)),
            vendor=data.get("vendor", "mock"),
            vendor_session_ref=data.get("vendor_session_ref"),
        )


class ReplayBureauAdapter(BureauAdapter):
    def __init__(self, stored_results: dict[str, dict]) -> None:
        self._stored = stored_results

    def _build(self, key: str, pull_type: str) -> BureauResult:
        data = _require(self._stored, key)
        fraud_signals: Any = data.get("fraud_signals")
        if not isinstance(fraud_signals, dict):
            fraud_signals = {}
        # WS-E: replay the discharge date (ISO string in the stored JSON payload)
        # so the engine's bankruptcy discharge policy sees it. Absent/invalid →
        # None, which the engine treats as active/undischarged (unchanged).
        raw_discharged = data.get("bankruptcy_discharged_at")
        discharged_at = None
        if isinstance(raw_discharged, date):
            discharged_at = raw_discharged
        elif isinstance(raw_discharged, str):
            try:
                discharged_at = date.fromisoformat(raw_discharged)
            except ValueError:
                discharged_at = None
        return BureauResult(
            pull_type=pull_type,  # type: ignore[arg-type]
            score=int(data["credit_score"]),
            result=data["result"],
            bankruptcy=bool(data.get("bankruptcy", False)),
            bankruptcy_discharged_at=discharged_at,
            fraud_signals=fraud_signals,
            confidence=float(data.get("confidence", 1.0)),
            vendor=data.get("vendor", "mock"),
        )

    async def soft_pull(self, patient: PatientProfile) -> BureauResult:
        return self._build("bureau_soft", "soft")

    async def hard_pull(self, patient: PatientProfile) -> BureauResult:
        return self._build("bureau_hard", "hard")


class ReplayBankAdapter(BankAdapter):
    def __init__(self, stored_results: dict[str, dict]) -> None:
        self._stored = stored_results

    async def link_account(self, patient: PatientProfile) -> BankAccountSummary:
        data = _require(self._stored, "bank_link")
        return BankAccountSummary(
            result=data["result"],
            monthly_income_after_tax_cents=int(data.get("monthly_income_cents", 0)),
            nsf_count_90d=int(data.get("nsf_count_90d", 0)),
            account_age_months=int(data.get("account_age_months", 0)),
            balance_current_cents=int(data.get("avg_balance_cents", 0)),
            confidence=float(data.get("confidence", 1.0)),
            vendor=data.get("vendor", "mock"),
        )
