"""HTTP admin API for platform credit products (P3).

Wraps `app.services.credit_products` (pure functions) behind FastAPI routes so
admins / future admin UI can manage products without direct DB access.

Conventions mirrored from `app/api/v1/endpoints/patients.py`:
- Pydantic schemas live in `app/api/schemas/credit_products.py`
- ValueError from service layer -> HTTP 400
- JSON Schema ValidationError from service -> HTTP 422
- Requires admin role for all mutating operations
- Reads (`GET`) require an authenticated user (any role)

Hard Rule #1 (WORM events) and Hard Rule #6 (no PII in event payloads) are
enforced by the service layer; this endpoint only forwards data.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from jsonschema import ValidationError as JsonSchemaValidationError
from sqlalchemy.orm import Session

from app.api.schemas.credit_products import (
    CreditProductCreate,
    CreditProductRead,
    CreditProductUpdate,
)
from app.core.auth import get_current_user, require_roles
from app.db.base import get_db
from app.schemas.pricing_config import PricingConfigError
from app.services import credit_products as service
from app.services import loan_quote

router = APIRouter()


def _validate_pricing_or_422(
    pricing_config: dict, min_amount_cents: int, max_amount_cents: int
) -> dict:
    """Validate pricing_config against the typed schema plus the per-frequency
    APR compliance gates (Criminal Code s.347 at every enabled frequency /
    boundary amount / boundary term / rate-band top, and the province-cap
    hook). Fail-closed at configuration, not just at booking.

    Returns the NORMALIZED typed shape to persist, so the DB converges on the
    schema (legacy-shape submissions are upgraded on write; existing rows are
    still readable through the tolerant loader)."""
    try:
        cfg = loan_quote.validate_pricing_config(
            pricing_config, min_amount_cents, max_amount_cents
        )
    except PricingConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"pricing_config invalid: {exc}",
        ) from exc
    return cfg.model_dump(mode="json", exclude_none=True)


@router.post(
    "",
    response_model=CreditProductRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a credit product",
    description=(
        "Admin-only. Validates `verification_matrix` against the JSON Schema "
        "and writes a `credit_product.created` event to platform_events."
    ),
    dependencies=[Depends(require_roles("admin"))],
)
async def create_credit_product(
    data: CreditProductCreate,
    db: Session = Depends(get_db),
):
    data.pricing_config = _validate_pricing_or_422(
        data.pricing_config, data.min_amount_cents, data.max_amount_cents
    )
    try:
        product = service.create_credit_product(db, data)
    except JsonSchemaValidationError as exc:
        # Invalid verification_matrix shape
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"verification_matrix failed JSON Schema validation: {exc.message}",
        ) from exc
    except ValueError as exc:
        # Duplicate code, etc.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return product


@router.get(
    "",
    response_model=list[CreditProductRead],
    summary="List credit products",
    description="Returns active products by default. Pass `active_only=false` to include drafts and archived.",
)
async def list_credit_products(
    active_only: bool = Query(default=True, description="Only return status='active' products"),
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    return service.list_credit_products(db, active_only=active_only)


@router.get(
    "/{product_id}",
    response_model=CreditProductRead,
    summary="Get a credit product by id",
)
async def get_credit_product(
    product_id: UUID,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    product = service.get_credit_product(db, product_id)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Credit product not found")
    return product


@router.get(
    "/by-code/{code}",
    response_model=CreditProductRead,
    summary="Get a credit product by its unique code",
)
async def get_credit_product_by_code(
    code: str,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    product = service.get_credit_product_by_code(db, code)
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Credit product not found")
    return product


@router.patch(
    "/{product_id}",
    response_model=CreditProductRead,
    summary="Update a credit product",
    description=(
        "Admin-only. Bumps `version` by 1 and writes a `credit_product.updated` "
        "event with the list of fields changed. If `verification_matrix` is supplied "
        "it is re-validated against the JSON Schema."
    ),
    dependencies=[Depends(require_roles("admin"))],
)
async def update_credit_product(
    product_id: UUID,
    data: CreditProductUpdate,
    db: Session = Depends(get_db),
):
    # Re-validate the EFFECTIVE (merged) pricing when a patch touches the
    # pricing config or either amount bound (schema + s.347 per frequency).
    if (
        data.pricing_config is not None
        or data.min_amount_cents is not None
        or data.max_amount_cents is not None
    ):
        existing = service.get_credit_product(db, product_id)
        if existing is not None:
            eff_min = data.min_amount_cents if data.min_amount_cents is not None else existing.min_amount_cents
            eff_max = data.max_amount_cents if data.max_amount_cents is not None else existing.max_amount_cents
            eff_pricing = data.pricing_config if data.pricing_config is not None else existing.pricing_config
            normalized = _validate_pricing_or_422(eff_pricing, eff_min, eff_max)
            if data.pricing_config is not None:
                # Persist the normalized typed shape (only when the patch itself
                # carries pricing — unrelated patches never rewrite stored config).
                data.pricing_config = normalized
    try:
        product = service.update_credit_product(db, product_id, data)
    except JsonSchemaValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"verification_matrix failed JSON Schema validation: {exc.message}",
        ) from exc
    except ValueError as exc:
        # 404 if it's a 'not found', 400 otherwise
        msg = str(exc)
        code = (
            status.HTTP_404_NOT_FOUND if "not found" in msg.lower()
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=code, detail=msg) from exc
    return product


@router.delete(
    "/{product_id}",
    response_model=CreditProductRead,
    summary="Deactivate (archive) a credit product",
    description=(
        "Admin-only. Soft-delete by setting `status='archived'`. Logs a "
        "`credit_product.deactivated` event. The product remains queryable for "
        "audit / version-snapshot purposes."
    ),
    dependencies=[Depends(require_roles("admin"))],
)
async def deactivate_credit_product(
    product_id: UUID,
    db: Session = Depends(get_db),
):
    try:
        product = service.deactivate_credit_product(db, product_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return product
