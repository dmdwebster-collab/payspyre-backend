"""Notification config service — DB-backed cadence/channels + send windows.

Single place that answers the three system-configurable questions the
notification subsystem now asks at runtime instead of reading hardcoded values:

1. **Cadence + channels per notification type** — reads
   ``platform_notification_rules`` (offsets, per-channel enable flags, per-channel
   content overrides). Falls back to the typed :class:`DunningPolicy` defaults
   when no row exists, so the system degrades safely if the table is empty.

2. **Per-province communication windows** (compliance MECHANISM only) — allowed
   local send hours per Canadian province/territory. See the big LEGAL comment on
   ``PROVINCE_SEND_WINDOWS``: the hours are a PERMISSIVE PLACEHOLDER, NOT
   authoritative consumer-protection contact hours.

3. **Borrower province lookup** — the province is NOT a column on
   ``platform_patients``; it lives in ``platform_patient_fields``
   (field_key='province'). :func:`get_patient_province` reads the current value
   from there.

Everything here is Canadian. No US law/terminology anywhere.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.platform.notification_rule import PlatformNotificationRule
from app.services.dunning import DunningPolicy, DEFAULT_POLICY

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Notification rule (cadence + channels) — DB-backed, dataclass fallback
# ---------------------------------------------------------------------------

# Channel names used across the subsystem. "dashboard" is the in-app channel;
# email/sms are vendor channels.
CHANNEL_EMAIL = "email"
CHANNEL_DASHBOARD = "dashboard"
CHANNEL_SMS = "sms"
ALL_CHANNELS = (CHANNEL_EMAIL, CHANNEL_DASHBOARD, CHANNEL_SMS)


@dataclass(frozen=True)
class ResolvedRule:
    """A notification type's effective config — from a DB row if present, else
    derived from the :class:`DunningPolicy` fallback."""
    notification_type: str
    offsets: tuple[int, ...]
    enabled_channels: tuple[str, ...]
    content_overrides: dict  # {channel: {"subject": .., "body": ..}}
    enabled: bool

    def override_for(self, channel: str) -> Optional[dict]:
        ov = self.content_overrides.get(channel) if self.content_overrides else None
        return ov if isinstance(ov, dict) else None


def _fallback_rule(notification_type: str, policy: DunningPolicy) -> ResolvedRule:
    """Build a ResolvedRule from the typed DunningPolicy defaults (no DB row)."""
    if notification_type == "payment_due_reminder":
        offsets = tuple(policy.reminder_days_before)
        channels = tuple(policy.reminder_channels)
    elif notification_type == "payment_overdue":
        offsets = tuple(policy.overdue_days_after)
        channels = tuple(policy.overdue_channels)
    else:
        offsets = ()
        channels = (CHANNEL_EMAIL,)
    return ResolvedRule(
        notification_type=notification_type,
        offsets=offsets,
        enabled_channels=channels,
        content_overrides={},
        enabled=True,
    )


def get_rule(
    db: Session, notification_type: str, policy: DunningPolicy = DEFAULT_POLICY
) -> ResolvedRule:
    """Resolve the effective rule for a notification type.

    Reads the ``platform_notification_rules`` row if present; otherwise falls
    back to the typed DunningPolicy defaults. ``enabled_channels`` is the ordered
    subset of (email, dashboard, sms) flagged on for the row."""
    row = (
        db.query(PlatformNotificationRule)
        .filter(PlatformNotificationRule.notification_type == notification_type)
        .first()
    )
    if row is None:
        return _fallback_rule(notification_type, policy)

    channels: list[str] = []
    if row.email_enabled:
        channels.append(CHANNEL_EMAIL)
    if row.dashboard_enabled:
        channels.append(CHANNEL_DASHBOARD)
    if row.sms_enabled:
        channels.append(CHANNEL_SMS)

    offsets = tuple(int(n) for n in (row.offsets or []))
    return ResolvedRule(
        notification_type=notification_type,
        offsets=offsets,
        enabled_channels=tuple(channels),
        content_overrides=dict(row.content_overrides or {}),
        enabled=bool(row.enabled),
    )


def channel_enabled(db: Session, notification_type: str, channel: str) -> bool:
    """True if ``channel`` is enabled for this notification type (rule or
    fallback). Disabled-rule (`enabled=False`) suppresses all channels."""
    rule = get_rule(db, notification_type)
    if not rule.enabled:
        return False
    return channel in rule.enabled_channels


def content_override(
    db: Session, notification_type: str, channel: str
) -> Optional[dict]:
    """Return the per-channel content override (``{"subject":.., "body":..}``)
    if one is configured, else None (caller uses the static template registry)."""
    return get_rule(db, notification_type).override_for(channel)


# ---------------------------------------------------------------------------
# Per-province communication windows (compliance MECHANISM only)
# ---------------------------------------------------------------------------
#
# PLACEHOLDER — LEGAL MUST CONFIRM per-province consumer-protection contact hours
# before launch. The hours below are a single PERMISSIVE default window
# (08:00–21:00 local) applied uniformly to every Canadian province/territory.
# They are NOT authoritative collection/contact-hour rules. Several provinces
# regulate when a creditor / collection agent may contact a consumer (and may
# differ for weekdays vs. Sundays/holidays); those authoritative hours have NOT
# been invented here. This map is the MECHANISM (per-province lookup + local-time
# gate); counsel supplies the real numbers per province/territory before launch.
#
# Keys are the standard Canadian province/territory two-letter codes. Each value
# is (open_hour_inclusive, close_hour_exclusive) in 24h LOCAL time.
_DEFAULT_WINDOW = (8, 21)  # 08:00–21:00 local — PLACEHOLDER, see comment above.

PROVINCE_SEND_WINDOWS: dict[str, tuple[int, int]] = {
    "AB": _DEFAULT_WINDOW,  # Alberta
    "BC": _DEFAULT_WINDOW,  # British Columbia
    "MB": _DEFAULT_WINDOW,  # Manitoba
    "NB": _DEFAULT_WINDOW,  # New Brunswick
    "NL": _DEFAULT_WINDOW,  # Newfoundland and Labrador
    "NS": _DEFAULT_WINDOW,  # Nova Scotia
    "NT": _DEFAULT_WINDOW,  # Northwest Territories
    "NU": _DEFAULT_WINDOW,  # Nunavut
    "ON": _DEFAULT_WINDOW,  # Ontario
    "PE": _DEFAULT_WINDOW,  # Prince Edward Island
    "QC": _DEFAULT_WINDOW,  # Quebec (note: PaySpyre does not yet operate in QC)
    "SK": _DEFAULT_WINDOW,  # Saskatchewan
    "YT": _DEFAULT_WINDOW,  # Yukon
}

# Best-effort province-code → IANA timezone for evaluating "local" hours. Canada
# spans many zones and some provinces straddle zones; this picks the populous /
# canonical zone per province. PLACEHOLDER alongside the windows above — legal +
# product should confirm the timezone basis (borrower-local vs. province-canonical)
# before launch.
PROVINCE_TIMEZONES: dict[str, str] = {
    "AB": "America/Edmonton",
    "BC": "America/Vancouver",
    "MB": "America/Winnipeg",
    "NB": "America/Moncton",
    "NL": "America/St_Johns",
    "NS": "America/Halifax",
    "NT": "America/Yellowknife",
    "NU": "America/Iqaluit",
    "ON": "America/Toronto",
    "PE": "America/Halifax",
    "QC": "America/Toronto",
    "SK": "America/Regina",
    "YT": "America/Whitehorse",
}


def _normalize_province(province: Optional[str]) -> Optional[str]:
    if not province:
        return None
    return province.strip().upper() or None


def is_within_send_window(province: Optional[str], now: datetime) -> bool:
    """True if ``now`` falls inside the allowed send window for ``province``.

    ``now`` should be timezone-aware (UTC); it is converted to the province's
    local time before the window check. Unknown / missing province → permissive
    (returns True) so a missing province never silently blocks all sends; the
    DEFER decision is the processor's, and a missing province is treated as "no
    window restriction known". See the LEGAL placeholder comment above.
    """
    code = _normalize_province(province)
    if code is None or code not in PROVINCE_SEND_WINDOWS:
        return True

    open_h, close_h = PROVINCE_SEND_WINDOWS[code]
    tz_name = PROVINCE_TIMEZONES.get(code)
    local = now
    if tz_name is not None:
        try:
            # If now is naive, assume UTC; zoneinfo conversion requires tz-aware.
            if now.tzinfo is None:
                from datetime import timezone as _tz

                local = now.replace(tzinfo=_tz.utc).astimezone(ZoneInfo(tz_name))
            else:
                local = now.astimezone(ZoneInfo(tz_name))
        except Exception:  # pragma: no cover - tz db missing; fail open
            local = now
    return open_h <= local.hour < close_h


# ---------------------------------------------------------------------------
# Borrower province lookup (from platform_patient_fields)
# ---------------------------------------------------------------------------


def get_patient_province(db: Session, patient_id: Optional[UUID]) -> Optional[str]:
    """Return the borrower's current province code from platform_patient_fields
    (field_key='province'), or None if unset. The province is stored as a JSONB
    value, so ``field_value`` may be a bare JSON string (``"ON"``); we coerce to
    a plain string."""
    if patient_id is None:
        return None
    row = db.execute(
        text(
            """
            SELECT field_value
            FROM platform_patient_fields
            WHERE patient_id = :pid
              AND field_key = 'province'
              AND is_current = TRUE
            ORDER BY id DESC
            LIMIT 1
            """
        ),
        {"pid": str(patient_id)},
    ).first()
    if row is None:
        return None
    value = row[0]
    if isinstance(value, str):
        return value or None
    # Defensive: some writers may store {"value": "ON"}.
    if isinstance(value, dict):
        v = value.get("value") or value.get("province")
        return str(v) if v else None
    return str(value) if value is not None else None
