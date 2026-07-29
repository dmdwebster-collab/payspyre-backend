"""Admin maintenance — the guarded demo-data purge.

Two endpoints, deliberately asymmetric:

* ``POST /admin/maintenance/demo-purge/dry-run`` — counts what WOULD go, per
  table. Deletes nothing, needs no token.
* ``POST /admin/maintenance/demo-purge`` — actually deletes, and only with the
  exact confirmation phrase in the body.

Both refuse outright when ``ENVIRONMENT`` is production. Every guard failure
returns 409 with the operator-facing reason, having changed nothing. See
``app/services/demo_purge.py`` for the full safety contract.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import require_roles
from app.db.base import get_db
from app.models.platform.event import PlatformEvent
from app.services import demo_purge

router = APIRouter()


class PurgeDryRunRequest(BaseModel):
    retain_emails: Optional[list[str]] = Field(
        default=None,
        description=(
            "Logins to keep. Defaults to the standing operator accounts "
            f"({', '.join(demo_purge.DEFAULT_RETAIN_EMAILS)})."
        ),
    )
    include_vendors: bool = Field(
        default=False,
        description="Also purge vendor records and their provider rosters.",
    )


class PurgeRequest(PurgeDryRunRequest):
    confirmation: str = Field(
        description=(
            "Must be exactly "
            f"{demo_purge.CONFIRMATION_TOKEN!r}. There is no default — this "
            "field is the safety interlock."
        )
    )


@router.post("/demo-purge/dry-run")
def demo_purge_dry_run(
    body: PurgeDryRunRequest | None = None,
    db: Session = Depends(get_db),
    _user=Depends(require_roles("admin")),
):
    """Count what a purge would delete. Nothing is written."""
    body = body or PurgeDryRunRequest()
    try:
        report = demo_purge.dry_run(
            db,
            retain_emails=body.retain_emails,
            include_vendors=body.include_vendors,
        )
    except demo_purge.PurgeRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return report.as_dict()


@router.post("/demo-purge")
def demo_purge_execute(
    body: PurgeRequest,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    """Delete the demonstration data. Transactional; all-or-nothing."""
    try:
        report = demo_purge.purge(
            db,
            confirmation=body.confirmation,
            retain_emails=body.retain_emails,
            include_vendors=body.include_vendors,
            commit=False,
        )
    except demo_purge.PurgeRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None

    # The audit row is written AFTER the deletes (platform_events is one of the
    # purged tables) and commits with them, so the record of the purge survives
    # the purge itself.
    payload = report.as_dict()
    db.add(
        PlatformEvent(
            event_type="demo_data_purged",
            actor=str(getattr(user, "id", "") or "unknown"),
            payload=payload,
        )
    )
    db.commit()
    return payload
