"""Marketplace listing service — the lead-engine orchestration layer (spec §6.3).

Module-level functions taking ``db: Session`` first. This layer owns the
lifecycle of a marketplace listing and the vendor-interest rows hanging off it:

  patient creates a listing -> vendors browse de-identified leads -> a vendor
  expresses interest -> the patient sees interested clinics and selects one ->
  a lead charge is RECORDED against the chosen interest at the configured
  trigger (spec §6.2).

Pricing (``base_lead_price_cents``) and vendor-visibility/redaction are delegated
to ``app.services.marketplace.pricing`` and ``app.services.marketplace.visibility``;
this module never re-implements either. PII never reaches a vendor pre-selection —
every vendor-facing read goes through ``vendor_view`` / ``is_visible_to_vendors``.

Money is ALWAYS integer cents. ``_charge_lead`` only RECORDS the charge on the
interest row (``lead_charge_cents`` / ``lead_charged_at`` / ``charge_trigger``).
There is no real vendor payment rail at launch — real Zumrails vendor billing is
out of scope / gated. The recorded charge is what the (future) billing run reads.

Exceptions are plain Python exceptions; the API layer maps them to HTTP codes:
  LookupError  -> 404 (listing/patient not found)
  PermissionError -> 403 (owner-scoped action by a non-owner)
  ValueError   -> 400 (invalid state, e.g. interest on an inactive listing)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.services import consent_service
from app.models.platform.event import PlatformEvent
from app.models.platform.marketplace import (
    PlatformMarketplaceListing,
    PlatformMarketplaceVendorInterest,
)
from app.models.platform.patient import PlatformPatient
from app.services.marketplace.pricing import get_charge_trigger, price_lead
from app.services.marketplace.visibility import is_visible_to_vendors, vendor_view


# Ordering for the ``min_verification`` filter. Higher = deeper verification.
# Anything unknown sorts BELOW the lowest known rung so an unrecognized snapshot
# never accidentally satisfies a "must be at least X" filter.
_VERIFICATION_ORDER: dict[str, int] = {
    "none": 0,
    "email_verified": 1,
    "phone_verified": 1,
    "id_verified": 2,
    "id_bank_verified": 3,
    "id_bank_cb_verified": 4,
}
_UNKNOWN_VERIFICATION_RANK = -1


def _verification_rank(depth: str | None) -> int:
    return _VERIFICATION_ORDER.get(depth, _UNKNOWN_VERIFICATION_RANK)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _emit_event(db: Session, *, event_type: str, payload: dict, patient_id=None) -> None:
    """Append a PlatformEvent (actor='marketplace'). Does NOT commit on its own."""
    db.add(
        PlatformEvent(
            event_type=event_type,
            actor="marketplace",
            payload=payload,
            patient_id=patient_id,
        )
    )


def _load_listing(db: Session, listing_id) -> PlatformMarketplaceListing:
    listing = (
        db.query(PlatformMarketplaceListing)
        .filter(PlatformMarketplaceListing.id == listing_id)
        .one_or_none()
    )
    if listing is None:
        raise LookupError(f"Listing not found: {listing_id}")
    return listing


def _is_live(listing: PlatformMarketplaceListing) -> bool:
    """A listing is live to vendors if active, unexpired, and lead_state visible."""
    if listing.status != "active":
        return False
    if listing.expires_at is not None and listing.expires_at <= _now():
        return False
    return is_visible_to_vendors(listing.lead_state)


# --- patient-side -----------------------------------------------------------


def create_listing(
    db: Session,
    *,
    patient_id,
    treatment_categories: list[str],
    treatment_urgency: str,
    estimated_budget_cents: int | None,
    location_postal_code: str,
    max_travel_km: int = 25,
    ttl_days: int = 30,
    consent_acknowledged: bool = False,
) -> PlatformMarketplaceListing:
    """Create an active listing for ``patient_id``.

    Requires the patient's ``marketplace_listing`` opt-in consent (PIPEDA: this
    discloses a de-identified lead to third-party clinics). The consent is
    recorded (verbatim text + version) before the listing is created.

    Denormalizes ``lead_state`` + ``verification_depth`` from the patient onto
    the listing (a stable historical snapshot), prices it via ``price_lead``,
    sets ``expires_at = now + ttl_days``, flips ``patient.marketplace_listed``,
    and emits ``marketplace.listing_created``. Commits.
    """
    # Exclude soft-deleted (PIPEDA-erased) patients — they must not be listed.
    patient = (
        db.query(PlatformPatient)
        .filter(PlatformPatient.id == patient_id)
        .filter(PlatformPatient.deleted_at.is_(None))
        .one_or_none()
    )
    if patient is None:
        raise LookupError(f"Patient not found: {patient_id}")

    # Opt-in consent gate (spec §8.1: marketplace_listing is opt-in, never bundled).
    if not consent_acknowledged:
        raise ValueError(
            "marketplace_listing consent is required before publishing a listing"
        )
    consent_service.record_consent(db, patient_id, "marketplace_listing", granted=True)

    lead_state = patient.lead_state
    verification_depth = patient.verification_depth

    base_price = price_lead(
        lead_state=lead_state,
        treatment_categories=treatment_categories,
        urgency=treatment_urgency,
        verification_depth=verification_depth,
    )

    listing = PlatformMarketplaceListing(
        patient_id=patient.id,
        status="active",
        treatment_categories=list(treatment_categories),
        treatment_urgency=treatment_urgency,
        estimated_budget_cents=estimated_budget_cents,
        location_postal_code=location_postal_code,
        max_travel_km=max_travel_km,
        lead_state=lead_state,
        verification_depth=verification_depth,
        base_lead_price_cents=base_price,
        expires_at=_now() + timedelta(days=ttl_days),
    )
    db.add(listing)

    patient.marketplace_listed = True

    db.flush()  # assign listing.id before the event references it
    _emit_event(
        db,
        event_type="marketplace.listing_created",
        payload={
            "treatment_categories": list(treatment_categories),
            "lead_state": lead_state,
        },
        patient_id=patient.id,
    )

    db.commit()
    db.refresh(listing)
    return listing


def _refresh_marketplace_listed(db: Session, patient_id) -> None:
    """Recompute ``patient.marketplace_listed`` from whether a LIVE listing exists.

    The flag is a denormalized cache; nothing reset it after pause/close/expiry, so
    it stayed True forever once set. Recompute it from the source of truth (an
    active, non-expired listing) on every status transition. Must be called before
    the surrounding commit."""
    db.flush()  # ensure the caller's pending status change is visible to the query
    has_live = (
        db.query(PlatformMarketplaceListing.id)
        .filter(PlatformMarketplaceListing.patient_id == patient_id)
        .filter(PlatformMarketplaceListing.status == "active")
        .filter(PlatformMarketplaceListing.expires_at > _now())
        .first()
        is not None
    )
    patient = (
        db.query(PlatformPatient).filter(PlatformPatient.id == patient_id).one_or_none()
    )
    if patient is not None:
        patient.marketplace_listed = has_live


def pause_listing(db: Session, *, listing_id, patient_id) -> PlatformMarketplaceListing:
    """Owner-scoped pause. PermissionError if the caller doesn't own the listing."""
    listing = _load_listing(db, listing_id)
    if listing.patient_id != patient_id:
        raise PermissionError("Listing is owned by another patient")
    listing.status = "paused"
    _refresh_marketplace_listed(db, patient_id)
    db.commit()
    db.refresh(listing)
    return listing


