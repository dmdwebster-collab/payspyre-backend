"""Business calendar (Workstream F — TL "Loan settings > Calendar" parity).

Dave (video 07 §2.20): "This would be great if it populated Canadian holidays…
banks don't process electronic payments on statutory holidays, so manually I go
in and send all of our borrowers that have due dates on that day a notification…
so we can avoid missed payments."

Three layers:

1. **Computed Canadian statutory holidays** — pure functions, per year + optional
   province (federal bank-processing set + provincial additions). Fixed-date
   holidays falling on a weekend are shifted to the observed weekday (Payments
   Canada convention). Never stored.
2. **Admin overrides** (``platform_business_calendar_overrides``) — add ad-hoc
   closure dates, or force a computed holiday to count as a business day.
   Override precedence: an explicit override always wins over the computation.
3. **Service API** — ``is_business_day`` / ``next_business_day`` for schedulers,
   and ``queue_payment_delay_notices`` — the event hook that emits a
   ``payment_delay_notice`` notification event (through the platform_events →
   notification-processor outbox lane; delivery is inert until SendGrid creds
   land — expected) for every open installment whose due date lands on a
   non-business day.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)

NATIONWIDE = "ALL"

DELAY_EVENT = "payment_delay_notice"


# ---------------------------------------------------------------------------
# Pure holiday computation
# ---------------------------------------------------------------------------


def easter_sunday(year: int) -> date:
    """Gregorian Easter (anonymous/Meeus algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741 — canonical algorithm name
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th ``weekday`` (Mon=0) of ``month``."""
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _monday_on_or_before(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _observed(d: date) -> date:
    """Weekend fixed-date holiday → the following Monday (observed date)."""
    if d.weekday() == 5:  # Saturday
        return d + timedelta(days=2)
    if d.weekday() == 6:  # Sunday
        return d + timedelta(days=1)
    return d


def statutory_holidays(year: int, province: Optional[str] = None) -> dict[date, str]:
    """Canadian statutory holidays for ``year`` as {observed_date: name}.

    The federal bank-processing set applies everywhere; ``province`` (two-letter
    code) adds that province's statutory days. Fixed-date holidays are shifted
    to their observed weekday when they fall on a weekend (Christmas/Boxing Day
    pair handled so both observe distinct weekdays).
    """
    prov = (province or "").strip().upper()
    easter = easter_sunday(year)
    holidays: dict[date, str] = {}

    def add(d: date, name: str) -> None:
        # First writer wins; observed-shift collisions nudge forward a weekday.
        dd = d
        while dd in holidays:
            dd += timedelta(days=1)
            while dd.weekday() >= 5:
                dd += timedelta(days=1)
        holidays[dd] = name

    # Federal (bank-processing) holidays.
    add(_observed(date(year, 1, 1)), "New Year's Day")
    add(easter - timedelta(days=2), "Good Friday")
    add(_monday_on_or_before(date(year, 5, 24)), "Victoria Day")
    add(_observed(date(year, 7, 1)), "Canada Day")
    add(_nth_weekday(year, 8, 0, 1), "Civic Holiday")
    add(_nth_weekday(year, 9, 0, 1), "Labour Day")
    add(_observed(date(year, 9, 30)), "National Day for Truth and Reconciliation")
    add(_nth_weekday(year, 10, 0, 2), "Thanksgiving")
    add(_observed(date(year, 11, 11)), "Remembrance Day")
    christmas = _observed(date(year, 12, 25))
    add(christmas, "Christmas Day")
    boxing = date(year, 12, 26)
    if boxing.weekday() >= 5 or boxing == christmas:
        boxing = christmas + timedelta(days=1)
        while boxing.weekday() >= 5:
            boxing += timedelta(days=1)
    add(boxing, "Boxing Day")

    # Provincial additions.
    if prov in ("AB", "BC", "SK", "ON", "NB"):
        add(_nth_weekday(year, 2, 0, 3), "Family Day")
    elif prov == "MB":
        add(_nth_weekday(year, 2, 0, 3), "Louis Riel Day")
    elif prov == "NS":
        add(_nth_weekday(year, 2, 0, 3), "Heritage Day")
    elif prov == "PE":
        add(_nth_weekday(year, 2, 0, 3), "Islander Day")
    if prov == "QC":
        add(_observed(date(year, 6, 24)), "Saint-Jean-Baptiste Day")
    if prov == "YT":
        add(_nth_weekday(year, 8, 0, 3), "Discovery Day")
    if prov in ("NT", "YT"):
        add(_observed(date(year, 6, 21)), "National Indigenous Peoples Day")
    if prov == "NL":
        add(_observed(date(year, 3, 17)), "St. Patrick's Day")
        add(easter + timedelta(days=1), "Easter Monday")

    return holidays


# ---------------------------------------------------------------------------
# Overrides + business-day API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalendarOverrides:
    """In-memory override sets (dates) for pure business-day evaluation."""

    closures: frozenset = field(default_factory=frozenset)
    forced_open: frozenset = field(default_factory=frozenset)


def load_overrides(
    db: Any, start: date, end: date, province: Optional[str] = None
) -> CalendarOverrides:
    """Overrides in [start, end] applying to ``province`` (nationwide + that
    province). Static SQL via the ORM model."""
    from app.models.platform.business_calendar import PlatformBusinessCalendarOverride

    prov = (province or NATIONWIDE).strip().upper() or NATIONWIDE
    provinces = [NATIONWIDE] if prov == NATIONWIDE else [NATIONWIDE, prov]
    rows = (
        db.query(PlatformBusinessCalendarOverride)
        .filter(
            PlatformBusinessCalendarOverride.date >= start,
            PlatformBusinessCalendarOverride.date <= end,
            PlatformBusinessCalendarOverride.province.in_(provinces),
        )
        .all()
    )
    closures = frozenset(r.date for r in rows if r.kind == "closure")
    forced = frozenset(r.date for r in rows if r.kind == "business_day")
    return CalendarOverrides(closures=closures, forced_open=forced)


def is_business_day_pure(
    d: date,
    holidays: dict[date, str],
    overrides: CalendarOverrides = CalendarOverrides(),
) -> bool:
    """Pure evaluation: overrides win, then weekends, then computed holidays."""
    if d in overrides.forced_open:
        return True
    if d in overrides.closures:
        return False
    if d.weekday() >= 5:
        return False
    return d not in holidays


def is_business_day(d: date, province: Optional[str] = None, db: Any = None) -> bool:
    """Is ``d`` a business (bank-processing) day for ``province``?

    With a ``db`` session, admin overrides apply; without one, computed
    holidays + weekends only."""
    holidays = statutory_holidays(d.year, province)
    overrides = (
        load_overrides(db, d, d, province) if db is not None else CalendarOverrides()
    )
    return is_business_day_pure(d, holidays, overrides)


def next_business_day(
    d: date, province: Optional[str] = None, db: Any = None, *, on_or_after: bool = True
) -> date:
    """The first business day on-or-after ``d`` (or strictly after when
    ``on_or_after=False``). Bounded scan — raises if none found in 60 days
    (cannot happen with a sane calendar)."""
    cur = d if on_or_after else d + timedelta(days=1)
    for _ in range(60):
        if is_business_day(cur, province, db):
            return cur
        cur += timedelta(days=1)
    raise RuntimeError(f"no business day within 60 days of {d}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Event hook — payment-delay notices for due dates on non-business days
# ---------------------------------------------------------------------------

# Mirror dunning's chaseable sets (kept local so this module stays importable
# without the dunning policy).
_OPEN_ITEM_STATUSES = ("scheduled", "partial", "late")
_CHASEABLE_LOAN_STATUSES = ("active", "delinquent")


@dataclass
class DelayScanResult:
    scanned_days: int = 0
    notices_emitted: int = 0


def _fmt_cents(cents: Optional[int]) -> str:
    return "${:,.2f}".format((cents or 0) / 100)


def _fmt_date(d: date) -> str:
    return d.strftime("%B %d, %Y")


def queue_payment_delay_notices(
    db: Any, as_of: date, days_ahead: int = 7
) -> DelayScanResult:
    """Emit a ``payment_delay_notice`` event for every open installment whose
    due date within the next ``days_ahead`` days lands on a NON-business day
    (statutory holiday / weekend / admin closure).

    The event is passthrough-shaped ({context, channels, loan_id}) so the
    notification processor renders + sends it exactly like a dunning event —
    through the outbox lane, deduped per installment+date. Idempotent: safe to
    re-run daily.
    """
    from sqlalchemy import bindparam, text

    from app.models.platform.event import PlatformEvent

    result = DelayScanResult()
    targets: list[date] = []
    for n in range(days_ahead + 1):
        day = as_of + timedelta(days=n)
        result.scanned_days += 1
        # Weekend due dates are usually schedule design, not surprises; the
        # notice targets HOLIDAY/closure processing delays (Dave's manual
        # workflow). Weekends are still non-business days for the API above.
        if day.weekday() >= 5:
            continue
        if not is_business_day(day, None, db):
            targets.append(day)

    if not targets:
        return result

    stmt = text(
        """
        SELECT s.id            AS item_id,
               s.total_cents   AS total_cents,
               s.paid_cents    AS paid_cents,
               s.due_date      AS due_date,
               l.id            AS loan_id,
               l.application_id AS application_id,
               a.patient_id    AS patient_id,
               p.legal_first_name AS first_name,
               p.legal_last_name  AS last_name
        FROM platform_loan_schedule s
        JOIN platform_loans l ON l.id = s.loan_id
        JOIN platform_credit_applications a ON a.id = l.application_id
        JOIN platform_patients p ON p.id = a.patient_id
        WHERE s.due_date IN :targets
          AND s.status IN :open_statuses
          AND l.status IN :loan_statuses
        """
    ).bindparams(
        bindparam("targets", expanding=True),
        bindparam("open_statuses", expanding=True),
        bindparam("loan_statuses", expanding=True),
    )
    rows = db.execute(
        stmt,
        {
            "targets": list(targets),
            "open_statuses": list(_OPEN_ITEM_STATUSES),
            "loan_statuses": list(_CHASEABLE_LOAN_STATUSES),
        },
    ).mappings().all()

    from app.core.config import settings

    base = settings.BORROWER_PORTAL_BASE_URL.rstrip("/")
    for row in rows:
        delay_key = f"{row['item_id']}:{row['due_date'].isoformat()}"
        if _delay_already_emitted(db, delay_key):
            continue
        holiday_name = statutory_holidays(row["due_date"].year).get(
            row["due_date"], "a statutory holiday"
        )
        name = " ".join(
            p for p in (row.get("first_name"), row.get("last_name")) if p
        ).strip() or "there"
        outstanding = max(0, (row["total_cents"] or 0) - (row.get("paid_cents") or 0))
        processing_date = next_business_day(
            row["due_date"], None, db, on_or_after=False
        )
        context = {
            "borrower_name": name,
            "loan_id": str(row["loan_id"])[:8],
            "payment_amount": _fmt_cents(outstanding),
            "due_date": _fmt_date(row["due_date"]),
            "holiday_name": holiday_name,
            "processing_date": _fmt_date(processing_date),
            "payment_url": f"{base}/account",
            "account_url": f"{base}/account",
        }
        payload = {
            "v": 1,
            "actor": {"type": "system", "id": "system"},
            "application_id": str(row["application_id"]) if row["application_id"] else None,
            "patient_id": str(row["patient_id"]) if row["patient_id"] else None,
            "loan_id": str(row["loan_id"]),
            "delay_key": delay_key,
            "channels": ["email", "dashboard"],
            "context": context,
        }
        db.add(
            PlatformEvent(
                event_type=DELAY_EVENT,
                actor="system",
                patient_id=row["patient_id"],
                application_id=row["application_id"],
                payload=payload,
            )
        )
        db.flush()
        result.notices_emitted += 1

    logger.info(
        "payment_delay_scan",
        as_of=as_of.isoformat(),
        non_business_targets=[t.isoformat() for t in targets],
        notices=result.notices_emitted,
    )
    return result


def _delay_already_emitted(db: Any, delay_key: str) -> bool:
    from sqlalchemy import text

    stmt = text(
        """
        SELECT 1 FROM platform_events
        WHERE event_type = :etype
          AND payload->>'delay_key' = :key
        LIMIT 1
        """
    )
    return db.execute(stmt, {"etype": DELAY_EVENT, "key": delay_key}).first() is not None


def list_calendar(
    year: int, province: Optional[str] = None, db: Any = None
) -> dict[str, Any]:
    """Admin view: computed holidays + overrides for a year."""
    holidays = statutory_holidays(year, province)
    overrides_rows: Iterable[Any] = ()
    if db is not None:
        from app.models.platform.business_calendar import (
            PlatformBusinessCalendarOverride,
        )

        prov = (province or NATIONWIDE).strip().upper() or NATIONWIDE
        provinces = [NATIONWIDE] if prov == NATIONWIDE else [NATIONWIDE, prov]
        overrides_rows = (
            db.query(PlatformBusinessCalendarOverride)
            .filter(
                PlatformBusinessCalendarOverride.date >= date(year, 1, 1),
                PlatformBusinessCalendarOverride.date <= date(year, 12, 31),
                PlatformBusinessCalendarOverride.province.in_(provinces),
            )
            .order_by(PlatformBusinessCalendarOverride.date.asc())
            .all()
        )
    return {
        "year": year,
        "province": (province or NATIONWIDE).upper(),
        "holidays": [
            {"date": d.isoformat(), "name": n} for d, n in sorted(holidays.items())
        ],
        "overrides": [
            {
                "id": str(r.id),
                "date": r.date.isoformat(),
                "province": r.province,
                "kind": r.kind,
                "label": r.label,
            }
            for r in overrides_rows
        ],
    }
