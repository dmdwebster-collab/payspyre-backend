"""Human-readable audit diffs (WS-I) — platform_events rendered as old→new.

The WORM event log (``platform_events``) is the system of record for "who did
what"; its payloads follow the ``{"v": 1, "actor": ..., "before": {...},
"after": {...}}`` convention. This module renders those payloads as
FIELD-LEVEL DIFFS — "status: under_review → approved" — so the cockpit's audit
tab reads like a change history instead of raw JSON.

Pure functions — DB-free testable; the endpoint feeds it queried events.
"""
from __future__ import annotations

import json
from typing import Any, Optional

# Payload keys that are envelope/context, not changed fields.
_ENVELOPE_KEYS = {"v", "actor", "before", "after"}


def _render_value(value: Any) -> str:
    """A compact human rendering of one field value."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        rendered = json.dumps(value, sort_keys=True, default=str)
        return rendered if len(rendered) <= 120 else rendered[:117] + "..."
    return str(value)


def diff_fields(before: Optional[dict], after: Optional[dict]) -> list[dict]:
    """Field-level old→new diffs between two payload dicts (pure).

    * key in both, values differ → changed (old → new)
    * key only in after          → set (— → new)
    * key only in before         → cleared (old → —)
    Unchanged keys are dropped. Deterministic (sorted by field name).
    """
    before = before or {}
    after = after or {}
    diffs: list[dict] = []
    for field in sorted(set(before) | set(after)):
        old = before.get(field)
        new = after.get(field)
        if field in before and field in after and old == new:
            continue
        diffs.append(
            {
                "field": field,
                "old": old,
                "new": new,
                "display": f"{field}: {_render_value(old)} → {_render_value(new)}",
            }
        )
    return diffs


def render_event_diff(
    event_id: int,
    occurred_at,
    event_type: str,
    actor: str,
    payload: Optional[dict],
) -> dict:
    """One event rendered as a change row (pure).

    ``changes`` come from the before/after envelope; ``context`` carries the
    payload's remaining non-envelope keys (loan_id, comment, reason, …) so the
    row stands alone. Events without before/after (pure facts — a payment
    recorded, a message sent) render their ``after``-style facts as set-diffs
    when present, else an empty change list with context only.
    """
    payload = payload or {}
    before = payload.get("before") if isinstance(payload.get("before"), dict) else None
    after = payload.get("after") if isinstance(payload.get("after"), dict) else None
    context = {k: v for k, v in payload.items() if k not in _ENVELOPE_KEYS}
    return {
        "event_id": event_id,
        "occurred_at": (
            occurred_at.isoformat() if hasattr(occurred_at, "isoformat") else occurred_at
        ),
        "event_type": event_type,
        "actor": actor,
        "changes": diff_fields(before, after),
        "context": context,
    }
