"""P0 T3 — the logic behind the six controls that rendered DISABLED.

Dave's 2026-07-21 Originations review specifies six buttons on the application
work surface. All six were wired to nothing; this module is their behaviour,
kept out of the router so it is unit-testable without a database.

    Bank Accounts tab   Add Bank Account · Bank Verification
    Contacts tab        Send Email · Send SMS
    Credit Report tab   Hard Pull · Soft Pull

Design commitments
------------------
**Honesty about mocks.** Flinks runs in simulator mode and the Equifax
subscriber agreement is unsigned. Every result carries ``simulated: True`` in
that case, sourced from what actually ran (adapter vendor / feature flags) —
never hard-coded optimism. The UI labels it; nobody reads a synthetic score as
a bureau file.

**Party selection.** A co-borrower is a SEPARATE application file linked by
``co_applicant_of_application_id`` (Dave mandate #1). ``resolve_party`` turns
the dialog's Borrower/Co-Borrower choice into the concrete file + patient, and
refuses ``co_borrower`` when none is attached — which is exactly the condition
under which the frontend greys the option out.

**Leading zeros.** Institution and transit numbers are TEXT end to end. "003"
is a real institution number; storing it as an integer silently destroys it.
:func:`validate_bank_account_input` enforces exact digit counts on strings and
never casts.

**Full account numbers.** Persisted Fernet-encrypted (``secret_crypto``); the
API only ever returns ``account_mask``. A PAD debit needs the real number, a
screen does not.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.secret_crypto import decrypt_secrets, encrypt_secrets
from app.models.platform.credit_application import PlatformCreditApplication

# --- constants ---------------------------------------------------------------

PARTY_BORROWER = "borrower"
PARTY_CO_BORROWER = "co_borrower"
PARTIES = (PARTY_BORROWER, PARTY_CO_BORROWER)

#: Dave's dropdown, verbatim. Stored lower-case.
ACCOUNT_TYPES = ("checking", "savings")

#: Dave's field limits (Originations review §3, Bank Accounts tab).
BANK_NAME_MAX = 25
INSTITUTION_DIGITS = 3
TRANSIT_DIGITS = 5
ACCOUNT_NUMBER_MAX = 15

SOURCE_MANUAL = "manual"
SOURCE_FLINKS = "flinks"


class ActionError(Exception):
    """Domain refusal; the router maps this to a 4xx with ``.status_code``."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


# --- bank-account input validation (pure) ------------------------------------


@dataclass(frozen=True)
class BankAccountInput:
    """A validated Add Bank Account dialog submission."""

    bank_name: str
    institution_number: str
    transit_number: str
    account_number: str
    account_type: str
    account_holder: Optional[str] = None


def _digits_only(value: str, *, field: str, exact: int) -> str:
    """Exactly ``exact`` ASCII digits, as a STRING (leading zeros preserved)."""
    cleaned = (value or "").strip()
    if not cleaned.isdigit():
        raise ActionError(f"{field} must be numeric (digits only).")
    if len(cleaned) != exact:
        raise ActionError(f"{field} must be exactly {exact} digits.")
    return cleaned


def validate_bank_account_input(
    *,
    bank_name: str,
    institution_number: str,
    transit_number: str,
    account_number: str,
    account_type: str,
    account_holder: Optional[str] = None,
) -> BankAccountInput:
    """Enforce Dave's field rules. Every field is mandatory (the dialog greys
    OK out until they are filled; the API refuses regardless)."""
    name = (bank_name or "").strip()
    if not name:
        raise ActionError("Bank Name is required.")
    if len(name) > BANK_NAME_MAX:
        raise ActionError(f"Bank Name must be {BANK_NAME_MAX} characters or fewer.")

    institution = _digits_only(
        institution_number, field="Institution Number", exact=INSTITUTION_DIGITS
    )
    transit = _digits_only(transit_number, field="Transit Number", exact=TRANSIT_DIGITS)

    account = (account_number or "").strip()
    if not account.isdigit():
        raise ActionError("Account Number must be numeric (digits only).")
    if len(account) > ACCOUNT_NUMBER_MAX:
        raise ActionError(
            f"Account Number must be {ACCOUNT_NUMBER_MAX} digits or fewer."
        )

    a_type = (account_type or "").strip().lower()
    if a_type not in ACCOUNT_TYPES:
        raise ActionError("Type must be one of: Checking, Savings.")

    holder = (account_holder or "").strip() or None
    return BankAccountInput(
        bank_name=name,
        institution_number=institution,
        transit_number=transit,
        account_number=account,
        account_type=a_type,
        account_holder=holder,
    )


