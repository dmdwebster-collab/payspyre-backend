"""Admin cutover-import API (WS-D): CSV upload -> preview -> confirm.

Turnkey's Tools -> Import workplace, rebuilt for migrating INTO PaySpyre
(docs/turnkey_parity/09__WP_Tools.md): per-entity templates, upload validates
into a preview report (NOTHING written to domain tables), a separate explicit
CONFIRM applies the rows idempotently, per-row failure-safe. Admin-only; every
lifecycle transition is audited via platform_events.

PII: responses carry the PII-safe preview/apply reports (row numbers + field
names). The raw normalized rows (which contain applicant PII for the customers
entity) are stored on the batch but never returned by list/detail endpoints
and never logged.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.core.auth import require_roles
from app.db.base import get_db
from app.models.platform.event import PlatformEvent
from app.models.platform.import_batch import PlatformImportBatch
from app.services.migration import csv_import

router = APIRouter()

# 560 loans / 580 customers is ~100 KB of CSV; 5 MB is generous headroom while
# still refusing an accidental multi-hundred-MB upload.
_MAX_UPLOAD_BYTES = 5 * 1024 * 1024


def _actor_id(user) -> str:
    return str(getattr(user, "id", "") or "unknown")


def _audit(db: Session, *, event_type: str, actor: str, payload: dict) -> None:
    db.add(PlatformEvent(event_type=event_type, actor=actor, payload=payload))


def _batch_out(batch: PlatformImportBatch, *, include_preview: bool = False,
               include_apply_report: bool = False) -> dict:
    out = {
        "id": str(batch.id),
        "entity_type": batch.entity_type,
        "filename": batch.filename,
        "status": batch.status,
        "row_count": batch.row_count,
        "error_count": batch.error_count,
        "created_by": batch.created_by,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "applied_at": batch.applied_at.isoformat() if batch.applied_at else None,
    }
    if include_preview:
        out["preview"] = batch.preview
    if include_apply_report:
        out["apply_report"] = batch.apply_report
    return out


def _get_batch(db: Session, batch_id: UUID) -> PlatformImportBatch:
    batch = (
        db.query(PlatformImportBatch)
        .filter(PlatformImportBatch.id == batch_id)
        .first()
    )
    if batch is None:
        raise HTTPException(status_code=404, detail="Import batch not found")
    return batch


@router.get("/templates/{entity_type}")
def get_template(entity_type: str, _user=Depends(require_roles("admin"))):
    """The documented column spec for an entity: expected header + example row."""
    if entity_type not in csv_import.ENTITY_TYPES:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown entity type; expected one of {', '.join(csv_import.ENTITY_TYPES)}",
        )
    return csv_import.template_for(entity_type)


@router.post("/batches")
async def upload_batch(
    entity_type: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    """Upload + validate a CSV. Returns the preview report; writes NOTHING to
    domain tables. A batch row is always created (even for a failed parse) so
    every upload attempt leaves an auditable record."""
    if entity_type not in csv_import.ENTITY_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"entity_type must be one of {', '.join(csv_import.ENTITY_TYPES)}",
        )
    filename = file.filename or "upload.csv"
    if filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(
            status_code=422,
            detail=".xlsx support pending dependency approval — upload CSV "
                   "(download the template for the expected header).",
        )
    raw = await file.read()
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (5 MB max)")
    try:
        text = raw.decode("utf-8-sig")  # BOM-tolerant (Excel CSV exports)
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="File is not valid UTF-8 text (is it an Excel binary? Upload CSV)")

    ctx = csv_import.build_context(db)
    result = csv_import.validate_csv(entity_type, text, ctx)

    ok = not result.file_errors
    batch = PlatformImportBatch(
        entity_type=entity_type,
        filename=filename,
        status="validated" if ok else "failed",
        row_count=result.row_count,
        error_count=len(result.errors),
        preview=result.report(),
        rows=result.valid_rows if ok else None,
        created_by=_actor_id(user),
    )
    db.add(batch)
    db.flush()
    _audit(
        db, event_type="import_batch_uploaded", actor=_actor_id(user),
        payload={
            "batch_id": str(batch.id), "entity_type": entity_type, "filename": filename,
            "status": batch.status, "row_count": result.row_count,
            "valid_count": len(result.valid_rows), "error_count": len(result.errors),
        },
    )
    db.commit()
    return _batch_out(batch, include_preview=True)


@router.get("/batches")
def list_batches(
    entity_type: Optional[str] = None,
    batch_status: Optional[str] = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    _user=Depends(require_roles("admin")),
):
    q = db.query(PlatformImportBatch)
    if entity_type:
        q = q.filter(PlatformImportBatch.entity_type == entity_type)
    if batch_status:
        q = q.filter(PlatformImportBatch.status == batch_status)
    batches = q.order_by(PlatformImportBatch.created_at.desc()).limit(min(limit, 200)).all()
    return {"batches": [_batch_out(b) for b in batches]}


@router.get("/batches/{batch_id}")
def get_batch(
    batch_id: UUID,
    db: Session = Depends(get_db),
    _user=Depends(require_roles("admin")),
):
    """Batch detail incl. the validation preview + (post-confirm) apply report."""
    batch = _get_batch(db, batch_id)
    return _batch_out(batch, include_preview=True, include_apply_report=True)


@router.post("/batches/{batch_id}/confirm")
def confirm_batch(
    batch_id: UUID,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    """Apply a validated batch's rows idempotently.

    Only valid rows are applied (rows with validation errors were excluded at
    upload). Per-row failure-safe: a failing row is recorded in the apply
    report and the rest proceed; the batch finishes 'applied' with error_count
    reflecting validation errors + apply failures.
    """
    batch = _get_batch(db, batch_id)
    # Optimistic claim: only one confirm can flip validated -> confirmed, so a
    # double-click / concurrent confirm can't double-apply.
    claimed = (
        db.query(PlatformImportBatch)
        .filter(
            PlatformImportBatch.id == batch_id,
            PlatformImportBatch.status == "validated",
        )
        .update({"status": "confirmed"}, synchronize_session=False)
    )
    if not claimed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Batch is '{batch.status}' — only a 'validated' batch can be confirmed.",
        )
    db.commit()  # persist the claim before the (potentially long) apply
    db.refresh(batch)

    rows = batch.rows or []
    if not rows:
        batch.status = "failed"
        db.commit()
        raise HTTPException(status_code=409, detail="Batch has no valid rows to apply.")

    actor = _actor_id(user)
    try:
        result = csv_import.apply_rows(db, batch.entity_type, rows)
    except Exception:
        db.rollback()
        batch = _get_batch(db, batch_id)
        batch.status = "failed"
        _audit(db, event_type="import_batch_failed", actor=actor,
               payload={"batch_id": str(batch.id), "entity_type": batch.entity_type})
        db.commit()
        raise HTTPException(status_code=500, detail="Import apply failed; batch marked failed. No partial rows from the failing pass were kept.")

    batch.status = "applied"
    batch.applied_at = datetime.now(timezone.utc)
    batch.apply_report = result.report()
    validation_errors = (batch.preview or {}).get("error_count", 0)
    batch.error_count = validation_errors + result.failed
    _audit(
        db, event_type="import_batch_confirmed", actor=actor,
        payload={
            "batch_id": str(batch.id), "entity_type": batch.entity_type,
            "filename": batch.filename, "applied": result.applied,
            "skipped_existing": result.skipped_existing, "failed": result.failed,
            "details": result.details,
        },
    )
    db.commit()  # rows + bookkeeping + audit land atomically
    return _batch_out(batch, include_apply_report=True)


@router.post("/batches/{batch_id}/cancel")
def cancel_batch(
    batch_id: UUID,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    """Discard a batch that has not been applied."""
    batch = _get_batch(db, batch_id)
    if batch.status in ("applied", "confirmed"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Batch is '{batch.status}' and can no longer be cancelled.",
        )
    if batch.status != "cancelled":
        batch.status = "cancelled"
        batch.rows = None  # drop the retained PII payload on discard
        _audit(db, event_type="import_batch_cancelled", actor=_actor_id(user),
               payload={"batch_id": str(batch.id), "entity_type": batch.entity_type,
                        "filename": batch.filename})
        db.commit()
    return _batch_out(batch)
