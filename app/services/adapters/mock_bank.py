"""Deterministic mock bank-link adapter.

Synthetic income / NSF / account-age seeded from the patient email, shaped like a
real Flinks summary (Hard Rule #11). Same email -> same numbers. Optional
overrides drive the engine in tests without breaking determinism.
"""
from __future__ import annotations

from typing import Optional

from app.services.adapters._synthetic import scaled, seed_from_email
from app.services.adapters.base import (
    BankAccountSummary,
    BankAdapter,
    PatientProfile,
    VerificationOutcome,
)


class MockBankAdapter(BankAdapter):
    def __init__(
        self,
        *,
        forced_result: Optional[VerificationOutcome] = None,
        forced_income_cents: Optional[int] = None,
        forced_nsf_count: Optional[int] = None,
        vendor: str = "mock_bank",
    ) -> None:
        self._forced_result = forced_result
        self._forced_income_cents = forced_income_cents
        self._forced_nsf_count = forced_nsf_count
        self._vendor = vendor

    async def link_account(self, patient: PatientProfile) -> BankAccountSummary:
        seed = seed_from_email(patient.email)

        # Monthly after-tax income, $2,500-$12,000 (cents).
        income = (
            self._forced_income_cents
            if self._forced_income_cents is not None
            else scaled(seed, "income", 250_000, 1_200_000)
        )
        nsf = self._forced_nsf_count if self._forced_nsf_count is not None else scaled(seed, "nsf", 0, 6)
        result: VerificationOutcome = self._forced_result if self._forced_result is not None else "passed"

        return BankAccountSummary(
            result=result,
            monthly_income_after_tax_cents=income,
            nsf_count_90d=nsf,
            account_age_months=scaled(seed, "account_age", 2, 180),
            balance_current_cents=scaled(seed, "balance", 0, 5_000_000),
            confidence=1.0,
            vendor=self._vendor,
        )
