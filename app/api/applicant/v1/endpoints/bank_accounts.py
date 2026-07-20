"""Borrower bank details (WS-J item 4) — read + default designation ONLY.

Dave (video 11 §5): "They cannot delete the bank accounts. We do not want them
to add bank accounts or add payment methods." The borrower surface therefore
exposes exactly two operations:

* ``GET /bank-accounts`` — the masked list of verified accounts;
* ``POST /bank-accounts/{id}/default`` — pick which ACTIVE verified account is
  the default for payments (TL's star toggle). SENSITIVE → step-up gated.

There is deliberately NO POST-create and NO DELETE here — adding (Flinks
re-verification or void-cheque/PAD + human review) and removing accounts are
staff-only operations on the admin surface
(``app/api/v1/endpoints/admin_borrower_security.py``).

Pay Now is locked to the default verified account: the payment-options
endpoint surfaces it read-only, and the payment modal never offers a choice.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.applicant.v1.deps import (
    ApplicantClaims,
    get_current_applicant,
    require_step_up,
)
from app.core.logging import get_logger
from app.db.base import get_db
from app.models.platform.borrower_portal import PlatformPatientBankAccount
from app.models.platform.event import PlatformEvent

logger = get_logger(__name__)
router = APIRouter(prefix="/bank-accounts", tags=["borrower-bank-accounts"])


class BankAccountOut(BaseModel):
    account_id: UUID
    institution_name: str
    currency: str
    account_type: Optional[str] = None
    routing_mask: Optional[str] = None
    account_mask: str
    verified_via: str
    is_default: bool


class BankAccountList(BaseModel):
    accounts: list[BankAccountOut]
    # Dave's controlled add-path message, surfaced instead of an add button.
    add_payment_method_notice: str


class SetDefaultResponse(BaseModel):
    account_id: UUID
    is_default: bool


_ADD_NOTICE = (
    "To add a payment method, please contact us — we will verify a new bank "
    "account through our secure bank-verification flow or a reviewed "
    "pre-authorized debit form. Accounts cannot be added or removed from the portal."
)


def _to_out(row: PlatformPatientBankAccount) -> BankAccountOut:
    return BankAccountOut(
        account_id=row.id,
        institution_name=row.institution_name,
        currency=row.currency,
        account_type=row.account_type,
        routing_mask=row.routing_mask,
        account_mask=row.account_mask,
        verified_via=row.verified_via,
        is_default=row.is_default,
    )


@router.get("", response_model=BankAccountList)
def list_bank_accounts(
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
):
    """The caller's ACTIVE verified accounts, masked. Removed accounts never
    surface here (they remain on file for staff/audit)."""
    rows = (
        db.query(PlatformPatientBankAccount)
        .filter(
            PlatformPatientBankAccount.patient_id == claims.patient_id,
            PlatformPatientBankAccount.status == "active",
        )
        .order_by(PlatformPatientBankAccount.created_at.asc())
        .all()
    )
    return BankAccountList(
        accounts=[_to_out(r) for r in rows],
        add_payment_method_notice=_ADD_NOTICE,
    )


@router.post("/{account_id}/default", response_model=SetDefaultResponse)
def set_default_account(
    account_id: UUID,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(require_step_up),
):
    """Designate the default-for-payments account (SENSITIVE — step-up gated).

    Only among the caller's own ACTIVE accounts; 404 otherwise (never reveal
    other borrowers' accounts). Clears the previous default atomically."""
    target = (
        db.query(PlatformPatientBankAccount)
        .filter(
            PlatformPatientBankAccount.id == account_id,
            PlatformPatientBankAccount.patient_id == claims.patient_id,
            PlatformPatientBankAccount.status == "active",
        )
        .first()
    )
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")

    others = (
        db.query(PlatformPatientBankAccount)
        .filter(
            PlatformPatientBankAccount.patient_id == claims.patient_id,
            PlatformPatientBankAccount.is_default.is_(True),
            PlatformPatientBankAccount.id != target.id,
        )
        .all()
    )
    for row in others:
        row.is_default = False
    # Flush the clears BEFORE setting the new default so the partial unique
    # index (one active default per patient) never sees two defaults.
    db.flush()
    target.is_default = True
    db.add(
        PlatformEvent(
            event_type="borrower_default_bank_account_changed",
            actor="patient",
            patient_id=claims.patient_id,
            payload={
                "v": 1,
                "actor": {"type": "patient", "id": str(claims.patient_id)},
                "patient_id": str(claims.patient_id),
                "account_id": str(target.id),
                # Masked value only — safe for the event log.
                "account_mask": target.account_mask,
            },
        )
    )
    db.commit()
    logger.info(
        "borrower_default_bank_account_changed",
        patient_id=str(claims.patient_id),
        account_id=str(target.id),
    )
    return SetDefaultResponse(account_id=target.id, is_default=True)
