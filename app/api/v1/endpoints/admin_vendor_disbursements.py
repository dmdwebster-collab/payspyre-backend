"""Admin cockpit — vendor disbursements oversight (W2-DISB).

The PaySpyre-side view of the vendor self-serve payout engine
(``app/services/vendor_disbursements.py``): inspect any vendor's derived wallet
(MTD collected / due / available), read payout history across vendors, and —
gated behind the money flag — trigger the monthly auto-sweep on demand for ops.

    GET  /admin/disbursements/vendors/{vendor_id}/wallet — one vendor's wallet
    GET  /admin/disbursements                            — payout ledger (all /
                                                           one vendor)
    POST /admin/disbursements/run-monthly                — fire the free monthly
                                                           sweep now (flag-gated)

Read-only except the sweep trigger; admin/staff gated. The sweep is a STRICT
no-op while ``VENDOR_DISBURSEMENTS_ENABLED`` is False (default) — it reports
``enabled: false`` and moves nothing. All money movement is audited via
``platform_events`` inside the service.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import require_roles
from app.db.base import get_db
from app.models.platform.vendor_disbursement import PlatformVendorDisbursement
from app.services import vendor_disbursements as vd

router = APIRouter(dependencies=[Depends(require_roles("admin", "staff"))])


class MonthlyRunResponse(BaseModel):
    as_of: str
    enabled: bool
    adapter_available: bool
    vendors_scanned: int
    payouts_initiated: int
    payouts_settled: int
    skipped: int
    duplicates: int
    errors: int


@router.get("/vendors/{vendor_id}/wallet")
def vendor_wallet(
    vendor_id: UUID,
    db: Session = Depends(get_db),
) -> dict:
    """Any vendor's derived wallet (read-only; no money moves)."""
    return vd.compute_wallet(db, vendor_id).as_dict()


@router.get("")
def list_all(
    vendor_id: Optional[UUID] = Query(None, description="Scope to one vendor"),
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Payout ledger across all vendors (or one), newest first."""
    q = db.query(PlatformVendorDisbursement)
    if vendor_id is not None:
        q = q.filter(PlatformVendorDisbursement.vendor_id == vendor_id)
    rows = (
        q.order_by(PlatformVendorDisbursement.created_at.desc())
        .limit(limit)
        .all()
    )
    return [vd._serialize(r) for r in rows]


@router.post("/run-monthly", response_model=MonthlyRunResponse)
def run_monthly(
    db: Session = Depends(get_db),
) -> MonthlyRunResponse:
    """Fire the free monthly auto-disbursement sweep now (ops trigger).

    STRICT NO-OP while the money flag is off — returns ``enabled: false`` and
    moves nothing. Idempotent per vendor+month (a same-month re-run is a no-op)."""
    result = vd.run_monthly_auto_disbursement(db)
    return MonthlyRunResponse(
        as_of=result.as_of.isoformat(),
        enabled=result.enabled,
        adapter_available=result.adapter_available,
        vendors_scanned=result.vendors_scanned,
        payouts_initiated=result.payouts_initiated,
        payouts_settled=result.payouts_settled,
        skipped=result.skipped,
        duplicates=result.duplicates,
        errors=result.errors,
    )
