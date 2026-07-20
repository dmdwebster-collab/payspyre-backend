"""Admin surface for WS-J borrower-portal depth — the STAFF-ONLY halves.

The borrower portal deliberately withholds these operations; they live here,
staff/admin-gated:

* ID-document READS — the borrower can only upload; staff get the metadata
  list and short-TTL presigned download URLs, every download audited to
  ``platform_events`` (Dave: back-office file of new + former IDs).
* Bank-account ADD / REMOVE — Dave: borrowers "cannot delete the bank
  accounts... we do not want them to add bank accounts". Adds record the
  provenance (``flinks`` re-verification or ``manual_staff`` void-cheque/PAD
  review); removals are soft (status='removed', mandatory reason) so the
  history survives.
* Per-patient 2FA ENFORCEMENT flag — staff can require a borrower to enroll.
* Payout-request QUEUE + respond — the staff task the borrower's payout
  inquiry created; the figure comes from the WS-I forward calculator, this
  endpoint just records the response.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_roles
from app.core.logging import get_logger
from app.db.base import get_db
from app.models.platform.borrower_portal import (
    PlatformPatientBankAccount,
    PlatformPatientIdDocument,
    PlatformPatientSecondFactor,
    PlatformPayoutRequest,
)
from app.models.platform.event import PlatformEvent
from app.models.platform.patient import PlatformPatient
from app.services import document_storage
from app.services.borrower_portal import mask_identifier

logger = get_logger(__name__)
router = APIRouter(dependencies=[Depends(require_roles("admin", "staff"))])


# --- schemas ----------------------------------------------------------------


class AdminIdDocOut(BaseModel):
    document_id: UUID
    bucket: str
    filename: Optional[str] = None
    size_bytes: Optional[int] = None
    content_type: Optional[str] = None
    status: str
    is_current: bool
    uploaded_at: Optional[datetime] = None
    created_at: datetime


class AdminIdDocDownload(BaseModel):
    document_id: UUID
    download_url: str
    expires_in_seconds: int = 300


class AdminBankAccountBody(BaseModel):
    institution_name: str = Field(..., min_length=1, max_length=200)
    currency: str = Field("CAD", min_length=3, max_length=3)
    account_type: Optional[str] = Field(None, max_length=100)
    # Staff paste the identifiers; ONLY masks are stored.
    routing_number: Optional[str] = Field(None, max_length=20)
    account_number: str = Field(..., min_length=2, max_length=30)
    verified_via: str = Field(..., pattern="^(flinks|manual_staff)$")
    external_ref: Optional[str] = Field(None, max_length=200)
    make_default: bool = False


class AdminBankAccountOut(BaseModel):
    account_id: UUID
    institution_name: str
    currency: str
    account_type: Optional[str] = None
    routing_mask: Optional[str] = None
    account_mask: str
    verified_via: str
    is_default: bool
    status: str


class RemoveBankAccountBody(BaseModel):
    reason: str = Field(..., min_length=3, max_length=500)


class SecondFactorEnforceBody(BaseModel):
    enforced: bool


class SecondFactorStateOut(BaseModel):
    patient_id: UUID
    enrolled: bool
    method: Optional[str] = None
    status: Optional[str] = None
    enforced: bool


class PayoutRequestAdminOut(BaseModel):
    request_id: UUID
    patient_id: UUID
    loan_id: UUID
    requested_payout_date: str
    note: Optional[str] = None
    status: str
    quoted_amount_cents: Optional[int] = None
    response_note: Optional[str] = None
    responded_by: Optional[str] = None
    created_at: datetime


class PayoutRespondBody(BaseModel):
    quoted_amount_cents: int = Field(..., gt=0)
    response_note: Optional[str] = Field(None, max_length=1000)


# --- helpers ----------------------------------------------------------------


def _require_patient(db: Session, patient_id: UUID) -> PlatformPatient:
    patient = db.query(PlatformPatient).filter(PlatformPatient.id == patient_id).first()
    if patient is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Patient not found")
    return patient


def _bank_out(row: PlatformPatientBankAccount) -> AdminBankAccountOut:
    return AdminBankAccountOut(
        account_id=row.id,
        institution_name=row.institution_name,
        currency=row.currency,
        account_type=row.account_type,
        routing_mask=row.routing_mask,
        account_mask=row.account_mask,
        verified_via=row.verified_via,
        is_default=row.is_default,
        status=row.status,
    )


# --- ID documents (staff reads, audited) ------------------------------------


@router.get("/patients/{patient_id}/id-documents", response_model=list[AdminIdDocOut])
def list_patient_id_documents(
    patient_id: UUID,
    db: Session = Depends(get_db),
):
    """The ongoing new+former ID file for one patient (metadata)."""
    _require_patient(db, patient_id)
    docs = (
        db.query(PlatformPatientIdDocument)
        .filter(PlatformPatientIdDocument.patient_id == patient_id)
        .order_by(PlatformPatientIdDocument.created_at.desc())
        .all()
    )
    return [
        AdminIdDocOut(
            document_id=d.id,
            bucket=d.bucket,
            filename=d.filename,
            size_bytes=d.size_bytes,
            content_type=d.content_type,
            status=d.status,
            is_current=d.is_current,
            uploaded_at=d.uploaded_at,
            created_at=d.created_at,
        )
        for d in docs
    ]


@router.get(
    "/patients/{patient_id}/id-documents/{document_id}/download-url",
    response_model=AdminIdDocDownload,
)
def get_id_document_download_url(
    patient_id: UUID,
    document_id: UUID,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Short-TTL presigned GET for a staff reviewer. EVERY issuance is audited
    (who / which document) to platform_events. 503 until storage is configured.

    This is the ONLY read path for ID images anywhere in the system — the
    borrower surface has none."""
    doc = (
        db.query(PlatformPatientIdDocument)
        .filter(
            PlatformPatientIdDocument.id == document_id,
            PlatformPatientIdDocument.patient_id == patient_id,
        )
        .first()
    )
    if doc is None or doc.status != "uploaded":
        raise HTTPException(status_code=404, detail="Document not found")
    if not document_storage.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Document storage is not configured.",
        )
    url = document_storage.presigned_get_url(doc.object_key)
    db.add(
        PlatformEvent(
            event_type="id_document_download_url_issued",
            actor="admin",
            patient_id=patient_id,
            payload={
                "v": 1,
                "actor": {"type": "staff", "id": str(getattr(user, "id", "unknown"))},
                "patient_id": str(patient_id),
                "document_id": str(document_id),
                "bucket": doc.bucket,
            },
        )
    )
    db.commit()
    logger.info(
        "id_document_download_url_issued",
        patient_id=str(patient_id),
        document_id=str(document_id),
        staff_id=str(getattr(user, "id", "unknown")),
    )
    return AdminIdDocDownload(document_id=doc.id, download_url=url)


