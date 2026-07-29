"""Admin portfolio import — upload a whole loan book, preview it, apply it.

Complements the per-entity CSV cutover import (``admin_import.py``). Where that
one takes PaySpyre-shaped CSVs one entity at a time, this takes a source
system's own WORKBOOK — accounts, transaction history, vendors and providers in
one file — and maps it through a declarative PROFILE
(``app/services/migration/portfolio_profile.py``). A source this repo has never
seen is supported by POSTing its profile, not by shipping code.

Flow:
    GET  /admin/import/portfolio/profiles      what shapes are known
    POST /admin/import/portfolio/preview       upload -> what's in it + whether
                                               it reconciles. Writes NOTHING.
    POST /admin/import/portfolio/apply         upload -> land it, then reconcile
                                               what landed. ``dry_run=true``
                                               performs every write inside the
                                               transaction and rolls back, so
                                               the report is the real one.

PLACEHOLDER CONTACT DETAILS are opt-in per request and never defaulted: the
caller states the e-mail domain and phone area code, and the choice is echoed
back on the report. See ``borrower_completion.PlaceholderPolicy``.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.core.auth import require_roles
from app.db.base import get_db
from app.models.platform.event import PlatformEvent
from app.services.migration import portfolio_import as importer
from app.services.migration.borrower_completion import PlaceholderPolicy
from app.services.migration.portfolio_profile import PROFILES, get_profile
from app.services.migration.portfolio_workbook import (
    ProfileMismatch,
    read_workbook,
    ExcelWorkbook,
)

router = APIRouter()

# A real book (hundreds of accounts, thousands of transactions) is a few MB of
# .xlsx; 50 MB is generous headroom while still refusing an absurd upload. For a
# file larger than this, use scripts/migration/portfolio_import.py, which streams
# from disk and never crosses the HTTP boundary.
_MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def _actor(user) -> str:
    return str(getattr(user, "id", "") or "unknown")


def _policy(
    generate_placeholders: bool,
    placeholder_email_domain: Optional[str],
    placeholder_phone_area_code: Optional[str],
) -> PlaceholderPolicy:
    if not generate_placeholders:
        return PlaceholderPolicy(enabled=False)
    policy = PlaceholderPolicy(
        enabled=True,
        email_domain=placeholder_email_domain or "portfolio-import.invalid",
        phone_area_code=placeholder_phone_area_code or "555",
    )
    problems = policy.validate()
    if problems:
        raise HTTPException(status_code=422, detail="; ".join(problems))
    return policy


async def _read_upload(file: UploadFile):
    filename = (file.filename or "").lower()
    if not filename.endswith((".xlsx", ".xlsm")):
        raise HTTPException(
            status_code=422,
            detail="Portfolio import expects an .xlsx workbook (per-entity CSVs go to /admin/import/batches).",
        )
    raw = await file.read()
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail="Workbook too large (50 MB max) — run scripts/migration/portfolio_import.py instead.",
        )
    return raw


def _read(raw: bytes, profile_name: Optional[str]):
    try:
        profile = get_profile(profile_name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    try:
        wb = ExcelWorkbook(raw)
    except Exception:
        # A truncated / non-.xlsx payload is the caller's problem, not a 500.
        raise HTTPException(
            status_code=422, detail="Upload is not a readable .xlsx workbook."
        ) from None
    try:
        return read_workbook(wb, profile)
    except ProfileMismatch as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Workbook does not match profile {profile.name!r}: {exc}",
        ) from None
    finally:
        wb.close()


@router.get("/portfolio/profiles")
def list_profiles(_user=Depends(require_roles("admin"))):
    """The declarative source shapes this deployment knows how to read."""
    return {
        "profiles": [
            {
                "name": p.name,
                "description": p.description,
                "money_unit": p.money_unit,
                "rate_unit": p.rate_unit,
                "name_format": p.name_format,
                "sheets": {
                    "accounts": p.accounts.sheet if p.accounts else None,
                    "transactions": p.transactions.sheet if p.transactions else None,
                    "vendors": p.vendors.sheet if p.vendors else None,
                },
                "status_map": p.status_map,
                "transaction_types": sorted(p.transaction_types),
            }
            for p in PROFILES.values()
        ]
    }


@router.get("/portfolio/profiles/{profile_name}")
def get_profile_detail(profile_name: str, _user=Depends(require_roles("admin"))):
    """The full column mapping, as JSON. Copy it, change the bindings, and POST
    it back to map a source system this deployment has never seen."""
    try:
        return get_profile(profile_name).to_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


@router.post("/portfolio/preview")
async def preview_portfolio(
    file: UploadFile = File(...),
    profile_name: Optional[str] = Form(default=None),
    tolerance_cents: int = Form(default=1),
    _db: Session = Depends(get_db),
    _user=Depends(require_roles("admin")),
):
    """What the workbook contains, and whether it reconciles against itself.
    Touches no table."""
    raw = await _read_upload(file)
    read = _read(raw, profile_name)
    options = importer.ImportOptions(
        profile_name=profile_name, tolerance_cents=tolerance_cents
    )
    return importer.preview(read, options).as_dict()


@router.post("/portfolio/apply")
async def apply_portfolio(
    file: UploadFile = File(...),
    profile_name: Optional[str] = Form(default=None),
    dry_run: bool = Form(default=True),
    create_missing_vendors: bool = Form(default=True),
    build_forward_schedule: bool = Form(default=True),
    tolerance_cents: int = Form(default=1),
    generate_placeholders: bool = Form(default=False),
    placeholder_email_domain: Optional[str] = Form(default=None),
    placeholder_phone_area_code: Optional[str] = Form(default=None),
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    """Land the workbook.

    ``dry_run=true`` (the default) performs every write inside the transaction
    and then rolls back — so the reconciliation it returns is the one the real
    run produces, not an estimate of it.
    """
    # Validate the RUN's parameters before touching the upload: an unsafe
    # placeholder shape should be rejected without parsing a 50 MB workbook.
    policy = _policy(
        generate_placeholders, placeholder_email_domain, placeholder_phone_area_code
    )
    raw = await _read_upload(file)
    read = _read(raw, profile_name)
    options = importer.ImportOptions(
        profile_name=profile_name,
        placeholders=policy,
        create_missing_vendors=create_missing_vendors,
        build_forward_schedule=build_forward_schedule,
        tolerance_cents=tolerance_cents,
    )
    try:
        if dry_run:
            try:
                result = importer.apply_import(db, read, options, commit=False)
            finally:
                db.rollback()
            return result.as_dict()
        result = importer.apply_import(db, read, options, commit=True)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None

    payload = result.as_dict()
    db.add(
        PlatformEvent(
            event_type="portfolio_imported",
            actor=_actor(user),
            payload={
                "filename": file.filename,
                "profile": options.profile_name,
                "loans_created": result.loans_created,
                "ledger_rows_created": result.ledger_rows_created,
                "borrowers_created": result.borrowers_created,
                "reconciliation_ok": payload["reconciliation"]["persisted"].get("ok"),
            },
        )
    )
    db.commit()
    return payload


@router.post("/portfolio/reconcile")
async def reconcile_portfolio(
    file: UploadFile = File(...),
    profile_name: Optional[str] = Form(default=None),
    tolerance_cents: int = Form(default=1),
    db: Session = Depends(get_db),
    _user=Depends(require_roles("admin")),
):
    """Tie an ALREADY-IMPORTED book back to the source workbook.

    Reports, per account, every measure where PaySpyre and the source disagree.
    Read-only: a mismatch is surfaced, never corrected.
    """
    raw = await _read_upload(file)
    read = _read(raw, profile_name)
    return importer.reconcile_existing(
        db, read, tolerance_cents=tolerance_cents
    ).as_dict()