def interested_clinics(db: Session, *, listing_id, patient_id) -> list[dict]:
    """Owner-scoped list of vendors that have expressed interest in the listing."""
    listing = _load_listing(db, listing_id)
    if listing.patient_id != patient_id:
        raise PermissionError("Listing is owned by another patient")
    return [
        {
            "vendor_id": i.vendor_id,
            "expressed_at": i.expressed_at,
            "appointment_booked_at": i.appointment_booked_at,
        }
        for i in listing.interests
    ]


def select_clinic(
    db: Session, *, listing_id, patient_id, vendor_id
) -> PlatformMarketplaceListing:
    """Owner-scoped: the patient picks one interested vendor and closes the listing.

    The chosen vendor must already have an interest row. Sets
    ``accepted_vendor_id`` / ``accepted_at`` on the listing, stamps the chosen
    interest's ``selected_by_patient_at``, flips status to ``closed``, and
    RECORDS the lead charge if the configured trigger is ``patient_selected``.
    """
    listing = _load_listing(db, listing_id)
    if listing.patient_id != patient_id:
        raise PermissionError("Listing is owned by another patient")

    interest = _find_interest(db, listing_id=listing_id, vendor_id=vendor_id)
    if interest is None:
        raise ValueError("Selected vendor has not expressed interest in this listing")

    now = _now()
    listing.accepted_vendor_id = vendor_id
    listing.accepted_at = now
    listing.status = "closed"
    interest.selected_by_patient_at = now

    if get_charge_trigger() == "patient_selected":
        _charge_lead(interest, listing, "patient_selected")

    _refresh_marketplace_listed(db, patient_id)
    db.commit()
    db.refresh(listing)
    return listing


