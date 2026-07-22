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
from app.schemas.product_policy_config import (
    ProductPolicyConfigError,
    assert_loan_type_change_allowed,
    parse_product_policy_config,
)
from app.services import credit_products as service
from app.services import loan_quote
from app.services import province_compliance

router = APIRouter()


def _validate_policy_or_422(
    policy_config: dict | None,
    *,
    existing_policy: dict | None = None,
    confirm_loan_type_change: bool = False,
) -> dict | None:
    """Validate policy_config against the typed schema + the engine-consistency
    guards (payoff grid, repayment allocation ordering, schedule building).
    Returns the normalized shape to persist (or None when the product carries no
    policy config — legacy rows stay NULL).

    On UPDATE, ``existing_policy`` enables TL's destructive-change guard on
    `Loan type`: switching it is refused with 409 unless the caller confirms.
    """
    if policy_config is None:
        return None
    try:
        cfg = parse_product_policy_config(policy_config)
    except ProductPolicyConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"policy_config invalid: {exc}",
        ) from exc
    try:
        assert_loan_type_change_allowed(
            existing_policy, cfg, confirm_loan_type_change=confirm_loan_type_change
        )
    except ProductPolicyConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    return cfg.model_dump(mode="json")


def _validate_pricing_or_422(
    db: Session,
    pricing_config: dict,
    min_amount_cents: int,
    max_amount_cents: int,
    provinces: Optional[list[str]] = None,
) -> dict:
    """Validate pricing_config against the typed schema plus the per-frequency
    APR compliance gates (Criminal Code s.347 at every enabled frequency /
    boundary amount / boundary term / rate-band top, PLUS the per-province
    compliance engine — APR caps and high-cost-credit licensing thresholds for
    every province the product targets). Fail-closed at configuration, not just
    at booking.

    ``provinces`` is resolved to the product's declared provinces, or — when the
    product declares none — every province PaySpyre operates in, so an "offered
    everywhere" product must clear the lowest cap. Returns the NORMALIZED typed
    shape to persist, so the DB converges on the schema."""
    effective_provinces = province_compliance.resolve_effective_provinces(db, provinces)
    province_check = province_compliance.make_pricing_province_check(db)
    try:
        cfg = loan_quote.validate_pricing_config(
            pricing_config,
            min_amount_cents,
            max_amount_cents,
            provinces=effective_provinces,
            province_check=province_check,
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
        db,
        data.pricing_config,
        data.min_amount_cents,
        data.max_amount_cents,
        provinces=data.provinces,
    )
    data.policy_config = _validate_policy_or_422(data.policy_config)
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
    confirm_loan_type_change: bool = Query(
        default=False,
        description=(
            "Confirms Turnkey's destructive `Loan type` change ('All filled data "
            "will be removed'). Required — and only consulted — when a patch "
            "changes policy_config.schedule_building.loan_type on a product that "
            "already has one; otherwise the update is refused with 409."
        ),
    ),
):
    # Re-validate the EFFECTIVE (merged) pricing when a patch touches the
    # pricing config, either amount bound, or the province set (schema + s.347
    # per frequency + per-province compliance caps).
    if (
        data.pricing_config is not None
        or data.min_amount_cents is not None
        or data.max_amount_cents is not None
        or data.provinces is not None
    ):
        existing = service.get_credit_product(db, product_id)
        if existing is not None:
            eff_min = data.min_amount_cents if data.min_amount_cents is not None else existing.min_amount_cents
            eff_max = data.max_amount_cents if data.max_amount_cents is not None else existing.max_amount_cents
            eff_pricing = data.pricing_config if data.pricing_config is not None else existing.pricing_config
            eff_provinces = data.provinces if data.provinces is not None else existing.provinces
            normalized = _validate_pricing_or_422(
                db, eff_pricing, eff_min, eff_max, provinces=eff_provinces
            )
            if data.pricing_config is not None:
                # Persist the normalized typed shape (only when the patch itself
                # carries pricing — unrelated patches never rewrite stored config).
                data.pricing_config = normalized
    if data.policy_config is not None:
        current = service.get_credit_product(db, product_id)
        data.policy_config = _validate_policy_or_422(
            data.policy_config,
            existing_policy=(current.policy_config if current is not None else None),
            confirm_loan_type_change=confirm_loan_type_change,
        )
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
