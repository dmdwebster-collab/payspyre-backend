"""Admin API — Customer Profiles (Dave's Credit Application v1.0).

Dave, 2026-07-21: *"there is no way to add, edit, lock or delete a user's
profile."* This module is that surface, plus the schema endpoint the manual form
and the applicant journey render from.

* ``GET  /admin/profile-schema`` — the whole field registry as data: categories,
  visibility triggers, mandatory flags, options, formats and character limits.
  Read-only, stateless, no DB. The UI must not hard-code any of it.
* ``POST /admin/customer-profiles`` — create a profile for a patient.
* ``GET  /admin/customer-profiles/{id}`` — the profile, masked per the registry.
* ``PATCH /admin/customer-profiles/{id}`` — edit; creates a NEW version, the
  prior values become "former" (never overwritten).
* ``GET  /admin/customer-profiles/{id}/history`` — the changelog (time + user).
* ``POST /admin/customer-profiles/{id}/lock`` / ``/unlock``
* ``DELETE /admin/customer-profiles/{id}`` — SOFT delete (PIPEDA retention).
* ``POST /admin/customer-profiles/{id}/validate`` — dry-run the registry rules.
* ``POST /admin/customer-profiles/{id}/applications`` — Dave's D2 "attach an
  EXISTING profile" path: create a credit application from a profile with no
  re-entry, freezing the profile state used for the decision.
* ``POST /admin/customer-profiles/backfill`` — materialise profiles from
  applications captured before profiles existed. Idempotent, admin-only.

PERMISSIONS
-----------
Reads and edits are admin/staff. Lock, unlock, delete, restore and backfill are
admin-only. Full (unmasked) bank transit / account numbers require ``admin`` AND
an explicit ``?include_sensitive=true``; the SIN is never returned in full to
anyone by any route here.
"""
from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import get_current_user, require_roles
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.customer_profile import PlatformCustomerProfile
from app.services import customer_profile as profiles
from app.services import customer_profile_schema as schema
from app.services.customer_profile_validation import (
    ProfileValidationError,
    completeness,
    validate_profile,
)

router = APIRouter(dependencies=[Depends(require_roles("admin", "staff"))])

ADMIN_ONLY = Depends(require_roles("admin"))


def _actor_id(user) -> str:
    return str(getattr(user, "id", "") or getattr(user, "email", "") or "unknown")


def _is_admin(user) -> bool:
    return "admin" in {ur.role.name for ur in getattr(user, "roles", [])}


def _validation_detail(error: ProfileValidationError) -> dict:
    return {"message": "Profile validation failed", "issues": [i.to_dict() for i in error.issues]}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

#: A profile payload: instance key ("personal", "bank_details#1") -> {field: value}
ProfileValuesBody = dict[str, dict[str, Any]]


class ProfileCreateRequest(BaseModel):
    patient_id: UUID
    values: ProfileValuesBody = Field(default_factory=dict)
    source: str = "self_reported"
    require_complete: bool = False


class ProfileUpdateRequest(BaseModel):
    values: ProfileValuesBody
    source: str = "staff"


class ReasonRequest(BaseModel):
    reason: Optional[str] = None


class ApplicationFromProfileRequest(BaseModel):
    """Dave's D2 back-office origination, existing-profile branch."""

    credit_product_id: UUID
    requested_amount_cents: int = Field(gt=0)
    vendor_id: Optional[UUID] = None
    provider_name: Optional[str] = None
    requested_term_months: Optional[int] = None
    requested_annual_rate_bps: Optional[int] = None
    promo_code: Optional[str] = None
    require_complete_profile: bool = False


# ---------------------------------------------------------------------------
# Schema registry
# ---------------------------------------------------------------------------


@router.get("/profile-schema", summary="Dave's Credit Application v1.0 field registry")
def get_profile_schema():
    """The ONE definition of the profile field set.

    The manual back-office form, the applicant journey and server-side validation
    all read this. Each field carries its category, label, type, options,
    mandatory flag, character limit, format and its **visibility trigger as
    evaluable data** (``{"kind": "not_equals", "field": "citizenship", "value":
    "canadian"}``), so no client has to reimplement Dave's conditional logic.
    """
    return schema.schema_payload()


