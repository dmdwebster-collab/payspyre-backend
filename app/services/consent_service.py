"""PaySpyre P5 — Consent service.

Two responsibilities, split so the filesystem half is testable without a database:

- ``ConsentTextLoader`` — loads immutable, versioned consent text from
  ``config/consent_text/<purpose>/<version>.md``, with the active version per
  purpose selected by ``config/consent_text/active.yaml`` (spec §8.2). Pure
  filesystem; no DB.
- ``ConsentService`` — records and revokes ``platform_consents`` rows, capturing
  the **exact text and version the applicant saw** at grant time, and queries
  consent status. Sync / ``Session``-based, matching the other platform services.

Immutability (spec §2.6, "non-negotiable for class-action defense"):
``consent_text_shown`` and ``consent_text_version`` are never updated. This is
enforced three ways: (1) the service only ever sets them at insert; (2) ``revoke``
only touches ``revoked_at``; (3) a module-level SQLAlchemy ``before_update`` guard
raises if either column is ever modified on an existing row. A DB-level trigger
(belt-and-suspenders, like ``platform_events``) is deferred — it would need a new
migration; flagged in the PR.

No PII in event payloads (carried hard rule): consent events record purpose,
version tag, and the grant/deny/revoke fact — never the consent text, IP, or
user-agent (those live on the row, which is the system of record).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import UUID

import yaml
from sqlalchemy import event, inspect as sa_inspect
from sqlalchemy.orm import Session

from app.models.platform.consent import PlatformConsent
from app.models.platform.event import PlatformEvent

# The nine granular consent purposes (spec §8.1 / §2.6; mirrors the
# platform_consent_purpose enum on the model).
VALID_PURPOSES: frozenset[str] = frozenset(
    {
        "id_verification",
        "bank_verification",
        "soft_bureau_pull",
        "hard_bureau_pull",
        "automated_decision_making",
        "marketing_email",
        "marketplace_listing",
        "cross_sell_eligibility",
        "aggregate_data_use",
    }
)

_DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "consent_text"

# Immutable columns — see module docstring.
_IMMUTABLE_CONSENT_COLUMNS = ("consent_text_shown", "consent_text_version")


@event.listens_for(PlatformConsent, "before_update")
def _block_consent_text_mutation(mapper, connection, target) -> None:  # noqa: ANN001
    """App-layer enforcement of the §2.6 immutability rule: refuse any UPDATE that
    changes the captured consent text or version. Re-consent creates a NEW row."""
    state = sa_inspect(target)
    for col in _IMMUTABLE_CONSENT_COLUMNS:
        if state.attrs[col].history.has_changes():
            raise ValueError(
                f"platform_consents.{col} is immutable (spec §2.6): consent text and "
                "version can never be updated. Create a new consent record instead."
            )


class ConsentTextLoader:
    """Loads versioned consent text from the filesystem. Pure; no database."""

    def __init__(self, config_dir: Optional[Path] = None) -> None:
        self.config_dir = config_dir or _DEFAULT_CONFIG_DIR
        self._active_map: Optional[dict[str, str]] = None
        self._text_cache: dict[tuple[str, str], str] = {}

    @staticmethod
    def _validate_purpose(purpose: str) -> None:
        if purpose not in VALID_PURPOSES:
            raise ValueError(
                f"Unknown consent purpose: {purpose!r}. Valid: {sorted(VALID_PURPOSES)}"
            )

    def _load_active_map(self) -> dict[str, str]:
        if self._active_map is None:
            active_path = self.config_dir / "active.yaml"
            if not active_path.is_file():
                raise FileNotFoundError(f"Consent active config not found: {active_path}")
            with active_path.open(encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            active = data.get("active") or {}
            if not isinstance(active, dict):
                raise ValueError("active.yaml: 'active' must be a mapping of purpose -> version")
            missing = VALID_PURPOSES - set(active)
            if missing:
                raise ValueError(f"active.yaml is missing active versions for: {sorted(missing)}")
            self._active_map = {str(k): str(v) for k, v in active.items()}
        return self._active_map

    def active_version(self, purpose: str) -> str:
        """Active version stem for a purpose, e.g. 'v1_2026-05'."""
        self._validate_purpose(purpose)
        return self._load_active_map()[purpose]

    def text(self, purpose: str, version: Optional[str] = None) -> str:
        """Exact consent text for a purpose at a version (defaults to active)."""
        self._validate_purpose(purpose)
        version = version or self.active_version(purpose)
        key = (purpose, version)
        if key not in self._text_cache:
            path = self.config_dir / purpose / f"{version}.md"
            if not path.is_file():
                raise FileNotFoundError(f"Consent text not found: {path}")
            text = path.read_text(encoding="utf-8").strip()
            if not text:
                raise ValueError(f"Consent text is empty: {path}")
            self._text_cache[key] = text
        return self._text_cache[key]

    def version_tag(self, purpose: str, version: Optional[str] = None) -> str:
        """Traceable version tag stored on the consent row, '<purpose>/<version>'."""
        self._validate_purpose(purpose)
        version = version or self.active_version(purpose)
        return f"{purpose}/{version}"


class ConsentService:
    """Records, revokes, and queries platform_consents rows."""

    def __init__(self, db: Session, loader: Optional[ConsentTextLoader] = None) -> None:
        self.db = db
        self.loader = loader or ConsentTextLoader()

    def record_consent(
        self,
        patient_id: UUID,
        purpose: str,
        consent_granted: bool,
        *,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        application_id: Optional[UUID] = None,
        actor: str = "patient",
    ) -> PlatformConsent:
        """Record a consent grant/denial, snapshotting the exact text + version shown."""
        version = self.loader.active_version(purpose)  # also validates the purpose
        text = self.loader.text(purpose, version)
        version_tag = self.loader.version_tag(purpose, version)

        consent = PlatformConsent(
            patient_id=patient_id,
            purpose=purpose,
            consent_granted=consent_granted,
            consent_text_shown=text,
            consent_text_version=version_tag,
            ip_address=ip_address,
            user_agent=user_agent,
            application_id=application_id,
        )
        self.db.add(consent)
        self.db.commit()
        self.db.refresh(consent)

        self._log_event(
            "consent.granted" if consent_granted else "consent.denied",
            patient_id=patient_id,
            application_id=application_id,
            actor=actor,
            payload={
                "purpose": purpose,
                "consent_text_version": version_tag,
                "granted": consent_granted,
            },
        )
        return consent

    def revoke_consent(self, consent_id: UUID, *, actor: str = "patient") -> PlatformConsent:
        """Revoke a previously granted consent. Sets revoked_at only — the captured
        text and version are never touched (immutability guard would block it)."""
        consent = (
            self.db.query(PlatformConsent).filter(PlatformConsent.id == consent_id).first()
        )
        if consent is None:
            raise ValueError(f"Consent {consent_id} not found")
        if consent.revoked_at is not None:
            raise ValueError(f"Consent {consent_id} is already revoked")

        consent.revoked_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(consent)

        self._log_event(
            "consent.revoked",
            patient_id=consent.patient_id,
            application_id=consent.application_id,
            actor=actor,
            payload={
                "purpose": consent.purpose,
                "consent_text_version": consent.consent_text_version,
                "consent_id": str(consent_id),
            },
        )
        return consent

    def has_active_consent(self, patient_id: UUID, purpose: str) -> bool:
        """True if the patient has a granted, non-revoked consent for the purpose."""
        ConsentTextLoader._validate_purpose(purpose)
        return self.get_active_consent(patient_id, purpose) is not None

    def get_active_consent(self, patient_id: UUID, purpose: str) -> Optional[PlatformConsent]:
        """Most recent granted, non-revoked consent for the patient + purpose, if any."""
        return (
            self.db.query(PlatformConsent)
            .filter(
                PlatformConsent.patient_id == patient_id,
                PlatformConsent.purpose == purpose,
                PlatformConsent.consent_granted.is_(True),
                PlatformConsent.revoked_at.is_(None),
            )
            .order_by(PlatformConsent.granted_at.desc())
            .first()
        )

    def get_consents_for_patient(
        self, patient_id: UUID, *, include_revoked: bool = False
    ) -> list[PlatformConsent]:
        q = self.db.query(PlatformConsent).filter(PlatformConsent.patient_id == patient_id)
        if not include_revoked:
            q = q.filter(PlatformConsent.revoked_at.is_(None))
        return q.order_by(PlatformConsent.granted_at.desc()).all()

    def get_consents_for_application(self, application_id: UUID) -> list[PlatformConsent]:
        return (
            self.db.query(PlatformConsent)
            .filter(PlatformConsent.application_id == application_id)
            .order_by(PlatformConsent.granted_at.desc())
            .all()
        )

    def _log_event(
        self,
        event_type: str,
        *,
        patient_id: Optional[UUID],
        application_id: Optional[UUID],
        actor: str,
        payload: dict,
    ) -> PlatformEvent:
        event = PlatformEvent(
            event_type=event_type,
            actor=actor,
            patient_id=patient_id,
            application_id=application_id,
            payload=payload,
        )
        self.db.add(event)
        self.db.commit()
        return event
