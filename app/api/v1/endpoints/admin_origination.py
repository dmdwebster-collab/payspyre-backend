"""The Originations workplace's product-driven New Application surface.

TWO endpoints, both driven entirely by the selected credit product (Dave: the
credit product is *"the calculation backbone of the platform, providing the
guardrails and information required to accurately calculate interest, fees,
payment amounts, cost of borrowing"*):

* ``GET  /admin/origination/constraints`` — the ONE call the form drives off.
  Vendor -> providers -> available credit products (with the auto-select flags
  for the single-provider / single-product cases), plus every field's default,
  minimum and maximum for the selected product: amount, term, interest rate,
  payment frequency, the start-date window and the custom first-payment window.
* ``POST /admin/origination/quote`` — the live calculation: the approximate
  payment for the chosen frequency, the **Principal + Interest + Fees = Total
  (APR)** disclosure line, and the preliminary amortization schedule
  (``#, Date, Payment, Principal, Interest, Fees, Balance``) that becomes the
  booked schedule if the loan proceeds.

The APR is the Canadian Cost of Borrowing figure (SOR/2001-104 s.3-4), which is
NOT the nominal annual interest rate once the product charges any
non-contingent fee.

Out-of-range inputs are refused with a FIELD-LEVEL error list
(``detail.errors[].field``), never a generic 422, so the form can highlight the
offending input. The same validator guards the application-create paths — see
``app/services/origination_constraints.py``.
"""
from __future__ import annotations

from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.auth import require_roles
from app.db.base import get_db
from app.models.platform.credit_product import PlatformCreditProduct
from app.schemas.pricing_config import PricingConfigError
from app.services.origination_constraints import (
    ConstraintsResponse,
    ConstraintViolation,
    OriginationQuote,
    SelectionInput,
    build_quote,
    product_pick_list,
    resolve_constraints,
)

router = APIRouter()


def _load_active_product(db: Session, product_id: UUID) -> PlatformCreditProduct:
    product = (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.id == product_id)
        .first()
    )
    if product is None or product.status != "active":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Credit product not found or not active",
        )
    return product


@router.get(
    "/constraints",
    response_model=ConstraintsResponse,
    summary="Everything the New Application form needs, resolved from the credit product",
)
def origination_constraints(
    vendor_id: Optional[UUID] = Query(
        None, description="Scopes the provider list and the product pick list."
    ),
    provider: Optional[str] = Query(
        None, description="Provider name (free text on the application today)."
    ),
    credit_product_id: Optional[UUID] = Query(
        None,
        description=(
            "Product to resolve guardrails for. Omit to use the vendor/provider "
            "default — which is auto-selected outright when only one product is "
            "available."
        ),
    ),
    as_of: Optional[date] = Query(
        None, description="Override 'today' (date-window defaults). Testing aid."
    ),
    db: Session = Depends(get_db),
    _user=Depends(require_roles("admin", "staff")),
):
    """Resolve the form's selectors and, for the chosen product, every bound.

    ``selection.single_product`` / ``single_provider`` mean the UI should
    auto-select and lock those fields. ``constraints`` is null only when no
    product is selected and none could be defaulted.
    """
    picks = product_pick_list(db, vendor_id=vendor_id, provider=provider)

    chosen = credit_product_id or picks.default_credit_product_id
    if chosen is None:
        return ConstraintsResponse(selection=picks, constraints=None)

    product = _load_active_product(db, chosen)
    try:
        resolved = resolve_constraints(product, as_of=as_of)
    except PricingConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Product pricing config invalid: {exc}",
        )
    # Reflect the effective selection back so the form never has to guess.
    picks = picks.model_copy(
        update={
            "products": [
                p.model_copy(update={"is_default": p.credit_product_id == chosen})
                for p in picks.products
            ],
            "default_credit_product_id": chosen,
        }
    )
    return ConstraintsResponse(selection=picks, constraints=resolved)


class QuoteRequest(BaseModel):
    """A New Application selection. Every term is optional — omitted values fall
    back to the product's own defaults, so the form's first render is a quote
    with no user input."""

    model_config = ConfigDict(extra="forbid")

    credit_product_id: UUID
    amount_cents: Optional[int] = Field(None, gt=0)
    term_months: Optional[int] = Field(None, gt=0)
    annual_rate_bps: Optional[int] = Field(None, ge=0)
    frequency: Optional[str] = None
    start_date: Optional[date] = None
    first_payment_date: Optional[date] = None
    as_of: Optional[date] = Field(
        None, description="Override 'today' for the date-window defaults. Testing aid."
    )


@router.post(
    "/quote",
    response_model=OriginationQuote,
    summary="Live payment, disclosure line and preliminary amortization schedule",
)
def origination_quote(
    body: QuoteRequest,
    db: Session = Depends(get_db),
    _user=Depends(require_roles("admin", "staff")),
):
    """Compute the form's live figures — nothing is persisted.

    Returns 422 with ``detail.errors = [{field, code, message, min, max,
    allowed}]`` when any input breaches the product's guardrails.
    """
    product = _load_active_product(db, body.credit_product_id)
    selection = SelectionInput(
        amount_cents=body.amount_cents,
        term_months=body.term_months,
        annual_rate_bps=body.annual_rate_bps,
        frequency=body.frequency,
        start_date=body.start_date,
        first_payment_date=body.first_payment_date,
    )
    try:
        return build_quote(product, selection, as_of=body.as_of)
    except ConstraintViolation as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.as_detail()
        )
    except PricingConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Product pricing config invalid: {exc}",
        )
