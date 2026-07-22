"""Server-side validation of a Customer Profile, driven entirely by the registry.

Every rule here reads :mod:`app.services.customer_profile_schema` — there is no
second copy of the field list, the char limits, the enums or the visibility
triggers. The manual back-office form, the applicant journey and this validator
therefore cannot drift.

The load-bearing rule: **a field that is not visible is never required.** Dave's
sheet marks nearly everything Mandatory=Yes, but most of those rows are gated
behind a Visibility Trigger — requiring an invisible field would make whole
categories of applicant un-submittable (e.g. Passport # for someone using a
driver's licence).

Two modes:

* ``partial=True``  — a draft/PATCH. Format, char limit, enum membership and
  "value supplied for an invisible field" are enforced; missing mandatory values
  are NOT. This is what profile edits use.
* ``partial=False`` — a completeness check ("is this profile ready to be scored?").
  Adds the mandatory-when-visible pass. This is what
  :func:`assert_complete_for_application` runs before a profile is attached to a
  credit application.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from app.services.customer_profile_schema import (
    BLOCKS,
    FieldFormat,
    FieldSpec,
    FieldType,
    ProfileValues,
    block_spec,
    instance_key,
    is_block_visible,
    is_field_visible,
    parse_instance_key,
)


# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------


class ErrorCode(str):
    UNKNOWN_BLOCK = "unknown_block"
    UNKNOWN_FIELD = "unknown_field"
    NOT_VISIBLE = "field_not_visible"
    REQUIRED = "required"
    TOO_LONG = "too_long"
    INVALID_OPTION = "invalid_option"
    INVALID_FORMAT = "invalid_format"
    NOT_REPEATABLE = "block_not_repeatable"
    READ_ONLY = "read_only_field"


@dataclass(frozen=True)
class ValidationIssue:
    block: str
    index: int
    field: Optional[str]
    code: str
    message: str

    def to_dict(self) -> dict:
        return {
            "block": self.block,
            "index": self.index,
            "field": self.field,
            "code": self.code,
            "message": self.message,
        }


class ProfileValidationError(Exception):
    """Raised when a write would leave the profile invalid. Carries the issues."""

    def __init__(self, issues: list[ValidationIssue]):
        self.issues = issues
        super().__init__(f"{len(issues)} profile validation issue(s)")


# ---------------------------------------------------------------------------
# Format validators — one per FieldFormat, all registry-driven
# ---------------------------------------------------------------------------

# Latin-1 letters + the punctuation real names carry (O'Brien, Jean-Luc, St. Pierre).
_ALPHA_RE = re.compile(r"^[A-Za-zÀ-ɏ' .\-/]+$")
_ALPHANUMERIC_RE = re.compile(r"^[A-Za-z0-9À-ɏ' .,\-#/()&]+$")
# Alphanumeric + Special Characters: any printable, no control characters.
_ALNUM_SPECIAL_RE = re.compile(r"^[^\x00-\x1f\x7f]+$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")
_NUMERIC_RE = re.compile(r"^[0-9]+$")
# Canadian postal code, with or without the separating space (char limit is 6).
_POSTAL_RE = re.compile(r"^[A-Za-z]\d[A-Za-z][ -]?\d[A-Za-z]\d$")
_SIN_RE = re.compile(r"^\d{9}$")


def _normalize_digits(value: str) -> str:
    return re.sub(r"[^0-9]", "", value)


#: Punctuation a human legitimately types into a phone number. Stripped before the
#: numeric check — but letters are NOT, so "555-CALL" still fails.
_PHONE_PUNCTUATION_RE = re.compile(r"[ ()\-.+]")


def check_format(spec: FieldSpec, value: Any) -> Optional[str]:
    """Return an error message when ``value`` violates the field's Format, else None."""
    fmt = spec.format

    # A dropdown's Format cell in the sheet ("Alpha Only") describes the LABEL the
    # user sees; the wire value is the registry's stable snake_case code
    # ("college_university"), which is deliberately not alpha-only. Enum
    # membership (check_option) is the real validation for these fields.
    if spec.field_type is FieldType.DROPDOWN and spec.options:
        return None

    if fmt is FieldFormat.DATE:
        if isinstance(value, date):
            return None
        try:
            date.fromisoformat(str(value)[:10])
        except (ValueError, TypeError):
            return "must be a date in ISO-8601 form (YYYY-MM-DD)"
        return None

    if fmt is FieldFormat.CURRENCY:
        try:
            amount = Decimal(str(value).replace("$", "").replace(",", "").strip())
        except (InvalidOperation, AttributeError):
            return "must be a currency amount"
        if amount < 0:
            return "must not be negative"
        if amount.as_tuple().exponent < -2:
            return "must have at most 2 decimal places"
        return None

    text = str(value)

    if fmt is FieldFormat.NUMERIC:
        # Phone fields arrive formatted — "(250) 555-1234" is fine, "555-CALL" is not,
        # so strip only the punctuation, never the letters.
        candidate = (
            _PHONE_PUNCTUATION_RE.sub("", text)
            if spec.field_type is FieldType.PHONE
            else text
        )
        if not _NUMERIC_RE.match(candidate):
            return "must contain digits only"
        return None

    if fmt is FieldFormat.ALPHA:
        return None if _ALPHA_RE.match(text) else "must contain letters only"
    if fmt is FieldFormat.ALPHANUMERIC:
        return None if _ALPHANUMERIC_RE.match(text) else "must be alphanumeric"
    if fmt is FieldFormat.ALPHANUMERIC_SPECIAL:
        return None if _ALNUM_SPECIAL_RE.match(text) else "must not contain control characters"
    if fmt is FieldFormat.EMAIL:
        return None if _EMAIL_RE.match(text) else "must be a valid email address"
    if fmt is FieldFormat.POSTAL:
        return None if _POSTAL_RE.match(text) else "must be a Canadian postal code (A1A1A1)"
    if fmt is FieldFormat.SIN:
        # 9 digits. Deliberately NOT Luhn-checked: the SIN is optional (Dave: it
        # legally cannot be required) and rejecting a mistyped-but-plausible SIN
        # at the form is worse than storing it for downstream verification.
        return None if _SIN_RE.match(_normalize_digits(text)) else "must be 9 digits"
    return None


