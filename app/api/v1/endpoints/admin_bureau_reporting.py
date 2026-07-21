"""Equifax month-end batch endpoints (WS-I) — TEST MODE ONLY.

Admin-only. Generate (the "custom update" manual trigger Dave asked for),
list, and download the Metro2-style batch files. Nothing is transmitted —
the Equifax agreement + certified layout are pending (see
``app/services/bureau_reporting.py``). The first-of-month automatic run is
``python -m app.jobs.bureau_batch`` (parked-cron pattern).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_roles
from app.db.base import get_db
from app.models.platform.bureau_batch import PlatformBureauBatch
from app.services import bureau_reporting

router = APIRouter(dependencies=[Depends(require_roles("admin"))])


class BatchRow(BaseModel):
    id: UUID
    batch_month: date
    mode: str
    file_name: str
    account_count: int
    total_balance_cents: int
    generation_count: int
    generated_by: str
    generated_at: datetime


class GenerateBody(BaseModel):
    # 'YYYY-MM' month to (re)generate; omitted = most recently completed month.
    month: Optional[str] = None


@router.get("/batches", response_model=list[BatchRow])
def list_batches(
    limit: int = Query(24, ge=1, le=120),
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """Generated month-end batches, newest month first (file content excluded —
    download it via /batches/{id}/file)."""
    return (
        db.query(PlatformBureauBatch)
        .order_by(PlatformBureauBatch.batch_month.desc())
        .limit(limit)
        .all()
    )


@router.post("/batches", response_model=BatchRow)
def generate_batch(
    body: GenerateBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Generate (or re-generate — idempotent per month) a month-end batch.
    This is Dave's manual "custom update" trigger; the automatic first-of-month
    run is the parked cron job. TEST MODE: stored, never transmitted."""
    month: Optional[date] = None
    if body.month:
        try:
            month = datetime.strptime(body.month, "%Y-%m").date().replace(day=1)
        except ValueError:
            raise HTTPException(status_code=422, detail="month must be 'YYYY-MM'")
    result = bureau_reporting.run_month_end_batch(
        db, month, generated_by=str(getattr(user, "id", "") or "unknown")
    )
    row = (
        db.query(PlatformBureauBatch)
        .filter(PlatformBureauBatch.id == UUID(result.batch_id))
        .first()
    )
    return row


@router.get("/batches/{batch_id}/file", response_class=PlainTextResponse)
def download_batch_file(
    batch_id: UUID,
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    """The batch's flat-file content (text/plain, Content-Disposition set)."""
    row = (
        db.query(PlatformBureauBatch)
        .filter(PlatformBureauBatch.id == batch_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    return PlainTextResponse(
        row.content,
        headers={"Content-Disposition": f'attachment; filename="{row.file_name}"'},
    )
