"""Mock verification dispatcher (P6).

Stands in for the real vendor adapters (Didit / Flinks / Equifax — Phase B). It
fakes the *initiation* of a verification and provides a test helper that builds a
rich callback payload shaped like a real webhook delivery.

The rich payload carries the vendor-returned data (credit score, bankruptcy
flag, NSF count, confidence) that the orchestrator persists verbatim into the
``verification_completed`` event payload and that the replay adapters later
reconstruct into typed result objects for ``run_flow()`` (see
``replay_adapters.py``). ``platform_verifications`` itself stores only ``status``
— the rich data lives only in the WORM event log.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4


@dataclass(frozen=True)
class MockInitiationResult:
    vendor: str
    vendor_session_ref: str
    started_at: datetime


# Default rich payloads keyed by platform_verification_type enum value. Callers
# override individual fields via the ``rich_payload`` kwarg of simulate_callback.
# fraud_signals is a DICT (BureauResult.fraud_signals is dict[str, object]; the
# engine calls .get("identity_high_risk") on it).
_DEFAULT_RICH_PAYLOADS: dict[str, dict] = {
    "kyc_id": {"confidence": 0.95, "method": "id_doc_scan"},
    "bureau_soft": {
        "credit_score": 720,
        "bankruptcy": False,
        "fraud_signals": {},
        "inquiries_6mo": 1,
    },
    "bureau_hard": {
        "credit_score": 720,
        "bankruptcy": False,
        "fraud_signals": {},
        "inquiries_6mo": 1,
    },
    "bank_link": {
        "monthly_income_cents": 800000,
        "nsf_count_90d": 0,
        "avg_balance_cents": 250000,
        "account_age_months": 36,
    },
}


class MockVerificationDispatcher:
    """Fake verification initiation + a callback simulator for tests."""

    def initiate(
        self,
        verification_type: str,
        application_id: UUID,
        patient_id: UUID,
        payload: dict,
    ) -> MockInitiationResult:
        """Return a fake initiation. Vendor is always 'mock'; session ref is unique."""
        return MockInitiationResult(
            vendor="mock",
            vendor_session_ref=f"mock_{uuid4()}",
            started_at=datetime.now(timezone.utc),
        )

    def simulate_callback(
        self,
        verification_type: str,
        result: str = "passed",  # 'passed' | 'failed'
        rich_payload: dict | None = None,
    ) -> dict:
        """Build the webhook payload shape ``handle_verification_result`` ingests.

        Returns ``{vendor, verification_type, result, rich_payload}`` where
        ``rich_payload`` is the per-type default merged with any caller override.
        """
        base = dict(_DEFAULT_RICH_PAYLOADS.get(verification_type, {}))
        if rich_payload:
            base.update(rich_payload)
        return {
            "vendor": "mock",
            "verification_type": verification_type,
            "result": result,
            "rich_payload": base,
        }