def check_length(spec: FieldSpec, value: Any) -> Optional[str]:
    """Character Limit from the sheet. Phone/SIN count digits, not formatting."""
    if spec.char_limit is None:
        return None
    text = value.isoformat() if isinstance(value, date) else str(value)
    if spec.format in (FieldFormat.NUMERIC, FieldFormat.SIN) and not text.isdigit():
        text = _normalize_digits(text)
    if len(text) > spec.char_limit:
        return f"exceeds the {spec.char_limit} character limit"
    return None


def check_option(spec: FieldSpec, value: Any) -> Optional[str]:
    """Dropdowns must carry one of the registry codes (unless options are external)."""
    if not spec.options:
        return None
    allowed = {o.value for o in spec.options}
    if str(value) not in allowed:
        return f"must be one of: {', '.join(sorted(allowed))}"
    return None


def validate_field(spec: FieldSpec, value: Any) -> list[str]:
    """All non-visibility checks for one supplied value."""
    if value is None or value == "":
        return []
    messages = []
    for check in (check_option, check_length, check_format):
        message = check(spec, value)
        if message:
            messages.append(message)
    return messages


# ---------------------------------------------------------------------------
# Whole-profile validation
# ---------------------------------------------------------------------------


def validate_profile(
    values: ProfileValues,
    *,
    partial: bool = False,
    today: Optional[date] = None,
    allow_staff_fields: bool = True,
    writing: bool = False,
) -> list[ValidationIssue]:
    """Validate a whole profile against the registry.

    ``values`` maps an instance key (``"personal"``, ``"bank_details#1"``) to that
    block instance's ``{field_key: value}``.

    ``allow_staff_fields=False`` rejects writes to Bank Details (Dave: those are
    "Completed by Bank Verification process or Backend staff" — never by the
    borrower). Set it from the caller's role, not from the request body.

    ``writing=True`` additionally rejects any value aimed at a READ-THROUGH block
    (Bank Details), whose owner is ``platform_patient_bank_accounts`` and its own
    API. Reads and completeness checks leave it False so those blocks still
    render and count.
    """
    issues: list[ValidationIssue] = []

    # --- pass 1: everything supplied must be a real, writable, valid field ---
    for key, supplied in values.items():
        block_name, index = parse_instance_key(key)
        spec = block_spec(block_name)
        if spec is None:
            issues.append(ValidationIssue(block_name, index, None, ErrorCode.UNKNOWN_BLOCK,
                                          f"Unknown profile block {block_name!r}"))
            continue
        if index != 0 and not spec.repeatable:
            issues.append(ValidationIssue(block_name, index, None, ErrorCode.NOT_REPEATABLE,
                                          f"Block {block_name!r} does not repeat"))
            continue
        if writing and spec.is_read_through:
            issues.append(ValidationIssue(
                block_name, index, None, ErrorCode.READ_ONLY,
                f"{spec.label} is owned by {spec.external_table}; "
                f"write it via {spec.owned_by} instead of the profile",
            ))
            continue
        if not allow_staff_fields and spec.filled_by.value == "staff_or_bank_verification":
            issues.append(ValidationIssue(
                block_name, index, None, ErrorCode.READ_ONLY,
                f"{spec.label} is completed by Bank Verification or back-office staff",
            ))
            continue

        by_key = spec.field_map()
        for field_key, value in (supplied or {}).items():
            field = by_key.get(field_key)
            if field is None:
                issues.append(ValidationIssue(block_name, index, field_key,
                                              ErrorCode.UNKNOWN_FIELD,
                                              f"Unknown field {field_key!r} in {block_name}"))
                continue
            if field.display_from is not None:
                # Derived/displayed value — accepted but never authoritative.
                continue
            if value in (None, ""):
                continue
            if not is_field_visible(field, values, index=index, today=today):
                issues.append(ValidationIssue(
                    block_name, index, field_key, ErrorCode.NOT_VISIBLE,
                    f"{field.label} is not applicable "
                    f"({field.visible_when.trigger_text})",
                ))
                continue
            for message in validate_field(field, value):
                code = (
                    ErrorCode.INVALID_OPTION
                    if message.startswith("must be one of")
                    else ErrorCode.TOO_LONG
                    if "character limit" in message
                    else ErrorCode.INVALID_FORMAT
                )
                issues.append(ValidationIssue(block_name, index, field_key, code,
                                              f"{field.label} {message}"))

    if partial:
        return issues

    # --- pass 2: every visible mandatory field must carry a value ---
    for block in BLOCKS:
        indices = sorted(
            {
                parse_instance_key(k)[1]
                for k in values
                if parse_instance_key(k)[0] == block.block.value
            }
        ) or [0]
        for index in indices:
            if not is_block_visible(block.block, values, index=index, today=today):
                continue
            supplied = values.get(instance_key(block.block, index)) or {}
            for field in block.fields:
                if not field.mandatory or field.display_from is not None:
                    continue
                if not is_field_visible(field, values, index=index, today=today):
                    continue
                if supplied.get(field.key) in (None, ""):
                    issues.append(ValidationIssue(
                        block.block.value, index, field.key, ErrorCode.REQUIRED,
                        f"{field.label} is required",
                    ))
    return issues