def encrypt_account_number(account_number: str) -> str:
    """Fernet-encrypt the full account number for storage.

    Reuses ``secret_crypto`` (the integration-credentials envelope) rather than
    inventing a scheme. With no key configured (local dev / CI) it is a
    documented pass-through, exactly like SIN handling.
    """
    return encrypt_secrets({"account_number": account_number})["account_number"]


def decrypt_account_number(stored: Optional[str]) -> Optional[str]:
    """Inverse of :func:`encrypt_account_number`. Deliberately NOT called by any
    API response path — reserved for the PAD/debit builder."""
    if stored is None:
        return None
    return decrypt_secrets({"account_number": stored})["account_number"]


# --- party resolution --------------------------------------------------------


@dataclass(frozen=True)
class ResolvedParty:
    """Which concrete application file + patient an action targets."""

    party: str
    application: PlatformCreditApplication

    @property
    def application_id(self) -> UUID:
        return self.application.id

    @property
    def patient_id(self) -> UUID:
        return self.application.patient_id


def primary_application(
    db: Session, application: PlatformCreditApplication
) -> PlatformCreditApplication:
    """Resolve through to the primary file when handed a co-borrower file."""
    if application.co_applicant_of_application_id is None:
        return application
    primary = (
        db.query(PlatformCreditApplication)
        .filter(
            PlatformCreditApplication.id == application.co_applicant_of_application_id
        )
        .first()
    )
    return primary or application


def co_borrower_applications(
    db: Session, primary: PlatformCreditApplication
) -> list[PlatformCreditApplication]:
    """The co-borrower files linked to ``primary``, oldest first."""
    return (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.co_applicant_of_application_id == primary.id)
        .order_by(PlatformCreditApplication.created_at.asc())
        .all()
    )


def resolve_party(
    db: Session,
    application: PlatformCreditApplication,
    *,
    party: str,
    co_application_id: Optional[UUID] = None,
) -> ResolvedParty:
    """Turn the Borrower/Co-Borrower selector into a concrete file.

    ``co_borrower`` is refused when no co-borrower is attached (the same
    condition under which the dialog hides the option), and requires an
    explicit ``co_application_id`` when more than one is linked — Dave: "a
    choice must be made".
    """
    if party not in PARTIES:
        raise ActionError(f"party must be one of {PARTIES}.")
    primary = primary_application(db, application)
    if party == PARTY_BORROWER:
        return ResolvedParty(party=PARTY_BORROWER, application=primary)

    linked = co_borrower_applications(db, primary)
    if not linked:
        raise ActionError(
            "No co-borrower is attached to this application; "
            "the Co-Borrower option does not apply."
        )
    if co_application_id is not None:
        for row in linked:
            if row.id == co_application_id:
                return ResolvedParty(party=PARTY_CO_BORROWER, application=row)
        raise ActionError(
            "co_application_id is not a co-borrower of this application.", 404
        )
    if len(linked) > 1:
        raise ActionError(
            "This application has multiple co-borrower files; "
            "co_application_id is required to choose one."
        )
    return ResolvedParty(party=PARTY_CO_BORROWER, application=linked[0])


# --- mock/simulator disclosure ----------------------------------------------


def bank_verification_is_simulated(vendor: Optional[str]) -> bool:
    """A bank verification is real only when a genuine Flinks session was
    created. The mock dispatcher reports ``mock_*`` vendors."""
    return not (vendor or "").startswith("flinks")


#: The bureau path is hard-wired to the mock adapter in
#: ``VerificationDispatcher`` until the Equifax subscriber agreement is signed
#: (documented in that module). This constant makes the reason greppable.
BUREAU_SIMULATED_REASON = (
    "Equifax subscriber agreement is not signed; this report came from the "
    "mock bureau adapter and is NOT a real credit-bureau pull."
)


def bureau_result_payload(result: Any) -> dict:
    """Flatten a ``BureauResult`` into the JSONB report column.

    Carries no SIN / DOB / address: the mock does not produce them and the real
    adapter is documented PII-light.
    """
    discharged = getattr(result, "bankruptcy_discharged_at", None)
    return {
        "v": 1,
        "pull_type": getattr(result, "pull_type", None),
        "score": getattr(result, "score", None),
        "result": getattr(result, "result", None),
        "bankruptcy": bool(getattr(result, "bankruptcy", False)),
        "bankruptcy_discharged_at": discharged.isoformat() if discharged else None,
        "fraud_signals": dict(getattr(result, "fraud_signals", {}) or {}),
        "confidence": getattr(result, "confidence", None),
        "vendor": getattr(result, "vendor", None),
    }
