"""Admin Vendor CRM (WS-G — video 09 ``/tools/vendors``).

Everything TL's vendor detail screen carries, on our single ``vendors`` table:
industry categories (directory + per-vendor link), contacts, bank accounts,
MSA/contract documents with expiry tracking, the onboarding checklist, vendor
users + the 9-role permission matrix, the vendor→provider→loan chaining view,
and the per-vendor portfolio report (reusing the clinic dashboard blocks).

Mounted at ``/api/v1/admin/crm`` (see ``app/api/v1/api.py``). Reads are
admin/staff; writes are admin-only (bank accounts and role assignments are
sensitive).

PII / money notes:
* Bank accounts are MASKED AT REST: create accepts a full ``account_number``
  but persists ONLY the last 4 (plus institution/transit). Responses always
  render ``•••• last4``. Full account capture rides the wave-2 Zumrails wallet
  work (money-path, human-reviewed).
* Document bytes never pass through the API — presigned PUT/GET against
  Spaces, exactly like applicant KYC documents (503 until SPACES_* is set).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.api.clinic.v1.endpoints.dashboard_loanbook import (
    loan_book_block,
    payments_block,
)
from app.core.auth import get_current_user, require_roles
from app.db.base import get_db
from app.models.loan import Vendor
from app.models.platform.clinic_membership import PlatformClinicMembership
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.crm import (
    ONBOARDING_STATUSES,
    VENDOR_DOCUMENT_TYPES,
    PlatformClinicRole,
    PlatformIndustryCategory,
    PlatformVendorBankAccount,
    PlatformVendorContact,
    PlatformVendorDocument,
    PlatformVendorOnboarding,
)
from app.models.platform.event import PlatformEvent
from app.models.platform.loan import PlatformLoan, PlatformLoanTransaction
from app.models.platform.patient import PlatformPatient
from app.models.user import User
from app.services import document_storage
from app.services.clinic_permissions import validate_role_assignment

router = APIRouter(dependencies=[Depends(require_roles("admin", "staff"))])

ADMIN_ONLY = Depends(require_roles("admin"))


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def mask_account(last4: str) -> str:
    """Masked display for a bank account: ``•••• 1234``."""
    return f"•••• {last4}"


def account_last4(account_number: str) -> str:
    """PURE: the last 4 digits of a (possibly formatted) account number."""
    digits = "".join(ch for ch in account_number if ch.isdigit())
    if len(digits) < 4:
        raise ValueError("Account number must contain at least 4 digits.")
    return digits[-4:]


def next_onboarding_statuses(current: str) -> list[str]:
    """PURE: statuses reachable from ``current`` (forward-only, any distance)."""
    try:
        idx = ONBOARDING_STATUSES.index(current)
    except ValueError:
        return []
    return list(ONBOARDING_STATUSES[idx + 1 :])


_ONBOARDING_TS_FIELD = {
    "invited": "invited_at",
    "docs_collected": "docs_collected_at",
    "msa_signed": "msa_signed_at",
    "live": "live_at",
}


def _get_vendor_or_404(db: Session, vendor_id: UUID) -> Vendor:
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if vendor is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vendor not found")
    return vendor


def _actor_id(user) -> str:
    return str(getattr(user, "id", "") or getattr(user, "email", "") or "unknown")


def _emit(db: Session, *, event_type: str, actor_id: str, after: dict) -> None:
    """§6-shaped audit row for an admin CRM action (no patient/application)."""
    db.add(
        PlatformEvent(
            event_type=event_type,
            actor="admin",
            payload={
                "v": 1,
                "actor": {"type": "admin", "id": actor_id},
                "before": {},
                "after": after,
            },
        )
    )


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class IndustryCategoryOut(BaseModel):
    id: UUID
    name: str
    active: bool
    sort_order: int


class IndustryCategoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1, max_length=100)
    sort_order: int = 0


class IndustryCategoryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    active: Optional[bool] = None
    sort_order: Optional[int] = None


class VendorIndustryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    industry_category_id: Optional[UUID] = None


class ContactOut(BaseModel):
    id: UUID
    vendor_id: UUID
    name: str
    position: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    is_primary: bool
    created_at: datetime


class ContactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1, max_length=255)
    position: Optional[str] = Field(None, max_length=255)
    phone: Optional[str] = Field(None, max_length=30)
    email: Optional[str] = Field(None, max_length=255)
    is_primary: bool = False


class ContactPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    position: Optional[str] = Field(None, max_length=255)
    phone: Optional[str] = Field(None, max_length=30)
    email: Optional[str] = Field(None, max_length=255)
    is_primary: Optional[bool] = None


class BankAccountOut(BaseModel):
    id: UUID
    vendor_id: UUID
    bank_name: str
    institution_number: Optional[str] = None
    transit_number: Optional[str] = None
    account_number_masked: str
    account_holder: Optional[str] = None
    is_primary: bool
    created_at: datetime


class BankAccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bank_name: str = Field(..., min_length=1, max_length=255)
    institution_number: Optional[str] = Field(None, min_length=1, max_length=3)
    transit_number: Optional[str] = Field(None, min_length=1, max_length=5)
    # Full number accepted transiently; ONLY the last 4 digits are persisted.
    account_number: str = Field(..., min_length=4, max_length=34)
    account_holder: Optional[str] = Field(None, max_length=255)
    is_primary: bool = False


class DocumentOut(BaseModel):
    id: UUID
    vendor_id: UUID
    doc_type: str
    title: str
    status: str
    content_type: Optional[str] = None
    effective_date: Optional[date] = None
    expiry_date: Optional[date] = None
    uploaded_at: Optional[datetime] = None
    created_at: datetime


class DocumentUploadBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doc_type: str
    title: str = Field(..., min_length=1, max_length=255)
    content_type: str = Field("application/pdf", max_length=100)
    effective_date: Optional[date] = None
    expiry_date: Optional[date] = None


class DocumentUploadOut(BaseModel):
    document_id: UUID
    upload_url: str
    object_key: str


class DocumentConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    effective_date: Optional[date] = None
    expiry_date: Optional[date] = None


class OnboardingOut(BaseModel):
    vendor_id: UUID
    status: str
    invited_at: Optional[datetime] = None
    docs_collected_at: Optional[datetime] = None
    msa_signed_at: Optional[datetime] = None
    live_at: Optional[datetime] = None
    note: Optional[str] = None
    next_statuses: list[str]


class OnboardingAdvanceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    note: Optional[str] = None


class ClinicRoleOut(BaseModel):
    key: str
    name: str
    description: Optional[str] = None
    is_addon: bool
    sort_order: int


class VendorUserOut(BaseModel):
    membership_id: UUID
    user_id: UUID
    email: Optional[str] = None
    full_name: Optional[str] = None
    role: str
    roles: Optional[list[str]] = None  # None = legacy full access
    created_at: datetime


class RolesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roles: list[str]


# Vendor lifecycle states (mirrors the ``vendor_status`` enum on ``vendors``).
VENDOR_STATUSES = ("pending", "active", "suspended", "terminated")


class VendorDirectoryItem(BaseModel):
    id: UUID
    business_name: str
    dba_name: Optional[str] = None
    status: str


class VendorDirectoryOut(BaseModel):
    vendors: list[VendorDirectoryItem]
    count: int  # total matching the filter (ignores limit/offset — for paging)
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Vendor directory (searchable list — the entry point into every workspace)
# ---------------------------------------------------------------------------


@router.get("/vendors", response_model=VendorDirectoryOut)
def list_vendors(
    q: Optional[str] = Query(
        None,
        max_length=255,
        description="Case-insensitive substring match on business / operating (DBA) name.",
    ),
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Searchable vendor directory — replaces "paste a UUID" in the CRM UI.

    Admin/staff read (router-level ``require_roles``). Returns a page of vendors
    plus ``count`` (the total matching the filter, so the UI can paginate).
    Ordered by business name. The ``q`` filter is a parameter-bound ILIKE over
    ``business_name`` and ``dba_name`` (SQLAlchemy binds the pattern — static
    SQL, no interpolation; bandit B608-safe).
    """
    query = db.query(Vendor)

    if q is not None and q.strip():
        pattern = f"%{q.strip()}%"
        query = query.filter(
            or_(
                Vendor.business_name.ilike(pattern),
                Vendor.dba_name.ilike(pattern),
            )
        )
    if status_filter is not None:
        if status_filter not in VENDOR_STATUSES:
            raise HTTPException(
                status_code=422,
                detail=f"status must be one of {', '.join(VENDOR_STATUSES)}",
            )
        query = query.filter(Vendor.status == status_filter)

    count = query.count()
    rows = (
        query.order_by(Vendor.business_name, Vendor.created_at)
        .offset(offset)
        .limit(limit)
        .all()
    )
    return VendorDirectoryOut(
        vendors=[
            VendorDirectoryItem(
                id=r.id,
                business_name=r.business_name,
                dba_name=r.dba_name,
                status=r.status,
            )
            for r in rows
        ],
        count=count,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Industry categories directory
# ---------------------------------------------------------------------------


@router.get("/industry-categories", response_model=list[IndustryCategoryOut])
def list_industry_categories(
    include_inactive: bool = Query(False),
    db: Session = Depends(get_db),
):
    q = db.query(PlatformIndustryCategory)
    if not include_inactive:
        q = q.filter(PlatformIndustryCategory.active.is_(True))
    rows = q.order_by(
        PlatformIndustryCategory.sort_order, PlatformIndustryCategory.name
    ).all()
    return [
        IndustryCategoryOut(
            id=r.id, name=r.name, active=r.active, sort_order=r.sort_order
        )
        for r in rows
    ]


@router.post(
    "/industry-categories",
    response_model=IndustryCategoryOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[ADMIN_ONLY],
)
def create_industry_category(
    body: IndustryCategoryCreate,
    db: Session = Depends(get_db),
):
    exists = (
        db.query(PlatformIndustryCategory)
        .filter(func.lower(PlatformIndustryCategory.name) == body.name.strip().lower())
        .first()
    )
    if exists is not None:
        raise HTTPException(status_code=409, detail="Industry category already exists")
    row = PlatformIndustryCategory(
        id=uuid4(), name=body.name.strip(), sort_order=body.sort_order
    )
    db.add(row)
    db.commit()
    return IndustryCategoryOut(
        id=row.id, name=row.name, active=row.active, sort_order=row.sort_order
    )


@router.patch(
    "/industry-categories/{category_id}",
    response_model=IndustryCategoryOut,
    dependencies=[ADMIN_ONLY],
)
def patch_industry_category(
    category_id: UUID,
    body: IndustryCategoryPatch,
    db: Session = Depends(get_db),
):
    row = (
        db.query(PlatformIndustryCategory)
        .filter(PlatformIndustryCategory.id == category_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Industry category not found")
    if body.name is not None:
        row.name = body.name.strip()
    if body.active is not None:
        row.active = body.active
    if body.sort_order is not None:
        row.sort_order = body.sort_order
    db.commit()
    return IndustryCategoryOut(
        id=row.id, name=row.name, active=row.active, sort_order=row.sort_order
    )


@router.put(
    "/vendors/{vendor_id}/industry-category",
    dependencies=[ADMIN_ONLY],
)
def set_vendor_industry_category(
    vendor_id: UUID,
    body: VendorIndustryBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    vendor = _get_vendor_or_404(db, vendor_id)
    if body.industry_category_id is not None:
        cat = (
            db.query(PlatformIndustryCategory)
            .filter(
                PlatformIndustryCategory.id == body.industry_category_id,
                PlatformIndustryCategory.active.is_(True),
            )
            .first()
        )
        if cat is None:
            raise HTTPException(status_code=422, detail="Unknown industry category")
    vendor.industry_category_id = body.industry_category_id
    _emit(
        db,
        event_type="vendor_industry_category_set",
        actor_id=_actor_id(user),
        after={
            "vendor_id": str(vendor_id),
            "industry_category_id": (
                str(body.industry_category_id) if body.industry_category_id else None
            ),
        },
    )
    db.commit()
    return {
        "vendor_id": str(vendor_id),
        "industry_category_id": (
            str(body.industry_category_id) if body.industry_category_id else None
        ),
    }


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------


def _contact_out(row: PlatformVendorContact) -> ContactOut:
    return ContactOut(
        id=row.id,
        vendor_id=row.vendor_id,
        name=row.name,
        position=row.position,
        phone=row.phone,
        email=row.email,
        is_primary=row.is_primary,
        created_at=row.created_at,
    )


@router.get("/vendors/{vendor_id}/contacts", response_model=list[ContactOut])
def list_contacts(vendor_id: UUID, db: Session = Depends(get_db)):
    _get_vendor_or_404(db, vendor_id)
    rows = (
        db.query(PlatformVendorContact)
        .filter(
            PlatformVendorContact.vendor_id == vendor_id,
            PlatformVendorContact.deleted_at.is_(None),
        )
        .order_by(PlatformVendorContact.is_primary.desc(), PlatformVendorContact.created_at)
        .all()
    )
    return [_contact_out(r) for r in rows]


def _clear_primary_contact(db: Session, vendor_id: UUID) -> None:
    db.query(PlatformVendorContact).filter(
        PlatformVendorContact.vendor_id == vendor_id,
        PlatformVendorContact.deleted_at.is_(None),
        PlatformVendorContact.is_primary.is_(True),
    ).update({PlatformVendorContact.is_primary: False}, synchronize_session=False)


@router.post(
    "/vendors/{vendor_id}/contacts",
    response_model=ContactOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[ADMIN_ONLY],
)
def create_contact(
    vendor_id: UUID,
    body: ContactCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _get_vendor_or_404(db, vendor_id)
    if body.is_primary:
        _clear_primary_contact(db, vendor_id)
    row = PlatformVendorContact(
        vendor_id=vendor_id,
        name=body.name.strip(),
        position=body.position,
        phone=body.phone,
        email=body.email,
        is_primary=body.is_primary,
        created_by=_actor_id(user),
    )
    db.add(row)
    db.commit()
    return _contact_out(row)


@router.patch(
    "/vendors/{vendor_id}/contacts/{contact_id}",
    response_model=ContactOut,
    dependencies=[ADMIN_ONLY],
)
def patch_contact(
    vendor_id: UUID,
    contact_id: UUID,
    body: ContactPatch,
    db: Session = Depends(get_db),
):
    row = (
        db.query(PlatformVendorContact)
        .filter(
            PlatformVendorContact.id == contact_id,
            PlatformVendorContact.vendor_id == vendor_id,
            PlatformVendorContact.deleted_at.is_(None),
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    if body.is_primary:
        _clear_primary_contact(db, vendor_id)
    for field in ("name", "position", "phone", "email", "is_primary"):
        value = getattr(body, field)
        if value is not None:
            setattr(row, field, value)
    db.commit()
    return _contact_out(row)


@router.delete(
    "/vendors/{vendor_id}/contacts/{contact_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[ADMIN_ONLY],
)
def delete_contact(vendor_id: UUID, contact_id: UUID, db: Session = Depends(get_db)):
    row = (
        db.query(PlatformVendorContact)
        .filter(
            PlatformVendorContact.id == contact_id,
            PlatformVendorContact.vendor_id == vendor_id,
            PlatformVendorContact.deleted_at.is_(None),
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    row.deleted_at = datetime.now(timezone.utc)
    db.commit()


# ---------------------------------------------------------------------------
# Bank accounts (masked)
# ---------------------------------------------------------------------------


def _bank_out(row: PlatformVendorBankAccount) -> BankAccountOut:
    return BankAccountOut(
        id=row.id,
        vendor_id=row.vendor_id,
        bank_name=row.bank_name,
        institution_number=row.institution_number,
        transit_number=row.transit_number,
        account_number_masked=mask_account(row.account_number_last4),
        account_holder=row.account_holder,
        is_primary=row.is_primary,
        created_at=row.created_at,
    )


def _clear_primary_bank(db: Session, vendor_id: UUID) -> None:
    db.query(PlatformVendorBankAccount).filter(
        PlatformVendorBankAccount.vendor_id == vendor_id,
        PlatformVendorBankAccount.deleted_at.is_(None),
        PlatformVendorBankAccount.is_primary.is_(True),
    ).update({PlatformVendorBankAccount.is_primary: False}, synchronize_session=False)


@router.get("/vendors/{vendor_id}/bank-accounts", response_model=list[BankAccountOut])
def list_bank_accounts(vendor_id: UUID, db: Session = Depends(get_db)):
    _get_vendor_or_404(db, vendor_id)
    rows = (
        db.query(PlatformVendorBankAccount)
        .filter(
            PlatformVendorBankAccount.vendor_id == vendor_id,
            PlatformVendorBankAccount.deleted_at.is_(None),
        )
        .order_by(
            PlatformVendorBankAccount.is_primary.desc(),
            PlatformVendorBankAccount.created_at,
        )
        .all()
    )
    return [_bank_out(r) for r in rows]


@router.post(
    "/vendors/{vendor_id}/bank-accounts",
    response_model=BankAccountOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[ADMIN_ONLY],
)
def create_bank_account(
    vendor_id: UUID,
    body: BankAccountCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Add a vendor bank account. Persists ONLY the last 4 digits of the
    account number (masked at rest — see module docstring)."""
    _get_vendor_or_404(db, vendor_id)
    try:
        last4 = account_last4(body.account_number)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if body.is_primary:
        _clear_primary_bank(db, vendor_id)
    row = PlatformVendorBankAccount(
        vendor_id=vendor_id,
        bank_name=body.bank_name.strip(),
        institution_number=body.institution_number,
        transit_number=body.transit_number,
        account_number_last4=last4,
        account_holder=body.account_holder,
        is_primary=body.is_primary,
        created_by=_actor_id(user),
    )
    db.add(row)
    _emit(
        db,
        event_type="vendor_bank_account_added",
        actor_id=_actor_id(user),
        after={
            "vendor_id": str(vendor_id),
            "bank_name": row.bank_name,
            "account_number_masked": mask_account(last4),
        },
    )
    db.commit()
    return _bank_out(row)


@router.post(
    "/vendors/{vendor_id}/bank-accounts/{account_id}/make-primary",
    response_model=BankAccountOut,
    dependencies=[ADMIN_ONLY],
)
def make_primary_bank_account(
    vendor_id: UUID, account_id: UUID, db: Session = Depends(get_db)
):
    row = (
        db.query(PlatformVendorBankAccount)
        .filter(
            PlatformVendorBankAccount.id == account_id,
            PlatformVendorBankAccount.vendor_id == vendor_id,
            PlatformVendorBankAccount.deleted_at.is_(None),
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Bank account not found")
    _clear_primary_bank(db, vendor_id)
    row.is_primary = True
    db.commit()
    return _bank_out(row)


@router.delete(
    "/vendors/{vendor_id}/bank-accounts/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[ADMIN_ONLY],
)
def delete_bank_account(
    vendor_id: UUID,
    account_id: UUID,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    row = (
        db.query(PlatformVendorBankAccount)
        .filter(
            PlatformVendorBankAccount.id == account_id,
            PlatformVendorBankAccount.vendor_id == vendor_id,
            PlatformVendorBankAccount.deleted_at.is_(None),
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Bank account not found")
    row.deleted_at = datetime.now(timezone.utc)
    _emit(
        db,
        event_type="vendor_bank_account_removed",
        actor_id=_actor_id(user),
        after={
            "vendor_id": str(vendor_id),
            "account_number_masked": mask_account(row.account_number_last4),
        },
    )
    db.commit()


# ---------------------------------------------------------------------------
# Documents (MSA / contracts) — presigned upload, expiry tracking
# ---------------------------------------------------------------------------


def _doc_out(row: PlatformVendorDocument) -> DocumentOut:
    return DocumentOut(
        id=row.id,
        vendor_id=row.vendor_id,
        doc_type=row.doc_type,
        title=row.title,
        status=row.status,
        content_type=row.content_type,
        effective_date=row.effective_date,
        expiry_date=row.expiry_date,
        uploaded_at=row.uploaded_at,
        created_at=row.created_at,
    )


@router.get("/vendors/{vendor_id}/documents", response_model=list[DocumentOut])
def list_vendor_documents(vendor_id: UUID, db: Session = Depends(get_db)):
    _get_vendor_or_404(db, vendor_id)
    rows = (
        db.query(PlatformVendorDocument)
        .filter(
            PlatformVendorDocument.vendor_id == vendor_id,
            PlatformVendorDocument.deleted_at.is_(None),
        )
        .order_by(PlatformVendorDocument.created_at.desc())
        .all()
    )
    return [_doc_out(r) for r in rows]


@router.post(
    "/vendors/{vendor_id}/documents/upload-url",
    response_model=DocumentUploadOut,
    dependencies=[ADMIN_ONLY],
)
def vendor_document_upload_url(
    vendor_id: UUID,
    body: DocumentUploadBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Issue a short-TTL presigned PUT for an MSA/contract upload.

    Mirrors the applicant KYC upload flow: bytes go browser→Spaces directly;
    the DB stores only the object key. 503 until SPACES_* is configured.
    """
    _get_vendor_or_404(db, vendor_id)
    if body.doc_type not in VENDOR_DOCUMENT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"doc_type must be one of {', '.join(VENDOR_DOCUMENT_TYPES)}",
        )
    if not document_storage.is_configured():
        raise HTTPException(
            status_code=503, detail="Document storage is not configured"
        )
    document_id = uuid4()
    object_key = f"vendors/{vendor_id}/{body.doc_type}/{document_id}"
    row = PlatformVendorDocument(
        id=document_id,
        vendor_id=vendor_id,
        doc_type=body.doc_type,
        title=body.title.strip(),
        object_key=object_key,
        content_type=body.content_type,
        status="pending",
        effective_date=body.effective_date,
        expiry_date=body.expiry_date,
        created_by=_actor_id(user),
    )
    db.add(row)
    upload_url = document_storage.presigned_put_url(object_key, body.content_type)
    db.commit()
    return DocumentUploadOut(
        document_id=document_id, upload_url=upload_url, object_key=object_key
    )


@router.post(
    "/vendors/{vendor_id}/documents/{document_id}/confirm",
    response_model=DocumentOut,
    dependencies=[ADMIN_ONLY],
)
def confirm_vendor_document(
    vendor_id: UUID,
    document_id: UUID,
    body: DocumentConfirmBody | None = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Mark an upload complete (+ optional expiry dates) and run the
    onboarding automation: any confirmed doc advances ``invited →
    docs_collected``; a confirmed MSA advances to ``msa_signed``."""
    row = (
        db.query(PlatformVendorDocument)
        .filter(
            PlatformVendorDocument.id == document_id,
            PlatformVendorDocument.vendor_id == vendor_id,
            PlatformVendorDocument.deleted_at.is_(None),
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found")
    now = datetime.now(timezone.utc)
    row.status = "uploaded"
    row.uploaded_at = row.uploaded_at or now
    if body is not None:
        if body.effective_date is not None:
            row.effective_date = body.effective_date
        if body.expiry_date is not None:
            row.expiry_date = body.expiry_date

    # Onboarding automation (only ever moves FORWARD).
    target = "msa_signed" if row.doc_type == "msa" else "docs_collected"
    _advance_onboarding(db, vendor_id, target, actor_id=_actor_id(user), now=now)

    _emit(
        db,
        event_type="vendor_document_uploaded",
        actor_id=_actor_id(user),
        after={
            "vendor_id": str(vendor_id),
            "document_id": str(document_id),
            "doc_type": row.doc_type,
            "expiry_date": row.expiry_date.isoformat() if row.expiry_date else None,
        },
    )
    db.commit()
    return _doc_out(row)


@router.get("/vendors/{vendor_id}/documents/{document_id}/download-url")
def vendor_document_download_url(
    vendor_id: UUID, document_id: UUID, db: Session = Depends(get_db)
):
    row = (
        db.query(PlatformVendorDocument)
        .filter(
            PlatformVendorDocument.id == document_id,
            PlatformVendorDocument.vendor_id == vendor_id,
            PlatformVendorDocument.deleted_at.is_(None),
            PlatformVendorDocument.status == "uploaded",
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found")
    if not document_storage.is_configured():
        raise HTTPException(status_code=503, detail="Document storage is not configured")
    return {"download_url": document_storage.presigned_get_url(row.object_key)}


@router.delete(
    "/vendors/{vendor_id}/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[ADMIN_ONLY],
)
def delete_vendor_document(
    vendor_id: UUID, document_id: UUID, db: Session = Depends(get_db)
):
    row = (
        db.query(PlatformVendorDocument)
        .filter(
            PlatformVendorDocument.id == document_id,
            PlatformVendorDocument.vendor_id == vendor_id,
            PlatformVendorDocument.deleted_at.is_(None),
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Document not found")
    row.deleted_at = datetime.now(timezone.utc)
    db.commit()


# ---------------------------------------------------------------------------
# Onboarding checklist
# ---------------------------------------------------------------------------


def _advance_onboarding(
    db: Session, vendor_id: UUID, target: str, *, actor_id: str, now: datetime
) -> Optional[PlatformVendorOnboarding]:
    """Move a vendor's onboarding FORWARD to ``target`` (creating the row on
    first touch), stamping every step passed through. No-op if already at or
    past ``target``."""
    ob = (
        db.query(PlatformVendorOnboarding)
        .filter(PlatformVendorOnboarding.vendor_id == vendor_id)
        .first()
    )
    if ob is None:
        ob = PlatformVendorOnboarding(vendor_id=vendor_id, status="invited", invited_at=now)
        db.add(ob)
    current_idx = ONBOARDING_STATUSES.index(ob.status)
    target_idx = ONBOARDING_STATUSES.index(target)
    if target_idx <= current_idx:
        return ob
    for step in ONBOARDING_STATUSES[current_idx + 1 : target_idx + 1]:
        field = _ONBOARDING_TS_FIELD[step]
        if getattr(ob, field) is None:
            setattr(ob, field, now)
    ob.status = target
    ob.updated_by = actor_id
    _emit(
        db,
        event_type="vendor_onboarding_advanced",
        actor_id=actor_id,
        after={"vendor_id": str(vendor_id), "status": target},
    )
    return ob


def _onboarding_out(vendor_id: UUID, ob: Optional[PlatformVendorOnboarding]) -> OnboardingOut:
    if ob is None:
        return OnboardingOut(
            vendor_id=vendor_id, status="invited", next_statuses=next_onboarding_statuses("invited")
        )
    return OnboardingOut(
        vendor_id=vendor_id,
        status=ob.status,
        invited_at=ob.invited_at,
        docs_collected_at=ob.docs_collected_at,
        msa_signed_at=ob.msa_signed_at,
        live_at=ob.live_at,
        note=ob.note,
        next_statuses=next_onboarding_statuses(ob.status),
    )


@router.get("/vendors/{vendor_id}/onboarding", response_model=OnboardingOut)
def get_onboarding(vendor_id: UUID, db: Session = Depends(get_db)):
    _get_vendor_or_404(db, vendor_id)
    ob = (
        db.query(PlatformVendorOnboarding)
        .filter(PlatformVendorOnboarding.vendor_id == vendor_id)
        .first()
    )
    return _onboarding_out(vendor_id, ob)


@router.post(
    "/vendors/{vendor_id}/onboarding/advance",
    response_model=OnboardingOut,
    dependencies=[ADMIN_ONLY],
)
def advance_onboarding(
    vendor_id: UUID,
    body: OnboardingAdvanceBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    _get_vendor_or_404(db, vendor_id)
    if body.status not in ONBOARDING_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {', '.join(ONBOARDING_STATUSES)}",
        )
    now = datetime.now(timezone.utc)
    ob = (
        db.query(PlatformVendorOnboarding)
        .filter(PlatformVendorOnboarding.vendor_id == vendor_id)
        .first()
    )
    if ob is not None and body.status != "invited":
        if ONBOARDING_STATUSES.index(body.status) <= ONBOARDING_STATUSES.index(ob.status):
            raise HTTPException(
                status_code=409,
                detail=f"Onboarding is already at or past '{ob.status}' (forward-only)",
            )
    ob = _advance_onboarding(db, vendor_id, body.status, actor_id=_actor_id(user), now=now)
    if body.note is not None:
        ob.note = body.note
    db.commit()
    return _onboarding_out(vendor_id, ob)


# ---------------------------------------------------------------------------
# Vendor users + 9-role matrix
# ---------------------------------------------------------------------------


@router.get("/roles", response_model=list[ClinicRoleOut])
def list_clinic_roles(db: Session = Depends(get_db)):
    rows = (
        db.query(PlatformClinicRole)
        .order_by(PlatformClinicRole.sort_order)
        .all()
    )
    return [
        ClinicRoleOut(
            key=r.key,
            name=r.name,
            description=r.description,
            is_addon=r.is_addon,
            sort_order=r.sort_order,
        )
        for r in rows
    ]


@router.get("/vendors/{vendor_id}/users", response_model=list[VendorUserOut])
def list_vendor_users(vendor_id: UUID, db: Session = Depends(get_db)):
    _get_vendor_or_404(db, vendor_id)
    rows = (
        db.query(PlatformClinicMembership, User)
        .join(User, PlatformClinicMembership.user_id == User.id)
        .filter(PlatformClinicMembership.vendor_id == vendor_id)
        .order_by(PlatformClinicMembership.created_at)
        .all()
    )
    return [
        VendorUserOut(
            membership_id=m.id,
            user_id=m.user_id,
            email=getattr(u, "email", None),
            full_name=getattr(u, "full_name", None),
            role=m.role,
            roles=list(m.roles) if m.roles is not None else None,
            created_at=m.created_at,
        )
        for m, u in rows
    ]


@router.put(
    "/vendors/{vendor_id}/users/{membership_id}/roles",
    response_model=VendorUserOut,
    dependencies=[ADMIN_ONLY],
)
def set_vendor_user_roles(
    vendor_id: UUID,
    membership_id: UUID,
    body: RolesBody,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Assign a clinic user's 9-role permission set (replaces the whole list).

    Enforced on every ``/api/clinic/v1`` role-gated endpoint from the next
    request. NULL (legacy full-access) can only become an explicit list —
    there is deliberately no way back to NULL.
    """
    m = (
        db.query(PlatformClinicMembership)
        .filter(
            PlatformClinicMembership.id == membership_id,
            PlatformClinicMembership.vendor_id == vendor_id,
        )
        .first()
    )
    if m is None:
        raise HTTPException(status_code=404, detail="Clinic membership not found")
    errors = validate_role_assignment(body.roles)
    if errors:
        raise HTTPException(status_code=422, detail=" ".join(errors))
    before = list(m.roles) if m.roles is not None else None
    m.roles = list(body.roles)
    _emit(
        db,
        event_type="clinic_user_roles_changed",
        actor_id=_actor_id(user),
        after={
            "vendor_id": str(vendor_id),
            "membership_id": str(membership_id),
            "user_id": str(m.user_id),
            "roles_before": before,
            "roles": list(body.roles),
        },
    )
    db.commit()
    u = db.query(User).filter(User.id == m.user_id).first()
    return VendorUserOut(
        membership_id=m.id,
        user_id=m.user_id,
        email=getattr(u, "email", None),
        full_name=getattr(u, "full_name", None),
        role=m.role,
        roles=list(m.roles),
        created_at=m.created_at,
    )


# ---------------------------------------------------------------------------
# Vendor → provider → loan chaining view
# ---------------------------------------------------------------------------


@router.get("/vendors/{vendor_id}/chain")
def vendor_chain(vendor_id: UUID, db: Session = Depends(get_db)):
    """TL's vendor→provider→loan chain (video 09 f0063: provider id = vendor
    id + provider number; loan ids chain from that).

    We have no providers table yet — ``provider_name`` is free text on the
    application — so the chain groups the vendor's applications/loans by
    provider name (``null`` → "Unassigned"). Vendor-safe fields only would be
    a clinic concern; this is the ADMIN view, so loan status/amounts are fine
    (still no bureau/risk data here).
    """
    vendor = _get_vendor_or_404(db, vendor_id)
    rows = (
        db.query(PlatformCreditApplication, PlatformLoan, PlatformPatient)
        .outerjoin(PlatformLoan, PlatformLoan.application_id == PlatformCreditApplication.id)
        .outerjoin(PlatformPatient, PlatformCreditApplication.patient_id == PlatformPatient.id)
        .filter(PlatformCreditApplication.vendor_id == vendor_id)
        .order_by(PlatformCreditApplication.created_at.desc())
        .all()
    )
    providers: dict[str, dict] = {}
    for app_row, loan, patient in rows:
        key = app_row.provider_name or "Unassigned"
        bucket = providers.setdefault(
            key, {"provider_name": key, "application_count": 0, "loans": []}
        )
        bucket["application_count"] += 1
        if loan is not None:
            name_parts = [
                getattr(patient, "legal_first_name", None),
                getattr(patient, "legal_last_name", None),
            ]
            bucket["loans"].append(
                {
                    "loan_id": str(loan.id),
                    "application_id": str(app_row.id),
                    "patient_name": " ".join(p for p in name_parts if p) or "Unknown",
                    "status": loan.status,
                    "principal_cents": loan.principal_cents,
                    "principal_balance_cents": loan.principal_balance_cents,
                    "current_bucket": loan.current_bucket,
                    "disbursed_at": loan.disbursed_at.isoformat() if loan.disbursed_at else None,
                }
            )
    return {
        "vendor": {
            "id": str(vendor.id),
            "business_name": vendor.business_name,
            "status": vendor.status,
            "industry_category_id": (
                str(vendor.industry_category_id) if vendor.industry_category_id else None
            ),
        },
        "providers": sorted(providers.values(), key=lambda p: p["provider_name"]),
    }


# ---------------------------------------------------------------------------
# Per-vendor portfolio report
# ---------------------------------------------------------------------------


@router.get("/vendors/{vendor_id}/portfolio")
def vendor_portfolio(
    vendor_id: UUID,
    frm: Optional[datetime] = Query(None, alias="from"),
    to: Optional[datetime] = Query(None),
    db: Session = Depends(get_db),
):
    """Per-vendor portfolio report (video 09 ``/tools/vendors/export``).

    Composes the EXISTING clinic dashboard blocks (``loan_book_block`` /
    ``payments_block`` — same definitions the vendor sees on their own
    dashboard, so admin and vendor never disagree) plus originations, a
    delinquency-bucket breakdown, and collected-revenue splits from the
    immutable ledger. ``from``/``to`` bound the payments/originations window
    (default: all time up to now).
    """
    vendor = _get_vendor_or_404(db, vendor_id)
    now = datetime.now(timezone.utc)
    frm = frm or datetime(2000, 1, 1, tzinfo=timezone.utc)
    to = to or now

    # Originations (window on application created_at / loan disbursed_at).
    apps_q = db.query(PlatformCreditApplication).filter(
        PlatformCreditApplication.vendor_id == vendor_id,
        PlatformCreditApplication.created_at >= frm,
        PlatformCreditApplication.created_at <= to,
    )
    applications_total = apps_q.count()
    applications_approved = apps_q.filter(
        PlatformCreditApplication.status == "approved"
    ).count()

    loans_disbursed_window = (
        db.query(
            func.count(PlatformLoan.id),
            func.coalesce(func.sum(PlatformLoan.principal_cents), 0),
        )
        .join(
            PlatformCreditApplication,
            PlatformLoan.application_id == PlatformCreditApplication.id,
        )
        .filter(
            PlatformCreditApplication.vendor_id == vendor_id,
            PlatformLoan.disbursed_at.isnot(None),
            PlatformLoan.disbursed_at >= frm,
            PlatformLoan.disbursed_at <= to,
        )
        .one()
    )

    # Delinquency: month-end bucket assignment counts across the vendor's book.
    bucket_rows = (
        db.query(PlatformLoan.current_bucket, func.count(PlatformLoan.id))
        .join(
            PlatformCreditApplication,
            PlatformLoan.application_id == PlatformCreditApplication.id,
        )
        .filter(PlatformCreditApplication.vendor_id == vendor_id)
        .group_by(PlatformLoan.current_bucket)
        .all()
    )

    # Revenue: interest + fees actually COLLECTED, from the immutable ledger
    # (payment transactions' allocation buckets; reversals net out as separate
    # reversal rows are excluded by txn_type).
    revenue = (
        db.query(
            func.coalesce(func.sum(PlatformLoanTransaction.interest_cents), 0),
            func.coalesce(func.sum(PlatformLoanTransaction.fees_cents), 0),
            func.coalesce(func.sum(PlatformLoanTransaction.amount_cents), 0),
        )
        .join(PlatformLoan, PlatformLoanTransaction.loan_id == PlatformLoan.id)
        .join(
            PlatformCreditApplication,
            PlatformLoan.application_id == PlatformCreditApplication.id,
        )
        .filter(
            PlatformCreditApplication.vendor_id == vendor_id,
            PlatformLoanTransaction.txn_type == "payment",
            PlatformLoanTransaction.effective_date >= frm.date(),
            PlatformLoanTransaction.effective_date <= to.date(),
        )
        .one()
    )

    return {
        "vendor": {"id": str(vendor.id), "business_name": vendor.business_name},
        "window": {"from": frm.isoformat(), "to": to.isoformat()},
        "originations": {
            "applications_total": applications_total,
            "applications_approved": applications_approved,
            "loans_disbursed": int(loans_disbursed_window[0]),
            "disbursed_amount_cents": int(loans_disbursed_window[1]),
        },
        "active_book": loan_book_block(db, vendor_id),
        "delinquency": {
            "buckets": {str(b): int(c) for b, c in bucket_rows if b is not None},
            **{
                k: v
                for k, v in payments_block(db, vendor_id, frm, to).items()
                if k in ("late_count", "delinquent_count", "on_time_rate")
            },
        },
        "revenue": {
            "interest_collected_cents": int(revenue[0]),
            "fees_collected_cents": int(revenue[1]),
            "total_collected_cents": int(revenue[2]),
        },
    }