# --- vendor-side ------------------------------------------------------------


def list_leads_for_vendor(
    db: Session,
    *,
    treatment_category: str | None = None,
    max_distance_km: int | None = None,
    min_verification: str | None = None,
) -> list[dict]:
    """Return ``vendor_view`` projections of every live, visible listing.

    "Live" = status 'active', not expired, and ``is_visible_to_vendors``. Filters:
      treatment_category: listing must carry this category.
      min_verification:   listing.verification_depth must rank >= this rung
                          (none < id_verified < id_bank_verified < id_bank_cb_verified;
                          unknown snapshots sort lowest so they never satisfy a floor).
      max_distance_km:    PLACEHOLDER — no geocoding yet, so this filters on
                          ``listing.max_travel_km <= max_distance_km``. Real
                          FSA-to-clinic distance is future work.
    """
    # Join the patient to exclude soft-deleted (PIPEDA-erased) patients' listings —
    # an erased patient must not remain discoverable as a marketplace lead.
    listings = (
        db.query(PlatformMarketplaceListing)
        .join(PlatformPatient, PlatformPatient.id == PlatformMarketplaceListing.patient_id)
        .filter(PlatformPatient.deleted_at.is_(None))
        .filter(PlatformMarketplaceListing.status == "active")
        .filter(PlatformMarketplaceListing.expires_at > _now())
        .all()
    )

    min_rank = _verification_rank(min_verification) if min_verification else None

    out: list[dict] = []
    for listing in listings:
        if not is_visible_to_vendors(listing.lead_state):
            continue
        if treatment_category is not None and treatment_category not in (
            listing.treatment_categories or []
        ):
            continue
        if min_rank is not None and _verification_rank(listing.verification_depth) < min_rank:
            continue
        # Placeholder distance filter — see docstring.
        if max_distance_km is not None and listing.max_travel_km > max_distance_km:
            continue
        out.append(vendor_view(listing))
    return out


def express_interest(
    db: Session, *, listing_id, vendor_id
) -> PlatformMarketplaceVendorInterest:
    """Vendor expresses interest in a listing. IDEMPOTENT.

    If an interest row already exists for (vendor_id, listing_id) it is returned
    unchanged. On first interest, if the configured charge trigger is
    ``expressed_interest`` the lead charge is recorded. Emits
    ``marketplace.vendor_interest``. The listing must be live + visible.
    """
    listing = _load_listing(db, listing_id)

    existing = _find_interest(db, listing_id=listing_id, vendor_id=vendor_id)
    if existing is not None:
        return existing

    if not _is_live(listing):
        raise ValueError("Listing is not available to vendors")

    interest = PlatformMarketplaceVendorInterest(
        listing_id=listing_id,
        vendor_id=vendor_id,
    )
    db.add(interest)

    if get_charge_trigger() == "expressed_interest":
        _charge_lead(interest, listing, "expressed_interest")

    _emit_event(
        db,
        event_type="marketplace.vendor_interest",
        payload={
            "vendor_id": str(vendor_id),
            "treatment_categories": list(listing.treatment_categories or []),
        },
        patient_id=listing.patient_id,
    )

    try:
        db.commit()
    except IntegrityError:
        # Race: a concurrent express_interest inserted the (vendor, listing) row
        # first. The unique index held (no double-charge) — return the winner's row
        # so the call stays idempotent instead of surfacing a 500.
        db.rollback()
        existing = _find_interest(db, listing_id=listing_id, vendor_id=vendor_id)
        if existing is not None:
            return existing
        raise
    db.refresh(interest)
    return interest


