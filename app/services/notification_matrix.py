"""Notification matrix (Workstream F — TL "General system notifications" parity).

Extends the PR #174 registry (``NOTIFICATION_TYPES`` + ``platform_notification_rules``)
into the full TL-style matrix: **4 audiences × 3 channels × event catalog**.

* Audiences: borrower / vendor / staff / admin. Each event belongs to a default
  audience set derived from the registry (``bo_*`` internal notices → staff+admin,
  ``vendor_*`` → vendor, everything else → borrower); the matrix stores per-cell
  (audience × channel) on/off in the ``audience_channels`` JSONB column ON THE
  SAME ``platform_notification_rules`` row the template editor already uses —
  storage is NOT forked.
* Channels: email / sms / dashboard. **Dashboard is always-on** (Dave, video 08:
  "all notifications are sent to dashboards... not configurable to not send") —
  the accessor forces dashboard True for any audience the event applies to,
  regardless of stored value.
* Templates: subject/body live in the existing ``content_overrides``; this
  module adds only the ``attachments`` list (TL's attachment picker) stored on
  the same row.

Nothing here changes the live send path's channel decisions: the processor keeps
reading ``get_rule`` per-channel flags (the borrower-audience row). The audience
matrix is the config surface the TL-parity send paths (vendor/staff fan-out)
consume as they come online.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

AUDIENCES = ("borrower", "vendor", "staff", "admin")
CHANNELS = ("email", "sms", "dashboard")

# TL attachment picker options (video 08 f0057).
ATTACHMENT_OPTIONS = (
    "loan_agreement",
    "terms_conditions",
    "privacy_policy",
    "loan_statements",
    "amortization_schedule",
)


def default_audiences(notification_type: str) -> tuple[str, ...]:
    """Which audiences an event applies to by default, from its registry key."""
    if notification_type.startswith("bo_"):
        return ("staff", "admin")
    if notification_type.startswith("vendor_"):
        return ("vendor",)
    return ("borrower",)


@dataclass(frozen=True)
class MatrixCell:
    audience: str
    channel: str
    enabled: bool
    locked: bool  # dashboard cells are locked-on (Dave)


@dataclass(frozen=True)
class EventMatrix:
    notification_type: str
    audiences: tuple[str, ...]
    cells: tuple[MatrixCell, ...]
    attachments: tuple[str, ...]

    def enabled(self, audience: str, channel: str) -> bool:
        for cell in self.cells:
            if cell.audience == audience and cell.channel == channel:
                return cell.enabled
        return False


def _default_cell_enabled(rule: Any, audience: str, channel: str,
                          primary_audiences: tuple[str, ...]) -> bool:
    """Default cell value with no stored audience matrix: the event's primary
    audiences inherit the legacy per-channel flags; other audiences default off."""
    if audience not in primary_audiences:
        return False
    if channel == "dashboard":
        return True  # always-on
    return channel in rule.enabled_channels


def get_event_matrix(db: Any, notification_type: str) -> EventMatrix:
    """The effective audience×channel matrix for one event.

    Stored cells come from ``platform_notification_rules.audience_channels``
    (``{audience: {channel: bool}}``); missing cells fall back to the legacy
    per-channel flags for the event's primary audiences. Dashboard is forced on
    for every audience the event applies to."""
    from app.models.platform.notification_rule import PlatformNotificationRule
    from app.services.notification_config import get_rule

    rule = get_rule(db, notification_type)
    row = db.get(PlatformNotificationRule, notification_type) if db is not None else None
    stored: dict = {}
    attachments: tuple[str, ...] = ()
    if row is not None:
        stored = getattr(row, "audience_channels", None) or {}
        attachments = tuple(getattr(row, "attachments", None) or [])

    primary = default_audiences(notification_type)
    cells: list[MatrixCell] = []
    for audience in AUDIENCES:
        audience_stored = stored.get(audience) if isinstance(stored, dict) else None
        for channel in CHANNELS:
            if channel == "dashboard":
                # Always-on for applicable audiences; off elsewhere unless an
                # admin explicitly added the audience (any stored cell counts).
                applicable = audience in primary or bool(audience_stored)
                cells.append(MatrixCell(audience, channel, applicable, locked=True))
                continue
            if isinstance(audience_stored, dict) and channel in audience_stored:
                enabled = bool(audience_stored[channel])
            else:
                enabled = _default_cell_enabled(rule, audience, channel, primary)
            cells.append(MatrixCell(audience, channel, enabled, locked=False))
    return EventMatrix(
        notification_type=notification_type,
        audiences=primary,
        cells=tuple(cells),
        attachments=attachments,
    )


def audience_channel_enabled(
    db: Any, notification_type: str, audience: str, channel: str
) -> bool:
    """Send-path accessor: is this audience×channel cell on for this event?
    Honours the event-level enable flag (a disabled event silences every cell)."""
    from app.services.notification_config import get_rule

    if not get_rule(db, notification_type).enabled:
        return False
    return get_event_matrix(db, notification_type).enabled(audience, channel)


def validate_matrix_update(
    audience_channels: Optional[dict], attachments: Optional[list]
) -> Optional[str]:
    """Validate a matrix edit; returns an error string or None when valid.
    Dashboard cells cannot be turned OFF (Dave's always-on rule)."""
    if audience_channels is not None:
        if not isinstance(audience_channels, dict):
            return "audience_channels must be an object"
        for audience, chans in audience_channels.items():
            if audience not in AUDIENCES:
                return f"unknown audience {audience!r}"
            if not isinstance(chans, dict):
                return f"audience {audience!r} must map channels to booleans"
            for channel, value in chans.items():
                if channel not in CHANNELS:
                    return f"unknown channel {channel!r}"
                if not isinstance(value, bool):
                    return f"cell {audience}.{channel} must be a boolean"
                if channel == "dashboard" and value is False:
                    return "dashboard channel is always-on and cannot be disabled"
    if attachments is not None:
        if not isinstance(attachments, list):
            return "attachments must be a list"
        for item in attachments:
            if item not in ATTACHMENT_OPTIONS:
                return (
                    f"unknown attachment {item!r}; options: "
                    f"{', '.join(ATTACHMENT_OPTIONS)}"
                )
    return None
