"""Clinic-facing application list + dashboard summary.

GET /clinic/v1/applications     — recent applications, shaped for the table.
GET /clinic/v1/dashboard/summary — status-bucket counts for the dashboard cards.

Both are staff-authenticated (platform JWT) reads. They share one query helper
so the summary counts always agree with the rows shown in the table.

SCOPING (follow-up): there is NO clinic<->application link modeled yet. The
``platform_credit_applications`` table has ``patient_id`` and ``vendor_id`` but
no ``clinic_id``, and there is no ``user -> clinic`` membership. So today these
endpoints return platform-wide data. Once a clinic principal + an
``application.clinic_id`` (or ``vendor_id -> clinic``) link exist, add a
``.filter(...)`` here keyed off the authenticated clinic. See module note in
``app/api/clinic/v1/deps.py``.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session, joinedload

from app.api.clinic.v1.deps import get_current_user
from app.api.clinic.v1.schemas import ClinicApplication, ClinicDashboardSummary
from app.api.clinic.v1.status_map import to_clinic_status
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication

router = APIRouter(tags=["clinic-applications"])


def _patient_name(patient) -> str:
    if patient is None:
        return "Unknown patient"
    parts = [patient.legal_first_name, patient.legal_last_name]
    name = " ".join(p for p in parts if p).strip()
    return name or "Unknown patient"


def _patient_contact(patient) -> str:
    if patient is None:
        return ""
    return patient.email or patient.phone_e164 or ""


def _product_name(product) -> str:
    if product is None:
        return "Financing"
    return product.name or "Financing"


def _product_currency(product) -> str:
    if product is None or not product.currency:
        return "CAD"
    return product.currency


def query_applications(db: Session, limit: int = 200) -> list[PlatformCreditApplication]:
    """Most-recent-first application rows with patient + product eager-loaded.

    NOTE: not clinic-scoped (see module docstring) — returns platform-wide.
    """
    return (
        db.query(PlatformCreditApplication)
        .options(
            joinedload(PlatformCreditApplication.patient),
            joinedload(PlatformCreditApplication.credit_product),
        )
        .order_by(PlatformCreditApplication.created_at.desc())
        .limit(limit)
        .all()
    )


def to_clinic_application(row: PlatformCreditApplication) -> ClinicApplication:
    return ClinicApplication(
        id=row.id,
        patient_name=_patient_name(row.patient),
        patient_contact=_patient_contact(row.patient),
        product_name=_product_name(row.credit_product),
        amount_cents=row.requested_amount_cents,
        currency=_product_currency(row.credit_product),
        status=to_clinic_status(row.status),
        created_at=row.created_at,
    )


def build_summary(rows: list[PlatformCreditApplication]) -> ClinicDashboardSummary:
    counts = {"started": 0, "approved": 0, "declined": 0, "manual_review": 0}
    for row in rows:
        counts[to_clinic_status(row.status)] += 1
    return ClinicDashboardSummary(total=len(rows), **counts)


# --- routes ----------------------------------------------------------------


@router.get("/applications", response_model=list[ClinicApplication])
def list_applications(
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    rows = query_applications(db)
    return [to_clinic_application(r) for r in rows]


@router.get("/dashboard/summary", response_model=ClinicDashboardSummary)
def dashboard_summary(
    db: Session = Depends(get_db),
    _user=Depends(get_current_user),
):
    rows = query_applications(db)
    return build_summary(rows)
