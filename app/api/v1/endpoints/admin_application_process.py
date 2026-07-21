"""Admin API — Application-process configuration (Wave 2 W2-APPCONFIG).

TL "Settings ▸ Application process" (videos 07-08): the platform-wide config for
the applicant journey — flow confirmations, offer expiry (30d) / max offers (3),
dictionaries, disclaimer, co-applicant, form variants — plus a read of a
product's effective policy config (grace/due-dates/payoff/… tabs; those are
EDITED through the credit-product PATCH so validation stays in one place).

Admin-only. Config replace is audited via platform_events. GET endpoints always
return a fully-resolved config (defaults when the platform is unconfigured), so
the admin UI renders the full catalog with zero seeding.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.auth import require_roles
from app.db.base import get_db
from app.models.platform.event import PlatformEvent
from app.schemas.application_process_config import (
    ApplicationProcessConfig,
    ApplicationProcessConfigError,
    parse_application_process_config,
)
from app.services import application_process_config as service
from app.services import credit_products as products_service
from app.services import product_policy

router = APIRouter(dependencies=[Depends(require_roles("admin"))])


def _actor_id(user) -> str:
    return str(getattr(user, "id", "") or "unknown")


@router.get(
    "/application-process",
    response_model=ApplicationProcessConfig,
    summary="Get the effective application-process config",
)
def get_application_process_config(db: Session = Depends(get_db)):
    return service.get_effective_config(db)


@router.get(
    "/application-process/defaults",
    response_model=ApplicationProcessConfig,
    summary="Get the shipped default application-process config",
)
def get_application_process_defaults():
    """The behaviour-preserving defaults (offer expiry 30 / max 3, Dave's
    dictionaries, co-applicant enabled). Useful as a 'reset' baseline in the UI."""
    return ApplicationProcessConfig()


@router.put(
    "/application-process",
    response_model=ApplicationProcessConfig,
    summary="Replace the application-process config",
    description=(
        "Admin-only. Validates the full config document against the typed schema "
        "and upserts the single config row. Audited via platform_events."
    ),
)
def put_application_process_config(
    payload: dict,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    try:
        cfg = parse_application_process_config(payload, context="admin-put")
    except ApplicationProcessConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    saved = service.save_config(db, cfg, updated_by=_actor_id(user))
    db.add(
        PlatformEvent(
            event_type="admin_application_process_config_updated",
            actor=_actor_id(user),
            payload={
                "offer_expiry_days": saved.flow.offer_expiry_days,
                "max_offers": saved.flow.max_offers,
                "co_applicant_enabled": saved.co_applicant.enabled,
                "dictionaries": sorted(saved.dictionaries.keys()),
            },
        )
    )
    db.commit()
    return saved


@router.get(
    "/credit-products/{product_id}/policy",
    summary="Get a credit product's effective policy config",
    description=(
        "The resolved product-policy tabs (grace period / due dates / payoff / "
        "disbursement / approval / repayment modes). NULL policy_config → the "
        "shipped defaults (current engine behaviour). Edits go through "
        "PATCH /credit-products/{id} with a `policy_config` body."
    ),
)
def get_product_policy(product_id: UUID, db: Session = Depends(get_db)):
    product = products_service.get_credit_product(db, product_id)
    if product is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Credit product not found"
        )
    policy = product_policy.policy_for_product(product)
    return {
        "product_id": str(product.id),
        "code": product.code,
        "configured": bool(getattr(product, "policy_config", None)),
        "policy": policy.model_dump(mode="json"),
        "payoff_descriptor": product_policy.payoff_policy_descriptor(policy),
    }
