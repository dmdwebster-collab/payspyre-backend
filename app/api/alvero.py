from __future__ import annotations

import hashlib
import hmac
import time
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.base import get_db
from app.models.loan import Vendor
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.patient import PlatformPatient

router = APIRouter(prefix="/v1/alvero", tags=["alvero"])


class AlveroPatient(BaseModel):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    email: str | None = None
    phone: str | None = None


class CreateAlveroApplicationBody(BaseModel):
    alvero_application_id: str = Field(min_length=1, max_length=120)
    tenant_slug: str = Field(min_length=1, max_length=40)
    provider_external_id: str = Field(min_length=1, max_length=80)
    patient: AlveroPatient
    requested_amount_cents: int = Field(gt=0)
    requested_term_months: int | None = None
    purpose: str = Field(min_length=1, max_length=255)


class CreateAlveroApplicationResponse(BaseModel):
    application_id: UUID
    party_id: UUID
    status: str
    wizard_url: str
    next_steps: list[str]


def _tenant_secret(tenant: str) -> str | None:
    if tenant == "kdc":
        return settings.ALVERO_TENANT_KEY_KDC or None
    return None


def _verify_signature(raw: bytes, tenant: str, timestamp: str | None, signature: str | None, method: str, path: str) -> None:
    secret = _tenant_secret(tenant)
    if not secret:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="tenant_not_configured")
    if not timestamp or not signature:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing_signature")
    try:
        ts = int(timestamp)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_timestamp")
    if abs(int(time.time()) - ts) > 300:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="stale_timestamp")

    body_hash = hashlib.sha256(raw).hexdigest()
    canonical = "\n".join([method.upper(), path, timestamp, body_hash])
    expected = hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad_signature")


def _find_or_create_patient(db: Session, body: CreateAlveroApplicationBody) -> PlatformPatient:
    patient = None
    if body.patient.phone:
        patient = db.query(PlatformPatient).filter(PlatformPatient.phone_e164 == body.patient.phone).first()
    if patient is None and body.patient.email:
        patient = db.query(PlatformPatient).filter(PlatformPatient.email == body.patient.email).first()
    if patient is not None:
        return patient
    patient = PlatformPatient(
        legal_first_name=body.patient.first_name,
        legal_last_name=body.patient.last_name,
        email=body.patient.email,
        phone_e164=body.patient.phone,
    )
    db.add(patient)
    db.flush()
    return patient


def _active_dental_product(db: Session, amount_cents: int) -> PlatformCreditProduct:
    product = (
        db.query(PlatformCreditProduct)
        .filter(
            PlatformCreditProduct.vertical == "dental",
            PlatformCreditProduct.status == "active",
            PlatformCreditProduct.min_amount_cents <= amount_cents,
            PlatformCreditProduct.max_amount_cents >= amount_cents,
        )
        .order_by(PlatformCreditProduct.created_at.asc())
        .first()
    )
    if product is None:
        product = (
            db.query(PlatformCreditProduct)
            .filter(PlatformCreditProduct.vertical == "dental", PlatformCreditProduct.status == "active")
            .order_by(PlatformCreditProduct.created_at.asc())
            .first()
        )
    if product is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="no_active_dental_product")
    return product


def _vendor(db: Session, provider_external_id: str) -> Vendor | None:
    needle = provider_external_id.lower()
    q = db.query(Vendor).filter(Vendor.status == "active")
    if needle == "kdc":
        return q.filter(Vendor.business_name.ilike("%Kelowna Dental Centre%")).first() or q.filter(Vendor.business_name.ilike("%KDC%")).first()
    return q.filter(Vendor.business_name.ilike(f"%{provider_external_id}%")).first()


def _response(application: PlatformCreditApplication) -> CreateAlveroApplicationResponse:
    portal = settings.PAYSPYRE_PORTAL_BASE_URL.rstrip("/")
    return CreateAlveroApplicationResponse(
        application_id=application.id,
        party_id=application.patient_id,
        status=application.status,
        wizard_url=f"{portal}/apply/{application.id}",
        next_steps=["idv", "income_verification", "pad_setup"],
    )


@router.post("/applications", response_model=CreateAlveroApplicationResponse, status_code=status.HTTP_201_CREATED)
async def create_application(
    request: Request,
    x_alvero_tenant: str = Header(...),
    x_alvero_timestamp: str | None = Header(None),
    x_alvero_signature: str | None = Header(None),
    db: Session = Depends(get_db),
) -> CreateAlveroApplicationResponse:
    raw = await request.body()
    _verify_signature(raw, x_alvero_tenant, x_alvero_timestamp, x_alvero_signature, request.method, request.url.path)
    body = CreateAlveroApplicationBody.model_validate_json(raw)
    if body.tenant_slug != x_alvero_tenant:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="tenant_mismatch")

    existing = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.treatment_plan_ref == body.alvero_application_id)
        .first()
    )
    if existing is not None:
        return _response(existing)

    product = _active_dental_product(db, body.requested_amount_cents)
    patient = _find_or_create_patient(db, body)
    vendor = _vendor(db, body.provider_external_id)

    application = PlatformCreditApplication(
        patient_id=patient.id,
        credit_product_id=product.id,
        credit_product_version=product.version,
        product_config_snapshot=product.verification_matrix,
        requested_amount_cents=body.requested_amount_cents,
        requested_amount_source="clinic",
        clinic_proposed_amount_cents=body.requested_amount_cents,
        patient_proposed_amount_cents=None,
        vendor_id=vendor.id if vendor else None,
        treatment_plan_ref=body.alvero_application_id,
        status="started",
        flow_state={
            "source": "alvero",
            "tenant_slug": body.tenant_slug,
            "purpose": body.purpose,
            "requested_term_months": body.requested_term_months,
        },
    )
    db.add(application)
    db.commit()
    db.refresh(application)
    return _response(application)
