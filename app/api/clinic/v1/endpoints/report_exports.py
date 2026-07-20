"""Clinic portal — vendor-scoped XLSX report downloads.

The same seven Turnkey-parity reports as the admin surface
(app/services/report_exports.py), hard-scoped to the caller's own vendor via
``ClinicPrincipal.vendor_id`` — a clinic can never pass a vendor_id and can
never see another vendor's borrowers, payments or schedule.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.api.clinic.v1.deps import ClinicPrincipal, get_current_clinic_user
from app.db.base import get_db
from app.services.report_exports import REPORT_KEYS, generate_report, report_catalog

router = APIRouter(tags=["clinic-reports"])

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/reports/exports")
def list_reports(
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
) -> list[dict]:
    """The report menu: stable keys + display titles for the clinic UI."""
    return report_catalog()


@router.get("/reports/exports/{report_key}")
def export_report(
    report_key: str,
    date_from: Optional[date] = Query(None, description="Inclusive window start"),
    date_to: Optional[date] = Query(None, description="Inclusive window end"),
    db: Session = Depends(get_db),
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
) -> Response:
    if report_key not in REPORT_KEYS:
        raise HTTPException(status_code=404, detail=f"Unknown report: {report_key}")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from is after date_to")

    result = generate_report(
        db,
        report_key,
        vendor_id=principal.vendor_id,
        date_from=date_from,
        date_to=date_to,
    )
    return Response(
        content=result.content,
        media_type=_XLSX,
        headers={
            "Content-Disposition": f'attachment; filename="{result.filename}"',
            "X-Report-Rows": str(result.row_count),
        },
    )
