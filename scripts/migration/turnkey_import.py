"""Run the Turnkey -> PaySpyre loan import end-to-end.

DRY RUN by default (no writes). With --execute it persists the mapped loans into the
target database via the idempotent persist step. Re-running is safe (dedups on the
legacy account number).

SAFETY: real borrower PII must never land on the dev-tools-enabled staging box, so the
script refuses a target URL that looks like staging unless --force is given.

Examples:
  # preview only
  python scripts/migration/turnkey_import.py export.xlsx
  # actually import into a clean target DB
  python scripts/migration/turnkey_import.py export.xlsx --execute \\
      --database-url postgresql+psycopg2://user:pw@host:5432/payspyre
"""
import argparse
import sys

import openpyxl
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.services.migration.turnkey import build_report
from app.services.migration.turnkey_persist import persist_loans

ACCOUNTS_FIRST_DATA_ROW = 4


def main() -> None:
    ap = argparse.ArgumentParser(description="Turnkey -> PaySpyre loan import")
    ap.add_argument("excel", help="path to the Turnkey export .xlsx")
    ap.add_argument("--execute", action="store_true", help="actually write (default: dry run)")
    ap.add_argument("--database-url", help="target DB (required with --execute)")
    ap.add_argument("--force", action="store_true", help="override the staging-URL safety guard")
    args = ap.parse_args()

    wb = openpyxl.load_workbook(args.excel, read_only=True, data_only=True)
    rows = wb["Accounts"].iter_rows(min_row=ACCOUNTS_FIRST_DATA_ROW, values_only=True)
    mapped, rep = build_report(rows)

    print(f"Parsed {rep.total_rows} loans -> {rep.importable} importable "
          f"({len(rep.skipped_unmapped)} unmapped, skipped).")
    print(f"  by PaySpyre status: {rep.by_paspyre_status}")
    print(f"  original principal ${rep.total_principal_cents/100:,.2f} | "
          f"current outstanding ${rep.total_outstanding_cents/100:,.2f}")
    if rep.skipped_unmapped:
        print(f"  WARNING — unmapped statuses left out: accts {rep.skipped_unmapped[:10]}")
    if rep.loans_with_warnings:
        print(f"  {len(rep.loans_with_warnings)} loan(s) have validation warnings (see dry-run report).")

    if not args.execute:
        print("\nDRY RUN — nothing written. Add --execute --database-url ... to persist.")
        return

    if not args.database_url:
        sys.exit("error: --database-url is required with --execute")
    if "staging" in args.database_url.lower() and not args.force:
        sys.exit(
            "REFUSING: target looks like staging. Real borrower PII must not land on the "
            "dev-tools-enabled staging environment. Use --force only if you are certain."
        )

    engine = create_engine(args.database_url)
    db = sessionmaker(bind=engine)()
    try:
        res = persist_loans(db, mapped, commit=True)
        print(f"\nPERSISTED: created {res.created}, skipped (already imported) {res.skipped_existing}.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
