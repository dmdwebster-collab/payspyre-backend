"""WS-H — Excel report builder v1 + scheduled reports engine (admin surface).

Two thin CRUD surfaces on top of the ``report_exports`` machinery:

  * /admin/reports/datasets                 the builder catalog (dataset keys +
                                             selectable columns)
  * /admin/reports/definitions  (CRUD)      saved report definitions (base
                                             dataset + column subset + filters)
  * /admin/reports/definitions/{id}/export  render a saved definition to XLSX
  * /admin/reports/schedules    (CRUD)      scheduled reports (report + cadence
                                             + recipients / per-vendor fan-out)
  * /admin/reports/schedules/run-due        manual trigger of the cron runner

Admin/staff gated. Delivery is inert without SendGrid — expected. Mounted under
the existing ``/admin/reports`` prefix alongside the stock exports.
"""
from __future__ import annotations

from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import require_roles
from app.db.base import get_db
from app.models.platform.report_schedule import (
    PlatformReportDefinition,
    PlatformReportSchedule,
    PlatformReportScheduleRun,
)
from app.services import report_exports, report_scheduler
from app.services.metrics import reports_depth as RD

router = APIRouter(dependencies=[Depends(require_roles("admin", "staff"))])

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _actor(user) -> str:
    return str(getattr(user, "id", None) or getattr(user, "email", None) or "admin")


# ---------------------------------------------------------------------------
# Builder catalog.
# ---------------------------------------------------------------------------


@router.get("/datasets")
def list_datasets() -> list[dict]:
    """Builder catalog: each dataset with its selectable column headers."""
    return report_exports.dataset_catalog()


# ---------------------------------------------------------------------------
# Saved report definitions (builder v1).
# ---------------------------------------------------------------------------


class DefinitionIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    base_dataset: str
    columns: list[str] = Field(min_length=1)
    filters: dict = Field(default_factory=dict)


class DefinitionOut(BaseModel):
    id: UUID
    name: str
    base_dataset: str
    columns: list[str]
    filters: dict
    active: bool


def _validate_definition(body: DefinitionIn) -> None:
    if body.base_dataset not in report_exports.REPORT_KEYS:
        raise HTTPException(status_code=422, detail=f"Unknown dataset: {body.base_dataset}")
    try:
        # Validates subset / no-dupes / non-empty against the real columns.
        RD.column_indices(report_exports.dataset_columns(body.base_dataset), body.columns)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _to_out(d: PlatformReportDefinition) -> DefinitionOut:
    return DefinitionOut(
        id=d.id,
        name=d.name,
        base_dataset=d.base_dataset,
        columns=list(d.columns or []),
        filters=dict(d.filters or {}),
        active=d.active,
    )


@router.get("/definitions", response_model=list[DefinitionOut])
def list_definitions(
    include_inactive: bool = Query(False),
    db: Session = Depends(get_db),
) -> list[DefinitionOut]:
    q = db.query(PlatformReportDefinition)
    if not include_inactive:
        q = q.filter(PlatformReportDefinition.active.is_(True))
    return [_to_out(d) for d in q.order_by(PlatformReportDefinition.created_at.desc()).all()]


