"""The single source of truth for a provider's SIMULATOR / LIVE mode.

Dave's Integration SIMULATOR mandate (see
``docs/dave_review_2026-07-21/DAVE_DIRECTIVES_2026-07-22.md``): every
integration has TWO toggle-able modes —

    simulator  a real, reviewable simulation of the vendor's behaviour. NOT a
               no-op and NOT "not available". A simulated result really drives
               the flow (a simulated signature really moves the loan forward),
               but is clearly LABELLED ``simulated: true`` on responses/events.
    live       the real vendor call. Requires the provider's credentials to be
               present; refusing to switch to live without them is enforced
               here.

This module is the ONE place that resolves that mode. Adapters, the loan
lifecycle, and the settings API all read it through here so the concept can
never fork into per-provider flags again.

RECONCILIATION with #207 (``FlinksConfig.test_mode``):
    #207 added a typed Flinks knob ``test_mode`` that routed bank verification
    back through the simulator. It is now SUBSUMED by ``mode``. Migration
    ``077_integration_mode`` backfilled the flinks row's ``mode`` from its
    stored ``test_mode``; the settings service keeps ``config.test_mode``
    mirrored to ``mode`` on every write so the two never diverge. Consumers
    should read :func:`is_simulator` (or :func:`forces_simulator`) — NOT
    ``test_mode`` — going forward.

Default (no row / ``db is None``) is ``simulator``: an unconfigured integration
is always exercisable without credentials, and no code path can accidentally hit
a live vendor before an admin explicitly flips the toggle.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

SIMULATOR = "simulator"
LIVE = "live"
VALID_MODES = (SIMULATOR, LIVE)
DEFAULT_MODE = SIMULATOR

#: Audit event type written to ``platform_events`` on every mode change.
MODE_CHANGED_EVENT = "integration_mode_changed"


class IntegrationModeError(ValueError):
    """A mode change was rejected (e.g. switching to live without credentials)."""


# Which secret keys each provider MUST have populated before it may run in
# ``live`` mode. Mirrors the auth inputs the connection-test probes require
# (``app.services.connection_test``) and the credential builders in
# ``app.services.loan_lifecycle`` / the verification dispatcher. Providers absent
# from this map have no hard credential gate (live is allowed without a check).
REQUIRED_LIVE_SECRETS: dict[str, tuple[str, ...]] = {
    "didit": ("api_key",),
    "flinks": ("api_key",),
    "equifax": ("security_code", "client_id", "client_secret"),
    # SignNow accepts either a static bearer ``api_key`` OR the OAuth quartet;
    # handled specially in :func:`missing_live_credentials`.
    "signnow": ("client_id", "client_secret", "username", "password"),
    "zumrails": ("api_key", "api_secret"),
    "sendgrid": ("api_key",),
    "twilio": ("account_sid", "auth_token"),
    "google_analytics": ("api_secret",),
}


def normalize_mode(mode: Optional[str]) -> str:
    """Coerce a mode string to a valid value, defaulting to ``simulator``.

    Raises :class:`IntegrationModeError` for a non-empty but invalid value so a
    typo can never silently persist as ``simulator``.
    """
    if mode is None or mode == "":
        return DEFAULT_MODE
    m = str(mode).strip().lower()
    if m not in VALID_MODES:
        raise IntegrationModeError(
            f"Invalid integration mode '{mode}'. Expected one of {VALID_MODES}."
        )
    return m


def _row(db: Optional[Session], provider: str):
    if db is None:
        return None
    from app.services import integration_settings

    return integration_settings.get(db, provider)


def resolve_mode(db: Optional[Session], provider: str) -> str:
    """Return the effective mode for ``provider`` (``simulator`` when no row)."""
    row = _row(db, provider)
    if row is None:
        return DEFAULT_MODE
    return normalize_mode(getattr(row, "mode", None))


def is_simulator(db: Optional[Session], provider: str) -> bool:
    return resolve_mode(db, provider) == SIMULATOR


def is_live(db: Optional[Session], provider: str) -> bool:
    return resolve_mode(db, provider) == LIVE


def forces_simulator(db: Optional[Session], provider: str) -> bool:
    """True only when an admin-CONFIGURED row is in simulator mode.

    Back-compat shim for the old ``integration_behaviour.flinks_forces_simulator``
    semantics: a consumer that used to fall through to ``USE_REAL_ADAPTERS`` when
    NO row existed keeps doing so (no row -> ``False`` here), while a saved row in
    simulator mode authoritatively forces the simulator. This preserves the exact
    behaviour of a tenant that never opened the Integrations page.
    """
    row = _row(db, provider)
    if row is None:
        return False
    return normalize_mode(getattr(row, "mode", None)) == SIMULATOR


def missing_live_credentials(
    db: Optional[Session],
    provider: str,
    secrets: Optional[dict] = None,
) -> list[str]:
    """Which required secret keys are absent for ``provider`` to go live.

    Pass ``secrets`` to validate a set of credentials that are being written in
    the same request (the authoritative values on an upsert); otherwise the
    stored row's secrets are inspected. Returns ``[]`` when live is permitted.
    """
    if secrets is None:
        row = _row(db, provider)
        secrets = dict(getattr(row, "secrets", None) or {}) if row is not None else {}

    def _present(key: str) -> bool:
        return bool((secrets or {}).get(key))

    # SignNow: a static bearer token OR the full OAuth quartet is sufficient.
    if provider == "signnow":
        if _present("api_key") or _present("access_token"):
            return []
        quartet = ("client_id", "client_secret", "username", "password")
        missing = [k for k in quartet if not _present(k)]
        # Only report the quartet as missing if NONE of it is present; a partial
        # quartet reports exactly what's outstanding.
        return missing if missing != list(quartet) else list(quartet)

    required = REQUIRED_LIVE_SECRETS.get(provider, ())
    return [k for k in required if not _present(k)]


def can_enable_live(
    db: Optional[Session],
    provider: str,
    secrets: Optional[dict] = None,
) -> bool:
    """True when ``provider`` has the credentials required to switch to live."""
    return not missing_live_credentials(db, provider, secrets=secrets)


def assert_live_allowed(
    db: Optional[Session],
    provider: str,
    secrets: Optional[dict] = None,
) -> None:
    """Raise :class:`IntegrationModeError` if live is requested without creds."""
    missing = missing_live_credentials(db, provider, secrets=secrets)
    if missing:
        raise IntegrationModeError(
            f"Cannot switch '{provider}' to live: missing required credentials "
            f"({', '.join(missing)}). Enter them in the settings area first, or "
            f"keep the integration in simulator mode."
        )


def record_mode_change(
    db: Session,
    provider: str,
    *,
    old_mode: str,
    new_mode: str,
    actor: Optional[str] = None,
) -> None:
    """Append an audit row for a mode change (no commit — shares the caller's txn).

    NEVER logs or stores secret material — only the provider slug and the two
    mode strings.
    """
    from app.models.platform.event import PlatformEvent

    db.add(
        PlatformEvent(
            event_type=MODE_CHANGED_EVENT,
            actor=actor or "system",
            payload={
                "v": 1,
                "provider": provider,
                "old_mode": old_mode,
                "new_mode": new_mode,
                "actor": {"type": "admin" if actor else "system", "id": actor or "system"},
            },
        )
    )
