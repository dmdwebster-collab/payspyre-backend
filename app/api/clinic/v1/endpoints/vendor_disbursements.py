"""Clinic-facing vendor disbursements — the self-serve payout surface (W2-DISB).

PRIMARY SPEC: ``docs/turnkey_parity/10__Vendor_Access.md`` §2/§4/§5 (the big
new "Disbursements" ask). Dave wants the vendor, on their own portfolio, to see
what's been collected month-to-date, what's due to them, and what's available
to request right now — then control when they get paid.

    GET  /clinic/v1/disbursements/wallet    — derived MTD collected / due /
                                               available (+ holdback view)
    GET  /clinic/v1/disbursements           — this vendor's payout history
    POST /clinic/v1/disbursements/request   — request an EXTRA (fee-charged)
                                               payout of the available balance

SCOPING: every read/write is hard-scoped to ``principal.vendor_id`` (clinics ARE
the ``vendors`` table). A vendor can only ever see/act on its OWN wallet.

MONEY SAFETY: the wallet GETs move no money and are always live. The POST is
FLAG-GATED — it returns 409 while ``VENDOR_DISBURSEMENTS_ENABLED`` is False (the
default), and additionally requires a configured payment processor + a resolved
recipient before any cent moves. The free monthly auto-payout is the cron job
(``app/jobs/vendor_disbursements.py``), not an endpoint.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.clinic.v1.deps import ClinicPrincipal, get_current_clinic_user
from app.db.base import get_db
from app.services import vendor_disbursements as vd
from app.services.clinic_permissions import require_clinic_permission

router = APIRouter(prefix="/disbursements", tags=["clinic-disbursements"])


# --- response schemas -------------------------------------------------------


class WalletResponse(BaseModel):
    vendor_id: str
    as_of: str
    holdback_cutoff: str
    holdback_business_days: int
    share_bps: int
    mtd_collected_cents: int
    total_collected_cents: int
    cleared_collected_cents: int
    held_back_cents: int
    due_to_vendor_cents: int
    disbursed_settled_cents: int
    disbursed_in_flight_cents: int
    available_cents: int


class DisbursementRow(BaseModel):
    id: str
    vendor_id: str
    kind: str
    status: str
    amount_cents: int
    fee_cents: int
    holdback_cutoff: str | None
    period_year: int | None
    period_month: int | None
    external_ref: str | None
    return_code: str | None
    requested_by: str
    created_at: str | None
    completed_at: str | None


class ExtraPayoutResponse(BaseModel):
    status: str
    disbursement_id: str | None
    amount_cents: int
    fee_cents: int


# --- routes -----------------------------------------------------------------


@router.get("/wallet", response_model=WalletResponse)
def get_wallet(
    db: Session = Depends(get_db),
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
) -> WalletResponse:
    """Derived wallet for the caller's vendor. READ ONLY — no money moves, no
    flag required."""
    snapshot = vd.compute_wallet(db, principal.vendor_id)
    return WalletResponse(**snapshot.as_dict())


@router.get("", response_model=list[DisbursementRow])
def list_history(
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
) -> list[DisbursementRow]:
    """The caller vendor's payout history, newest first."""
    return [
        DisbursementRow(**row)
        for row in vd.list_disbursements(db, principal.vendor_id, limit=limit)
    ]


@router.post(
    "/request",
    response_model=ExtraPayoutResponse,
    dependencies=[Depends(require_clinic_permission("loan_servicing"))],
)
def request_payout(
    db: Session = Depends(get_db),
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
) -> ExtraPayoutResponse:
    """Request an EXTRA (fee-charged) payout of the whole available balance.

    Flag-gated: returns 409 while disbursements are disabled, or when nothing is
    releasable / no processor or recipient is configured. The free monthly sweep
    remains the automated path."""
    try:
        result = vd.request_extra_payout(
            db,
            principal.vendor_id,
            requested_by=str(principal.user_id),
        )
    except vd.DisbursementError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc

    if result.status not in ("initiated", "settled"):
        # Claimed-but-unresolved (transient) or vendor-rejected — surface it.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Disbursement could not be completed (status: {result.status}).",
        )
    return ExtraPayoutResponse(
        status=result.status,
        disbursement_id=result.disbursement_id,
        amount_cents=result.amount_cents,
        fee_cents=result.fee_cents,
    )
