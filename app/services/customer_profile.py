"""Customer Profile service — CRUD, versioning, masking, snapshots, backfill.

The profile is the persistent applicant entity (Dave, 2026-07-21); a credit
application is *profile + finance terms + score*. Everything here is driven by
:mod:`app.services.customer_profile_schema` — no field list is restated.

VERSIONING (Dave's mandate: never overwrite)
--------------------------------------------
``update_profile`` bumps ``profile.version`` once per edit and, for each changed
field, marks the current row ``is_current=False`` with ``superseded_at`` /
``superseded_by_id`` and inserts a new current row stamped with the new version
and the acting user. The field-row history IS the changelog (time + user
stamped) — no separate audit table, and a prior address or employer is always
recoverable as a "former" value.

MASKING
-------
Bank transit exposes its last 2 digits and the account number its last 3 (Dave's
Bank Details rows); the SIN never leaves the system at all beyond ``sin_last3``.
Which fields mask, and by how much, is declared on the registry (``FieldSpec.
masking``) — never by ad-hoc slicing at a call site. ``read_profile`` masks by
default; the full value is returned only when the caller passes
``include_sensitive=True``, which the API grants to admins only.

:func:`mask_value` builds and validates every masked value with the SHARED
contract in :mod:`app.api.clinic.v1.masking` (PR #198): ``mask_tail`` produces
the redacted string and ``validate_masked`` / ``validate_last4`` — the same
validators behind the ``MaskedValue`` / ``Last4`` types — refuse to let an
unmasked number through. One masking contract, not two. The response shape stays
``{"masked", "last", "is_masked"}`` for the frontend.

BANK DETAILS ARE READ-THROUGH
-----------------------------
``platform_patient_bank_accounts`` (migration 064, extended by 069) owns borrower
bank accounts: it Fernet-encrypts the full account number, enforces one default
payment source per patient and records Flinks-vs-manual provenance. This service
READS that table to populate the Bank Details block and REJECTS profile writes to
it, pointing the caller at the owning API — a second copy could not stay in step
and would put an unencrypted account number in JSONB.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Mapping, Optional
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.clinic.v1.masking import mask_tail, validate_last4, validate_masked
from app.core.sin_crypto import encrypt_sin
from app.models.platform.borrower_portal import PlatformPatientBankAccount
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.customer_profile import (
    PlatformCustomerProfile,
    PlatformCustomerProfileField,
)
from app.models.platform.event import PlatformEvent
from app.models.platform.patient import PlatformPatient
from app.services import customer_profile_schema as schema
from app.services.customer_profile_schema import (
    BLOCKS,
    FieldSpec,
    ProfileBlock,
    instance_key,
    parse_instance_key,
)
from app.services.customer_profile_validation import (
    ProfileValidationError,
    assert_no_read_through_writes,
    assert_valid,
    completeness,
    validate_profile,
)


class ProfileError(Exception):
    """Business-rule failure (not found / locked / deleted). Routers map to 4xx."""


class ProfileLockedError(ProfileError):
    pass


#: Where a value came from. Dave: automation changes WHO fills a field, not WHAT.
FIELD_SOURCES = (
    "self_reported",
    "staff",
    "id_doc",
    "bank_verification",
    "bureau",
    "backfill",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------


def mask_value(value: Any, visible_suffix: int) -> dict:
    """Mask all but the last ``visible_suffix`` digits, via the SHARED contract.

    The redacted string comes from :func:`app.api.clinic.v1.masking.mask_tail`
    and is then run through ``validate_masked`` / ``validate_last4`` — the same
    validators that back the ``MaskedValue`` / ``Last4`` types — so an unmasked
    number cannot escape this function even if a caller passes a silly
    ``visible_suffix``. The ``{masked, last, is_masked}`` shape is what the
    frontend consumes.
    """
    if value in (None, ""):
        return {"masked": "", "last": "", "is_masked": True}

    masked = mask_tail(str(value), reveal=visible_suffix)
    if masked is None:
        # No digits to reveal (e.g. an alphanumeric account ref): block it whole.
        return {"masked": "•••", "last": "", "is_masked": True}

    validate_masked(masked)
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    suffix = digits[-visible_suffix:] if visible_suffix > 0 else ""
    if suffix:
        validate_last4(suffix)
    return {"masked": masked, "last": suffix, "is_masked": True}


def present_value(spec: FieldSpec, value: Any, *, include_sensitive: bool) -> Any:
    """Wire representation of one stored value, honouring the registry's masking."""
    if spec.masking is None:
        return value
    if include_sensitive and spec.external_storage is None:
        # The SIN (external_storage set) is never returned in full by ANY caller:
        # the profile row only ever holds its last 3 digits in the first place.
        return value
    return mask_value(value, spec.masking.visible_suffix)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_profile(
    db: Session, profile_id: UUID, *, include_deleted: bool = False
) -> PlatformCustomerProfile:
    profile = (
        db.query(PlatformCustomerProfile)
        .filter(PlatformCustomerProfile.id == profile_id)
        .first()
    )
    if profile is None:
        raise ProfileError(f"Customer profile {profile_id} not found")
    if profile.deleted_at is not None and not include_deleted:
        raise ProfileError(f"Customer profile {profile_id} has been deleted")
    return profile


