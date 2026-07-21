"""Application-process config service (Wave 2 W2-APPCONFIG).

Effective-config resolution mirrors ``app/services/company_info.py``:

  1. the ``platform_application_process_config`` row (id=1), if present;
  2. the shipped :data:`ApplicationProcessConfig` defaults otherwise.

The applicant flow + admin UI read the effective config; the offer engine reads
:func:`effective_offer_policy` for expiry-days / max-offers. Every accessor is
FALLIBLE-SAFE: a missing table (pre-migration) or DB error degrades to the
shipped defaults rather than breaking origination — the defaults equal the
current ``settings.OFFER_*`` values, so behaviour is preserved either way.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.exc import SQLAlchemyError

from app.core.logging import get_logger
from app.schemas.application_process_config import (
    ApplicationProcessConfig,
    ApplicationProcessConfigError,
    parse_application_process_config,
)

logger = get_logger(__name__)

_SINGLETON_ID = 1


def _get_row(db: Any) -> Any:
    from app.models.platform.application_process_config import (
        PlatformApplicationProcessConfig,
    )

    return db.get(PlatformApplicationProcessConfig, _SINGLETON_ID)


def get_effective_config(db: Any) -> ApplicationProcessConfig:
    """The effective application-process config (DB row ⊕ defaults).

    NEVER raises for a read: a missing/failed row → shipped defaults. A row that
    fails to parse is logged and defaults are used (a bad stored blob must not
    brick origination)."""
    if db is None:
        return ApplicationProcessConfig()
    try:
        row = _get_row(db)
    except SQLAlchemyError:  # pragma: no cover - defensive (pre-migration/outage)
        logger.warning("application_process_config read failed; using defaults")
        return ApplicationProcessConfig()
    if row is None or not getattr(row, "config", None):
        return ApplicationProcessConfig()
    try:
        return parse_application_process_config(row.config, context="db-row")
    except ApplicationProcessConfigError:
        logger.error("stored application_process_config is invalid; using defaults")
        return ApplicationProcessConfig()


@dataclass(frozen=True)
class OfferPolicy:
    expiry_days: int
    max_offers: int
    allow_multiple: bool


def effective_offer_policy(db: Any) -> OfferPolicy:
    """Offer expiry / max-offers, read from config with a settings fallback.

    Behaviour-preserving: the config's defaults equal ``settings.OFFER_*`` so an
    unconfigured platform (no row) returns exactly the values the offer engine
    used before this workstream existed."""
    cfg = get_effective_config(db)
    flow = cfg.flow
    return OfferPolicy(
        expiry_days=flow.offer_expiry_days,
        max_offers=flow.max_offers,
        allow_multiple=flow.allow_multiple_offers,
    )


def form_variant_for_product(db: Any, variant_name: Optional[str]):
    """Resolve the form-variant a product uses to its definition (or default)."""
    return get_effective_config(db).variant(variant_name)


def save_config(
    db: Any, cfg: ApplicationProcessConfig, *, updated_by: Optional[str] = None
) -> ApplicationProcessConfig:
    """Upsert row 1 with a validated config document. Returns the saved config.

    Writes the normalized JSON (``exclude_none``) so the stored blob converges on
    the typed shape. Commits."""
    from app.models.platform.application_process_config import (
        PlatformApplicationProcessConfig,
    )

    payload = cfg.model_dump(mode="json")
    row = _get_row(db)
    if row is None:
        row = PlatformApplicationProcessConfig(
            id=_SINGLETON_ID, config=payload, updated_by=updated_by
        )
        db.add(row)
    else:
        row.config = payload
        row.updated_by = updated_by
    db.commit()
    db.refresh(row)
    return parse_application_process_config(row.config, context="save")
