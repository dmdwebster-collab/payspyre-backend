"""Validation + versioning helpers for 3-year address/employment history.

Dave's stability mandate: "ideally we want three years of both address and
employment information to look at stability factors." When a history list is
submitted, its date ranges must cover the required window — the last
``HISTORY_YEARS_REQUIRED`` years, shortened to the applicant's age of majority
when they reached majority more recently (a 19-year-old cannot have 3 years of
adult address history). Overlaps are allowed; at least one CURRENT entry is
required; small gaps up to ``HISTORY_GAP_TOLERANCE_DAYS`` are tolerated
(month-granularity moves).

Also holds the pure "version, don't overwrite" snapshot builders: when the
CURRENT address/employment scalar fields on the application are edited, the
prior values are captured into a history row (``entry_source='versioned_edit'``)
instead of being silently lost.

Everything here is pure (no DB, no request) so it is unit-testable and shared
by the manual-application and finalize paths.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Mapping, Optional, Sequence


def _get(entry: Any, field: str) -> Any:
    if isinstance(entry, Mapping):
        return entry.get(field)
    return getattr(entry, field, None)


def _add_years(d: date, years: int) -> date:
    """d + years, clamping Feb 29 to Feb 28 on non-leap years."""
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # Feb 29 -> Feb 28
        return d.replace(year=d.year + years, day=28)


def required_window_start(
    *,
    today: date,
    date_of_birth: Optional[date],
    years_required: int,
    age_of_majority_years: int,
) -> date:
    """Start of the window history must cover: ``years_required`` back from
    today, or the applicant's age-of-majority date if that is more recent."""
    start = _add_years(today, -years_required)
    if date_of_birth is not None:
        majority = _add_years(date_of_birth, age_of_majority_years)
        if majority > start:
            start = min(majority, today)
    return start


def validate_history_entries(
    entries: Sequence[Any],
    *,
    today: date,
    date_of_birth: Optional[date] = None,
    label: str = "history",
    years_required: Optional[int] = None,
    age_of_majority_years: Optional[int] = None,
    gap_tolerance_days: Optional[int] = None,
) -> list[str]:
    """Validate a submitted history list. Returns human-readable errors
    (empty list = valid).

    Entries may be pydantic models, ORM rows or dicts exposing ``from_date``,
    ``to_date`` and ``is_current``. Rules:

      * at least one entry, and at least one flagged ``is_current``;
      * every entry has ``from_date``; ``to_date`` (when set) >= ``from_date``;
      * a current entry is open-ended for coverage (treated as reaching today);
      * the union of ranges covers [window_start, today] where window_start is
        ``required_window_start`` — overlaps allowed, gaps up to the tolerance
        allowed.

    Policy knobs default from Settings (config-driven, flagged for Dave).
    """
    if years_required is None or age_of_majority_years is None or gap_tolerance_days is None:
        from app.core.config import settings

        if years_required is None:
            years_required = settings.HISTORY_YEARS_REQUIRED
        if age_of_majority_years is None:
            age_of_majority_years = settings.HISTORY_AGE_OF_MAJORITY_YEARS
        if gap_tolerance_days is None:
            gap_tolerance_days = settings.HISTORY_GAP_TOLERANCE_DAYS

    errors: list[str] = []
    if not entries:
        errors.append(f"{label}: at least one entry (the current one) is required")
        return errors

    intervals: list[tuple[date, date]] = []
    current_count = 0
    for i, entry in enumerate(entries):
        from_date = _get(entry, "from_date")
        to_date = _get(entry, "to_date")
        is_current = bool(_get(entry, "is_current"))
        if is_current:
            current_count += 1

        if from_date is None:
            errors.append(f"{label}[{i}]: from_date is required")
            continue
        if from_date > today:
            errors.append(f"{label}[{i}]: from_date cannot be in the future")
            continue
        if to_date is not None and to_date < from_date:
            errors.append(f"{label}[{i}]: to_date must be on or after from_date")
            continue
        # A current entry (or an entry without an end) is open-ended -> today.
        effective_end = today if (is_current or to_date is None) else min(to_date, today)
        intervals.append((from_date, effective_end))

    if current_count == 0:
        errors.append(f"{label}: at least one entry must be marked is_current")

    if errors:
        return errors

    window_start = required_window_start(
        today=today,
        date_of_birth=date_of_birth,
        years_required=years_required,
        age_of_majority_years=age_of_majority_years,
    )
    tolerance = timedelta(days=gap_tolerance_days)

    intervals.sort(key=lambda pair: pair[0])
    covered_to: Optional[date] = None
    for start, end in intervals:
        if covered_to is None:
            if start > window_start + tolerance:
                break  # earliest entry starts too late
            covered_to = end
            continue
        if start > covered_to + tolerance:
            break  # gap larger than tolerance
        if end > covered_to:
            covered_to = end

    if covered_to is None or intervals[0][0] > window_start + tolerance:
        errors.append(
            f"{label}: entries must reach back to {window_start.isoformat()} "
            f"({years_required} years, or since age of majority)"
        )
    elif covered_to + tolerance < today:
        errors.append(
            f"{label}: entries leave a gap of more than {gap_tolerance_days} days "
            f"before today (covered to {covered_to.isoformat()})"
        )

    return errors