def get_profile_for_patient(
    db: Session, patient_id: UUID, *, include_deleted: bool = False
) -> Optional[PlatformCustomerProfile]:
    query = db.query(PlatformCustomerProfile).filter(
        PlatformCustomerProfile.patient_id == patient_id
    )
    if not include_deleted:
        query = query.filter(PlatformCustomerProfile.deleted_at.is_(None))
    return query.first()


def current_fields(db: Session, profile_id: UUID) -> list[PlatformCustomerProfileField]:
    return (
        db.query(PlatformCustomerProfileField)
        .filter(
            PlatformCustomerProfileField.profile_id == profile_id,
            PlatformCustomerProfileField.is_current.is_(True),
        )
        .all()
    )


#: Bank Details field key -> the ``platform_patient_bank_accounts`` column that
#: owns it. ``account_number`` is deliberately absent: that table stores the full
#: number ONLY Fernet-encrypted and exposes ``account_mask`` for display, so the
#: profile surfaces the mask and never decrypts.
_BANK_ACCOUNT_COLUMNS: dict[str, str] = {
    "bank_name": "institution_name",
    "institution_number": "institution_number",
    "transit_number": "transit_number",
    "account_holder_name": "account_holder",
    "account_type": "account_type",
}


def bank_details_instances(db: Session, patient_id: UUID) -> dict[str, dict[str, Any]]:
    """Read the Bank Details block THROUGH to ``platform_patient_bank_accounts``.

    That table (064/069) owns borrower bank accounts. Returning its rows here
    means the Originations "Bank Details" block renders real data without the
    profile keeping a parallel — and inevitably stale — copy.

    ``account_number`` comes back as the table's stored ``account_mask``: the full
    number lives there Fernet-encrypted and is never decrypted for display.
    """
    rows = (
        db.query(PlatformPatientBankAccount)
        .filter(
            PlatformPatientBankAccount.patient_id == patient_id,
            PlatformPatientBankAccount.status == "active",
        )
        .order_by(
            PlatformPatientBankAccount.is_default.desc(),
            PlatformPatientBankAccount.created_at.asc(),
        )
        .all()
    )
    out: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        values: dict[str, Any] = {
            field_key: getattr(row, column, None)
            for field_key, column in _BANK_ACCOUNT_COLUMNS.items()
            if getattr(row, column, None) not in (None, "")
        }
        if row.account_mask:
            values["account_number"] = row.account_mask
        out[instance_key(ProfileBlock.BANK_DETAILS, index)] = values
    return out


def profile_values(
    db: Session, profile_id: UUID, *, include_read_through: bool = True
) -> dict[str, dict[str, Any]]:
    """Raw current values, keyed by block instance — the validator's input.

    ``include_read_through`` pulls the Bank Details block from its owning table.
    Set it False when validating a WRITE, so a read-through block can never be
    mistaken for something this service stores.
    """
    values: dict[str, dict[str, Any]] = {}
    for row in current_fields(db, profile_id):
        values.setdefault(instance_key(row.block, row.block_index), {})[row.field_key] = row.value

    if include_read_through:
        profile = (
            db.query(PlatformCustomerProfile)
            .filter(PlatformCustomerProfile.id == profile_id)
            .first()
        )
        if profile is not None:
            values.update(bank_details_instances(db, profile.patient_id))
    return values


def read_profile(
    db: Session, profile_id: UUID, *, include_sensitive: bool = False
) -> dict:
    """The profile as the API returns it: values by block, masked per the registry."""
    profile = get_profile(db, profile_id, include_deleted=True)
    raw = profile_values(db, profile_id)

    blocks: list[dict] = []
    for block in sorted(BLOCKS, key=lambda b: b.order):
        indices = sorted(
            {
                parse_instance_key(k)[1]
                for k in raw
                if parse_instance_key(k)[0] == block.block.value
            }
        ) or [0]
        by_key = block.field_map()
        for index in indices:
            stored = raw.get(instance_key(block.block, index)) or {}
            if index != 0 and not stored:
                continue
            blocks.append(
                {
                    "block": block.block.value,
                    "label": block.label,
                    "index": index,
                    "visible": schema.is_block_visible(block.block, raw, index=index),
                    "read_through": block.is_read_through,
                    "owned_by": block.owned_by,
                    "values": {
                        # A read-through block's values already arrive display-safe
                        # from their owning table (``account_mask`` and friends) —
                        # masking them again would double-redact.
                        key: (
                            value
                            if block.is_read_through
                            else present_value(
                                by_key[key], value, include_sensitive=include_sensitive
                            )
                        )
                        for key, value in stored.items()
                        if key in by_key
                    },
                }
            )

    return {
        "id": str(profile.id),
        "patient_id": str(profile.patient_id),
        "version": profile.version,
        "schema_version": profile.schema_version,
        "locked": profile.is_locked,
        "locked_at": profile.locked_at,
        "lock_reason": profile.lock_reason,
        "deleted": profile.is_deleted,
        "deleted_at": profile.deleted_at,
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
        "blocks": blocks,
        "completeness": completeness(raw),
    }


