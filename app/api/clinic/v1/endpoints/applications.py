"""Clinic-facing application list + dashboard summary.

GET /clinic/v1/applications     — recent applications, shaped for the table.
GET /clinic/v1/dashboard/summary — status-bucket counts for the dashboard cards.

Both are staff-authenticated (platform JWT) reads, SCOPED to the caller's
clinic. They share one query helper so the summary counts always agree with the
rows shown in the table.

SCOPING: clinics ARE the ``vendors`` table (spec §13).
``platform_credit_applications.vendor_id`` is the clinic that originated the
application; ``get_current_clinic_user`` resolves the caller's ``vendor_id``
(via ``platform_clinic_memberships``) and ``query_applications`` filters on it,
so each clinic sees only its own applications. See ``app/api/clinic/v1/deps.py``.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.api.clinic.v1.deps import ClinicPrincipal, get_current_clinic_user
from app.services.clinic_permissions import require_clinic_permission
from app.api.clinic.v1.schemas import ClinicApplication, ClinicDashboardSummary
from app.api.clinic.v1.status_map import to_vendor_visible_status
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


def query_applications(
    db: Session, vendor_id: UUID, limit: int = 200
) -> list[PlatformCreditApplication]:
    """Most-recent-first application rows for ONE clinic (``vendor_id``).

    Clinic-scoped: only applications whose ``vendor_id`` matches the caller's
    clinic are returned. Patient + product are eager-loaded for shaping.
    """
    return (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.vendor_id == vendor_id)
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
        # WS-I: vendors see the SILENT-ESCALATION view — an auto-decline pending
        # human review surfaces as manual_review, never as a final decline.
        status=to_vendor_visible_status(row.status, getattr(row, "decision_by", None)),
        created_at=row.created_at,
    )


def _summary_from_status_counts(triples) -> ClinicDashboardSummary:
    """Fold ``(platform_status, decision_by, count)`` triples into the 4 clinic
    buckets, through the VENDOR-VISIBLE mapping (WS-I silent-escalation rule —
    the cards must agree with the table, so an auto-decline pending human
    review counts as manual_review, not declined).

    Pure (no DB) so the bucketing — and the fact that it counts *all* rows, not a
    capped slice — is unit-testable.
    """
    counts = {"started": 0, "approved": 0, "declined": 0, "manual_review": 0}
    total = 0
    for status, decision_by, n in triples:
        total += n
        counts[to_vendor_visible_status(status, decision_by)] += n
    return ClinicDashboardSummary(total=total, **counts)


def summarize_applications(db: Session, vendor_id: UUID) -> ClinicDashboardSummary:
    """Status-bucket counts for ONE clinic via a SQL ``GROUP BY``.

    Counts every application for the clinic regardless of volume. (The previous
    path counted a 200-row capped fetch, so a busy clinic's dashboard cards
    silently undercounted past 200.) Clinic-scoped to ``vendor_id``. Grouped by
    (status, decision_by) so the vendor-visible mapping can distinguish an
    auto-decline (pending human review → manual_review) from a human-confirmed
    decline.
    """
    triples = (
        db.query(
            PlatformCreditApplication.status,
            PlatformCreditApplication.decision_by,
            func.count().label("n"),
        )
        .filter(PlatformCreditApplication.vendor_id == vendor_id)
        .group_by(
            PlatformCreditApplication.status, PlatformCreditApplication.decision_by
        )
        .all()
    )
    return _summary_from_status_counts(triples)


# --- routes ----------------------------------------------------------------


@router.get(
    "/applications",
    response_model=list[ClinicApplication],
    dependencies=[
        Depends(
            require_clinic_permission(
                "loan_origination", "loan_servicing", "monitoring"
            )
        )
    ],
)
def list_applications(
    db: Session = Depends(get_db),
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
):
    rows = query_applications(db, principal.vendor_id)
    return [to_clinic_application(r) for r in rows]


@router.get(
    "/dashboard/summary",
    response_model=ClinicDashboardSummary,
    dependencies=[
        Depends(
            require_clinic_permission(
                "loan_origination", "loan_servicing", "monitoring"
            )
        )
    ],
)
def dashboard_summary(
    db: Session = Depends(get_db),
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
):
    return summarize_applications(db, principal.vendor_id)
