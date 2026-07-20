"""Customer/loan flags — directory CRUD + per-subject assignment (WS-E).

Turnkey parity video 01: named flags staff raise against a customer or a loan
with a reason. The directory is admin-managed (create / edit / soft-deactivate
— never hard-delete, matching the decision-reason directory idiom). Raising
and clearing flags is admin+staff. Definitions with
``suppress_notifications=True`` gate the notification processor's vendor
sends: the send is skipped but the skip is still written to
``platform_events`` (suppress sends, still log — see ``app/services/flags``).

Every directory change, raise and clear is audited to ``platform_events``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.core.auth import require_roles
from app.db.base import get_db
from app.models.platform.event import PlatformEvent
from app.models.platform.flag import PlatformFlagAssignment, PlatformFlagDefinition
from app.models.platform.loan import PlatformLoan
from app.models.platform.patient import PlatformPatient

router = APIRouter()


def _actor_id(user) -> str:
    return str(getattr(user, "id", "") or "unknown")


def _audit(
    db: Session,
    *,
    event_type: str,
    actor: str,
    payload: dict,
    patient_id: Optional[UUID] = None,
) -> None:
    db.add(
        PlatformEvent(event_type=event_type, actor=actor, patient_id=patient_id, payload=payload)
    )
    db.flush()


# ---------------------------------------------------------------------------
# Directory (admin CRUD, soft-deactivate only)
# ---------------------------------------------------------------------------


class FlagDefinitionRow(BaseModel):
    id: UUID
    code: str
    label: str
    description: Optional[str] = None
    applies_to: str
    suppress_notifications: bool
    is_active: bool
    created_at: datetime


class FlagDefinitionCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)
    label: str = Field(..., min_length=1)
    description: Optional[str] = None
    applies_to: Literal["customer", "loan", "both"] = "both"
    suppress_notifications: bool = False


class FlagDefinitionPatch(BaseModel):
    label: Optional[str] = None
    description: Optional[str] = None
    applies_to: Optional[Literal["customer", "loan", "both"]] = None
    suppress_notifications: Optional[bool] = None
    is_active: Optional[bool] = None


def _definition_row(d: PlatformFlagDefinition) -> FlagDefinitionRow:
    return FlagDefinitionRow(
        id=d.id,
        code=d.code,
        label=d.label,
        description=d.description,
        applies_to=d.applies_to,
        suppress_notifications=d.suppress_notifications,
        is_active=d.is_active,
        created_at=d.created_at,
    )


@router.get("/flags", response_model=list[FlagDefinitionRow])
def list_flag_definitions(
    include_inactive: bool = Query(False),
    db: Session = Depends(get_db),
    _user=Depends(require_roles("admin", "staff")),
):
    """The flag directory (staff read it to raise flags; admin manages it)."""
    q = db.query(PlatformFlagDefinition)
    if not include_inactive:
        q = q.filter(PlatformFlagDefinition.is_active.is_(True))
    return [_definition_row(d) for d in q.order_by(PlatformFlagDefinition.code).all()]


@router.post("/flags", response_model=FlagDefinitionRow, status_code=201)
def create_flag_definition(
    body: FlagDefinitionCreate,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    d = PlatformFlagDefinition(
        code=body.code.strip(),
        label=body.label.strip(),
        description=body.description,
        applies_to=body.applies_to,
        suppress_notifications=body.suppress_notifications,
    )
    db.add(d)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A flag with code {body.code!r} already exists.",
        )
    _audit(
        db,
        event_type="admin_flag_definition_created",
        actor=_actor_id(user),
        payload={
            "flag_id": str(d.id),
            "code": d.code,
            "applies_to": d.applies_to,
            "suppress_notifications": d.suppress_notifications,
        },
    )
    db.commit()
    return _definition_row(d)


@router.patch("/flags/{flag_id}", response_model=FlagDefinitionRow)
def update_flag_definition(
    flag_id: UUID,
    body: FlagDefinitionPatch,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    """Edit a definition (label/description/scope/suppression) or soft-
    deactivate it (``is_active=false``). ``code`` is immutable; there is no
    hard delete — history refers to it."""
    d = db.query(PlatformFlagDefinition).filter(PlatformFlagDefinition.id == flag_id).first()
    if d is None:
        raise HTTPException(status_code=404, detail="Flag definition not found")
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="No changes submitted")
    change_log = {}
    for field_name, new_value in changes.items():
        old_value = getattr(d, field_name)
        if new_value != old_value:
            change_log[field_name] = {"old": old_value, "new": new_value}
            setattr(d, field_name, new_value)
    if change_log:
        _audit(
            db,
            event_type="admin_flag_definition_updated",
            actor=_actor_id(user),
            payload={"flag_id": str(d.id), "code": d.code, "changes": change_log},
        )
    db.commit()
    return _definition_row(d)


# ---------------------------------------------------------------------------
# Assignments (raise / list / clear)
# ---------------------------------------------------------------------------


class FlagAssignmentRow(BaseModel):
    id: UUID
    flag_id: UUID
    flag_code: str
    flag_label: str
    suppress_notifications: bool
    patient_id: Optional[UUID] = None
    loan_id: Optional[UUID] = None
    reason: Optional[str] = None
    created_by: Optional[str] = None
    created_at: datetime
    cleared_at: Optional[datetime] = None
    cleared_by: Optional[str] = None
    cleared_reason: Optional[str] = None


class RaiseFlagBody(BaseModel):
    flag_code: str
    reason: Optional[str] = None


def _assignment_row(a: PlatformFlagAssignment) -> FlagAssignmentRow:
    return FlagAssignmentRow(
        id=a.id,
        flag_id=a.flag_id,
        flag_code=a.flag.code,
        flag_label=a.flag.label,
        suppress_notifications=a.flag.suppress_notifications,
        patient_id=a.patient_id,
        loan_id=a.loan_id,
        reason=a.reason,
        created_by=a.created_by,
        created_at=a.created_at,
        cleared_at=a.cleared_at,
        cleared_by=a.cleared_by,
        cleared_reason=a.cleared_reason,
    )


def _get_active_definition(
    db: Session, flag_code: str, subject: Literal["customer", "loan"]
) -> PlatformFlagDefinition:
    d = (
        db.query(PlatformFlagDefinition)
        .filter(
            PlatformFlagDefinition.code == flag_code,
            PlatformFlagDefinition.is_active.is_(True),
        )
        .first()
    )
    if d is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown or inactive flag code {flag_code!r} (see GET /admin/flags).",
        )
    if d.applies_to not in (subject, "both"):
        raise HTTPException(
            status_code=422,
            detail=f"Flag {flag_code!r} applies to {d.applies_to!r}, not {subject!r}.",
        )
    return d


def _raise_flag(
    db: Session,
    definition: PlatformFlagDefinition,
    *,
    patient_id: Optional[UUID],
    loan_id: Optional[UUID],
    reason: Optional[str],
    user,
) -> PlatformFlagAssignment:
    a = PlatformFlagAssignment(
        flag_id=definition.id,
        patient_id=patient_id,
        loan_id=loan_id,
        reason=reason,
        created_by=_actor_id(user),
    )
    db.add(a)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Flag {definition.code!r} is already active on this subject.",
        )
    _audit(
        db,
        event_type="admin_flag_raised",
        actor=_actor_id(user),
        patient_id=patient_id,
        payload={
            "assignment_id": str(a.id),
            "flag_code": definition.code,
            "suppress_notifications": definition.suppress_notifications,
            "patient_id": str(patient_id) if patient_id else None,
            "loan_id": str(loan_id) if loan_id else None,
            "reason": reason,
        },
    )
    db.commit()
    return a


def _list_assignments(db: Session, *, patient_id=None, loan_id=None, include_cleared=False):
    q = db.query(PlatformFlagAssignment).options(joinedload(PlatformFlagAssignment.flag))
    if patient_id is not None:
        q = q.filter(PlatformFlagAssignment.patient_id == patient_id)
    if loan_id is not None:
        q = q.filter(PlatformFlagAssignment.loan_id == loan_id)
    if not include_cleared:
        q = q.filter(PlatformFlagAssignment.cleared_at.is_(None))
    return [_assignment_row(a) for a in q.order_by(PlatformFlagAssignment.created_at.desc()).all()]


@router.get("/customers/{patient_id}/flags", response_model=list[FlagAssignmentRow])
def list_customer_flags(
    patient_id: UUID,
    include_cleared: bool = Query(False),
    db: Session = Depends(get_db),
    _user=Depends(require_roles("admin", "staff")),
):
    return _list_assignments(db, patient_id=patient_id, include_cleared=include_cleared)


@router.post(
    "/customers/{patient_id}/flags", response_model=FlagAssignmentRow, status_code=201
)
def raise_customer_flag(
    patient_id: UUID,
    body: RaiseFlagBody,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin", "staff")),
):
    if db.query(PlatformPatient.id).filter(PlatformPatient.id == patient_id).first() is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    d = _get_active_definition(db, body.flag_code, "customer")
    a = _raise_flag(db, d, patient_id=patient_id, loan_id=None, reason=body.reason, user=user)
    return _assignment_row(a)


@router.get("/loans/{loan_id}/flags", response_model=list[FlagAssignmentRow])
def list_loan_flags(
    loan_id: UUID,
    include_cleared: bool = Query(False),
    db: Session = Depends(get_db),
    _user=Depends(require_roles("admin", "staff")),
):
    return _list_assignments(db, loan_id=loan_id, include_cleared=include_cleared)


@router.post("/loans/{loan_id}/flags", response_model=FlagAssignmentRow, status_code=201)
def raise_loan_flag(
    loan_id: UUID,
    body: RaiseFlagBody,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin", "staff")),
):
    if db.query(PlatformLoan.id).filter(PlatformLoan.id == loan_id).first() is None:
        raise HTTPException(status_code=404, detail="Loan not found")
    d = _get_active_definition(db, body.flag_code, "loan")
    a = _raise_flag(db, d, patient_id=None, loan_id=loan_id, reason=body.reason, user=user)
    return _assignment_row(a)


class ClearFlagBody(BaseModel):
    reason: Optional[str] = None


@router.post("/flag-assignments/{assignment_id}/clear", response_model=FlagAssignmentRow)
def clear_flag(
    assignment_id: UUID,
    body: ClearFlagBody,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin", "staff")),
):
    """Clear a raised flag (stamps cleared_at/by; the row stays as history)."""
    a = (
        db.query(PlatformFlagAssignment)
        .options(joinedload(PlatformFlagAssignment.flag))
        .filter(PlatformFlagAssignment.id == assignment_id)
        .first()
    )
    if a is None:
        raise HTTPException(status_code=404, detail="Flag assignment not found")
    if a.cleared_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Flag is already cleared.")
    a.cleared_at = datetime.now(timezone.utc)
    a.cleared_by = _actor_id(user)
    a.cleared_reason = body.reason
    _audit(
        db,
        event_type="admin_flag_cleared",
        actor=_actor_id(user),
        patient_id=a.patient_id,
        payload={
            "assignment_id": str(a.id),
            "flag_code": a.flag.code,
            "patient_id": str(a.patient_id) if a.patient_id else None,
            "loan_id": str(a.loan_id) if a.loan_id else None,
            "reason": body.reason,
        },
    )
    db.commit()
    return _assignment_row(a)
