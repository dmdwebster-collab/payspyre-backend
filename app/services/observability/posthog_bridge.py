"""PostHog bridge — P8.0 observability fan-out hook.

Single integration point between ``platform_events`` and PostHog. Called from
``FlowOrchestrator._emit_event()`` (and from the small set of non-orchestrator
``PlatformEvent`` insertion sites — webhook receivers + the real notification
dispatcher) immediately after ``db.add(event)``. Never raises — a PostHog
failure must not break event emission or the surrounding DB transaction.

PII rules (Hard Rule #6 — non-negotiable):

- ``distinct_id`` = ``sha256(str(application_id))[:16]`` hex. Never raw UUID.
- Never forwarded: email, phone, name, dob, sin, raw token, ``recipient_redacted``
  hash (it's still recipient-derived and not needed downstream), raw
  ``application_id`` / ``patient_id`` UUIDs, or any ``rich_payload`` contents.
- ``error_class`` = exception class name only (vendor error messages may echo
  PII).

Design:

- Fire-and-forget: ``posthog.capture()`` uses a background flush thread; this
  function returns immediately.
- ``OBSERVABILITY_ENABLED=False`` is a complete no-op (no network I/O).
- If ``posthog`` is not installed or fails to import, ``capture_event()``
  degrades silently — event emission and DB writes are unaffected.
- The allowlist is read from ``settings`` at call time (not module load time),
  so tests can patch ``settings.OBSERVABILITY_POSTHOG_ALLOWLIST`` without
  reimport.
- If the event type is not in the allowlist, returns immediately (no PostHog
  call, no log noise).
"""
from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.platform.event import PlatformEvent

logger = logging.getLogger(__name__)


def _hashed_id(raw_id: object) -> str:
    """Return first 16 hex chars of ``sha256(str(raw_id))``.

    The truncation is deliberate — 16 hex chars (64 bits) is plenty of entropy
    to distinguish applications in PostHog while keeping the distinct_id short
    enough to fit comfortably in dashboards and filters.
    """
    return hashlib.sha256(str(raw_id).encode()).hexdigest()[:16]


def _safe_properties(event: "PlatformEvent") -> dict:
    """Build a PostHog ``properties`` dict from a ``PlatformEvent``.

    Extracts only metadata fields from ``event.payload`` — never raw PII.
    Every value is scalar (``str``/``int``/``bool``/``None``) — no nested
    dicts from ``rich_payload`` or ``before``/``after`` that might contain PII.
    """
    payload = event.payload or {}
    return {
        "event_type": event.event_type,
        "platform_event_id": event.id,        # bigint — safe, links back to Postgres
        "application_id_hash": (
            _hashed_id(event.application_id) if event.application_id else None
        ),
        # Metadata fields extracted from payload — only if present.
        "vendor": payload.get("vendor"),
        "result": payload.get("result"),
        "decision": payload.get("decision"),
        "error_class": payload.get("error_class"),    # class name only, no message
        "verification_type": payload.get("verification_type"),
        "contact_method": payload.get("contact_method"),  # "sms"|"email" — NOT address
        "status": payload.get("status"),              # for notification_status_updated
    }


def capture_event(event: "PlatformEvent") -> None:
    """Mirror a ``PlatformEvent`` to PostHog if it is on the allowlist.

    Safe to call inside a SQLAlchemy unit of work — never raises, never blocks
    the calling thread beyond a dict construction. Returns immediately if the
    feature flag is off, the API key is unset, the event type is not allowed,
    or the ``posthog`` SDK isn't available.
    """
    try:
        # Local import — avoids pulling settings into the module namespace at
        # import time (any circulars in app.core.* are sidestepped).
        from app.core.config import settings

        if not settings.OBSERVABILITY_ENABLED:
            return
        if not settings.POSTHOG_API_KEY:
            return

        allowlist = {
            t.strip()
            for t in settings.OBSERVABILITY_POSTHOG_ALLOWLIST.split(",")
            if t.strip()
        }
        if event.event_type not in allowlist:
            return

        # Optional dependency — local import gives graceful degradation if the
        # package isn't installed (tests simulate this via patch.dict).
        import posthog

        posthog.api_key = settings.POSTHOG_API_KEY
        posthog.host = settings.POSTHOG_HOST

        distinct_id = (
            _hashed_id(event.application_id) if event.application_id else "anonymous"
        )

        posthog.capture(
            distinct_id=distinct_id,
            event=event.event_type,
            properties=_safe_properties(event),
        )

    except Exception:  # noqa: BLE001
        # PostHog failure must never propagate — log at WARNING so it's
        # visible in Sentry / CloudWatch without breaking the caller.
        logger.warning("posthog_bridge.capture_event failed silently", exc_info=True)
