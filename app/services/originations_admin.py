"""Originations-admin domain logic (WS-E, Turnkey parity video 01).

Pure, DB-free helpers shared by the admin endpoints:

* **Assignment gating** — working an application's decision/editing actions
  requires being its assignee, or an override (the ``admin`` role, which is
  implicitly allowed everywhere per the ``require_permission_or_admin`` model,
  or an explicit ``applications``/``assignment_override`` permission grant a
  senior-staff role can carry).
* **Admin field-level application editing** — a whitelist of editable
  canonical fields with type/enum validation, an old→new change log for the
  ``platform_events`` audit row, and "version, don't overwrite" snapshots of
  the current address/employment via the existing
  :mod:`app.core.application_history` builders (Dave's schema rules).
* **Staff offer editing** — bounds enforcement from the typed
  :class:`~app.schemas.pricing_config.PricingConfig` (amount, term, rate band,
  frequency) plus the ``interest.rate_edit_roles`` gate for non-default rates.
* **Waiting time** — the pipeline queue's time-in-status figure, derived from
  ``status_updated_at`` (stamped by every audited status transition; falls
  back to ``created_at`` for pre-043 rows).

Everything raising here raises domain exceptions; HTTP translation lives in
the endpoint modules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Mapping, Optional

from app.core.application_history import (
    snapshot_prior_address,
    snapshot_prior_employment,
)
from app.schemas.pricing_config import (
    PricingConfig,
    coerce_frequency,
)
from app.services.loan_quote import _rate_bounds, _term_bounds

# ---------------------------------------------------------------------------
# Assignment gating
# ---------------------------------------------------------------------------

#: The explicit permission grant that lets a non-admin work files assigned to
#: someone else (or unassigned files). Seeded/granted via the RBAC tables.
ASSIGNMENT_OVERRIDE_PERMISSION = ("applications", "assignment_override")


class AssignmentRequired(Exception):
    """The actor may not work this application (not assignee, no override)."""


def has_assignment_override(
    user_roles: set[str], user_permissions: set[tuple[str, str]]
) -> bool:
    """True when the actor may work applications regardless of assignment.

    ``admin`` is implicitly allowed everywhere (the repo's
    ``require_permission_or_admin`` model); anyone else needs the explicit
    ``applications``/``assignment_override`` Role→Permission grant.
    """
    return "admin" in user_roles or ASSIGNMENT_OVERRIDE_PERMISSION in user_permissions


def check_assignment(
    assigned_to_user_id: Optional[Any],
    actor_id: str,
    *,
    override_allowed: bool,
) -> bool:
    """Enforce the WS-E assignment gate. Returns ``True`` when the actor is
    working the file via an override (not as its assignee) so callers can put
    ``assignment_override_used`` in the audit payload.

    Raises :class:`AssignmentRequired` when the actor is neither the assignee
    nor override-entitled — both for a file assigned to someone else AND for
    an unassigned file (TL flow: "assign to me" first, then work it).
    """
    if assigned_to_user_id is not None and str(assigned_to_user_id) == str(actor_id):
        return False
    if override_allowed:
        return True
    if assigned_to_user_id is None:
        raise AssignmentRequired(
            "Application is unassigned — assign it to yourself first "
            "(POST .../assign) or use an account with assignment override."
        )
    raise AssignmentRequired(
        "Application is assigned to another user; only its assignee (or an "
        "account with assignment override) can perform this action."
    )


# ---------------------------------------------------------------------------
# Waiting time (pipeline queue)
# ---------------------------------------------------------------------------


def waiting_seconds(
    status_updated_at: Optional[datetime],
    created_at: Optional[datetime],
    now: datetime,
) -> Optional[int]:
    """Whole seconds the application has sat in its current status.

    ``status_updated_at`` is stamped by every audited status transition (the
    same transitions that append ``platform_events`` rows); ``created_at`` is
    the fallback anchor. ``None`` when neither anchor exists."""
    anchor = status_updated_at or created_at
    if anchor is None:
        return None
    delta = (now - anchor).total_seconds()
    return max(0, int(delta))


# ---------------------------------------------------------------------------
# Admin field-level application editing
# ---------------------------------------------------------------------------

# Enum value sets mirrored from the model's PG enums (kept in sync with
# migration 043; validated here so a bad admin edit 422s instead of 500ing at
# flush time with an opaque DBAPI error).
INCOME_TYPES = frozenset(
    {
        "employed_full_time",
        "employed_part_time",
        "employed_seasonal",
        "self_employed",
        "retirement_pension",
        "disability",
        "employment_insurance",
        "other",
    }
)
CAR_OWNERSHIP_VALUES = frozenset({"fully_paid", "financing", "leasing", "none"})

#: field name -> expected python type ("date" for date fields). The whitelist
#: is the canonical Dave field set (migration 043) + ``branch`` (059). It
#: deliberately EXCLUDES: SIN (never stored on the application — encrypted on
#: platform_patients only), status/decision (owned by the decision endpoints),
#: amounts/terms (owned by the offer-editing endpoint), and assignment.
EDITABLE_FIELDS: dict[str, str] = {
    # personal
    "first_name": "str",
    "middle_name": "str",
    "last_name": "str",
    "date_of_birth": "date",
    "marital_status": "str",
    "number_of_dependents": "int",
    "citizenship": "str",
    "education": "str",
    "main_phone": "str",
    "alternative_phone": "str",
    "email": "str",
    # ID verification
    "id_type": "str",
    "id_number": "str",
    "id_province_of_issue": "str",
    "id_expiry": "date",
    # residence (current — prior values are versioned, never overwritten)
    "residence_street": "str",
    "residence_unit": "str",
    "residence_city": "str",
    "residence_province": "str",
    "residence_postal_code": "str",
    "time_at_address_years": "int",
    "time_at_address_months": "int",
    "residential_status": "str",
    "monthly_housing_payment_cents": "int",
    # primary income / employment (current — prior values versioned)
    "income_type": "enum:income_type",
    "net_monthly_income_cents": "int",
    "next_pay_date": "date",
    "pay_frequency": "str",
    "employer_name": "str",
    "hire_date": "date",
    "job_title": "str",
    "work_phone": "str",
    "work_phone_ext": "str",
    "ok_to_contact_at_work": "bool",
    # financial
    "number_of_credit_accounts": "int",
    "car_ownership": "enum:car_ownership",
    "monthly_car_payment_cents": "int",
    "non_discretionary_expenses_cents": "int",
    # origination context
    "branch": "str",
    "treatment_plan_ref": "str",
}

_ENUM_SETS = {"income_type": INCOME_TYPES, "car_ownership": CAR_OWNERSHIP_VALUES}


class InvalidEdit(Exception):
    """An admin edit payload failed whitelist/type/enum validation."""


def _coerce(field_name: str, kind: str, value: Any) -> Any:
    """Validate + coerce one submitted value (None always allowed = clear)."""
    if value is None:
        return None
    if kind == "str":
        if not isinstance(value, str):
            raise InvalidEdit(f"{field_name}: expected a string")
        return value.strip() or None
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidEdit(f"{field_name}: expected an integer")
        if value < 0:
            raise InvalidEdit(f"{field_name}: must be >= 0")
        return value
    if kind == "bool":
        if not isinstance(value, bool):
            raise InvalidEdit(f"{field_name}: expected a boolean")
        return value
    if kind == "date":
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value))
        except ValueError as exc:
            raise InvalidEdit(f"{field_name}: expected an ISO date (YYYY-MM-DD)") from exc
    if kind.startswith("enum:"):
        allowed = _ENUM_SETS[kind.split(":", 1)[1]]
        if value not in allowed:
            raise InvalidEdit(
                f"{field_name}: {value!r} is not one of {sorted(allowed)}"
            )
        return value
    raise InvalidEdit(f"{field_name}: unsupported field kind {kind!r}")  # pragma: no cover


def validate_changes(raw_changes: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist + type-check a submitted ``{field: new_value}`` mapping."""
    if not raw_changes:
        raise InvalidEdit("No changes submitted")
    unknown = sorted(set(raw_changes) - set(EDITABLE_FIELDS))
    if unknown:
        raise InvalidEdit(
            f"Field(s) not editable here: {unknown}. Editable fields: "
            f"{sorted(EDITABLE_FIELDS)}"
        )
    return {
        name: _coerce(name, EDITABLE_FIELDS[name], value)
        for name, value in raw_changes.items()
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


@dataclass
class AdminEditResult:
    """Outcome of applying an admin edit to an application row."""

    change_log: dict[str, dict[str, Any]] = field(default_factory=dict)
    address_snapshot: Optional[dict[str, Any]] = None
    employment_snapshot: Optional[dict[str, Any]] = None

    @property
    def changed(self) -> bool:
        return bool(self.change_log)


# Fields that participate in the address / employment versioning triggers
# (the app-column keys of the snapshot field maps).
_ADDRESS_TRIGGER_FIELDS = frozenset(
    {
        "residence_street",
        "residence_unit",
        "residence_city",
        "residence_province",
        "residence_postal_code",
        "residential_status",
        "monthly_housing_payment_cents",
    }
)
_EMPLOYMENT_TRIGGER_FIELDS = frozenset(
    {
        "employer_name",
        "job_title",
        "income_type",
        "net_monthly_income_cents",
        "pay_frequency",
        "hire_date",
    }
)


def apply_admin_edit(
    application: Any, changes: Mapping[str, Any], *, today: date
) -> AdminEditResult:
    """Apply validated ``changes`` to the application row IN PLACE.

    Returns the old→new change log (JSON-safe, for the platform_events audit
    row) plus history-row kwargs versioning the PRIOR current address /
    employment when those fields changed (Dave: never overwrite — the file
    keeps its full audit picture). No-op fields (new == old) are dropped.
    The caller owns persisting the snapshots and the audit event.
    """
    # Prior values for the versioning builders (they diff prior vs new).
    prior = {
        name: getattr(application, name, None)
        for name in (_ADDRESS_TRIGGER_FIELDS | _EMPLOYMENT_TRIGGER_FIELDS)
    }
    # Enum columns read back as enum-ish objects in some drivers — normalize.
    if prior.get("income_type") is not None:
        prior["income_type"] = str(prior["income_type"])

    result = AdminEditResult()
    effective: dict[str, Any] = {}
    for name, new_value in changes.items():
        old_value = getattr(application, name, None)
        if name == "income_type" and old_value is not None:
            old_value = str(old_value)
        if new_value == old_value:
            continue
        effective[name] = new_value
        result.change_log[name] = {
            "old": _jsonable(old_value),
            "new": _jsonable(new_value),
        }

    if not effective:
        return result

    if _ADDRESS_TRIGGER_FIELDS & set(effective):
        result.address_snapshot = snapshot_prior_address(prior, effective, today=today)
    if _EMPLOYMENT_TRIGGER_FIELDS & set(effective):
        result.employment_snapshot = snapshot_prior_employment(
            prior, effective, today=today
        )

    for name, new_value in effective.items():
        setattr(application, name, new_value)
    return result


# ---------------------------------------------------------------------------
# Staff offer editing — product min/max enforcement from PricingConfig
# ---------------------------------------------------------------------------


class OfferOutOfBounds(Exception):
    """Requested offer terms fall outside the product's configured bounds."""


class RateRoleNotPermitted(Exception):
    """Actor's roles are not in the product's ``interest.rate_edit_roles``."""


@dataclass(frozen=True)
class ValidatedOffer:
    amount_cents: int
    term_months: int
    annual_rate_bps: int
    frequency: str


def validate_offer(
    cfg: PricingConfig,
    *,
    amount_cents: int,
    term_months: int,
    annual_rate_bps: Optional[int],
    frequency: str,
    actor_roles: set[str],
    product_min_amount_cents: Optional[int] = None,
    product_max_amount_cents: Optional[int] = None,
) -> ValidatedOffer:
    """Validate staff-edited offer terms against the product's bounds.

    * AMOUNT — inside the intersection of the product row's
      ``min/max_amount_cents`` columns (authoritative) and the config's
      optional ``amount_min/max_cents``.
    * TERM — inside the config's term range (canonical ``_term_bounds`` rule,
      same one vendor origination uses).
    * FREQUENCY — one the product offers (spelling-tolerant).
    * RATE — ``None`` means the product default; a custom rate must sit inside
      the ``[min, max]`` band AND the actor must hold a role listed in
      ``interest.rate_edit_roles`` (403-shaped :class:`RateRoleNotPermitted`).

    Raises :class:`OfferOutOfBounds` (422-shaped) on any bounds failure.
    """
    if amount_cents <= 0:
        raise OfferOutOfBounds("amount_cents must be positive")
    if term_months <= 0:
        raise OfferOutOfBounds("term_months must be positive")

    lo_candidates = [
        v for v in (product_min_amount_cents, cfg.amount_min_cents) if v is not None
    ]
    hi_candidates = [
        v for v in (product_max_amount_cents, cfg.amount_max_cents) if v is not None
    ]
    amount_lo = max(lo_candidates) if lo_candidates else None
    amount_hi = min(hi_candidates) if hi_candidates else None
    if amount_lo is not None and amount_cents < amount_lo:
        raise OfferOutOfBounds(
            f"Amount {amount_cents} cents is below the product minimum {amount_lo}."
        )
    if amount_hi is not None and amount_cents > amount_hi:
        raise OfferOutOfBounds(
            f"Amount {amount_cents} cents is above the product maximum {amount_hi}."
        )

    term_min, term_max, _ = _term_bounds(cfg)
    if not (term_min <= term_months <= term_max):
        raise OfferOutOfBounds(
            f"Term {term_months} months is outside the product's range "
            f"[{term_min}, {term_max}]."
        )

    freq = coerce_frequency(frequency)
    if freq is None or freq not in cfg.payment_frequencies:
        allowed = ", ".join(f.value for f in cfg.payment_frequencies)
        raise OfferOutOfBounds(
            f"Payment frequency {frequency!r} is not offered by this product "
            f"(allowed: {allowed})."
        )

    interest = _rate_bounds(cfg)
    if annual_rate_bps is None or annual_rate_bps == interest.annual_rate_bps:
        resolved_rate = interest.annual_rate_bps
    else:
        if not (interest.min_rate_bps <= annual_rate_bps <= interest.max_rate_bps):
            raise OfferOutOfBounds(
                f"Rate {annual_rate_bps} bps is outside the product's allowed band "
                f"[{interest.min_rate_bps}, {interest.max_rate_bps}] bps."
            )
        allowed_roles = set(interest.rate_edit_roles or [])
        if not (actor_roles & allowed_roles):
            raise RateRoleNotPermitted(
                "Your role is not permitted to set a custom interest rate on "
                f"this product (allowed roles: {sorted(allowed_roles)})."
            )
        resolved_rate = annual_rate_bps

    return ValidatedOffer(
        amount_cents=amount_cents,
        term_months=term_months,
        annual_rate_bps=resolved_rate,
        frequency=freq.value,
    )