def field_history(
    db: Session,
    profile_id: UUID,
    *,
    block: Optional[str] = None,
    field_key: Optional[str] = None,
    include_sensitive: bool = False,
) -> list[dict]:
    """The changelog: every version of every field, newest first, user + time stamped."""
    query = db.query(PlatformCustomerProfileField).filter(
        PlatformCustomerProfileField.profile_id == profile_id
    )
    if block:
        query = query.filter(PlatformCustomerProfileField.block == block)
    if field_key:
        query = query.filter(PlatformCustomerProfileField.field_key == field_key)
    rows = query.order_by(
        PlatformCustomerProfileField.created_at.desc(),
        PlatformCustomerProfileField.id.desc(),
    ).all()

    out = []
    for row in rows:
        spec = schema.field_spec(row.block, row.field_key)
        out.append(
            {
                "id": row.id,
                "block": row.block,
                "index": row.block_index,
                "field": row.field_key,
                "label": spec.label if spec else row.field_key,
                "value": (
                    present_value(spec, row.value, include_sensitive=include_sensitive)
                    if spec
                    else None
                ),
                "source": row.source,
                "profile_version": row.profile_version,
                "is_current": row.is_current,
                "changed_at": row.created_at,
                "changed_by": row.created_by,
                "superseded_at": row.superseded_at,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _log(
    db: Session,
    *,
    patient_id: UUID,
    event_type: str,
    actor: str,
    payload: dict,
) -> None:
    """Append to platform_events. Hard Rule #6: NEVER any field value in the payload."""
    db.add(
        PlatformEvent(
            patient_id=patient_id, event_type=event_type, actor=actor, payload=payload
        )
    )


def _normalize(value: Any) -> Any:
    """JSONB-safe representation of a supplied value."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def create_profile(
    db: Session,
    *,
    patient_id: UUID,
    values: Mapping[str, Mapping[str, Any]],
    actor: str,
    source: str = "self_reported",
    allow_staff_fields: bool = True,
    require_complete: bool = False,
) -> PlatformCustomerProfile:
    """Create the patient's profile (one per patient) and write version 1 of its fields."""
    patient = db.query(PlatformPatient).filter(PlatformPatient.id == patient_id).first()
    if patient is None:
        raise ProfileError(f"Patient {patient_id} not found")
    if get_profile_for_patient(db, patient_id, include_deleted=True) is not None:
        raise ProfileError(f"Patient {patient_id} already has a customer profile")

    assert_valid(
        values,
        partial=not require_complete,
        allow_staff_fields=allow_staff_fields,
        writing=True,
    )

    profile = PlatformCustomerProfile(
        patient_id=patient_id,
        version=1,
        schema_version=schema.SCHEMA_VERSION,
        created_by=actor,
        updated_by=actor,
    )
    db.add(profile)
    db.flush()

    written = _write_values(db, profile, values, actor=actor, source=source)
    _sync_patient_identity(db, profile, values)

    _log(
        db,
        patient_id=patient_id,
        event_type="customer_profile.created",
        actor=actor,
        payload={
            "profile_id": str(profile.id),
            "version": 1,
            "schema_version": schema.SCHEMA_VERSION,
            "field_count": written,
            "source": source,
        },
    )
    db.commit()
    db.refresh(profile)
    return profile


class DuplicateBorrowerError(ProfileError):
    """A borrower with the supplied email/phone already exists.

    Carries the existing borrower so the caller can point the admin at the
    profile they should be using instead of minting a silent duplicate.
    """

    def __init__(self, patient: PlatformPatient, matched_on: str, profile_id: Optional[UUID]):
        self.patient_id = patient.id
        self.matched_on = matched_on
        self.profile_id = profile_id
        super().__init__(
            f"A borrower already exists with this {matched_on.replace('_', ' ')} "
            f"(patient {patient.id}). Use the existing profile instead of creating "
            "a duplicate."
        )


def _identity_from_values(values: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Pull the denormalized ``platform_patients`` identity out of a profile payload.

    Reads through the registry's own instance keys (``personal`` / ``contact``,
    index 0) rather than restating the field list — the same mapping
    ``_sync_patient_identity`` uses, plus the phone (which has no profile→patient
    sync because it is set once at borrower creation).
    """
    identity: dict[str, Any] = {}
    for key, supplied in values.items():
        block_name, index = parse_instance_key(key)
        if index != 0:
            continue
        for field_key, value in (supplied or {}).items():
            if value in (None, ""):
                continue
            column = _PATIENT_IDENTITY_SYNC.get((block_name, field_key))
            if column is not None:
                identity[column] = value
            elif (block_name, field_key) == (ProfileBlock.CONTACT.value, "main_phone"):
                identity["phone_e164"] = value
    dob = identity.get("dob")
    if dob is not None and not isinstance(dob, date):
        identity["dob"] = date.fromisoformat(str(dob)[:10])
    return identity


def find_duplicate_borrower(
    db: Session, *, email: Optional[str], phone: Optional[str]
) -> Optional[tuple[PlatformPatient, str]]:
    """An existing, non-deleted borrower matching this email or phone (email first).

    Deliberately case-insensitive on email and exact on phone: the phone is
    already stored E.164-normalized by every origination entry point, while
    emails arrive in whatever case the admin typed.
    """
    if email:
        row = (
            db.query(PlatformPatient)
            .filter(
                PlatformPatient.deleted_at.is_(None),
                func.lower(PlatformPatient.email) == email.strip().lower(),
            )
            .order_by(PlatformPatient.created_at.asc())
            .first()
        )
        if row is not None:
            return row, "email"
    if phone:
        row = (
            db.query(PlatformPatient)
            .filter(
                PlatformPatient.deleted_at.is_(None),
                PlatformPatient.phone_e164 == phone.strip(),
            )
            .order_by(PlatformPatient.created_at.asc())
            .first()
        )
        if row is not None:
            return row, "phone"
    return None


def create_borrower(
    db: Session,
    *,
    values: Mapping[str, Mapping[str, Any]],
    actor: str,
    source: str = "staff",
    allow_staff_fields: bool = True,
    require_complete: bool = False,
) -> PlatformCustomerProfile:
    """Mint a NEW borrower (``platform_patients``) AND its customer profile.

    Dave, 2026-07-21: *"there is no way to initiate a new credit application in
    the backend"* — back-office origination starts with a brand-new borrower, and
    until now nothing under ``/admin`` could create one: every profile route
    required a ``patient_id`` that only the applicant journey could mint.

    This is deliberately NOT a parallel profile-creation path: the payload is
    validated by, and persisted through, :func:`create_profile`, so the registry
    rules (mandatory-when-visible, formats, char limits, read-through blocks, SIN
    handling) apply identically. SIN stays OPTIONAL — Dave's legal mandate; the
    registry, not this function, decides what is mandatory.

    Duplicate handling: an email/phone that already belongs to a borrower raises
    :class:`DuplicateBorrowerError` carrying that borrower's ids, so the admin is
    routed to the existing profile instead of creating a second file for the same
    person. Nothing is written when it raises.
    """
    identity = _identity_from_values(values)
    duplicate = find_duplicate_borrower(
        db, email=identity.get("email"), phone=identity.get("phone_e164")
    )
    if duplicate is not None:
        existing, matched_on = duplicate
        existing_profile = get_profile_for_patient(db, existing.id, include_deleted=True)
        raise DuplicateBorrowerError(
            existing, matched_on, existing_profile.id if existing_profile else None
        )

    patient = PlatformPatient(**identity)
    db.add(patient)
    db.flush()

    try:
        profile = create_profile(
            db,
            patient_id=patient.id,
            values=values,
            actor=actor,
            source=source,
            allow_staff_fields=allow_staff_fields,
            require_complete=require_complete,
        )
    except Exception:
        # create_profile validates BEFORE writing; roll the bare patient back so a
        # rejected payload cannot leave an orphan borrower behind.
        db.rollback()
        raise

    _log(
        db,
        patient_id=patient.id,
        event_type="borrower.created",
        actor=actor,
        payload={
            "profile_id": str(profile.id),
            "source": source,
            "origin": "back_office",
            # Keys only — never values (Hard Rule #6).
            "identity_fields": sorted(identity),
        },
    )
    db.commit()
    db.refresh(profile)
    return profile


def update_profile(
    db: Session,
    profile_id: UUID,
    *,
    values: Mapping[str, Mapping[str, Any]],
    actor: str,
    source: str = "staff",
    allow_staff_fields: bool = True,
) -> PlatformCustomerProfile:
    """Edit the profile. Creates a NEW version; prior values become 'former'."""
    profile = get_profile(db, profile_id)
    if profile.is_locked:
        raise ProfileLockedError(
            f"Customer profile {profile_id} is locked; unlock it before editing"
        )

    # Read-through blocks are rejected on their own terms first — merging them
    # in below would make an owned block look like something we store. Only the
    # ownership check runs here: a patch in isolation legitimately lacks the
    # driver fields its own visibility triggers depend on.
    assert_no_read_through_writes(values)

    # Validate the MERGED state, not the patch alone: a visibility trigger may be
    # satisfied by a value the caller is not resending.
    merged = profile_values(db, profile_id, include_read_through=False)
    for key, supplied in values.items():
        merged.setdefault(key, {}).update({k: _normalize(v) for k, v in supplied.items()})
    assert_valid(merged, partial=True, allow_staff_fields=allow_staff_fields)

    profile.version += 1
    profile.updated_by = actor
    db.flush()

    changed = _write_values(db, profile, values, actor=actor, source=source)
    _sync_patient_identity(db, profile, values)

    _log(
        db,
        patient_id=profile.patient_id,
        event_type="customer_profile.updated",
        actor=actor,
        payload={
            "profile_id": str(profile.id),
            "version": profile.version,
            # Keys only — never values (Hard Rule #6).
            "changed_fields": sorted(
                f"{parse_instance_key(k)[0]}.{fk}"
                for k, supplied in values.items()
                for fk in supplied
            ),
            "changed_count": changed,
            "source": source,
        },
    )
    db.commit()
    db.refresh(profile)
    return profile


def _write_values(
    db: Session,
    profile: PlatformCustomerProfile,
    values: Mapping[str, Mapping[str, Any]],
    *,
    actor: str,
    source: str,
) -> int:
    """Supersede-and-insert each supplied value. Returns the number of rows written."""
    if source not in FIELD_SOURCES:
        raise ProfileError(f"Invalid source {source!r}; must be one of {FIELD_SOURCES}")

    written = 0
    for key, supplied in values.items():
        block_name, index = parse_instance_key(key)
        block = schema.block_spec(block_name)
        if block is None or block.is_read_through:
            # Read-through blocks are owned elsewhere; the validator already
            # rejected them, this is the belt-and-braces so no code path can
            # ever create a parallel copy.
            continue
        by_key = block.field_map()
        for field_key, raw_value in (supplied or {}).items():
            spec = by_key.get(field_key)
            if spec is None or spec.display_from is not None:
                # Unknown keys are rejected by the validator; derived values are
                # displayed from their source field and never stored.
                continue

            value = _normalize(raw_value)
            if spec.external_storage == "platform_patients.sin_encrypted":
                value = _store_sin(db, profile.patient_id, value)
                if value is None:
                    continue

            existing = (
                db.query(PlatformCustomerProfileField)
                .filter(
                    PlatformCustomerProfileField.profile_id == profile.id,
                    PlatformCustomerProfileField.block == block_name,
                    PlatformCustomerProfileField.block_index == index,
                    PlatformCustomerProfileField.field_key == field_key,
                    PlatformCustomerProfileField.is_current.is_(True),
                )
                .first()
            )
            if existing is not None and existing.value == value:
                continue  # no-op edit: do not manufacture a version

            row = PlatformCustomerProfileField(
                profile_id=profile.id,
                block=block_name,
                block_index=index,
                field_key=field_key,
                value=value,
                source=source,
                profile_version=profile.version,
                is_current=True,
                created_by=actor,
            )
            db.add(row)
            db.flush()
            written += 1

            if existing is not None:
                existing.is_current = False
                existing.superseded_at = _now()
                existing.superseded_by_id = row.id
                db.flush()
    return written


def _store_sin(db: Session, patient_id: UUID, value: Any) -> Optional[dict]:
    """SIN never lands in profile storage.

    The full value is encrypted onto ``platform_patients.sin_encrypted`` (the one
    place in the system that holds it); the profile field row keeps only the last
    three digits, so no read path can ever surface more.
    """
    if value in (None, ""):
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if not digits:
        return None
    patient = db.query(PlatformPatient).filter(PlatformPatient.id == patient_id).first()
    if patient is not None:
        patient.sin_encrypted = encrypt_sin(digits)
        patient.sin_last3 = digits[-3:]
        patient.sin_collected_at = _now()
        db.flush()
    return {"last3": digits[-3:], "stored": "platform_patients.sin_encrypted"}


#: profile field -> the denormalized identity column it keeps in step on
#: ``platform_patients``. Those columns are read across the platform (queues,
#: search, notifications), so an edit to the profile must not leave them stale.
_PATIENT_IDENTITY_SYNC: dict[tuple[str, str], str] = {
    (ProfileBlock.PERSONAL.value, "first_name"): "legal_first_name",
    (ProfileBlock.PERSONAL.value, "last_name"): "legal_last_name",
    (ProfileBlock.PERSONAL.value, "date_of_birth"): "dob",
    (ProfileBlock.CONTACT.value, "email"): "email",
}


def _sync_patient_identity(
    db: Session,
    profile: PlatformCustomerProfile,
    values: Mapping[str, Mapping[str, Any]],
) -> None:
    patient = (
        db.query(PlatformPatient).filter(PlatformPatient.id == profile.patient_id).first()
    )
    if patient is None:
        return
    for key, supplied in values.items():
        block_name, index = parse_instance_key(key)
        if index != 0:
            continue
        for field_key, value in (supplied or {}).items():
            column = _PATIENT_IDENTITY_SYNC.get((block_name, field_key))
            if column is None or value in (None, ""):
                continue
            if column == "dob":
                value = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
            setattr(patient, column, value)
    db.flush()


# ---------------------------------------------------------------------------
# Lock / unlock / soft-delete
# ---------------------------------------------------------------------------


def lock_profile(
    db: Session, profile_id: UUID, *, actor: str, reason: Optional[str] = None
) -> PlatformCustomerProfile:
    profile = get_profile(db, profile_id)
    if profile.is_locked:
        return profile
    profile.locked_at = _now()
    profile.locked_by = actor
    profile.lock_reason = reason
    _log(
        db,
        patient_id=profile.patient_id,
        event_type="customer_profile.locked",
        actor=actor,
        payload={"profile_id": str(profile.id), "version": profile.version, "reason": reason},
    )
    db.commit()
    db.refresh(profile)
    return profile


def unlock_profile(
    db: Session, profile_id: UUID, *, actor: str, reason: Optional[str] = None
) -> PlatformCustomerProfile:
    profile = get_profile(db, profile_id)
    if not profile.is_locked:
        return profile
    profile.locked_at = None
    profile.locked_by = None
    profile.lock_reason = None
    _log(
        db,
        patient_id=profile.patient_id,
        event_type="customer_profile.unlocked",
        actor=actor,
        payload={"profile_id": str(profile.id), "version": profile.version, "reason": reason},
    )
    db.commit()
    db.refresh(profile)
    return profile


def delete_profile(
    db: Session, profile_id: UUID, *, actor: str, reason: Optional[str] = None
) -> PlatformCustomerProfile:
    """SOFT delete only — PIPEDA retention. Field history is preserved verbatim."""
    profile = get_profile(db, profile_id)
    profile.deleted_at = _now()
    profile.deleted_by = actor
    profile.delete_reason = reason
    _log(
        db,
        patient_id=profile.patient_id,
        event_type="customer_profile.deleted",
        actor=actor,
        payload={"profile_id": str(profile.id), "version": profile.version, "reason": reason},
    )
    db.commit()
    db.refresh(profile)
    return profile


def restore_profile(db: Session, profile_id: UUID, *, actor: str) -> PlatformCustomerProfile:
    profile = get_profile(db, profile_id, include_deleted=True)
    profile.deleted_at = None
    profile.deleted_by = None
    profile.delete_reason = None
    _log(
        db,
        patient_id=profile.patient_id,
        event_type="customer_profile.restored",
        actor=actor,
        payload={"profile_id": str(profile.id), "version": profile.version},
    )
    db.commit()
    db.refresh(profile)
    return profile


# ---------------------------------------------------------------------------
# Application <- profile
# ---------------------------------------------------------------------------


def build_snapshot(db: Session, profile_id: UUID) -> dict:
    """The frozen profile state an application is decided on.

    Extends the existing ``product_config_snapshot`` pattern: the decision must
    stay reproducible after the borrower edits their profile. Bank transit and
    account numbers are stored MASKED in the snapshot — a decision never needs
    the full number, and an un-purgeable JSONB copy of it is a liability.
    """
    profile = get_profile(db, profile_id, include_deleted=True)
    raw = profile_values(db, profile_id)
    frozen: dict[str, dict[str, Any]] = {}
    for key, stored in raw.items():
        block_name, _ = parse_instance_key(key)
        block = schema.block_spec(block_name)
        if block is None:
            continue
        by_key = block.field_map()
        frozen[key] = {
            field_key: (
                value
                if block.is_read_through  # already display-safe from its owner
                else present_value(by_key[field_key], value, include_sensitive=False)
            )
            for field_key, value in stored.items()
            if field_key in by_key
        }
    return {
        "profile_id": str(profile.id),
        "profile_version": profile.version,
        "schema_version": profile.schema_version,
        "captured_at": _now().isoformat(),
        "completeness": completeness(raw),
        "values": frozen,
    }


def attach_profile_to_application(
    db: Session,
    application: PlatformCreditApplication,
    profile_id: UUID,
    *,
    actor: str,
    freeze: bool = True,
) -> PlatformCreditApplication:
    """Link an application to a profile and (by default) freeze the profile state.

    Also populates the application's structured, scored columns from the profile
    so the existing decision engine keeps working unchanged.
    """
    profile = get_profile(db, profile_id)
    if profile.patient_id != application.patient_id:
        raise ProfileError("Profile and application belong to different patients")

    application.customer_profile_id = profile.id
    application.profile_version = profile.version
    if freeze:
        application.profile_snapshot = build_snapshot(db, profile_id)
        application.profile_snapshot_at = _now()

    apply_profile_to_application_columns(db, application, profile_id)

    _log(
        db,
        patient_id=application.patient_id,
        event_type="customer_profile.attached_to_application",
        actor=actor,
        payload={
            "profile_id": str(profile.id),
            "application_id": str(application.id),
            "profile_version": profile.version,
            "frozen": freeze,
        },
    )
    db.commit()
    db.refresh(application)
    return application


#: profile (block, field) -> the migration-043 column on
#: ``platform_credit_applications`` it populates. Backwards compatibility: the
#: decision engine and every existing test read these columns, so a
#: profile-originated application must fill them exactly as a hand-entered one did.
_APPLICATION_COLUMN_MAP: dict[tuple[str, str], str] = {
    ("personal", "first_name"): "first_name",
    ("personal", "middle_name"): "middle_name",
    ("personal", "last_name"): "last_name",
    ("personal", "date_of_birth"): "date_of_birth",
    ("personal", "marital_status"): "marital_status",
    ("personal", "number_of_dependents"): "number_of_dependents",
    ("personal", "citizenship"): "citizenship",
    ("personal", "education"): "education",
    ("contact", "email"): "email",
    ("contact", "main_phone"): "main_phone",
    ("contact", "alternative_phone"): "alternative_phone",
    ("identification", "id_type"): "id_type",
    ("identification", "province_of_issue"): "id_province_of_issue",
    ("identification", "expiry_date"): "id_expiry",
    ("current_address", "street_address"): "residence_street",
    ("current_address", "apartment_unit"): "residence_unit",
    ("current_address", "city"): "residence_city",
    ("current_address", "province"): "residence_province",
    ("current_address", "postal_code"): "residence_postal_code",
    ("current_address", "residential_status"): "residential_status",
    ("primary_income", "next_pay_date"): "next_pay_date",
    ("primary_income", "pay_frequency"): "pay_frequency",
    ("primary_income", "employer_name"): "employer_name",
    ("primary_income", "job_title"): "job_title",
    ("primary_income", "hire_date"): "hire_date",
    ("primary_income", "work_phone"): "work_phone",
    ("primary_income", "work_phone_extension"): "work_phone_ext",
    ("financial", "number_of_credit_accounts"): "number_of_credit_accounts",
}

_DATE_COLUMNS = {
    "date_of_birth",
    "id_expiry",
    "next_pay_date",
    "hire_date",
}

_CURRENCY_TO_CENTS: dict[tuple[str, str], str] = {
    ("primary_income", "net_monthly_income"): "net_monthly_income_cents",
    ("financial", "monthly_car_payment"): "monthly_car_payment_cents",
    ("financial", "other_monthly_expenses"): "non_discretionary_expenses_cents",
}

#: Dave's Car Owner codes -> the existing ``platform_car_ownership`` enum.
_CAR_OWNER_TO_ENUM = {
    "yes_paid_in_full": "fully_paid",
    "yes_financing_leasing": "financing",
    "no": "none",
}


def _to_cents(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(round(float(str(value).replace("$", "").replace(",", "")) * 100))
    except (TypeError, ValueError):
        return None


def apply_profile_to_application_columns(
    db: Session, application: PlatformCreditApplication, profile_id: UUID
) -> None:
    """Populate the application's structured columns from the profile.

    Backwards compatibility, deliberately one-way: the profile is the source of
    truth, the columns are the decision engine's (unchanged) input. Nothing here
    clears a column the profile has no value for, so an application that was
    hand-populated before this feature keeps whatever it had.
    """
    raw = profile_values(db, profile_id)
    for (block_name, field_key), column in _APPLICATION_COLUMN_MAP.items():
        value = (raw.get(block_name) or {}).get(field_key)
        if value in (None, ""):
            continue
        if column in _DATE_COLUMNS:
            try:
                value = date.fromisoformat(str(value)[:10])
            except ValueError:
                continue
        setattr(application, column, value)

    for (block_name, field_key), column in _CURRENCY_TO_CENTS.items():
        cents = _to_cents((raw.get(block_name) or {}).get(field_key))
        if cents is not None:
            setattr(application, column, cents)

    # Housing payment: whichever of rent/mortgage the residential status made visible.
    address = raw.get(ProfileBlock.CURRENT_ADDRESS.value) or {}
    housing = _to_cents(address.get("monthly_rent") or address.get("monthly_mortgage_payment"))
    if housing is not None:
        application.monthly_housing_payment_cents = housing

    income_type = (raw.get(ProfileBlock.PRIMARY_INCOME.value) or {}).get("income_type")
    engine_income = schema.INCOME_TYPE_TO_ENGINE_ENUM.get(str(income_type))
    if engine_income:
        application.income_type = engine_income

    car_owner = (raw.get(ProfileBlock.FINANCIAL.value) or {}).get("car_owner")
    engine_car = _CAR_OWNER_TO_ENUM.get(str(car_owner))
    if engine_car:
        application.car_ownership = engine_car

    # The ID number field is one of four, gated on id_type.
    identification = raw.get(ProfileBlock.IDENTIFICATION.value) or {}
    id_number = next(
        (
            identification.get(key)
            for key in (
                "drivers_license_number",
                "government_photo_id_number",
                "permanent_residence_card_number",
                "passport_number",
            )
            if identification.get(key)
        ),
        None,
    )
    if id_number:
        application.id_number = id_number

    db.flush()


def assert_complete_for_application(db: Session, profile_id: UUID) -> None:
    """Raise unless every visible mandatory field is populated."""
    issues = validate_profile(profile_values(db, profile_id), partial=False)
    if issues:
        raise ProfileValidationError(issues)


# ---------------------------------------------------------------------------
# Backfill — existing applications -> profiles
# ---------------------------------------------------------------------------

#: inverse of ``_APPLICATION_COLUMN_MAP`` plus the derived cases; used to
#: materialise a profile from an application captured before profiles existed.
_BACKFILL_FROM_COLUMNS: dict[str, tuple[str, str]] = {
    column: block_field for block_field, column in _APPLICATION_COLUMN_MAP.items()
}

_ENGINE_INCOME_TO_PROFILE = {
    engine: code for code, engine in schema.INCOME_TYPE_TO_ENGINE_ENUM.items()
}
_ENUM_CAR_TO_PROFILE = {
    "fully_paid": "yes_paid_in_full",
    "financing": "yes_financing_leasing",
    "leasing": "yes_financing_leasing",
    "none": "no",
}


def application_to_profile_values(
    application: PlatformCreditApplication,
) -> dict[str, dict[str, Any]]:
    """Read an existing application's columns back into registry-shaped values.

    Lossy by construction — the pre-profile schema has no Sex, Alternative Phone
    Type, Bank Details or previous-address block, and stores a single
    ``monthly_housing_payment_cents`` rather than rent/mortgage separately. Those
    stay unset and surface as "missing" in ``completeness``; nothing is invented.
    """
    values: dict[str, dict[str, Any]] = {}

    def put(block: str, key: str, value: Any) -> None:
        if value in (None, ""):
            return
        if isinstance(value, (date, datetime)):
            value = value.isoformat()
        values.setdefault(block, {})[key] = value

    for column, (block_name, field_key) in _BACKFILL_FROM_COLUMNS.items():
        put(block_name, field_key, getattr(application, column, None))

    for (block_name, field_key), column in _CURRENCY_TO_CENTS.items():
        cents = getattr(application, column, None)
        if cents is not None:
            put(block_name, field_key, f"{cents / 100:.2f}")

    income_type = _ENGINE_INCOME_TO_PROFILE.get(str(application.income_type or ""))
    if income_type:
        put(ProfileBlock.PRIMARY_INCOME.value, "income_type", income_type)

    car = _ENUM_CAR_TO_PROFILE.get(str(application.car_ownership or ""))
    if car:
        put(ProfileBlock.FINANCIAL.value, "car_owner", car)

    # Housing: assign to rent or mortgage according to the residential status, so
    # the value survives into the field the registry makes visible.
    housing = getattr(application, "monthly_housing_payment_cents", None)
    status = getattr(application, "residential_status", None)
    if housing is not None and status:
        amount = f"{housing / 100:.2f}"
        if str(status).startswith("own"):
            put(ProfileBlock.CURRENT_ADDRESS.value, "monthly_mortgage_payment", amount)
        elif str(status) == "rent":
            put(ProfileBlock.CURRENT_ADDRESS.value, "monthly_rent", amount)

    # ID number lands in the field its id_type makes visible.
    id_field = {
        "drivers_license": "drivers_license_number",
        "government_photo_id": "government_photo_id_number",
        "permanent_residence_card": "permanent_residence_card_number",
        "passport": "passport_number",
    }.get(str(application.id_type or ""))
    if id_field and application.id_number:
        put(ProfileBlock.IDENTIFICATION.value, id_field, application.id_number)

    # Address tenure: the old model stored years+months at address, not a date.
    years = getattr(application, "time_at_address_years", None)
    months = getattr(application, "time_at_address_months", None)
    if years is not None or months is not None:
        total_months = (years or 0) * 12 + (months or 0)
        reference = application.created_at or _now()
        approx_year = reference.year - total_months // 12
        approx_month = reference.month - total_months % 12
        if approx_month <= 0:
            approx_month += 12
            approx_year -= 1
        put(
            ProfileBlock.CURRENT_ADDRESS.value,
            "resided_since",
            date(approx_year, approx_month, 1).isoformat(),
        )

    return values


def backfill_profile_from_application(
    db: Session,
    application: PlatformCreditApplication,
    *,
    actor: str = "system",
    attach: bool = True,
) -> Optional[PlatformCustomerProfile]:
    """Materialise (or top up) the patient's profile from one application.

    Idempotent and non-destructive: an existing profile is only topped up with
    fields it does not already hold, so a real, user-maintained profile is never
    overwritten by older application data.
    """
    values = application_to_profile_values(application)
    profile = get_profile_for_patient(db, application.patient_id, include_deleted=True)

    if profile is None:
        if not values:
            return None
        profile = create_profile(
            db,
            patient_id=application.patient_id,
            values=values,
            actor=actor,
            source="backfill",
        )
    else:
        existing = profile_values(db, profile.id)
        gaps = {
            block: {
                key: value
                for key, value in supplied.items()
                if (existing.get(block) or {}).get(key) in (None, "")
            }
            for block, supplied in values.items()
        }
        gaps = {block: v for block, v in gaps.items() if v}
        if gaps and not profile.is_locked:
            update_profile(db, profile.id, values=gaps, actor=actor, source="backfill")

    if attach and application.customer_profile_id is None:
        application.customer_profile_id = profile.id
        application.profile_version = profile.version
        db.commit()
    return profile


def backfill_all(
    db: Session, *, actor: str = "system", limit: Optional[int] = None
) -> dict:
    """Backfill every application that has no profile link yet. Re-runnable."""
    query = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.customer_profile_id.is_(None))
        .order_by(PlatformCreditApplication.created_at.asc())
    )
    if limit:
        query = query.limit(limit)

    created = topped_up = skipped = 0
    seen: set[UUID] = set()
    for application in query.all():
        had_profile = get_profile_for_patient(
            db, application.patient_id, include_deleted=True
        ) is not None
        try:
            profile = backfill_profile_from_application(db, application, actor=actor)
        except (ProfileError, ProfileValidationError):
            db.rollback()
            skipped += 1
            continue
        if profile is None:
            skipped += 1
        elif had_profile:
            topped_up += 1
        else:
            created += 1
        seen.add(application.patient_id)
    return {
        "created": created,
        "topped_up": topped_up,
        "skipped": skipped,
        "patients": len(seen),
    }


__all__ = [
    "FIELD_SOURCES",
    "DuplicateBorrowerError",
    "ProfileError",
    "ProfileLockedError",
    "application_to_profile_values",
    "assert_complete_for_application",
    "attach_profile_to_application",
    "backfill_all",
    "backfill_profile_from_application",
    "build_snapshot",
    "create_borrower",
    "create_profile",
    "current_fields",
    "delete_profile",
    "field_history",
    "find_duplicate_borrower",
    "get_profile",
    "get_profile_for_patient",
    "lock_profile",
    "mask_value",
    "profile_values",
    "read_profile",
    "restore_profile",
    "unlock_profile",
    "update_profile",
]
