"""Admin CRUD for the per-province compliance rule engine (Workstream W2).

Net-new beyond Turnkey. Admins manage the province rule table that BLOCKS
non-compliant product configurations (APR caps / high-cost-credit licensing
thresholds) at create/update, and surface every rule still on placeholder
values so counsel can confirm them.

All routes are admin-role gated. The service layer (app/services/
province_compliance.py) owns validation, WORM events, and the DB-free
evaluation core.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.schemas.province_compliance import (
    ProvinceComplianceRuleRead,
    ProvinceComplianceRuleUpdate,
    ProvinceComplianceRuleUpsert,
)
from app.core.auth import require_roles
from app.db.base import get_db
from app.services import province_compliance as service

router = APIRouter()


@router.get(
    "/provinces",
    response_model=list[ProvinceComplianceRuleRead],
    summary="List all province compliance rules",
    dependencies=[Depends(require_roles("admin"))],
)
async def list_province_rules(db: Session = Depends(get_db)):
    return service.list_rules(db)


@router.get(
    "/provinces/unconfirmed",
    response_model=list[ProvinceComplianceRuleRead],
    summary="List province rules still on placeholder values (counsel_confirmed=False)",
    description=(
        "Surfaces every rule whose legal inputs are unconfirmed placeholders. "
        "Nothing here should be treated as an authoritative legal number until "
        "Dave/legal set counsel_confirmed=True."
    ),
    dependencies=[Depends(require_roles("admin"))],
)
async def list_unconfirmed_province_rules(db: Session = Depends(get_db)):
    return service.list_unconfirmed_rules(db)


@router.post(
    "/provinces/seed",
    response_model=list[ProvinceComplianceRuleRead],
    summary="Idempotently seed placeholder rules for every Canadian province",
    description=(
        "Ensures a placeholder row exists for all 13 provinces/territories. "
        "Existing rows are left untouched, so counsel-confirmed edits survive."
    ),
    dependencies=[Depends(require_roles("admin"))],
)
async def seed_province_rules(db: Session = Depends(get_db)):
    service.seed_placeholder_rules(db)
    return service.list_rules(db)


@router.get(
    "/provinces/{province_code}",
    response_model=ProvinceComplianceRuleRead,
    summary="Get one province compliance rule",
    dependencies=[Depends(require_roles("admin"))],
)
async def get_province_rule(province_code: str, db: Session = Depends(get_db)):
    rule = service.get_rule(db, province_code)
    if rule is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No compliance rule for province '{province_code}'",
        )
    return rule


@router.put(
    "/provinces/{province_code}",
    response_model=ProvinceComplianceRuleRead,
    summary="Create or replace a province compliance rule",
    dependencies=[Depends(require_roles("admin"))],
)
async def upsert_province_rule(
    province_code: str,
    data: ProvinceComplianceRuleUpsert,
    db: Session = Depends(get_db),
):
    try:
        return service.upsert_rule(
            db, province_code, data.model_dump(exclude_unset=True)
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.patch(
    "/provinces/{province_code}",
    response_model=ProvinceComplianceRuleRead,
    summary="Update a province compliance rule",
    dependencies=[Depends(require_roles("admin"))],
)
async def update_province_rule(
    province_code: str,
    data: ProvinceComplianceRuleUpdate,
    db: Session = Depends(get_db),
):
    try:
        return service.update_rule(
            db, province_code, data.model_dump(exclude_unset=True)
        )
    except ValueError as exc:
        msg = str(exc)
        code = (
            status.HTTP_404_NOT_FOUND if "no compliance rule" in msg.lower()
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=code, detail=msg) from exc
