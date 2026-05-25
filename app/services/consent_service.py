"""Consent service — PR P5.

Two distinct responsibilities live in this single module:

1. **Consent text loader (pure).** Reads immutable consent markdown from
   ``config/consent_text/<purpose>/<version>.md``, driven by
   ``config/consent_text/active.json`` (a flat ``{purpose: version}`` map).
   Results are cached in-process with no TTL — a process restart (re-deploy)
   is how new versions are picked up, which matches the immutability model.
   The loader performs no DB or network I/O.

2. **Consent recorder / revoker (impure).** Writes ``platform_consents`` rows
   (migration 020). The pure loader supplies the verbatim text + version that
   get embedded in each row. These functions DO write to the DB — they are the
   service primitives the P6 API/orchestration layer will call.

Hard rules enforced here (spec §2.6, §8.1/§8.2):

- ``consent_text_shown`` / ``consent_text_version`` are **never UPDATEd**.
  Revocation sets ``revoked_at`` only. New language => new version; old
  consents preserve their language verbatim.
- Consent text is **filesystem-loaded, never DB-stored** (version-controlled
  in git, immutable per file).
- The loader is **pure** — no DB, no network.
- **No PII in logs**: only ``purpose``, ``version``, ``patient_id`` / consent
  ``id`` (UUIDs). Never the IP, user agent, or text body.
- ``record_consent`` embeds the loaded text **verbatim** (byte-for-byte) — no
  reformatting, no template substitution.

Note: the active-version manifest is ``active.json`` (stdlib ``json``), not
YAML — chosen to avoid adding an undeclared production dependency.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.platform.consent import PlatformConsent

logger = get_logger(__name__)

# Repo-root-relative: app/services/consent_service.py -> parents[2] == repo root.
_CONSENT_TEXT_DIR = Path(__file__).resolve().parents[2] / "config" / "consent_text"
_ACTIVE_CONFIG_NAME = "active.json"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConsentServiceError(Exception):
    """Base class for consent service errors."""


class UnknownConsentPurposeError(ConsentServiceError):
    """Raised when a purpose has no entry in active.json."""


class ConsentVersionNotFoundError(ConsentServiceError):
    """Raised when active.json points at a consent-text file that does not exist."""


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsentText:
    """Immutable snapshot of the active consent text for a single purpose.

    Frozen by design. The loader returns the *same* cached instance on repeated
    calls for a purpose (identity is part of the test contract).
    """

    purpose: str
    version: str
    text: str
    loaded_at: datetime  # UTC, set when the instance is inserted into the cache


# ---------------------------------------------------------------------------
# Loader (pure: filesystem only, no DB / no network)
# ---------------------------------------------------------------------------

# Module-level, in-process caches. No TTL, no invalidation — see module docstring.
_text_cache: dict[str, ConsentText] = {}
_active_versions: dict[str, str] | None = None


def _load_active_versions() -> dict[str, str]:
    """Read and cache the ``active.json`` ``{purpose: version}`` map (once)."""
    global _active_versions
    if _active_versions is None:
        config_path = _CONSENT_TEXT_DIR / _ACTIVE_CONFIG_NAME
        try:
            raw = config_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ConsentServiceError(
                f"Active consent config not found: {config_path}"
            ) from exc
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConsentServiceError(
                f"Active consent config is not valid JSON: {config_path}"
            ) from exc
        if not isinstance(parsed, dict):
            raise ConsentServiceError(
                f"Active consent config must be a JSON object: {config_path}"
            )
        _active_versions = {str(k): str(v) for k, v in parsed.items()}
    return _active_versions


def get_active_consent_text(purpose: str) -> ConsentText:
    """Return the active :class:`ConsentText` for ``purpose``.

    Loads from the filesystem on first call per purpose and caches the result;
    repeated calls return the same cached instance (identity preserved).

    Raises:
        UnknownConsentPurposeError: ``purpose`` is absent from active.json.
        ConsentVersionNotFoundError: active.json names a version whose markdown
            file does not exist on disk.
    """
    cached = _text_cache.get(purpose)
    if cached is not None:
        return cached

    active = _load_active_versions()
    if purpose not in active:
        raise UnknownConsentPurposeError(
            f"No active consent text configured for purpose '{purpose}'. "
            f"Known purposes: {sorted(active)}."
        )

    version = active[purpose]
    path = _CONSENT_TEXT_DIR / purpose / f"{version}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConsentVersionNotFoundError(
            f"active.json maps purpose '{purpose}' to version '{version}', "
            f"but no consent text file exists at {path}."
        ) from exc

    consent = ConsentText(
        purpose=purpose,
        version=version,
        text=text,
        loaded_at=datetime.now(timezone.utc),
    )
    _text_cache[purpose] = consent
    return consent


def _reset_cache() -> None:
    """Test-only: clear the in-process caches. Production code never calls this."""
    global _active_versions
    _text_cache.clear()
    _active_versions = None


# ---------------------------------------------------------------------------
# Recorder / revoker (impure: writes platform_consents)
# ---------------------------------------------------------------------------


def record_consent(
    db: Session,
    patient_id: UUID,
    purpose: str,
    granted: bool,
    application_id: UUID | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> PlatformConsent:
    """Record a consent decision against ``platform_consents`` and return the row.

    Looks up the active text + version via the pure loader and embeds the text
    **verbatim** (Hard Rule #5). ``granted_at`` is stamped now (UTC) and
    ``revoked_at`` is left NULL — the row is a permanent, immutable record of
    what the patient saw and decided.
    """
    consent_text = get_active_consent_text(purpose)  # raises on unknown/missing
    now = datetime.now(timezone.utc)

    consent = PlatformConsent(
        patient_id=patient_id,
        purpose=purpose,
        consent_granted=granted,
        consent_text_shown=consent_text.text,  # verbatim, byte-for-byte (Hard Rule #5)
        consent_text_version=consent_text.version,
        granted_at=now,
        revoked_at=None,
        application_id=application_id,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    db.add(consent)
    db.commit()
    db.refresh(consent)

    # Hard Rule #4: log only non-PII identifiers — never ip_address / user_agent / text.
    logger.info(
        "consent_recorded",
        consent_id=str(consent.id),
        patient_id=str(patient_id),
        purpose=purpose,
        version=consent_text.version,
        granted=granted,
    )
    return consent


def revoke_consent(db: Session, consent_id: UUID) -> PlatformConsent:
    """Mark a consent row revoked (sets ``revoked_at`` only) and return it.

    Hard Rule #1 (spec §2.6, §8.2): ``consent_text_shown`` and
    ``consent_text_version`` are NEVER touched here. Revocation is recorded
    purely by stamping ``revoked_at``; the original language is preserved
    verbatim for class-action defense.
    """
    consent = (
        db.query(PlatformConsent)
        .filter(PlatformConsent.id == consent_id)
        .first()
    )
    if consent is None:
        raise ValueError(f"Consent {consent_id} not found")

    consent.revoked_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(consent)

    logger.info(
        "consent_revoked",
        consent_id=str(consent.id),
        patient_id=str(consent.patient_id),
        purpose=consent.purpose,
        version=consent.consent_text_version,
    )
    return consent


def get_active_consents_for_patient(
    db: Session, patient_id: UUID
) -> list[PlatformConsent]:
    """Return a patient's non-revoked consents, newest grant first.

    The ``revoked_at IS NULL`` filter matches the partial index
    ``idx_platform_consents_patient_purpose`` (migration 020).
    """
    return (
        db.query(PlatformConsent)
        .filter(
            PlatformConsent.patient_id == patient_id,
            PlatformConsent.revoked_at.is_(None),
        )
        .order_by(PlatformConsent.granted_at.desc())
        .all()
    )