@router.post("/profile-schema/validate", summary="Validate a profile payload (no persistence)")
def validate_payload(
    values: ProfileValuesBody = Body(...),
    partial: bool = Query(True, description="Skip mandatory-when-visible checks"),
):
    """Dry-run the registry rules. Used by the form for pre-submit feedback."""
    issues = validate_profile(values, partial=partial)
    return {
        "valid": not issues,
        "issues": [i.to_dict() for i in issues],
        "completeness": completeness(values),
        "visible_fields": [f.full_key for f in schema.visible_fields(values)],
    }


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.post(
    "/customer-profiles",
    status_code=status.HTTP_201_CREATED,
    summary="Create a customer profile",
)
def create_profile(
    payload: ProfileCreateRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    try:
        profile = profiles.create_profile(
            db,
            patient_id=payload.patient_id,
            values=payload.values,
            actor=_actor_id(user),
            source=payload.source,
            allow_staff_fields=_is_admin(user),
            require_complete=payload.require_complete,
        )
    except ProfileValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, _validation_detail(exc))
    except profiles.ProfileError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return profiles.read_profile(db, profile.id)


@router.get("/customer-profiles", summary="List customer profiles")
def list_profiles(
    db: Session = Depends(get_db),
    include_deleted: bool = Query(False),
    locked: Optional[bool] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    query = db.query(PlatformCustomerProfile)
    if not include_deleted:
        query = query.filter(PlatformCustomerProfile.deleted_at.is_(None))
    if locked is True:
        query = query.filter(PlatformCustomerProfile.locked_at.isnot(None))
    elif locked is False:
        query = query.filter(PlatformCustomerProfile.locked_at.is_(None))
    total = query.count()
    rows = (
        query.order_by(PlatformCustomerProfile.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "total": total,
        "items": [
            {
                "id": str(p.id),
                "patient_id": str(p.patient_id),
                "version": p.version,
                "locked": p.is_locked,
                "deleted": p.is_deleted,
                "created_at": p.created_at,
                "updated_at": p.updated_at,
            }
            for p in rows
        ],
    }


@router.get("/customer-profiles/{profile_id}", summary="Read a customer profile")
def read_profile(
    profile_id: UUID,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    include_sensitive: bool = Query(
        False, description="Admin only: unmask bank transit/account numbers"
    ),
):
    unmask = include_sensitive and _is_admin(user)
    if include_sensitive and not unmask:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "include_sensitive requires admin")
    try:
        return profiles.read_profile(db, profile_id, include_sensitive=unmask)
    except profiles.ProfileError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))


