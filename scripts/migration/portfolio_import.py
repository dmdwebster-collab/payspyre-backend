"""Import a loan portfolio workbook into PaySpyre — CLI.

The practical path for a real cutover: the workbook stays on disk, never crosses
an HTTP boundary, and the whole run is one transaction.

    # 1. What's in the file, and does it tie to itself? No DB writes at all.
    python scripts/migration/portfolio_import.py BOOK.xlsx --inspect

    # 2. Full rehearsal: every write executed, then ROLLED BACK. The
    #    reconciliation printed is the one the real run produces.
    DATABASE_URL=... python scripts/migration/portfolio_import.py BOOK.xlsx \\
        --dry-run --placeholder-emails --placeholder-phones

    # 3. For real.
    DATABASE_URL=... python scripts/migration/portfolio_import.py BOOK.xlsx \\
        --execute --placeholder-emails --placeholder-phones

    # 4. Tie an already-imported book back to the source (read-only).
    DATABASE_URL=... python scripts/migration/portfolio_import.py BOOK.xlsx --reconcile

PLACEHOLDER CONTACT DETAILS are OFF unless asked for, and their shape is stated
on the command line (``--placeholder-email-domain`` / ``--placeholder-area-code``)
so it is visible in the shell history and echoed on the report. Defaults are the
unroutable testing shape: a ``.kom`` e-mail domain and area code 555, so an
imported borrower can never receive a real e-mail or SMS.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.services.migration import portfolio_import as importer  # noqa: E402
from app.services.migration.borrower_completion import PlaceholderPolicy  # noqa: E402
from app.services.migration.portfolio_profile import PROFILES, get_profile  # noqa: E402
from app.services.migration.portfolio_workbook import read_file  # noqa: E402

#: The one-time testing shape: unroutable everywhere.
DEFAULT_PLACEHOLDER_DOMAIN = "payspyre-import.kom"
DEFAULT_PLACEHOLDER_AREA_CODE = "555"


def _session(url: str | None):
    url = url or os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit("set DATABASE_URL (or pass --database-url) for a DB run")
    engine = create_engine(url)
    return sessionmaker(bind=engine)()


def _print(payload) -> None:
    print(json.dumps(payload, indent=2, default=str))


def main() -> int:
    ap = argparse.ArgumentParser(description="PaySpyre portfolio import")
    ap.add_argument("workbook", help="path to the source .xlsx loan book")
    ap.add_argument(
        "--profile",
        default=None,
        help=f"source mapping profile (default: the built-in; known: {', '.join(sorted(PROFILES))})",
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--inspect", action="store_true",
                      help="read + self-reconcile the file only; never opens a DB connection")
    mode.add_argument("--dry-run", action="store_true",
                      help="perform every write, report, then ROLL BACK (default DB mode)")
    mode.add_argument("--execute", action="store_true", help="commit the import")
    mode.add_argument("--reconcile", action="store_true",
                      help="tie an already-imported book back to the file (read-only)")

    ap.add_argument("--database-url", default=None)
    ap.add_argument("--tolerance-cents", type=int, default=1,
                    help="cent tolerance when tying derived figures to stated ones")
    ap.add_argument("--no-create-vendors", action="store_true",
                    help="do not create vendors the book references but PaySpyre lacks")
    ap.add_argument("--no-forward-schedule", action="store_true",
                    help="do not build forward amortization schedules for active loans")

    ap.add_argument("--placeholder-emails", action="store_true",
                    help="synthesize a placeholder e-mail where the source has none")
    ap.add_argument("--placeholder-phones", action="store_true",
                    help="synthesize a placeholder phone where the source has none")
    ap.add_argument("--placeholder-addresses", action="store_true",
                    help="synthesize a placeholder address where the source has none")
    ap.add_argument("--placeholder-email-domain", default=DEFAULT_PLACEHOLDER_DOMAIN,
                    help=f"domain for synthetic e-mails (default {DEFAULT_PLACEHOLDER_DOMAIN})")
    ap.add_argument("--placeholder-area-code", default=DEFAULT_PLACEHOLDER_AREA_CODE,
                    help=f"area code for synthetic phones (default {DEFAULT_PLACEHOLDER_AREA_CODE})")
    args = ap.parse_args()

    profile = get_profile(args.profile)
    read = read_file(args.workbook, profile)

    wants_placeholders = (
        args.placeholder_emails or args.placeholder_phones or args.placeholder_addresses
    )
    policy = PlaceholderPolicy(
        enabled=wants_placeholders,
        email_domain=args.placeholder_email_domain,
        phone_area_code=args.placeholder_area_code,
        fill_email=args.placeholder_emails,
        fill_phone=args.placeholder_phones,
        fill_address=args.placeholder_addresses,
    )
    problems = policy.validate()
    if problems:
        raise SystemExit("; ".join(problems))

    options = importer.ImportOptions(
        profile_name=profile.name,
        placeholders=policy,
        create_missing_vendors=not args.no_create_vendors,
        build_forward_schedule=not args.no_forward_schedule,
        tolerance_cents=args.tolerance_cents,
    )

    if args.inspect:
        _print(importer.preview(read, options).as_dict())
        return 0

    db = _session(args.database_url)
    try:
        if args.reconcile:
            _print(
                importer.reconcile_existing(
                    db, read, tolerance_cents=args.tolerance_cents
                ).as_dict()
            )
            return 0
        if args.execute:
            result = importer.apply_import(db, read, options, commit=True)
            _print(result.as_dict())
            return 0
        # Default: a full rehearsal that rolls back.
        try:
            result = importer.apply_import(db, read, options, commit=False)
            _print(result.as_dict())
        finally:
            db.rollback()
        print("\nDRY RUN — everything above was rolled back. Re-run with --execute to commit.",
              file=sys.stderr)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