@router.post("/definitions", response_model=DefinitionOut, status_code=201)
def create_definition(
    body: DefinitionIn,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin", "staff")),
) -> DefinitionOut:
    _validate_definition(body)
    d = PlatformReportDefinition(
        name=body.name.strip(),
        base_dataset=body.base_dataset,
        columns=body.columns,
        filters=body.filters or {},
        created_by=_actor(user),
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return _to_out(d)


@router.delete("/definitions/{definition_id}", status_code=204)
def deactivate_definition(
    definition_id: UUID, db: Session = Depends(get_db)
) -> Response:
    d = (
        db.query(PlatformReportDefinition)
        .filter(PlatformReportDefinition.id == definition_id)
        .first()
    )
    if d is None:
        raise HTTPException(status_code=404, detail="Definition not found")
    d.active = False  # soft-deactivate (parity with the rest of the config surfaces)
    db.commit()
    return Response(status_code=204)


@router.get("/definitions/{definition_id}/export")
def export_definition(
    definition_id: UUID,
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    db: Session = Depends(get_db),
) -> Response:
    d = (
        db.query(PlatformReportDefinition)
        .filter(PlatformReportDefinition.id == definition_id)
        .first()
    )
    if d is None:
        raise HTTPException(status_code=404, detail="Definition not found")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from is after date_to")

    filters = dict(d.filters or {})
    vendor_ids = [UUID(str(v)) for v in filters.get("vendor_ids", [])] or None
    win_from = date_from or _parse_date(filters.get("date_from"))
    win_to = date_to or _parse_date(filters.get("date_to"))
    try:
        result = report_exports.generate_custom_report(
            db,
            name=d.name,
            base_dataset=d.base_dataset,
            columns=list(d.columns or []),
            vendor_ids=vendor_ids,
            date_from=win_from,
            date_to=win_to,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return Response(
        content=result.content,
        media_type=_XLSX,
        headers={
            "Content-Disposition": f'attachment; filename="{result.filename}"',
            "X-Report-Rows": str(result.row_count),
        },
    )


def _parse_date(value) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Scheduled reports.
# ---------------------------------------------------------------------------


class ScheduleIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    report_key: Optional[str] = None
    definition_id: Optional[UUID] = None
    cadence: str
    params: dict = Field(default_factory=dict)
    recipients: list[str] = Field(default_factory=list)
    per_vendor: bool = False


class ScheduleOut(BaseModel):
    id: UUID
    name: str
    report_key: Optional[str]
    definition_id: Optional[UUID]
    cadence: str
    params: dict
    recipients: list[str]
    per_vendor: bool
    active: bool
    last_period_key: Optional[str]


def _validate_schedule(body: ScheduleIn, db: Session) -> None:
    if bool(body.report_key) == bool(body.definition_id):
        raise HTTPException(
            status_code=422,
            detail="exactly one of report_key / definition_id must be set",
        )
    if body.cadence not in RD.CADENCES:
        raise HTTPException(
            status_code=422,
            detail=f"cadence must be one of {', '.join(RD.CADENCES)}",
        )
    if body.report_key and body.report_key not in report_exports.REPORT_KEYS:
        raise HTTPException(status_code=422, detail=f"Unknown report: {body.report_key}")
    if body.definition_id is not None:
        exists = (
            db.query(PlatformReportDefinition.id)
            .filter(PlatformReportDefinition.id == body.definition_id)
            .first()
        )
        if exists is None:
            raise HTTPException(status_code=422, detail="definition_id not found")
    if not body.per_vendor and not body.recipients:
        raise HTTPException(
            status_code=422,
            detail="recipients required unless per_vendor is true",
        )


def _schedule_out(s: PlatformReportSchedule) -> ScheduleOut:
    return ScheduleOut(
        id=s.id,
        name=s.name,
        report_key=s.report_key,
        definition_id=s.definition_id,
        cadence=s.cadence,
        params=dict(s.params or {}),
        recipients=list(s.recipients or []),
        per_vendor=s.per_vendor,
        active=s.active,
        last_period_key=s.last_period_key,
    )


@router.get("/schedules", response_model=list[ScheduleOut])
def list_schedules(
    include_inactive: bool = Query(False), db: Session = Depends(get_db)
) -> list[ScheduleOut]:
    q = db.query(PlatformReportSchedule)
    if not include_inactive:
        q = q.filter(PlatformReportSchedule.active.is_(True))
    return [
        _schedule_out(s)
        for s in q.order_by(PlatformReportSchedule.created_at.desc()).all()
    ]


@router.post("/schedules", response_model=ScheduleOut, status_code=201)
def create_schedule(
    body: ScheduleIn,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin", "staff")),
) -> ScheduleOut:
    _validate_schedule(body, db)
    s = PlatformReportSchedule(
        name=body.name.strip(),
        report_key=body.report_key,
        definition_id=body.definition_id,
        cadence=body.cadence,
        params=body.params or {},
        recipients=body.recipients or [],
        per_vendor=body.per_vendor,
        created_by=_actor(user),
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return _schedule_out(s)


@router.delete("/schedules/{schedule_id}", status_code=204)
def deactivate_schedule(schedule_id: UUID, db: Session = Depends(get_db)) -> Response:
    s = (
        db.query(PlatformReportSchedule)
        .filter(PlatformReportSchedule.id == schedule_id)
        .first()
    )
    if s is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    s.active = False
    db.commit()
    return Response(status_code=204)


class RunDueResult(BaseModel):
    schedules_considered: int
    schedules_run: int
    reports_rendered: int
    emails_sent: int
    statuses: dict


@router.post("/schedules/run-due", response_model=RunDueResult)
def run_due(db: Session = Depends(get_db)) -> RunDueResult:
    """Manually trigger the scheduled-reports runner (also the cron entrypoint).

    Renders + best-effort delivers every due schedule. Delivery is inert
    without SendGrid — the runs are logged ``rendered`` rather than ``sent``.
    """
    summary = report_scheduler.run_due_schedules(db)
    return RunDueResult(
        schedules_considered=summary.schedules_considered,
        schedules_run=summary.schedules_run,
        reports_rendered=summary.reports_rendered,
        emails_sent=summary.emails_sent,
        statuses=summary.statuses,
    )


class ScheduleRunOut(BaseModel):
    period_key: str
    status: str
    detail: Optional[str]
    report_count: int
    recipient_count: int


@router.get("/schedules/{schedule_id}/runs", response_model=list[ScheduleRunOut])
def list_runs(
    schedule_id: UUID,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[ScheduleRunOut]:
    rows = (
        db.query(PlatformReportScheduleRun)
        .filter(PlatformReportScheduleRun.schedule_id == schedule_id)
        .order_by(PlatformReportScheduleRun.ran_at.desc())
        .limit(limit)
        .all()
    )
    return [
        ScheduleRunOut(
            period_key=r.period_key,
            status=r.status,
            detail=r.detail,
            report_count=r.report_count,
            recipient_count=r.recipient_count,
        )
        for r in rows
    ]
