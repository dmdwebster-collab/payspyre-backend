"""Applicant-facing product catalogue (P8.x).

Unauthenticated, like ``POST /applications`` — this is part of the entry point:
a patient needs to see which financing products are available (and their amount
bounds) before starting an application. Returns only ``active`` products.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.applicant.v1.schemas import ProductSummary, ProductsResponse
from app.db.base import get_db
from app.models.platform.credit_product import PlatformCreditProduct

router = APIRouter(prefix="/products", tags=["applicant-products"])


@router.get("", response_model=ProductsResponse)
def list_products(db: Session = Depends(get_db)):
    rows = (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.status == "active")
        .order_by(PlatformCreditProduct.name)
        .all()
    )
    return ProductsResponse(products=[ProductSummary.model_validate(r) for r in rows])