# --- bank accounts (staff-only mutations) -----------------------------------


@router.get("/patients/{patient_id}/bank-accounts", response_model=list[AdminBankAccountOut])
def list_patient_bank_accounts(
    patient_id: UUID,
    db: Session = Depends(get_db),
):
    """All of a patient's accounts including removed ones (audit view)."""
    _require_patient(db, patient_id)
    rows = (
        db.query(PlatformPatientBankAccount)
        .filter(PlatformPatientBankAccount.patient_id == patient_id)
        .order_by(PlatformPatientBankAccount.created_at.asc())
        .all()
    )
    return [_bank_out(r) for r in rows]


@router.post(
    "/patients/{patient_id}/bank-accounts",
    response_model=AdminBankAccountOut,
    status_code=status.HTTP_201_CREATED,
)
def add_patient_bank_account(
    patient_id: UUID,
    body: AdminBankAccountBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Staff-add a verified payment account (Flinks result or reviewed
    void-cheque/PAD). Full identifiers are masked BEFORE persistence — only
    display masks are ever stored."""
    _require_patient(db, patient_id)
    row = PlatformPatientBankAccount(
        patient_id=patient_id,
        institution_name=body.institution_name,
        currency=body.currency.upper(),
        account_type=body.account_type,
        routing_mask=mask_identifier(body.routing_number, keep=2),
        account_mask=mask_identifier(body.account_number, keep=4),
        verified_via=body.verified_via,
        external_ref=body.external_ref,
        is_default=False,
        status="active",
        added_by=str(getattr(user, "id", "unknown")),
    )
    db.add(row)
    db.flush()
    if body.make_default:
        others = (
            db.query(PlatformPatientBankAccount)
            .filter(
                PlatformPatientBankAccount.patient_id == patient_id,
                PlatformPatientBankAccount.is_default.is_(True),
                PlatformPatientBankAccount.id != row.id,
            )
            .all()
        )
        for other in others:
            other.is_default = False
        db.flush()
        row.is_default = True
    db.add(
        PlatformEvent(
            event_type="patient_bank_account_added",
            actor="admin",
            patient_id=patient_id,
            payload={
                "v": 1,
                "actor": {"type": "staff", "id": str(getattr(user, "id", "unknown"))},
                "patient_id": str(patient_id),
                "account_id": str(row.id),
                "verified_via": body.verified_via,
                "account_mask": row.account_mask,
                "made_default": body.make_default,
            },
        )
    )
    db.commit()
    db.refresh(row)
    return _bank_out(row)


@router.post(
    "/patients/{patient_id}/bank-accounts/{account_id}/remove",
    response_model=AdminBankAccountOut,
)
def remove_patient_bank_account(
    patient_id: UUID,
    account_id: UUID,
    body: RemoveBankAccountBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Soft-remove (status='removed', reason mandatory). The row survives for
    audit; a removed default leaves the patient with NO default until one is
    designated again."""
    row = (
        db.query(PlatformPatientBankAccount)
        .filter(
            PlatformPatientBankAccount.id == account_id,
            PlatformPatientBankAccount.patient_id == patient_id,
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if row.status == "removed":
        raise HTTPException(status_code=409, detail="Account already removed")
    row.status = "removed"
    row.is_default = False
    row.removed_by = str(getattr(user, "id", "unknown"))
    row.removed_reason = body.reason
    row.removed_at = datetime.now(timezone.utc)
    db.add(
        PlatformEvent(
            event_type="patient_bank_account_removed",
            actor="admin",
            patient_id=patient_id,
            payload={
                "v": 1,
                "actor": {"type": "staff", "id": str(getattr(user, "id", "unknown"))},
                "patient_id": str(patient_id),
                "account_id": str(row.id),
                "reason": body.reason,
            },
        )
    )
    db.commit()
    db.refresh(row)
    return _bank_out(row)


# --- 2FA enforcement --------------------------------------------------------


@router.patch("/patients/{patient_id}/second-factor", response_model=SecondFactorStateOut)
def set_second_factor_enforcement(
    patient_id: UUID,
    body: SecondFactorEnforceBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Set the per-patient 2FA enforcement flag. Enforcing an unenrolled
    patient blocks their sensitive actions until they enroll (the portal
    surfaces the enrollment requirement)."""
    _require_patient(db, patient_id)
    row = (
        db.query(PlatformPatientSecondFactor)
        .filter(PlatformPatientSecondFactor.patient_id == patient_id)
        .first()
    )
    if row is None:
        # Enforcement ahead of enrollment: hold the flag on a method-less
        # placeholder ('sms' pending — replaced wholesale when the borrower
        # actually enrolls).
        row = PlatformPatientSecondFactor(
            patient_id=patient_id, method="sms", status="pending"
        )
        db.add(row)
    row.enforced = body.enforced
    db.add(
        PlatformEvent(
            event_type="second_factor_enforcement_changed",
            actor="admin",
            patient_id=patient_id,
            payload={
                "v": 1,
                "actor": {"type": "staff", "id": str(getattr(user, "id", "unknown"))},
                "patient_id": str(patient_id),
                "enforced": body.enforced,
            },
        )
    )
    db.commit()
    db.refresh(row)
    return SecondFactorStateOut(
        patient_id=patient_id,
        enrolled=row.status == "active",
        method=row.method,
        status=row.status,
        enforced=row.enforced,
    )


# --- payout-request queue ---------------------------------------------------


def _payout_admin_out(row: PlatformPayoutRequest) -> PayoutRequestAdminOut:
    return PayoutRequestAdminOut(
        request_id=row.id,
        patient_id=row.patient_id,
        loan_id=row.loan_id,
        requested_payout_date=row.requested_payout_date.isoformat(),
        note=row.note,
        status=row.status,
        quoted_amount_cents=row.quoted_amount_cents,
        response_note=row.response_note,
        responded_by=row.responded_by,
        created_at=row.created_at,
    )


@router.get("/payout-requests", response_model=list[PayoutRequestAdminOut])
def list_payout_requests(
    status_filter: Optional[str] = Query(None, alias="status"),
    db: Session = Depends(get_db),
):
    """The staff payout-figure queue, oldest first (work order)."""
    q = db.query(PlatformPayoutRequest)
    if status_filter is not None:
        if status_filter not in ("open", "responded", "cancelled"):
            raise HTTPException(status_code=422, detail="Invalid status filter")
        q = q.filter(PlatformPayoutRequest.status == status_filter)
    return [_payout_admin_out(r) for r in q.order_by(PlatformPayoutRequest.created_at.asc()).all()]


@router.post("/payout-requests/{request_id}/respond", response_model=PayoutRequestAdminOut)
def respond_payout_request(
    request_id: UUID,
    body: PayoutRespondBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Record the payout figure (computed with the WS-I forward calculator)
    against an OPEN request. Scheduled payments remain untouched — responding
    to an inquiry never suspends anything."""
    row = (
        db.query(PlatformPayoutRequest)
        .filter(PlatformPayoutRequest.id == request_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Payout request not found")
    if row.status != "open":
        raise HTTPException(status_code=409, detail=f"Request is already {row.status}")
    row.status = "responded"
    row.quoted_amount_cents = body.quoted_amount_cents
    row.response_note = body.response_note
    row.responded_by = str(getattr(user, "id", "unknown"))
    row.responded_at = datetime.now(timezone.utc)
    db.add(
        PlatformEvent(
            event_type="payout_request_responded",
            actor="admin",
            patient_id=row.patient_id,
            payload={
                "v": 1,
                "actor": {"type": "staff", "id": str(getattr(user, "id", "unknown"))},
                "patient_id": str(row.patient_id),
                "loan_id": str(row.loan_id),
                "payout_request_id": str(row.id),
                "quoted_amount_cents": body.quoted_amount_cents,
            },
        )
    )
    db.commit()
    db.refresh(row)
    return _payout_admin_out(row)
