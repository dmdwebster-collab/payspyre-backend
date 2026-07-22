"""Originations-admin write/read surface (WS-E, Turnkey parity video 01).

Mounted under ``/admin/applications`` alongside the read-only queue/detail
(``admin_applications``) and the decision loop (``admin_actions``):

* ``PATCH /{id}`` — admin field-level editing of the credit application with
  an old→new change log audited to ``platform_events``, honoring Dave's
  schema rules: SIN never stored here, and the current address/employment are
  VERSIONED into history rows on edit — never overwritten.
* ``PUT /{id}/offer`` — staff offer editing with product min/max enforcement
  from the typed PricingConfig (amount / term / rate band / frequency) and the
  ``interest.rate_edit_roles`` gate; the edited terms land on the decision
  payload the booking path (``_resolve_pricing``) reads. Criminal-rate (s.347)
  guarded via the quote engine.
* ``GET /{id}/header`` — the loan/application header block: key facts, email +
  phone under the name, assignment, loan linkage, profile-photo URL.
* ``POST /{id}/profile-photo`` (+ ``/confirm``) — profile-photo slot reusing
  the existing presigned-PUT document storage. ID-photo match automation is
  DESCOPED (noted in the parity plan); this is the manual slot it will fill.
* ``GET/POST /{id}/co-borrowers`` — Dave mandate #1: co-borrowers are fully
  SEPARATE applicant files linked by id; endpoints to view / link / unlink the
  group. Each linked file is a normal application (identical tab structure via
  the existing detail/document/message endpoints).

Assignment gating: mutating actions here require the actor to be the file's
assignee, or hold an override (``admin`` role — implicitly allowed everywhere —
or the explicit ``applications``/``assignment_override`` permission).
All writes append ``platform_events`` audit rows.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload

from app.core.auth import require_roles
from app.db.base import get_db
from app.models.loan import Vendor
from app.models.platform.application_document import PlatformApplicationDocument
from app.models.platform.application_history import (
    PlatformApplicationAddressHistory,
    PlatformApplicationEmploymentHistory,
)
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.event import PlatformEvent
from app.models.platform.loan import PlatformLoan
from app.services import document_storage, originations_admin
from app.services.loan_quote import product_fees_cents, quote_loan
from app.services.originations_admin import (
    AssignmentRequired,
    InvalidEdit,
    OfferOutOfBounds,
    RateRoleNotPermitted,
)
from app.schemas.pricing_config import PricingConfigError, parse_pricing_config

router = APIRouter()

PROFILE_PHOTO_DOC_TYPE = "profile_photo"
_PHOTO_CONTENT_TYPES = ("image/jpeg", "image/png", "image/heic", "image/webp")

#: Statuses where the file is closed and neither edits nor offer changes apply.
_CLOSED_STATUSES = ("declined", "withdrawn", "expired")


def _actor_id(user) -> str:
    return str(getattr(user, "id", "") or "unknown")


def _user_roles(user) -> set[str]:
    return {ur.role.name for ur in getattr(user, "roles", [])}


def _user_permissions(user) -> set[tuple[str, str]]:
    perms: set[tuple[str, str]] = set()
    for ur in getattr(user, "roles", []):
        for rp in getattr(ur.role, "permissions", []):
            perms.add((rp.permission.resource, rp.permission.action))
    return perms


def _get_application(db: Session, application_id: UUID) -> PlatformCreditApplication:
    app = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.id == application_id)
        .first()
    )
    if app is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")
    return app


def require_assignment_or_override(app_row: PlatformCreditApplication, user) -> bool:
    """WS-E assignment gate (shared with admin_actions). Returns True when the
    actor proceeded via an override rather than as the assignee; raises 403
    when neither applies."""
    try:
        return originations_admin.check_assignment(
            app_row.assigned_to_user_id,
            _actor_id(user),
            override_allowed=originations_admin.has_assignment_override(
                _user_roles(user), _user_permissions(user)
            ),
        )
    except AssignmentRequired as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))


def _audit(
    db: Session,
    *,
    event_type: str,
    actor: str,
    app_row: PlatformCreditApplication,
    payload: dict,
) -> None:
    db.add(
        PlatformEvent(
            event_type=event_type,
            actor=actor,
            patient_id=app_row.patient_id,
            application_id=app_row.id,
            payload=payload,
        )
    )
    db.flush()


# ---------------------------------------------------------------------------
# 1. Admin field-level application editing (change-logged, versioned)
# ---------------------------------------------------------------------------


class ApplicationEditBody(BaseModel):
    """``changes`` maps editable canonical fields to their new values (null =
    clear). The whitelist + typing live in ``originations_admin.EDITABLE_FIELDS``."""

    changes: dict[str, Any] = Field(..., min_length=1)
    note: Optional[str] = None


@router.patch("/{application_id}")
def edit_application(
    application_id: UUID,
    body: ApplicationEditBody,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin", "staff")),
):
    """Field-level admin edit of the credit application (WS-E).

    Every applied change is written to ``platform_events`` as an
    ``admin_application_edited`` row with per-field old→new, the editor and
    the timestamp. Address/employment edits VERSION the prior current entry
    into the history tables (``entry_source='versioned_edit'``) instead of
    overwriting — Dave's rule. SIN is not editable here (never stored on the
    application). No-op changes are dropped; an all-no-op edit returns
    ``changed: false`` and writes no audit row."""
    app_row = _get_application(db, application_id)
    if app_row.status in _CLOSED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Application is closed ({app_row.status}); its file is read-only.",
        )
    override_used = require_assignment_or_override(app_row, user)

    try:
        changes = originations_admin.validate_changes(body.changes)
    except InvalidEdit as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    today = datetime.now(timezone.utc).date()
    result = originations_admin.apply_admin_edit(app_row, changes, today=today)
    if not result.changed:
        return {"application_id": str(app_row.id), "changed": False, "changes": {}}

    if result.address_snapshot:
        db.add(
            PlatformApplicationAddressHistory(
                application_id=app_row.id, **result.address_snapshot
            )
        )
    if result.employment_snapshot:
        db.add(
            PlatformApplicationEmploymentHistory(
                application_id=app_row.id, **result.employment_snapshot
            )
        )

    actor = _actor_id(user)
    _audit(
        db,
        event_type="admin_application_edited",
        actor=actor,
        app_row=app_row,
        payload={
            "v": 1,
            "actor": {"type": "admin", "id": actor},
            "application_id": str(app_row.id),
            "changes": result.change_log,
            "address_versioned": result.address_snapshot is not None,
            "employment_versioned": result.employment_snapshot is not None,
            "assignment_override_used": override_used,
            "note": body.note,
            "edited_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    db.commit()
    return {
        "application_id": str(app_row.id),
        "changed": True,
        "changes": result.change_log,
        "address_versioned": result.address_snapshot is not None,
        "employment_versioned": result.employment_snapshot is not None,
    }


# ---------------------------------------------------------------------------
# 2. Staff offer editing (product min/max enforcement from PricingConfig)
# ---------------------------------------------------------------------------


class OfferEditBody(BaseModel):
    amount_cents: int = Field(..., gt=0)
    term_months: int = Field(..., gt=0)
    annual_rate_bps: Optional[int] = Field(
        None,
        ge=0,
        description="Omit for the product's default rate. A custom rate must sit "
        "within the product's [min, max] band and requires a role listed in "
        "interest.rate_edit_roles.",
    )
    frequency: str = "monthly"
    note: Optional[str] = None


@router.put("/{application_id}/offer")
def edit_offer(
    application_id: UUID,
    body: OfferEditBody,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin", "staff")),
):
    """Edit the application's offer terms (WS-E staff offer editing).

    Bounds come from the product row (min/max amount) + its typed
    ``PricingConfig`` (term range, rate band, frequencies) — out-of-bounds is
    rejected 422, unauthorized custom rate 403. The validated terms are stored
    on the decision payload exactly where booking's ``_resolve_pricing`` reads
    them, and the FULL disclosure quote (installments, APR, cost of borrowing)
    is returned. s.347 criminal-rate offers are refused."""
    app_row = (
        db.query(PlatformCreditApplication)
        .options(joinedload(PlatformCreditApplication.credit_product))
        .filter(PlatformCreditApplication.id == application_id)
        .first()
    )
    if app_row is None:
        raise HTTPException(status_code=404, detail="Application not found")
    if app_row.status in _CLOSED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Application is closed ({app_row.status}); offer terms are frozen.",
        )
    existing_loan = (
        db.query(PlatformLoan.id)
        .filter(PlatformLoan.application_id == application_id)
        .first()
    )
    if existing_loan is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A loan is already booked from this application "
                   f"({existing_loan[0]}); offer terms are locked.",
        )
    override_used = require_assignment_or_override(app_row, user)

    product = app_row.credit_product
    if product is None:
        raise HTTPException(status_code=409, detail="Application has no credit product.")
    try:
        cfg = parse_pricing_config(product.pricing_config, context="admin offer edit")
    except PricingConfigError as exc:
        raise HTTPException(status_code=502, detail=f"Product pricing config invalid: {exc}")

    try:
        offer = originations_admin.validate_offer(
            cfg,
            amount_cents=body.amount_cents,
            term_months=body.term_months,
            annual_rate_bps=body.annual_rate_bps,
            frequency=body.frequency,
            actor_roles=_user_roles(user),
            product_min_amount_cents=product.min_amount_cents,
            product_max_amount_cents=product.max_amount_cents,
        )
    except OfferOutOfBounds as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except RateRoleNotPermitted as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))

    fees_cents = product_fees_cents(
        product.pricing_config, offer.amount_cents, offer.term_months, offer.frequency
    )
    quote = quote_loan(
        offer.amount_cents,
        offer.annual_rate_bps,
        offer.term_months,
        offer.frequency,
        fees_cents=fees_cents,
    )
    if quote.exceeds_criminal_rate:
        raise HTTPException(
            status_code=422,
            detail=f"Refusing offer: APR {(quote.apr_bps or 0) / 100:.2f}% reaches "
                   f"the Criminal Code s.347 cap.",
        )

    decision = dict(app_row.decision or {})
    before = {
        k: decision.get(k) for k in ("amount_cents", "apr_bps", "term_months", "frequency")
    }
    after = {
        # Keys are the booking contract _resolve_pricing reads ("apr_bps" is
        # the contract annual rate key on the decision payload).
        "amount_cents": offer.amount_cents,
        "apr_bps": offer.annual_rate_bps,
        "term_months": offer.term_months,
        "frequency": offer.frequency,
    }
    decision.update(after)
    app_row.decision = decision

    actor = _actor_id(user)
    _audit(
        db,
        event_type="admin_offer_edited",
        actor=actor,
        app_row=app_row,
        payload={
            "v": 1,
            "actor": {"type": "admin", "id": actor},
            "application_id": str(app_row.id),
            "before": before,
            "after": after,
            "fees_cents": fees_cents,
            "regulatory_apr_bps": quote.apr_bps,
            "assignment_override_used": override_used,
            "note": body.note,
        },
    )
    db.commit()
    return {
        "application_id": str(app_row.id),
        "offer": after,
        "quote": {
            "num_payments": quote.num_payments,
            "installment_cents": quote.installment_cents,
            "final_installment_cents": quote.final_installment_cents,
            "total_of_payments_cents": quote.total_of_payments_cents,
            "interest_cents": quote.interest_cents,
            "fees_cents": quote.fees_cents,
            "cost_of_borrowing_cents": quote.cost_of_borrowing_cents,
            "apr_bps": quote.apr_bps,
        },
    }


# ---------------------------------------------------------------------------
# 3. Header (key facts + contact under name + profile-photo slot)
# ---------------------------------------------------------------------------


class ApplicationHeader(BaseModel):
    application_id: UUID
    #: The borrower (``platform_patients.id``). Admin-only: it is the join key
    #: every borrower-scoped tab (Borrower details, Co-Borrower, Bank Accounts,
    #: Bank Statements, Documents) needs, so the UI no longer has to recover it
    #: by scanning ``platform_events`` audit history.
    patient_id: UUID
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    status: str
    applicant_role: str
    branch: Optional[str] = None
    product_name: str
    vendor_name: str
    requested_amount_cents: int
    province: Optional[str] = None
    decision_outcome: Optional[str] = None
    assigned_to_user_id: Optional[UUID] = None
    loan_id: Optional[UUID] = None
    loan_status: Optional[str] = None
    principal_balance_cents: Optional[int] = None
    created_at: datetime
    # Short-TTL presigned GET for the uploaded profile photo; null when no
    # photo has been uploaded or storage is unconfigured.
    profile_photo_url: Optional[str] = None


@router.get("/{application_id}/header", response_model=ApplicationHeader)
def application_header(
    application_id: UUID,
    db: Session = Depends(get_db),
    _user=Depends(require_roles("admin", "staff")),
):
    """The file header Dave asked for: key facts with the borrower's email and
    phone directly under the name, plus the profile-photo slot. (Automated
    ID-photo matching is descoped — the slot is filled by manual upload.)"""
    app_row = (
        db.query(PlatformCreditApplication)
        .options(
            joinedload(PlatformCreditApplication.patient),
            joinedload(PlatformCreditApplication.credit_product),
        )
        .filter(PlatformCreditApplication.id == application_id)
        .first()
    )
    if app_row is None:
        raise HTTPException(status_code=404, detail="Application not found")
    patient = app_row.patient

    name = " ".join(x for x in (app_row.first_name, app_row.last_name) if x).strip()
    if not name and patient is not None:
        name = " ".join(
            x for x in (patient.legal_first_name, patient.legal_last_name) if x
        ).strip()
    email = app_row.email or (patient.email if patient else None)
    phone = app_row.main_phone or (patient.phone_e164 if patient else None)

    vendor_name = "—"
    if app_row.vendor_id is not None:
        v = db.query(Vendor.business_name).filter(Vendor.id == app_row.vendor_id).first()
        vendor_name = v[0] if v else "—"

    loan = (
        db.query(PlatformLoan)
        .filter(PlatformLoan.application_id == application_id)
        .first()
    )

    photo_url = None
    photo = (
        db.query(PlatformApplicationDocument)
        .filter(
            PlatformApplicationDocument.application_id == application_id,
            PlatformApplicationDocument.doc_type == PROFILE_PHOTO_DOC_TYPE,
            PlatformApplicationDocument.status == "uploaded",
        )
        .order_by(PlatformApplicationDocument.created_at.desc())
        .first()
    )
    if photo is not None and document_storage.is_configured():
        try:
            photo_url = document_storage.presigned_get_url(photo.object_key)
        except document_storage.StorageNotConfigured:  # pragma: no cover - race
            photo_url = None

    return ApplicationHeader(
        application_id=app_row.id,
        patient_id=app_row.patient_id,
        name=name or "Unknown applicant",
        email=email,
        phone=phone,
        status=str(app_row.status),
        applicant_role=str(app_row.applicant_role),
        branch=app_row.branch,
        product_name=(app_row.credit_product.name if app_row.credit_product else None)
        or "Financing",
        vendor_name=vendor_name,
        requested_amount_cents=app_row.requested_amount_cents,
        province=app_row.residence_province,
        decision_outcome=(app_row.decision or {}).get("outcome"),
        assigned_to_user_id=app_row.assigned_to_user_id,
        loan_id=loan.id if loan else None,
        loan_status=loan.status if loan else None,
        principal_balance_cents=loan.principal_balance_cents if loan else None,
        created_at=app_row.created_at,
        profile_photo_url=photo_url,
    )


class PhotoUploadBody(BaseModel):
    content_type: str


@router.post("/{application_id}/profile-photo")
def request_profile_photo_upload(
    application_id: UUID,
    body: PhotoUploadBody,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin", "staff")),
):
    """Issue a short-TTL presigned PUT for the header profile photo (reuses the
    existing document storage). 503 until SPACES_* is configured."""
    if body.content_type not in _PHOTO_CONTENT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"content_type must be one of {list(_PHOTO_CONTENT_TYPES)}",
        )
    if not document_storage.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Document storage is not configured yet.",
        )
    app_row = _get_application(db, application_id)

    document_id = uuid4()
    object_key = document_storage.build_object_key(
        application_id, PROFILE_PHOTO_DOC_TYPE, document_id
    )
    url = document_storage.presigned_put_url(object_key, body.content_type)
    db.add(
        PlatformApplicationDocument(
            id=document_id,
            application_id=application_id,
            doc_type=PROFILE_PHOTO_DOC_TYPE,
            object_key=object_key,
            content_type=body.content_type,
            status="pending",
        )
    )
    _audit(
        db,
        event_type="admin_profile_photo_upload_requested",
        actor=_actor_id(user),
        app_row=app_row,
        payload={"document_id": str(document_id)},
    )
    db.commit()
    return {"document_id": str(document_id), "upload_url": url, "object_key": object_key}


@router.post("/{application_id}/profile-photo/{document_id}/confirm")
def confirm_profile_photo(
    application_id: UUID,
    document_id: UUID,
    db: Session = Depends(get_db),
    _user=Depends(require_roles("admin", "staff")),
):
    """Mark the profile photo uploaded once the PUT to storage succeeded."""
    doc = (
        db.query(PlatformApplicationDocument)
        .filter(
            PlatformApplicationDocument.id == document_id,
            PlatformApplicationDocument.application_id == application_id,
            PlatformApplicationDocument.doc_type == PROFILE_PHOTO_DOC_TYPE,
        )
        .first()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Photo document not found")
    doc.status = "uploaded"
    doc.uploaded_at = datetime.now(timezone.utc)
    db.commit()
    return {"document_id": str(doc.id), "status": doc.status}


# ---------------------------------------------------------------------------
# 4. Co-borrower linked files (Dave mandate #1)
# ---------------------------------------------------------------------------


class CoBorrowerFile(BaseModel):
    application_id: UUID
    applicant_role: str
    relationship_to_primary: Optional[str] = None
    name: str
    email: Optional[str] = None
    status: str
    created_at: datetime


class CoBorrowerGroup(BaseModel):
    primary_application_id: UUID
    files: list[CoBorrowerFile]


def _file_row(app_row: PlatformCreditApplication) -> CoBorrowerFile:
    return CoBorrowerFile(
        application_id=app_row.id,
        applicant_role=str(app_row.applicant_role),
        relationship_to_primary=app_row.relationship_to_primary,
        name=" ".join(x for x in (app_row.first_name, app_row.last_name) if x).strip()
        or "Unknown applicant",
        email=app_row.email,
        status=str(app_row.status),
        created_at=app_row.created_at,
    )


@router.get("/{application_id}/co-borrowers", response_model=CoBorrowerGroup)
def list_co_borrowers(
    application_id: UUID,
    db: Session = Depends(get_db),
    _user=Depends(require_roles("admin", "staff")),
):
    """The applicant group as SEPARATE linked files (Dave: "a completely
    separate file for each individual"). Resolves through to the primary when
    called with a co-borrower's id, then returns primary + all linked files.
    Each file is a full application — open it with the normal detail/document/
    message endpoints (identical tab structure)."""
    app_row = _get_application(db, application_id)
    primary = app_row
    if app_row.co_applicant_of_application_id is not None:
        primary = _get_application(db, app_row.co_applicant_of_application_id)
    linked = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.co_applicant_of_application_id == primary.id)
        .order_by(PlatformCreditApplication.created_at.asc())
        .all()
    )
    return CoBorrowerGroup(
        primary_application_id=primary.id,
        files=[_file_row(primary)] + [_file_row(x) for x in linked],
    )


class LinkBody(BaseModel):
    co_application_id: UUID
    relationship_to_primary: Optional[str] = Field(
        None, description="Declared relationship (spouse, parent, …)."
    )


@router.post("/{application_id}/co-borrowers/link")
def link_co_borrower(
    application_id: UUID,
    body: LinkBody,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin", "staff")),
):
    """Link an existing application file to this primary as a co-borrower file.

    Guards: no self-link; the primary must itself be a primary file (no
    chains); the target must not already be linked elsewhere and must not have
    co-borrowers of its own. Audited."""
    if body.co_application_id == application_id:
        raise HTTPException(status_code=422, detail="An application cannot co-borrow with itself.")
    primary = _get_application(db, application_id)
    if primary.co_applicant_of_application_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Application {application_id} is itself a co-borrower file; "
                   f"link against its primary "
                   f"({primary.co_applicant_of_application_id}).",
        )
    target = _get_application(db, body.co_application_id)
    if target.co_applicant_of_application_id is not None:
        if target.co_applicant_of_application_id == primary.id:
            return {  # idempotent re-link
                "primary_application_id": str(primary.id),
                "co_application_id": str(target.id),
                "linked": True,
            }
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Target application is already linked to a different primary.",
        )
    has_children = (
        db.query(PlatformCreditApplication.id)
        .filter(PlatformCreditApplication.co_applicant_of_application_id == target.id)
        .first()
    )
    if has_children is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Target application has co-borrower files of its own; unlink those first.",
        )

    target.co_applicant_of_application_id = primary.id
    target.applicant_role = "co_applicant"
    if body.relationship_to_primary is not None:
        target.relationship_to_primary = body.relationship_to_primary

    actor = _actor_id(user)
    _audit(
        db,
        event_type="admin_co_borrower_linked",
        actor=actor,
        app_row=primary,
        payload={
            "v": 1,
            "actor": {"type": "admin", "id": actor},
            "primary_application_id": str(primary.id),
            "co_application_id": str(target.id),
            "relationship_to_primary": body.relationship_to_primary,
        },
    )
    db.commit()
    return {
        "primary_application_id": str(primary.id),
        "co_application_id": str(target.id),
        "linked": True,
    }


class UnlinkBody(BaseModel):
    co_application_id: UUID
    note: Optional[str] = None


@router.post("/{application_id}/co-borrowers/unlink")
def unlink_co_borrower(
    application_id: UUID,
    body: UnlinkBody,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin", "staff")),
):
    """Detach a co-borrower file from this primary. The file survives as an
    independent primary application (files are never deleted). Audited."""
    primary = _get_application(db, application_id)
    target = _get_application(db, body.co_application_id)
    if target.co_applicant_of_application_id != primary.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Target application is not linked to this primary.",
        )
    prior_relationship = target.relationship_to_primary
    target.co_applicant_of_application_id = None
    target.applicant_role = "primary"
    target.relationship_to_primary = None

    actor = _actor_id(user)
    _audit(
        db,
        event_type="admin_co_borrower_unlinked",
        actor=actor,
        app_row=primary,
        payload={
            "v": 1,
            "actor": {"type": "admin", "id": actor},
            "primary_application_id": str(primary.id),
            "co_application_id": str(target.id),
            "prior_relationship_to_primary": prior_relationship,
            "note": body.note,
        },
    )
    db.commit()
    return {
        "primary_application_id": str(primary.id),
        "co_application_id": str(target.id),
        "linked": False,
    }