def read_through_issues(values: ProfileValues) -> list[ValidationIssue]:
    """ONLY the "this block is owned elsewhere" check.

    Kept separate from :func:`validate_profile` so a PATCH can be screened for
    read-through blocks *before* it is merged with the stored profile, without
    dragging in visibility checks — a patch in isolation legitimately lacks the
    driver fields its own visibility triggers depend on.
    """
    issues: list[ValidationIssue] = []
    for key in values:
        block_name, index = parse_instance_key(key)
        spec = block_spec(block_name)
        if spec is not None and spec.is_read_through:
            issues.append(ValidationIssue(
                block_name, index, None, ErrorCode.READ_ONLY,
                f"{spec.label} is owned by {spec.external_table}; "
                f"write it via {spec.owned_by} instead of the profile",
            ))
    return issues


def assert_no_read_through_writes(values: ProfileValues) -> None:
    issues = read_through_issues(values)
    if issues:
        raise ProfileValidationError(issues)


def assert_valid(values: ProfileValues, **kwargs: Any) -> None:
    """Raise :class:`ProfileValidationError` when the profile has any issue."""
    issues = validate_profile(values, **kwargs)
    if issues:
        raise ProfileValidationError(issues)


def completeness(values: ProfileValues, *, today: Optional[date] = None) -> dict:
    """How complete is this profile? Used by the Originations 'Borrower details' tab."""
    required: list[str] = []
    filled: list[str] = []
    for block in BLOCKS:
        if not is_block_visible(block.block, values, today=today):
            continue
        supplied = values.get(instance_key(block.block)) or {}
        for field in block.fields:
            if not field.mandatory or field.display_from is not None:
                continue
            if not is_field_visible(field, values, today=today):
                continue
            required.append(field.full_key)
            if supplied.get(field.key) not in (None, ""):
                filled.append(field.full_key)
    missing = [k for k in required if k not in set(filled)]
    return {
        "required_count": len(required),
        "filled_count": len(filled),
        "missing": missing,
        "is_complete": not missing,
        "percent": round(100 * len(filled) / len(required)) if required else 100,
    }


__all__ = [
    "ErrorCode",
    "ProfileValidationError",
    "ValidationIssue",
    "assert_valid",
    "completeness",
    "validate_field",
    "validate_profile",
]
