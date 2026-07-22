"""Resolve a provider's TYPED behaviour config for its consumers.

``app.schemas.integration_config`` defines the shapes (Flinks / Equifax) and
``app.services.integration_settings`` stores them. This module is the single
read-side entry point every *consumer* uses, so a knob can never drift back
into the "stored, validated, shown in the UI, read by nobody" state:

    from app.services import integration_behaviour
    flinks = integration_behaviour.flinks_config(db)
    depth  = flinks.transaction_depth_days

Rules:
  * ``db=None`` or no row → the schema defaults. Those defaults are Dave's
    on-screen values, and every consumer's behaviour at the default is exactly
    what it was before the knob was wired.
  * A row that fails validation resolves to defaults rather than raising — a
    bad settings row must never 500 a webhook or a borrower-facing read.
  * ``has_row`` tells a consumer whether an admin has actually configured the
    provider, for the cases where "unconfigured" and "configured at the
    default" must behave differently (see :func:`flinks_forces_simulator`).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from app.schemas.integration_config import EquifaxConfig, FlinksConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedFlinks:
    config: FlinksConfig
    has_row: bool
    enabled: bool


@dataclass(frozen=True)
class ResolvedEquifax:
    config: EquifaxConfig
    has_row: bool
    enabled: bool


def _raw(db: Optional[Session], provider: str) -> tuple[Optional[dict], bool, bool]:
    if db is None:
        return None, False, False
    from app.services import integration_settings

    row = integration_settings.get(db, provider)
    if row is None:
        return None, False, False
    return (row.config or {}), True, bool(getattr(row, "enabled", False))


def _parse(model, raw: Optional[dict], provider: str):
    try:
        return model.model_validate(raw or {})
    except Exception:  # noqa: BLE001 — tolerant read; writes are what validate
        logger.warning(
            "integration_behaviour: stored %s config failed validation; "
            "falling back to schema defaults",
            provider,
        )
        return model()


def resolve_flinks(db: Optional[Session]) -> ResolvedFlinks:
    raw, has_row, enabled = _raw(db, "flinks")
    return ResolvedFlinks(
        config=_parse(FlinksConfig, raw, "flinks"), has_row=has_row, enabled=enabled
    )


def flinks_config(db: Optional[Session]) -> FlinksConfig:
    """The effective Flinks behaviour config (defaults when unconfigured)."""
    return resolve_flinks(db).config


def resolve_equifax(db: Optional[Session]) -> ResolvedEquifax:
    raw, has_row, enabled = _raw(db, "equifax")
    return ResolvedEquifax(
        config=_parse(EquifaxConfig, raw, "equifax"), has_row=has_row, enabled=enabled
    )


def equifax_config(db: Optional[Session]) -> EquifaxConfig:
    """The effective Equifax behaviour config (defaults when unconfigured)."""
    return resolve_equifax(db).config


def flinks_forces_simulator(db: Optional[Session]) -> bool:
    """True when an ADMIN-CONFIGURED Flinks row asks for test mode.

    ``test_mode`` defaults to True (Dave's screen), so it is honoured only when
    a settings row actually exists — otherwise merely enabling
    ``USE_REAL_ADAPTERS`` on a tenant that never touched the Integrations page
    would silently flip back to the simulator, which would be a behaviour
    change rather than a wiring fix.
    """
    resolved = resolve_flinks(db)
    return resolved.has_row and bool(resolved.config.test_mode)


def bank_verification_policy(db: Optional[Session]) -> dict:
    """The applicant-facing half of the Flinks block, as an API payload.

    Consumed by ``GET /applications/{id}/consents`` so the applicant client can
    sequence the bank-verification step (``when_to_verify``), decide whether to
    offer a Skip control (``allow_customer_skip``), and tell the borrower how
    long a completed verification stays good for / when they will be reminded.
    """
    cfg = flinks_config(db)
    return {
        "when_to_verify": cfg.when_to_verify.value,
        "allow_customer_skip": cfg.allow_customer_skip,
        "verification_expiry_days": cfg.verification_expiry_days,
        "verification_reminder_days": cfg.verification_reminder_days,
    }
