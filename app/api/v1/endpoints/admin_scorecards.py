"""Admin scorecard editor + per-vendor assignment (WS-D, Dave mandate #3).

Editable 5-band verified-data scorecards: additive bin-based attributes with
per-bin points, natural min/max (no clamping), and 5 bands
(Excellent/Good/Average/Weak/Poor-Fail) each carrying a decision outcome +
per-band credit limit and rate. Assignment: per-vendor override → platform
default → none (none = the legacy 680/600 path — nothing regresses until an
admin activates a scorecard).

Lifecycle: draft → active → archived. Only ACTIVE scorecards ever score. The
migration-058 partial unique index enforces a single platform default.
Every write is audited to platform_events.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app.core.auth import require_roles
from app.db.base import get_db
from app.models.loan import Vendor
from app.models.platform.event import PlatformEvent
from app.models.platform.scorecard import PlatformScorecard, PlatformVendorScorecard
from app.services.scorecards import ScorecardDefinition

router = APIRouter(dependencies=[Depends(require_roles("admin"))])


def _actor_id(user) -> str:
    return str(getattr(user, "id", "") or "unknown")


def _audit(db: Session, *, event_type: str, actor: str, payload: dict) -> None:
    db.add(PlatformEvent(event_type=event_type, actor=actor, payload=payload))


def _validate_definition(attributes: list, bands: list) -> ScorecardDefinition:
    try:
        return ScorecardDefinition(attributes=attributes, bands=bands)
    except (ValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid scorecard definition: {exc}",
        )


def _get_scorecard(db: Session, scorecard_id: UUID) -> PlatformScorecard:
    row = (
        db.query(PlatformScorecard)
        .filter(PlatformScorecard.id == scorecard_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scorecard not found")
    return row


class ScorecardBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = None
    attributes: list = Field(min_length=1)
    bands: list = Field(min_length=5, max_length=5)


class ScorecardRow(BaseModel):
    id: UUID
    name: str
    description: Optional[str] = None
    status: str
    is_default: bool
    version: int
    attributes: list
    bands: list
    natural_min: int
    natural_max: int
    created_at: datetime
    updated_at: datetime


def _to_row(row: PlatformScorecard) -> ScorecardRow:
    definition = ScorecardDefinition(attributes=row.attributes or [], bands=row.bands or [])
    nat_min, nat_max = definition.natural_range()
    return ScorecardRow(
        id=row.id,
        name=row.name,
        description=row.description,
        status=str(row.status),
        is_default=bool(row.is_default),
        version=row.version,
        attributes=row.attributes,
        bands=row.bands,
        natural_min=nat_min,
        natural_max=nat_max,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("", response_model=list[ScorecardRow])
def list_scorecards(db: Session = Depends(get_db)):
    rows = db.query(PlatformScorecard).order_by(PlatformScorecard.created_at.asc()).all()
    return [_to_row(r) for r in rows]


@router.post("", response_model=ScorecardRow, status_code=status.HTTP_201_CREATED)
def create_scorecard(
    body: ScorecardBody,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    """Create a scorecard in DRAFT (activate explicitly once reviewed)."""
    definition = _validate_definition(body.attributes, body.bands)
    if db.query(PlatformScorecard.id).filter(PlatformScorecard.name == body.name).first():
        raise HTTPException(status_code=409, detail=f"Scorecard name {body.name!r} already exists")
    row = PlatformScorecard(
        name=body.name,
        description=body.description,
        status="draft",
        attributes=definition.model_dump(mode="json")["attributes"],
        bands=definition.model_dump(mode="json")["bands"],
        created_by=_actor_id(user),
    )
    db.add(row)
    db.flush()
    _audit(
        db,
        event_type="admin_scorecard_created",
        actor=_actor_id(user),
        payload={"scorecard_id": str(row.id), "name": row.name},
    )
    db.commit()
    return _to_row(row)


@router.get("/{scorecard_id}", response_model=ScorecardRow)
def get_scorecard(scorecard_id: UUID, db: Session = Depends(get_db)):
    return _to_row(_get_scorecard(db, scorecard_id))


@router.put("/{scorecard_id}", response_model=ScorecardRow)
def update_scorecard(
    scorecard_id: UUID,
    body: ScorecardBody,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    """Edit a scorecard (any status; version bumps). Decisions already made
    keep their immutable snapshot on the application — edits are forward-only."""
    row = _get_scorecard(db, scorecard_id)
    definition = _validate_definition(body.attributes, body.bands)
    clash = (
        db.query(PlatformScorecard.id)
        .filter(PlatformScorecard.name == body.name, PlatformScorecard.id != scorecard_id)
        .first()
    )
    if clash:
        raise HTTPException(status_code=409, detail=f"Scorecard name {body.name!r} already exists")
    dumped = definition.model_dump(mode="json")
    row.name = body.name
    row.description = body.description
    row.attributes = dumped["attributes"]
    row.bands = dumped["bands"]
    row.version = row.version + 1
    _audit(
        db,
        event_type="admin_scorecard_updated",
        actor=_actor_id(user),
        payload={"scorecard_id": str(row.id), "version": row.version},
    )
    db.commit()
    return _to_row(row)


class StatusBody(BaseModel):
    pass


@router.post("/{scorecard_id}/activate", response_model=ScorecardRow)
def activate_scorecard(
    scorecard_id: UUID,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    row = _get_scorecard(db, scorecard_id)
    _validate_definition(row.attributes or [], row.bands or [])  # belt: stored shape still valid
    row.status = "active"
    _audit(
        db,
        event_type="admin_scorecard_activated",
        actor=_actor_id(user),
        payload={"scorecard_id": str(row.id)},
    )
    db.commit()
    return _to_row(row)


@router.post("/{scorecard_id}/archive", response_model=ScorecardRow)
def archive_scorecard(
    scorecard_id: UUID,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    """Archive — the scorecard stops scoring immediately (assignments keep the
    row for audit; resolution skips non-active scorecards)."""
    row = _get_scorecard(db, scorecard_id)
    row.status = "archived"
    if row.is_default:
        row.is_default = False
    _audit(
        db,
        event_type="admin_scorecard_archived",
        actor=_actor_id(user),
        payload={"scorecard_id": str(row.id)},
    )
    db.commit()
    return _to_row(row)


@router.post("/{scorecard_id}/set-default", response_model=ScorecardRow)
def set_default_scorecard(
    scorecard_id: UUID,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    """Make this the platform default (must be ACTIVE). Clears any previous
    default; the 058 partial unique index backstops."""
    row = _get_scorecard(db, scorecard_id)
    if row.status != "active":
        raise HTTPException(
            status_code=409, detail="Only an ACTIVE scorecard can be the platform default."
        )
    db.query(PlatformScorecard).filter(
        PlatformScorecard.is_default.is_(True), PlatformScorecard.id != scorecard_id
    ).update({PlatformScorecard.is_default: False}, synchronize_session="fetch")
    row.is_default = True
    _audit(
        db,
        event_type="admin_scorecard_default_set",
        actor=_actor_id(user),
        payload={"scorecard_id": str(row.id)},
    )
    db.commit()
    return _to_row(row)


@router.post("/{scorecard_id}/clear-default", response_model=ScorecardRow)
def clear_default_scorecard(
    scorecard_id: UUID,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    """Remove default status — unassigned vendors fall back to the legacy path."""
    row = _get_scorecard(db, scorecard_id)
    row.is_default = False
    _audit(
        db,
        event_type="admin_scorecard_default_cleared",
        actor=_actor_id(user),
        payload={"scorecard_id": str(row.id)},
    )
    db.commit()
    return _to_row(row)


# --- per-vendor assignment ---------------------------------------------------


class AssignmentRow(BaseModel):
    vendor_id: UUID
    vendor_name: str
    scorecard_id: UUID
    scorecard_name: str


class AssignBody(BaseModel):
    scorecard_id: UUID


@router.get("/assignments/list", response_model=list[AssignmentRow])
def list_assignments(db: Session = Depends(get_db)):
    rows = (
        db.query(PlatformVendorScorecard, Vendor.business_name, PlatformScorecard.name)
        .join(Vendor, Vendor.id == PlatformVendorScorecard.vendor_id)
        .join(PlatformScorecard, PlatformScorecard.id == PlatformVendorScorecard.scorecard_id)
        .all()
    )
    return [
        AssignmentRow(
            vendor_id=a.vendor_id,
            vendor_name=vendor_name,
            scorecard_id=a.scorecard_id,
            scorecard_name=scorecard_name,
        )
        for a, vendor_name, scorecard_name in rows
    ]


@router.put("/assignments/{vendor_id}", response_model=AssignmentRow)
def assign_vendor_scorecard(
    vendor_id: UUID,
    body: AssignBody,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    """Assign (or reassign) a vendor's scorecard override."""
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if vendor is None:
        raise HTTPException(status_code=404, detail="Vendor not found")
    scorecard = _get_scorecard(db, body.scorecard_id)
    if scorecard.status != "active":
        raise HTTPException(
            status_code=409, detail="Only an ACTIVE scorecard can be assigned to a vendor."
        )
    assignment = (
        db.query(PlatformVendorScorecard)
        .filter(PlatformVendorScorecard.vendor_id == vendor_id)
        .first()
    )
    if assignment is None:
        assignment = PlatformVendorScorecard(
            vendor_id=vendor_id, scorecard_id=scorecard.id, created_by=_actor_id(user)
        )
        db.add(assignment)
    else:
        assignment.scorecard_id = scorecard.id
    _audit(
        db,
        event_type="admin_scorecard_assigned",
        actor=_actor_id(user),
        payload={"vendor_id": str(vendor_id), "scorecard_id": str(scorecard.id)},
    )
    db.commit()
    return AssignmentRow(
        vendor_id=vendor_id,
        vendor_name=vendor.business_name,
        scorecard_id=scorecard.id,
        scorecard_name=scorecard.name,
    )


@router.delete("/assignments/{vendor_id}")
def unassign_vendor_scorecard(
    vendor_id: UUID,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    """Remove a vendor's override — it falls back to the platform default."""
    assignment = (
        db.query(PlatformVendorScorecard)
        .filter(PlatformVendorScorecard.vendor_id == vendor_id)
        .first()
    )
    if assignment is None:
        raise HTTPException(status_code=404, detail="No scorecard assignment for this vendor")
    db.delete(assignment)
    _audit(
        db,
        event_type="admin_scorecard_unassigned",
        actor=_actor_id(user),
        payload={"vendor_id": str(vendor_id)},
    )
    db.commit()
    return {"vendor_id": str(vendor_id), "unassigned": True}