@router.patch("/customer-profiles/{profile_id}", summary="Edit a profile (new version)")
def update_profile(
    profile_id: UUID,
    payload: ProfileUpdateRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Never overwrites: the prior value of every changed field becomes 'former'."""
    try:
        profiles.update_profile(
            db,
            profile_id,
            values=payload.values,
            actor=_actor_id(user),
            source=payload.source,
            allow_staff_fields=_is_admin(user),
        )
    except ProfileValidationError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, _validation_detail(exc))
    except profiles.ProfileLockedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    except profiles.ProfileError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return profiles.read_profile(db, profile_id)


@router.get(
    "/customer-profiles/{profile_id}/history",
    summary="Profile changelog — every version, time + user stamped",
)
def profile_history(
    profile_id: UUID,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    block: Optional[str] = Query(None),
    field: Optional[str] = Query(None),
):
    try:
        profiles.get_profile(db, profile_id, include_deleted=True)
    except profiles.ProfileError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return {
        "profile_id": str(profile_id),
        "entries": profiles.field_history(
            db, profile_id, block=block, field_key=field, include_sensitive=False
        ),
    }


@router.post(
    "/customer-profiles/{profile_id}/lock",
    dependencies=[ADMIN_ONLY],
    summary="Lock a profile (no edits until unlocked)",
)
def lock_profile(
    profile_id: UUID,
    payload: ReasonRequest = Body(default=ReasonRequest()),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    try:
        profiles.lock_profile(db, profile_id, actor=_actor_id(user), reason=payload.reason)
    except profiles.ProfileError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return profiles.read_profile(db, profile_id)


@router.post(
    "/customer-profiles/{profile_id}/unlock",
    dependencies=[ADMIN_ONLY],
    summary="Unlock a profile",
)
def unlock_profile(
    profile_id: UUID,
    payload: ReasonRequest = Body(default=ReasonRequest()),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    try:
        profiles.unlock_profile(db, profile_id, actor=_actor_id(user), reason=payload.reason)
    except profiles.ProfileError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return profiles.read_profile(db, profile_id)


@router.delete(
    "/customer-profiles/{profile_id}",
    dependencies=[ADMIN_ONLY],
    summary="Soft-delete a profile (PIPEDA: history is retained)",
)
def delete_profile(
    profile_id: UUID,
    reason: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    try:
        profiles.delete_profile(db, profile_id, actor=_actor_id(user), reason=reason)
    except profiles.ProfileError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return {"profile_id": str(profile_id), "deleted": True}


@router.post(
    "/customer-profiles/{profile_id}/restore",
    dependencies=[ADMIN_ONLY],
    summary="Restore a soft-deleted profile",
)
def restore_profile(
    profile_id: UUID,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    try:
        profiles.restore_profile(db, profile_id, actor=_actor_id(user))
    except profiles.ProfileError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    return profiles.read_profile(db, profile_id)


@router.post(
    "/customer-profiles/{profile_id}/validate",
    summary="Validate the stored profile against the registry",
)
def validate_stored_profile(
    profile_id: UUID,
    db: Session = Depends(get_db),
    partial: bool = Query(False),
):
    try:
        profiles.get_profile(db, profile_id, include_deleted=True)
    except profiles.ProfileError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    values = profiles.profile_values(db, profile_id)
    issues = validate_profile(values, partial=partial)
    return {
        "profile_id": str(profile_id),
        "valid": not issues,
        "issues": [i.to_dict() for i in issues],
        "completeness": completeness(values),
    }


# ---------------------------------------------------------------------------
# Application from an existing profile (Dave's D2, "existing borrower" branch)
# ---------------------------------------------------------------------------


@router.post(
    "/customer-profiles/{profile_id}/applications",
    status_code=status.HTTP_201_CREATED,
    summary="Create a credit application from an EXISTING profile (no re-entry)",
)
def create_application_from_profile(
    profile_id: UUID,
    payload: ApplicationFromProfileRequest,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Profile + finance terms -> application, with the profile state FROZEN.

    The application's structured (migration-043) columns are populated from the
    profile so the existing decision engine is untouched, and
    ``profile_snapshot`` records exactly the values the decision will be made on.
    """
    try:
        profile = profiles.get_profile(db, profile_id)
    except profiles.ProfileError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc))

    product = (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.id == payload.credit_product_id)
        .first()
    )
    if product is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Credit product {payload.credit_product_id} not found"
        )

    if payload.require_complete_profile:
        try:
            profiles.assert_complete_for_application(db, profile_id)
        except ProfileValidationError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, _validation_detail(exc))

    application = PlatformCreditApplication(
        patient_id=profile.patient_id,
        credit_product_id=product.id,
        credit_product_version=getattr(product, "version", 1) or 1,
        requested_amount_cents=payload.requested_amount_cents,
        requested_amount_source="clinic_proposed",
        vendor_id=payload.vendor_id,
        provider_name=payload.provider_name,
        requested_term_months=payload.requested_term_months,
        requested_annual_rate_bps=payload.requested_annual_rate_bps,
        status="started",
        self_reported={"promo_code": payload.promo_code} if payload.promo_code else {},
    )
    db.add(application)
    db.flush()

    profiles.attach_profile_to_application(
        db, application, profile_id, actor=_actor_id(user), freeze=True
    )
    return {
        "application_id": str(application.id),
        "patient_id": str(application.patient_id),
        "customer_profile_id": str(profile.id),
        "profile_version": application.profile_version,
        "status": application.status,
        "requested_amount_cents": application.requested_amount_cents,
        "completeness": (application.profile_snapshot or {}).get("completeness"),
    }


@router.post(
    "/applications/{application_id}/attach-profile",
    summary="Attach (and freeze) a profile onto an existing application",
)
def attach_profile(
    application_id: UUID,
    profile_id: UUID = Query(...),
    freeze: bool = Query(True),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    application = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.id == application_id)
        .first()
    )
    if application is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Application {application_id} not found")
    try:
        profiles.attach_profile_to_application(
            db, application, profile_id, actor=_actor_id(user), freeze=freeze
        )
    except profiles.ProfileError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    return {
        "application_id": str(application.id),
        "customer_profile_id": str(application.customer_profile_id),
        "profile_version": application.profile_version,
        "frozen": freeze,
    }


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


@router.post(
    "/customer-profiles/backfill",
    dependencies=[ADMIN_ONLY],
    summary="Materialise profiles from applications captured before profiles existed",
)
def backfill(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
    limit: Optional[int] = Query(None, ge=1, le=5000),
):
    """Idempotent and non-destructive.

    A patient with no profile gets one built from their earliest application; a
    patient who already has one is only topped up in the fields it does not
    already hold, so a maintained profile is never clobbered by older data.
    """
    return profiles.backfill_all(db, actor=_actor_id(user), limit=limit)
