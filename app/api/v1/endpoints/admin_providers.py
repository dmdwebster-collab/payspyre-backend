"""Admin CRUD for the vendor provider roster (migration 083).

Providers belong to a vendor. Before this, "provider" was free text on the
application and the Originations dropdown was a SELECT DISTINCT over past
applications — so the option list was whatever anyone had ever typed. These
endpoints make the roster an editable directory: add a practitioner, retire one
(never delete — historical loans still point at it), and read the list a
dropdown should offer.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import require_roles
from app.db.base import get_db
from app.models.loan import Vendor
from app.models.platform.event import PlatformEvent
from app.models.platform.provider import PROVIDER_SOURCE_MANUAL, PlatformProvider
from app.services import providers as providers_service

router = APIRouter()


class ProviderOut(BaseModel):
    id: UUID
    vendor_id: UUID
    name: str
    external_code: Optional[str] = None
    is_active: bool
    source: str


class ProviderCreate(BaseModel):
    vendor_id: UUID
    name: str = Field(min_length=1, max_length=255)
    external_code: Optional[str] = Field(default=None, max_length=64)


class ProviderUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    external_code: Optional[str] = Field(default=None, max_length=64)
    is_active: Optional[bool] = None


def _out(p: PlatformProvider) -> ProviderOut:
    return ProviderOut(
        id=p.id,
        vendor_id=p.vendor_id,
        name=p.name,
        external_code=p.external_code,
        is_active=p.is_active,
        source=p.source,
    )


@router.get("/providers", response_model=list[ProviderOut])
def list_providers(
    vendor_id: Optional[UUID] = None,
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    _user=Depends(require_roles("admin")),
):
    """The provider roster, optionally narrowed to one vendor."""
    q = db.query(PlatformProvider)
    if vendor_id is not None:
        q = q.filter(PlatformProvider.vendor_id == vendor_id)
    if not include_inactive:
        q = q.filter(PlatformProvider.is_active.is_(True))
    return [_out(p) for p in q.order_by(PlatformProvider.name).all()]


@router.post("/providers", response_model=ProviderOut, status_code=201)
def create_provider(
    body: ProviderCreate,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    if db.query(Vendor).filter(Vendor.id == body.vendor_id).first() is None:
        raise HTTPException(status_code=404, detail="Vendor not found")
    if providers_service.find(db, body.vendor_id, body.name) is not None:
        raise HTTPException(
            status_code=409, detail="This vendor already has a provider by that name"
        )
    provider = providers_service.find_or_create(
        db,
        body.vendor_id,
        body.name,
        source=PROVIDER_SOURCE_MANUAL,
        external_code=body.external_code,
    )
    db.add(
        PlatformEvent(
            event_type="provider_created",
            actor=str(getattr(user, "id", "") or "unknown"),
            payload={"provider_id": str(provider.id), "vendor_id": str(body.vendor_id)},
        )
    )
    db.commit()
    return _out(provider)


@router.patch("/providers/{provider_id}", response_model=ProviderOut)
def update_provider(
    provider_id: UUID,
    body: ProviderUpdate,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    """Rename, re-code, or RETIRE a provider. Retiring (``is_active=false``)
    removes it from dropdowns while leaving every historical link intact —
    there is no delete."""
    provider = (
        db.query(PlatformProvider).filter(PlatformProvider.id == provider_id).first()
    )
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    if body.name is not None:
        clash = providers_service.find(db, provider.vendor_id, body.name)
        if clash is not None and clash.id != provider.id:
            raise HTTPException(
                status_code=409, detail="This vendor already has a provider by that name"
            )
        provider.name = providers_service.normalize_name(body.name)
    if body.external_code is not None:
        provider.external_code = body.external_code
    if body.is_active is not None:
        provider.is_active = body.is_active
    db.add(
        PlatformEvent(
            event_type="provider_updated",
            actor=str(getattr(user, "id", "") or "unknown"),
            payload={"provider_id": str(provider.id), "is_active": provider.is_active},
        )
    )
    db.commit()
    db.refresh(provider)
    return _out(provider)
