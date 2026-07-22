"""Clinic-facing product catalogue.

GET /clinic/v1/products — the active financing products a practice can offer.

The product catalogue is GLOBAL (clinic-allowed): every clinic offers the same
platform financing products, so this list is intentionally NOT filtered by
``vendor_id``. It still requires a valid clinic principal
(``get_current_clinic_user``) so only authenticated clinic members can read it —
a staff user with no clinic membership gets 403, same as the other endpoints.
Returns only ``status='active'`` products, shaped to the frontend
``ClinicProduct`` interface.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.clinic.v1.deps import ClinicPrincipal, get_current_clinic_user
from app.services.clinic_permissions import require_clinic_permission
from app.api.clinic.v1.schemas import ClinicProduct
from app.db.base import get_db
from app.models.platform.credit_product import PlatformCreditProduct

router = APIRouter(prefix="/products", tags=["clinic-products"])


@router.get(
    "",
    response_model=list[ClinicProduct],
    dependencies=[Depends(require_clinic_permission("loan_origination", "monitoring"))],
)
def list_products(
    db: Session = Depends(get_db),
    _principal: ClinicPrincipal = Depends(get_current_clinic_user),
):
    rows = (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.status == "active")
        .order_by(PlatformCreditProduct.name)
        .all()
    )
    return [ClinicProduct.model_validate(r) for r in rows]
