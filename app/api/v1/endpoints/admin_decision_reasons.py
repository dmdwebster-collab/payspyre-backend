"""Decision-reason directory CRUD (WS-E underwriting ops). Admin-only.

The Settings directory of REJECT and CANCEL reasons (Turnkey parity,
02__WP_Underwriting.md — "These are added in a directory in the settings so we
can add and remove particular rejection reasons"). Rules:

* ``code`` and ``kind`` are IMMUTABLE after creation — decisions and audit
  events reference reasons by (kind, code), so a code can never be repointed.
* No hard delete: reasons are soft-deactivated (``active=false``) via PATCH so a
  code that was ever used in a decision stays resolvable for audit.
* Every write is audited via platform_events.
"""
from __future__ import annotations

from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.auth import require_roles
from app.db.base import get_db
from app.models.platform.decision_reason import PlatformDecisionReason
from app.models.platform.event import PlatformEvent
from app.services.decision_reasons import validate_reason_fields

router = APIRouter(dependencies=[Depends(require_roles("admin"))])


class ReasonRow(BaseModel):
    id: UUID
    kind: str
    code: str
    internal_label: str
    borrower_facing_text: str
    active: bool
    sort_order: int


class ReasonCreateBody(BaseModel):
    kind: Literal["reject", "cancel"]
    code: str
    internal_label: str
    borrower_facing_text: str
    sort_order: int = 0


class ReasonPatchBody(BaseModel):
    internal_label: Optional[str] = None
    borrower_facing_text: Optional[str] = None
    sort_order: Optional[int] = None
    active: Optional[bool] = None  # false = soft-deactivate


def _row(r: PlatformDecisionReason) -> ReasonRow:
    return ReasonRow(
        id=r.id, kind=str(r.kind), code=r.code, internal_label=r.internal_label,
        borrower_facing_text=r.borrower_facing_text, active=r.active,
        sort_order=r.sort_order,
    )


def _audit(db: Session, event_type: str, actor: str, payload: dict) -> None:
    db.add(PlatformEvent(event_type=event_type, actor=actor, payload=payload))
    db.flush()


@router.get("", response_model=list[ReasonRow])
def list_reasons(
    kind: Optional[Literal["reject", "cancel"]] = Query(None),
    include_inactive: bool = Query(True),
    db: Session = Depends(get_db),
):
    """The directory, ordered for pickers (kind, sort_order, code)."""
    q = db.query(PlatformDecisionReason)
    if kind:
        q = q.filter(PlatformDecisionReason.kind == kind)
    if not include_inactive:
        q = q.filter(PlatformDecisionReason.active.is_(True))
    rows = q.order_by(
        PlatformDecisionReason.kind,
        PlatformDecisionReason.sort_order,
        PlatformDecisionReason.code,
    ).all()
    return [_row(r) for r in rows]


@router.post("", response_model=ReasonRow, status_code=status.HTTP_201_CREATED)
def create_reason(
    body: ReasonCreateBody,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    try:
        validate_reason_fields(
            kind=body.kind, code=body.code, internal_label=body.internal_label,
            borrower_facing_text=body.borrower_facing_text,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    reason = PlatformDecisionReason(
        kind=body.kind, code=body.code, internal_label=body.internal_label,
        borrower_facing_text=body.borrower_facing_text, sort_order=body.sort_order,
        active=True,
    )
    db.add(reason)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A {body.kind} reason with code {body.code!r} already exists.",
        )
    _audit(
        db, "admin_decision_reason_created", str(getattr(user, "id", "unknown")),
        {"reason_id": str(reason.id), "kind": body.kind, "code": body.code},
    )
    db.commit()
    return _row(reason)


@router.patch("/{reason_id}", response_model=ReasonRow)
def update_reason(
    reason_id: UUID,
    body: ReasonPatchBody,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    """Edit label/wording/order or soft-(de)activate. code + kind are immutable;
    there is no DELETE."""
    reason = (
        db.query(PlatformDecisionReason)
        .filter(PlatformDecisionReason.id == reason_id)
        .first()
    )
    if reason is None:
        raise HTTPException(status_code=404, detail="Reason not found")

    changes: dict = {}
    if body.internal_label is not None:
        if not body.internal_label.strip():
            raise HTTPException(status_code=422, detail="internal_label must not be empty")
        changes["internal_label"] = body.internal_label
        reason.internal_label = body.internal_label
    if body.borrower_facing_text is not None:
        if not body.borrower_facing_text.strip():
            raise HTTPException(status_code=422, detail="borrower_facing_text must not be empty")
        changes["borrower_facing_text"] = body.borrower_facing_text
        reason.borrower_facing_text = body.borrower_facing_text
    if body.sort_order is not None:
        changes["sort_order"] = body.sort_order
        reason.sort_order = body.sort_order
    if body.active is not None:
        changes["active"] = body.active
        reason.active = body.active
    if not changes:
        raise HTTPException(status_code=422, detail="No fields to update")

    _audit(
        db, "admin_decision_reason_updated", str(getattr(user, "id", "unknown")),
        {"reason_id": str(reason.id), "kind": str(reason.kind), "code": reason.code,
         "changes": changes},
    )
    db.commit()
    return _row(reason)
