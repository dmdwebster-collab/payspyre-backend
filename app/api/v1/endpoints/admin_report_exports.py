"""Admin cockpit — Turnkey-parity XLSX report downloads.

Serves the seven operational reports Dave previously generated inside Turnkey
Lender (see app/services/report_exports.py and
docs/turnkey_parity/report_exports.md). Whole-book by default; ``vendor_id``
narrows to one vendor, ``date_from``/``date_to`` window each report's natural
time axis. Read-only; admin/staff gated.
"""
from __future__ import annotations

from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.core.auth import require_roles
from app.db.base import get_db
from app.services.report_exports import REPORT_KEYS, generate_report, report_catalog

router = APIRouter(dependencies=[Depends(require_roles("admin", "staff"))])

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/exports")
def list_reports() -> list[dict]:
    """The report menu: stable keys + display titles for the cockpit UI."""
    return report_catalog()


@router.get("/exports/{report_key}")
def export_report(
    report_key: str,
    vendor_id: Optional[UUID] = Query(None, description="Scope to one vendor"),
    date_from: Optional[date] = Query(None, description="Inclusive window start"),
    date_to: Optional[date] = Query(None, description="Inclusive window end"),
    db: Session = Depends(get_db),
) -> Response:
    if report_key not in REPORT_KEYS:
        raise HTTPException(status_code=404, detail=f"Unknown report: {report_key}")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from is after date_to")

    result = generate_report(
        db, report_key, vendor_id=vendor_id, date_from=date_from, date_to=date_to
    )
    return Response(
        content=result.content,
        media_type=_XLSX,
        headers={
            "Content-Disposition": f'attachment; filename="{result.filename}"',
            "X-Report-Rows": str(result.row_count),
        },
    )
