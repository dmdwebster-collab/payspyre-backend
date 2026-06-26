"""Applicant-facing product catalogue (P8.x).

Unauthenticated, like ``POST /applications`` — this is part of the entry point:
a patient needs to see which financing products are available (and their amount
bounds) before starting an application. Returns only ``active`` products.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.applicant.v1.schemas import ProductSummary, ProductsResponse
from app.db.base import get_db
from app.models.platform.credit_product import PlatformCreditProduct
from app.services import loan_quote

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


def _active_product(db: Session, product_id: UUID) -> PlatformCreditProduct:
    product = (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.id == product_id, PlatformCreditProduct.status == "active")
        .first()
    )
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not available")
    return product


class FrequencyOption(BaseModel):
    value: str
    label: str


class ProductTerms(BaseModel):
    product_id: UUID
    name: str
    currency: str
    min_amount_cents: int
    max_amount_cents: int
    term_options: list[int]
    term_min: int
    term_max: int
    frequencies: list[FrequencyOption]
    annual_rate_bps: int


@router.get("/{product_id}/terms", response_model=ProductTerms)
def product_terms(product_id: UUID, db: Session = Depends(get_db)):
    """The client-adjustable parameters for the terms calculator (entry point)."""
    product = _active_product(db, product_id)
    params = loan_quote.product_terms(product.pricing_config)
    return ProductTerms(
        product_id=product.id, name=product.name, currency=product.currency,
        min_amount_cents=product.min_amount_cents, max_amount_cents=product.max_amount_cents,
        term_options=params["term_options"], term_min=params["term_min"], term_max=params["term_max"],
        frequencies=params["frequencies"],
        annual_rate_bps=params["annual_rate_bps"],
    )


class QuoteRequest(BaseModel):
    amount_cents: int
    term_months: int
    frequency: str


class QuoteResponse(BaseModel):
    amount_cents: int
    term_months: int
    frequency: str
    frequency_label: str
    num_payments: int
    installment_cents: int
    final_installment_cents: int
    total_of_payments_cents: int   # principal + interest + fees
    interest_cents: int            # total interest over the term
    fees_cents: int                # applicable fees (per credit product)
    cost_of_borrowing_cents: int   # interest + fees
    annual_rate_bps: int
    apr_bps: int | None            # Canadian regulatory APR (SOR/2001-104 s.3-4)
    exceeds_criminal_rate: bool    # APR >= Criminal Code s.347 cap (compliance flag)


@router.post("/{product_id}/quote", response_model=QuoteResponse)
def quote(product_id: UUID, body: QuoteRequest, db: Session = Depends(get_db)):
    """Compute the regulated disclosure figures for a selected set of terms.

    Validates the selection against the product's parameters (amount bounds,
    allowed term, allowed frequency), then returns installment / total of
    payments / cost of borrowing. APR is deferred (see loan_quote)."""
    product = _active_product(db, product_id)
    params = loan_quote.product_terms(product.pricing_config)

    if not (product.min_amount_cents <= body.amount_cents <= product.max_amount_cents):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Amount must be between {product.min_amount_cents} and "
                   f"{product.max_amount_cents} cents.",
        )
    if not (params["term_min"] <= body.term_months <= params["term_max"]):
        raise HTTPException(
            status_code=422,
            detail=f"Term must be between {params['term_min']} and {params['term_max']} months.",
        )
    if body.frequency not in {f["value"] for f in params["frequencies"]}:
        raise HTTPException(status_code=422, detail="Unsupported payment frequency for this product.")

    try:
        q = loan_quote.quote_loan(
            body.amount_cents, params["annual_rate_bps"], body.term_months, body.frequency,
            fees_cents=params["fees_cents"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return QuoteResponse(
        amount_cents=q.amount_cents, term_months=q.term_months, frequency=q.frequency,
        frequency_label=q.frequency_label, num_payments=q.num_payments,
        installment_cents=q.installment_cents, final_installment_cents=q.final_installment_cents,
        total_of_payments_cents=q.total_of_payments_cents,
        interest_cents=q.interest_cents, fees_cents=q.fees_cents,
        cost_of_borrowing_cents=q.cost_of_borrowing_cents,
        annual_rate_bps=q.annual_rate_bps, apr_bps=q.apr_bps,
        exceeds_criminal_rate=q.exceeds_criminal_rate,
    )