def get_listing_for_vendor(db: Session, *, listing_id, vendor_id) -> dict | None:
    """Fuller vendor view — ONLY if this vendor has expressed interest, else None.

    Still PII-free unless the patient has already selected this vendor. The base
    projection comes from ``vendor_view``; once selected, the vendor additionally
    learns that it was selected (``selected_by_patient`` / ``accepted_at``).
    Returns None if the vendor has no interest row (endpoint -> 404).
    """
    listing = _load_listing(db, listing_id)
    interest = _find_interest(db, listing_id=listing_id, vendor_id=vendor_id)
    if interest is None:
        return None

    view = vendor_view(listing)
    selected = listing.accepted_vendor_id == vendor_id
    view["selected_by_patient"] = selected
    view["expressed_at"] = (
        interest.expressed_at.isoformat() if interest.expressed_at else None
    )
    view["appointment_booked_at"] = (
        interest.appointment_booked_at.isoformat()
        if interest.appointment_booked_at
        else None
    )
    if selected and listing.accepted_at is not None:
        view["accepted_at"] = listing.accepted_at.isoformat()
    return view


def book_appointment(
    db: Session, *, listing_id, vendor_id
) -> PlatformMarketplaceVendorInterest:
    """Vendor books the appointment. Requires prior interest.

    Stamps ``appointment_booked_at`` and records the charge if the configured
    trigger is ``appointment_booked``.
    """
    _load_listing(db, listing_id)  # 404 if listing is gone
    interest = _find_interest(db, listing_id=listing_id, vendor_id=vendor_id)
    if interest is None:
        raise ValueError("Vendor has not expressed interest in this listing")

    interest.appointment_booked_at = _now()

    if get_charge_trigger() == "appointment_booked":
        listing = _load_listing(db, listing_id)
        _charge_lead(interest, listing, "appointment_booked")

    db.commit()
    db.refresh(interest)
    return interest


def vendor_billing(db: Session, *, vendor_id) -> list[dict]:
    """Lead-charge history for a vendor (only rows where a charge was recorded)."""
    rows = (
        db.query(PlatformMarketplaceVendorInterest)
        .filter(PlatformMarketplaceVendorInterest.vendor_id == vendor_id)
        .filter(PlatformMarketplaceVendorInterest.lead_charge_cents.isnot(None))
        .order_by(PlatformMarketplaceVendorInterest.lead_charged_at.desc())
        .all()
    )
    return [
        {
            "listing_id": r.listing_id,
            "lead_charge_cents": r.lead_charge_cents,
            "lead_charged_at": r.lead_charged_at,
            "charge_trigger": r.charge_trigger,
        }
        for r in rows
    ]


# --- internals --------------------------------------------------------------


def _find_interest(
    db: Session, *, listing_id, vendor_id
) -> PlatformMarketplaceVendorInterest | None:
    return (
        db.query(PlatformMarketplaceVendorInterest)
        .filter(PlatformMarketplaceVendorInterest.listing_id == listing_id)
        .filter(PlatformMarketplaceVendorInterest.vendor_id == vendor_id)
        .one_or_none()
    )


def _charge_lead(
    interest: PlatformMarketplaceVendorInterest,
    listing: PlatformMarketplaceListing,
    trigger: str,
) -> None:
    """RECORD the lead charge on the interest row (integer cents).

    NOTE: this only records the charge — there is no real vendor payment rail at
    launch. Real Zumrails vendor billing is out of scope / gated. The recorded
    ``lead_charge_cents`` / ``lead_charged_at`` / ``charge_trigger`` is what a
    future billing run reads. Idempotent in practice because each trigger fires
    once per interest lifecycle; if already charged we leave the original stamp.
    """
    if interest.lead_charge_cents is not None:
        return
    interest.lead_charge_cents = listing.base_lead_price_cents
    interest.lead_charged_at = _now()
    interest.charge_trigger = trigger