# ---------------------------------------------------------------------------
# "Version, don't overwrite" snapshots of the CURRENT scalar entry
# ---------------------------------------------------------------------------

# application column -> history column
ADDRESS_SNAPSHOT_FIELDS: dict[str, str] = {
    "residence_street": "street",
    "residence_unit": "unit",
    "residence_city": "city",
    "residence_province": "province",
    "residence_postal_code": "postal_code",
    "residential_status": "residential_status",
    "monthly_housing_payment_cents": "monthly_housing_payment_cents",
}

EMPLOYMENT_SNAPSHOT_FIELDS: dict[str, str] = {
    "employer_name": "employer_name",
    "job_title": "job_title",
    "income_type": "income_type",
    "net_monthly_income_cents": "net_monthly_income_cents",
    "pay_frequency": "pay_frequency",
    # The current employment's start date becomes the snapshot's from_date.
    "hire_date": "from_date",
}


def _snapshot(
    prior: Mapping[str, Any],
    new: Mapping[str, Any],
    field_map: Mapping[str, str],
    anchor_field: str,
    today: date,
) -> Optional[dict[str, Any]]:
    """Build history-row kwargs for the prior current entry, or None.

    Returns a snapshot only when (a) any mapped field actually CHANGED (the
    edit is real — new value provided and different) and (b) the prior entry
    was meaningful (its anchor field, street/employer, was set).
    """
    changed = any(
        app_col in new and new[app_col] != prior.get(app_col) for app_col in field_map
    )
    if not changed:
        return None
    if not prior.get(anchor_field):
        return None  # nothing meaningful to version
    row = {hist_col: prior.get(app_col) for app_col, hist_col in field_map.items()}
    # Exact start unknown unless the field map supplies one (employment maps
    # hire_date -> from_date; address snapshots have no reliable start).
    row.setdefault("from_date", None)
    row["is_current"] = False
    row["entry_source"] = "versioned_edit"
    row["to_date"] = today
    return row


def snapshot_prior_address(
    prior: Mapping[str, Any], new: Mapping[str, Any], *, today: date
) -> Optional[dict[str, Any]]:
    """History-row kwargs versioning the prior current address, or None when
    the address did not change / there was nothing meaningful to version."""
    return _snapshot(prior, new, ADDRESS_SNAPSHOT_FIELDS, "residence_street", today)


def snapshot_prior_employment(
    prior: Mapping[str, Any], new: Mapping[str, Any], *, today: date
) -> Optional[dict[str, Any]]:
    """History-row kwargs versioning the prior current employment, or None."""
    return _snapshot(prior, new, EMPLOYMENT_SNAPSHOT_FIELDS, "employer_name", today)
