"""Adapter interfaces and value objects for the P4 flow engine.

These are the entire I/O abstraction for the pure flow engine (PR P4). The engine
calls adapters to perform verifications and reads back immutable result value
objects; the engine itself performs no DB writes and no network calls (Hard Rule
#2). Real vendor implementations (Equifax, Flinks) are deferred; this PR ships the
abstract interfaces, deterministic mocks, and a Didit wrapper only.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Optional
from uuid import UUID

# Terminal outcome of a single verification. "unknown" is reserved for the
# timeout/indeterminate case and is treated by the engine as manual_review,
# never as a failure (per kickoff "adapter timeout policy"). "manual_review"
# is a vendor-asserted human-review signal (P7.5/P7.6) — distinct from
# "unknown" (timeout/indeterminate) — and is currently produced by the
# Didit "In Review" path through the replay verification adapter; bureau
# and bank adapters do not construct results with this value in practice.
VerificationOutcome = Literal["passed", "failed", "unknown", "manual_review"]


@dataclass(frozen=True)
class PatientProfile:
    """PII-light view of a patient that the engine and adapters consume.

    The raw PlatformPatient row has no `province` column (province lives in
    platform_patient_fields), so the pure engine consumes this value object
    rather than the ORM row. P6 orchestration assembles it from the patient row
    plus current source-tagged fields. `email` is only a deterministic seed for
    the mock adapters and is NEVER written into events (Hard Rule #6).
    """

    patient_id: UUID
    province: Optional[str] = None
    email: Optional[str] = None
    legal_first_name: Optional[str] = None
    legal_last_name: Optional[str] = None
    dob: Optional[date] = None


@dataclass(frozen=True)
class VerificationResult:
    """Result of an identity (KYC) verification."""

    verification_type: str  # e.g. "identity"
    method: str  # e.g. "id_doc_scan", "email_otp"
    result: VerificationOutcome
    confidence: float  # 0.0 - 1.0
    vendor: Optional[str] = None
    vendor_session_ref: Optional[str] = None


@dataclass(frozen=True)
class BureauResult:
    """Result of a credit-bureau pull (soft or hard)."""

    pull_type: Literal["soft", "hard"]
    score: int
    result: VerificationOutcome
    bankruptcy: bool = False
    # Discharge date when the reported bankruptcy is DISCHARGED (WS-E bankruptcy
    # policy). None = active/undischarged OR the bureau did not report a discharge
    # — both are treated as the (unchanged) hard-decline case by the engine, so
    # adapters that never populate this keep today's behavior exactly.
    bankruptcy_discharged_at: Optional[date] = None
    # SafeScan-style synthetic fraud signals (Hard Rule #10), e.g.
    # {"safescan_score": 120, "identity_high_risk": False, "velocity_alert": False}.
    fraud_signals: dict[str, object] = field(default_factory=dict)
    confidence: float = 1.0
    vendor: Optional[str] = None


@dataclass(frozen=True)
class BankAccountSummary:
    """Result of a bank-link / income verification, shaped like a Flinks summary
    (Hard Rule #11)."""

    result: VerificationOutcome
    monthly_income_after_tax_cents: int = 0
    nsf_count_90d: int = 0
    account_age_months: int = 0
    balance_current_cents: int = 0
    confidence: float = 1.0
    vendor: Optional[str] = None


class VerificationAdapter(ABC):
    """Identity verification (KYC). Default vendor today is Didit."""

    @abstractmethod
    async def verify_identity(self, patient: PatientProfile, method: str) -> VerificationResult:
        ...


class BureauAdapter(ABC):
    """Credit bureau access. Soft pull first; hard pull only when warranted."""

    @abstractmethod
    async def soft_pull(self, patient: PatientProfile) -> BureauResult:
        ...

    @abstractmethod
    async def hard_pull(self, patient: PatientProfile) -> BureauResult:
        ...


class BankAdapter(ABC):
    """Bank-account linking / income verification."""

    @abstractmethod
    async def link_account(self, patient: PatientProfile) -> BankAccountSummary:
        ...


@dataclass(frozen=True)
class FlowAdapters:
    """The set of adapters the flow engine needs. Swapping any one is a config
    change with no engine changes (Hard Rule #4)."""

    verification: VerificationAdapter
    bureau: BureauAdapter
    bank: BankAdapter
