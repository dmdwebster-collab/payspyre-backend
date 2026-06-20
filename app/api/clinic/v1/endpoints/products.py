"""Clinic-facing product catalogue.

GET /clinic/v1/products — the active financing products a practice can offer.
Staff-authenticated (platform JWT). Returns only ``status='active'`` products,
shaped to the frontend ``ClinicProduct`` interface.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.clinic.v1.deps import get_current_user
from app.api.clinic.v1.schemas import ClinicProduct
from app.db.base import get_db
from app.models.platform.credit_product import PlatformCreditProduct

router = APIRouter(prefix="/products", tags=["clinic-products"])


@router.get("", response_model=list[ClinicProduct])
def list_products(
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    rows = (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.status == "active")
        .order_by(PlatformCreditProduct.name)
        .all()
    )
    return [ClinicProduct.model_validate(r) for r in rows]
