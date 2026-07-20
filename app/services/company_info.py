"""Company-info accessor (Workstream F — TL "Company settings > Company information").

Single source for company identity fields consumed by documents + notifications
(legal name → legal documents; operating/brand name → payment notifications;
phone/email → template merge fields — the COMPANY_PHONE-style lookups).

Resolution order:
1. the ``platform_company_info`` row (admin-edited via /admin/settings/company-info);
2. the shipped :data:`DEFAULTS` (video-07 UAT values + env settings), so the
   system renders correctly on an empty table.

``get_company_context()`` (no db argument) is the fallible convenience wrapper
used from template rendering: it opens a short-lived session, caches the result
for :data:`_CACHE_TTL_SECONDS`, and NEVER raises — a DB outage degrades to the
shipped defaults rather than breaking a send.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger(__name__)

_CACHE_TTL_SECONDS = 60.0
_cache: dict[str, Any] = {"at": 0.0, "value": None}


@dataclass(frozen=True)
class CompanyInfo:
    legal_name: str
    operating_name: str
    brand_name: str
    lending_type: Optional[str]
    logo_ref: Optional[str]
    favicon_ref: Optional[str]
    contacts: list[dict] = field(default_factory=list)

    def primary(self, kind: str) -> Optional[str]:
        """First contact value of ``kind`` flagged is_primary, else first of kind."""
        first = None
        for c in self.contacts:
            if not isinstance(c, dict) or c.get("kind") != kind:
                continue
            if c.get("is_primary"):
                return c.get("value")
            if first is None:
                first = c.get("value")
        return first


def _default_contacts() -> list[dict]:
    from app.core.config import settings

    phone = getattr(settings, "COMPANY_PHONE", "") or "1 (877) 729-1973"
    email = getattr(settings, "SUPPORT_EMAIL", "support@payspyre.com")
    return [
        {"kind": "phone", "label": "Main", "value": phone, "is_primary": True},
        {"kind": "email", "label": "Support", "value": email, "is_primary": True},
        {"kind": "website", "label": "Main", "value": "https://www.payspyre.com",
         "is_primary": True},
        {"kind": "address", "label": "Head office",
         "value": "2033 Gordon Dr #100, Kelowna, BC, V1Y 3J2", "is_primary": True},
    ]


def get_defaults() -> CompanyInfo:
    """The shipped defaults (video-07 §2.3 values; env settings for phone/email)."""
    return CompanyInfo(
        legal_name="PaySpyre Financial Inc.",
        operating_name="PaySpyre Financial",
        brand_name="PaySpyre",
        lending_type="Retail Financing - Installment",
        logo_ref=None,
        favicon_ref=None,
        contacts=_default_contacts(),
    )


def from_row(row: Any) -> CompanyInfo:
    """Build a :class:`CompanyInfo` from a ``platform_company_info`` row,
    filling any blank field from the defaults (partial rows degrade safely)."""
    d = get_defaults()
    contacts = list(row.contacts or []) or d.contacts
    return CompanyInfo(
        legal_name=row.legal_name or d.legal_name,
        operating_name=row.operating_name or d.operating_name,
        brand_name=row.brand_name or d.brand_name,
        lending_type=row.lending_type or d.lending_type,
        logo_ref=row.logo_ref,
        favicon_ref=row.favicon_ref,
        contacts=contacts,
    )


def get_company_info(db: Any) -> CompanyInfo:
    """The effective company info: DB row 1 if present, else the defaults."""
    from app.models.platform.company_info import PlatformCompanyInfo

    row = db.get(PlatformCompanyInfo, 1) if db is not None else None
    return from_row(row) if row is not None else get_defaults()


def to_context(info: CompanyInfo) -> dict:
    """Merge-field dict for template rendering (notifications + documents)."""
    return {
        "company_name": info.legal_name,          # historical key = legal name
        "company_legal_name": info.legal_name,
        "company_operating_name": info.operating_name,
        "company_brand_name": info.brand_name,
        "company_phone": info.primary("phone") or "",
        "support_email": info.primary("email") or "",
        "website_url": info.primary("website") or "",
        "company_address": info.primary("address") or "",
    }


def _context_if_customized(db: Any) -> dict:
    """The merge-field context IF an admin has saved company info; else ``{}``.

    Returning ``{}`` on the default path keeps ``notification_render``'s static
    values (and env-settings monkeypatching in tests) authoritative until
    someone actually edits company info."""
    from app.models.platform.company_info import PlatformCompanyInfo

    row = db.get(PlatformCompanyInfo, 1)
    if row is None:
        return {}
    return to_context(from_row(row))


def get_company_context(db: Any = None) -> dict:
    """Fallible, cached merge-field OVERRIDE dict for template rendering.

    Empty when no admin edit exists (shipped defaults stay authoritative).
    With a ``db`` session: resolves directly. Without one: opens a short-lived
    session, caching the result for :data:`_CACHE_TTL_SECONDS`; any failure
    (no DB in unit tests, outage) silently degrades to ``{}`` — a config read
    must never break a send.
    """
    if db is not None:
        try:
            return _context_if_customized(db)
        except Exception:  # noqa: BLE001 — rendering must not break on config reads
            logger.warning("company_info_read_failed_falling_back", exc_info=True)
            return {}

    now = time.monotonic()
    if _cache["value"] is not None and now - _cache["at"] < _CACHE_TTL_SECONDS:
        return dict(_cache["value"])
    try:
        from app.db.base import SessionLocal

        session = SessionLocal()
        try:
            ctx = _context_if_customized(session)
        finally:
            session.close()
    except Exception:  # noqa: BLE001 — degrade to no-override, never raise
        ctx = {}
    _cache["value"] = dict(ctx)
    _cache["at"] = now
    return ctx


def invalidate_cache() -> None:
    """Drop the no-db cache (called after an admin edit so renders see it)."""
    _cache["value"] = None
    _cache["at"] = 0.0
