"""Provider roster service — find/create providers under a vendor.

Providers are owned by the vendor (migration 083). Everything that needs a
provider — the importer, the Originations dropdown, vendor intake — goes through
here so the roster has exactly one writer and one normalization rule.

NORMALIZATION: the DB enforces case-insensitive uniqueness per vendor; this
module case-folds and collapses whitespace before matching, so " dr.  jane roe "
resolves to the existing "Dr. Jane Roe" instead of tripping the index.
"""
from __future__ import annotations

from typing import Iterable, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.platform.provider import (
    PROVIDER_SOURCE_IMPORT,
    PROVIDER_SOURCE_MANUAL,
    PlatformProvider,
)


def normalize_name(raw: Optional[str]) -> Optional[str]:
    """Display form: whitespace collapsed, nothing else changed."""
    if raw is None:
        return None
    cleaned = " ".join(str(raw).split())
    return cleaned or None


def match_key(raw: Optional[str]) -> Optional[str]:
    """The key uniqueness is decided on (matches the DB's ``lower(name)`` index)."""
    name = normalize_name(raw)
    return name.casefold() if name else None


def roster(db: Session, vendor_id: UUID, *, active_only: bool = True) -> list[PlatformProvider]:
    q = db.query(PlatformProvider).filter(PlatformProvider.vendor_id == vendor_id)
    if active_only:
        q = q.filter(PlatformProvider.is_active.is_(True))
    return q.order_by(PlatformProvider.name).all()


def find(db: Session, vendor_id: UUID, name: Optional[str]) -> Optional[PlatformProvider]:
    key = match_key(name)
    if key is None:
        return None
    for p in db.query(PlatformProvider).filter(PlatformProvider.vendor_id == vendor_id).all():
        if match_key(p.name) == key:
            return p
    return None


def find_or_create(
    db: Session,
    vendor_id: UUID,
    name: Optional[str],
    *,
    source: str = PROVIDER_SOURCE_MANUAL,
    external_code: Optional[str] = None,
    flush: bool = True,
) -> Optional[PlatformProvider]:
    """Resolve a provider name to a row under ``vendor_id``, creating it if new.

    Returns None for an empty name (the source did not state a provider) — the
    caller keeps whatever free text it has and leaves ``provider_id`` NULL.
    """
    display = normalize_name(name)
    if display is None or vendor_id is None:
        return None
    existing = find(db, vendor_id, display)
    if existing is not None:
        if external_code and not existing.external_code:
            existing.external_code = external_code
        return existing
    provider = PlatformProvider(
        vendor_id=vendor_id,
        name=display,
        external_code=external_code,
        source=source,
        is_active=True,
    )
    db.add(provider)
    if flush:
        db.flush()
    return provider


def seed_roster(
    db: Session,
    vendor_id: UUID,
    names: Iterable[Optional[str]],
    *,
    source: str = PROVIDER_SOURCE_IMPORT,
) -> tuple[int, int]:
    """Ensure every name exists under the vendor. Returns (created, existing).

    Idempotent: re-seeding the same roster creates nothing.
    """
    created = existing = 0
    seen: set[str] = set()
    for raw in names:
        key = match_key(raw)
        if key is None or key in seen:
            continue
        seen.add(key)
        before = find(db, vendor_id, raw)
        find_or_create(db, vendor_id, raw, source=source)
        if before is None:
            created += 1
        else:
            existing += 1
    return created, existing
