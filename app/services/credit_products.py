import json
from pathlib import Path
from typing import Optional
from uuid import UUID

from jsonschema import ValidationError
from jsonschema.validators import Draft202012Validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.schemas.credit_products import CreditProductCreate, CreditProductUpdate
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.event import PlatformEvent

_SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "credit_product_verification_matrix.json"
with _SCHEMA_PATH.open() as _f:
    _VERIFICATION_MATRIX_SCHEMA = json.load(_f)

_VALIDATOR = Draft202012Validator(_VERIFICATION_MATRIX_SCHEMA)


def _validate_verification_matrix(matrix: dict) -> None:
    """Raises jsonschema.ValidationError if matrix does not conform to the JSON Schema."""
    _VALIDATOR.validate(matrix)


def _log_event(db: Session, event_type: str, actor: str, payload: dict) -> PlatformEvent:
    event = PlatformEvent(
        event_type=event_type,
        actor=actor,
        payload=payload,
    )
    db.add(event)
    db.commit()
    return event


def create_credit_product(db: Session, data: CreditProductCreate) -> PlatformCreditProduct:
    """Create a new credit product. Validates verification_matrix before writing."""
    _validate_verification_matrix(data.verification_matrix)

    product = PlatformCreditProduct(
        code=data.code,
        name=data.name,
        vertical=data.vertical,
        status=data.status,
        min_amount_cents=data.min_amount_cents,
        max_amount_cents=data.max_amount_cents,
        currency=data.currency,
        verification_matrix=data.verification_matrix,
        decision_ruleset=data.decision_ruleset,
        pricing_config=data.pricing_config,
        policy_config=data.policy_config,
        funding_source=data.funding_source,
        provinces=data.provinces,
        created_by=data.created_by,
    )
    db.add(product)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError(f"Credit product with code '{data.code}' already exists") from exc
    db.refresh(product)

    _log_event(
        db,
        event_type="credit_product.created",
        actor="system",
        payload={"product_id": str(product.id), "code": product.code},
    )
    return product


def update_credit_product(
    db: Session, product_id: UUID, data: CreditProductUpdate
) -> PlatformCreditProduct:
    """Update an existing credit product. Validates verification_matrix if provided."""
    product = get_credit_product(db, product_id)
    if product is None:
        raise ValueError(f"Credit product {product_id} not found")

    if data.verification_matrix is not None:
        _validate_verification_matrix(data.verification_matrix)

    update_fields = data.model_dump(exclude_none=True)
    for field, value in update_fields.items():
        setattr(product, field, value)

    product.version = (product.version or 1) + 1
    db.commit()
    db.refresh(product)

    _log_event(
        db,
        event_type="credit_product.updated",
        actor="system",
        payload={"product_id": str(product.id), "code": product.code, "fields_updated": list(update_fields.keys())},
    )
    return product


def get_credit_product(db: Session, product_id: UUID) -> Optional[PlatformCreditProduct]:
    """Return credit product by primary key, or None if not found."""
    return (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.id == product_id)
        .first()
    )


def get_credit_product_by_code(db: Session, code: str) -> Optional[PlatformCreditProduct]:
    """Return credit product by unique code, or None if not found."""
    return (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == code)
        .first()
    )


def list_credit_products(
    db: Session, active_only: bool = True
) -> list[PlatformCreditProduct]:
    """List credit products. With active_only=True, returns only status='active' products."""
    query = db.query(PlatformCreditProduct)
    if active_only:
        query = query.filter(PlatformCreditProduct.status == "active")
    return query.all()


def deactivate_credit_product(db: Session, product_id: UUID) -> PlatformCreditProduct:
    """Set product status to 'archived' and log a deactivation event."""
    product = get_credit_product(db, product_id)
    if product is None:
        raise ValueError(f"Credit product {product_id} not found")

    product.status = "archived"
    db.commit()
    db.refresh(product)

    _log_event(
        db,
        event_type="credit_product.deactivated",
        actor="system",
        payload={"product_id": str(product.id), "code": product.code},
    )
    return product
