"""POST /clinic/v1/financing-links — create a pre-filled patient application.

The practice fills in the product, amount, and the patient's name + contact;
we find-or-create the patient, create an application via the orchestrator
(``requested_amount_source='clinic'``), and return a shareable patient-flow URL
the practice can hand off.

The patient still has to authenticate (magic link) when they open the URL — we
deliberately do NOT mint a patient JWT here, so the link carries only the
application reference, not a credential. This mirrors what the frontend mock
synthesizes (``${APP_ORIGIN}/?ref=...&product=...&amount=...``).

Staff-authenticated (platform JWT) and clinic-scoped: the new application is
stamped with the caller's ``vendor_id`` (their clinic, per spec §13), so it
shows up only in that clinic's list/summary. See ``app/api/clinic/v1/deps.py``.
"""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.clinic.v1.deps import ClinicPrincipal, get_current_clinic_user, get_orchestrator
from app.api.clinic.v1.schemas import ClinicFinancingLink, CreateFinancingLinkBody
from app.core.config import settings
from app.db.base import get_db
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.patient import PlatformPatient
from app.services.flow_orchestrator import FlowOrchestrator, OrchestratorError

router = APIRouter(prefix="/financing-links", tags=["clinic-financing-links"])


def _split_name(full_name: str) -> tuple[str | None, str | None]:
    parts = full_name.strip().split()
    if not parts:
        return None, None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], " ".join(parts[1:])


def _looks_like_email(contact: str) -> bool:
    return "@" in contact


def _find_or_create_patient(db: Session, body: CreateFinancingLinkBody) -> PlatformPatient:
    """Match an existing patient by the provided contact, else create one.

    Mirrors the applicant ``POST /applications`` find-or-create (phone first,
    then email) but the clinic body only carries a single ``patient_contact``
    field, so we classify it as email vs phone heuristically.
    """
    contact = body.patient_contact.strip()
    email = contact if _looks_like_email(contact) else None
    phone = None if _looks_like_email(contact) else contact

    patient: PlatformPatient | None = None
    if phone:
        patient = (
            db.query(PlatformPatient).filter(PlatformPatient.phone_e164 == phone).first()
        )
    if patient is None and email:
        patient = db.query(PlatformPatient).filter(PlatformPatient.email == email).first()
    if patient is not None:
        return patient

    first, last = _split_name(body.patient_name)
    patient = PlatformPatient(
        legal_first_name=first,
        legal_last_name=last,
        email=email,
        phone_e164=phone,
    )
    db.add(patient)
    db.commit()
    db.refresh(patient)
    return patient


def _build_patient_flow_url(application_id, credit_product_id, amount_cents) -> str:
    """Build the shareable patient-flow URL (mirrors the frontend mock shape).

    Uses ``settings.WEBHOOK_PUBLIC_BASE_URL`` as the public origin when set,
    otherwise a root-relative URL the caller/frontend can resolve against its
    own origin. NOTE: a dedicated ``PATIENT_APP_BASE_URL`` setting is the right
    long-term home for this origin (follow-up — config is a shared file).
    """
    origin = (settings.WEBHOOK_PUBLIC_BASE_URL or "").rstrip("/")
    query = urlencode(
        {
            "ref": str(application_id),
            "product": str(credit_product_id),
            "amount": str(amount_cents),
        }
    )
    return f"{origin}/?{query}"


@router.post("", response_model=ClinicFinancingLink, status_code=status.HTTP_201_CREATED)
def create_financing_link(
    body: CreateFinancingLinkBody,
    db: Session = Depends(get_db),
    orchestrator: FlowOrchestrator = Depends(get_orchestrator),
    principal: ClinicPrincipal = Depends(get_current_clinic_user),
):
    product = (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.id == body.credit_product_id)
        .first()
    )
    if product is None or product.status != "active":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Credit product not found or not active",
        )

    patient = _find_or_create_patient(db, body)

    # WS-G customer lock/block: a blocked customer gets no new originations.
    from app.services.customer_blocks import CustomerBlockedError, ensure_not_blocked

    try:
        ensure_not_blocked(db, patient.id)
    except CustomerBlockedError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))

    try:
        application = orchestrator.create_application(
            patient_id=patient.id,
            credit_product_id=body.credit_product_id,
            requested_amount_cents=body.amount_cents,
            requested_amount_source="clinic",
            clinic_proposed_amount_cents=body.amount_cents,
            vendor_id=principal.vendor_id,
        )
    except OrchestratorError as exc:
        # Product existence is pre-checked above; this guards any other
        # orchestrator-level failure (e.g. a product deleted mid-request).
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    return ClinicFinancingLink(
        application_ref=str(application.id),
        url=_build_patient_flow_url(application.id, body.credit_product_id, body.amount_cents),
        patient_name=body.patient_name,
        amount_cents=body.amount_cents,
        product_name=product.name,
    )
