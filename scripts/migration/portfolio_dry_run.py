"""Dry-run the portfolio importer against a source export. NO DB writes.

Reads the "Accounts" sheet of a source export, maps every loan, and prints a
migration preview: counts by status, what would be imported vs skipped, validation
warnings, and portfolio totals. Loans are referenced by Acct# only (no PII).

Usage:  python scripts/migration/portfolio_dry_run.py "/path/to/export.xlsx"
"""
import sys

import openpyxl

from app.services.migration.portfolio_accounts import build_report

ACCOUNTS_FIRST_DATA_ROW = 4  # header is row 3


def main(path: str) -> None:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows = wb["Accounts"].iter_rows(min_row=ACCOUNTS_FIRST_DATA_ROW, values_only=True)
    mapped, rep = build_report(rows)

    print("=" * 64)
    print("TURNKEY -> PAYSPYRE  MIGRATION DRY-RUN  (no data written)")
    print("=" * 64)
    print(f"Loan rows parsed:        {rep.total_rows}")
    print(f"Importable:              {rep.importable}")
    print(f"Skipped (unmapped):      {len(rep.skipped_unmapped)}"
          + (f"  -> accts {rep.skipped_unmapped[:10]}" if rep.skipped_unmapped else ""))
    print("\nBy source status:")
    for k, v in sorted(rep.by_source_status.items()):
        print(f"   {k:<22} {v}")
    print("\nWould create as PaySpyre status:")
    for k, v in sorted(rep.by_payspyre_status.items()):
        print(f"   {k:<22} {v}")
    print("\nPortfolio totals (importable loans):")
    print(f"   original principal:   ${rep.total_principal_cents/100:,.2f}")
    print(f"   current outstanding:  ${rep.total_outstanding_cents/100:,.2f}")

    active = [m for m in mapped if m.status == "active"]
    with_sched = [m for m in active if m.forward_schedule]
    print(f"\nActive loans:            {len(active)}  "
          f"({len(with_sched)} got a forward schedule, "
          f"{len(active)-len(with_sched)} could not — see warnings)")

    print(f"\nLoans with warnings:     {len(rep.loans_with_warnings)}")
    import collections
    wc = collections.Counter(w for _, ws in rep.loans_with_warnings for w in ws)
    for w, n in wc.most_common():
        print(f"   [{n:>3}] {w}")
    if rep.loans_with_warnings:
        print("\n   first few:")
        for acct, ws in rep.loans_with_warnings[:6]:
            print(f"     acct {acct}: {ws}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    main(sys.argv[1])
