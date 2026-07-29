"""A SYNTHETIC loan-book workbook shaped like a real servicing export.

Entirely invented: two vendors, four borrowers, five accounts and a transaction
history that exercises originations, autopay, a customer payment, an NSF return,
a non-cash adjustment, a payoff and a voided loan. No real portfolio data is in
this repository, and none should ever be added.

The sheets reproduce the awkward parts of a genuine export on purpose, because
those are what the importer has to survive:

* headers on row 3 (accounts) / row 14 (transactions), with junk above them;
* a TOTALS row at the bottom carrying a row count where an identifier belongs;
* a TRANSPOSED vendor sheet — attribute labels down column A, one vendor per
  column, with the provider roster in a labelled row block;
* money as dollars-with-float-noise, rates as decimal fractions,
  ``Last, First`` names, and a "Closed" sentinel in a date column.
"""
from __future__ import annotations

from datetime import datetime

from app.services.migration.portfolio_workbook import InMemoryWorkbook

ACCOUNT_HEADER = [
    "#", "Vendor", "Provider", "Acct#", "Status", "Sub-Status", "Days Past Due",
    "Name", "Co-Borrower", "Sales Value", "Insurance", "Downpayment",
    "Fees Renewal", "Interest Renewal", "Principal Renewal", "Amount Financed",
    "Term Months", "Rate %", "Regular Payment", "Payment Frequency",
    "Cost of Borrowing", "First Pmt Date", "Final Pmt Date", "DinY",
    "Plat", "PdCode", "Override", "Ven. Share Fees", "Ven. Share Interest",
    "Ven. Share Principal", "Origination", "Org. Type", "New Advance",
    "Non-Pmt", "Pmt", "Total", "Payment Amount", "Fees Paid", "Interest Paid",
    "Principal Paid", "Fees Balance", "Interest Balance", "Principal Balance",
    "Total Owed", "Next Due Date", "Last Tansaction Date",
    "Last Transaction Type", "Close Date", "Close Type", "NSF / RETURN",
    "Deferment",
]

TRANSACTION_HEADER = [
    "L", "T#", "Vendor", "Provider", "Plat.", "PdCode", "Override", "#",
    "Acct #", "Rate", "DinY", "Name", "Date", "Payment", "Fees Charged",
    "Fees Paid", "Fee Balance", "Accrued Interest", "Interest Due",
    "Interest Paid", "Interest Balance", "Principal Paid", "Principal Balance",
    "Total Owed", "Type", "VP", "Comments",
]


def _account(**kw) -> list:
    """One Accounts row, positional per ACCOUNT_HEADER, with sane blanks."""
    row: list = [None] * len(ACCOUNT_HEADER)
    index = {name: i for i, name in enumerate(ACCOUNT_HEADER)}
    for key, value in kw.items():
        row[index[key]] = value
    return row


def _txn(**kw) -> list:
    row: list = [None] * len(TRANSACTION_HEADER)
    index = {name: i for i, name in enumerate(TRANSACTION_HEADER)}
    for key, value in kw.items():
        row[index[key]] = value
    return row


def _d(y: int, m: int, day: int) -> datetime:
    return datetime(y, m, day)


# --- Accounts ---------------------------------------------------------------
# A1  OPEN/ACTIVE, monthly, mid-life. Has an NSF return in its history.
# A2  CLOSED/PAID, bi-weekly, paid off.
# A3  OPEN/ACTIVE at a SECOND vendor, with a non-cash fee adjustment.
# A4  VOIDED — must not be imported at all.
# A5  CLOSED/PAID whose stated balances CONTRADICT its transactions — the
#     reconciliation exception the importer must REPORT, not fix.

ACCOUNTS_ROWS = [
    _account(
        **{"#": 1, "Vendor": "BC1000", "Provider": "Dr. Ada Lovelace", "Acct#": 5001,
           "Status": "OPEN", "Sub-Status": "ACTIVE", "Days Past Due": 0,
           "Name": "Ramsey, Nora", "Sales Value": 5000, "Downpayment": 500,
           "Amount Financed": 4500, "Term Months": 24, "Rate %": 0.0999,
           "Regular Payment": 207.36, "Payment Frequency": "Monthly",
           "Cost of Borrowing": 476.64, "First Pmt Date": _d(2025, 2, 1),
           "Final Pmt Date": _d(2027, 1, 1), "DinY": 365,
           "Origination": _d(2025, 1, 1), "Org. Type": "LOAN-NEW",
           "Payment Amount": 414.72, "Fees Paid": 0, "Interest Paid": 74.25,
           "Principal Paid": 340.47,
           # The NSF fee is charged and still outstanding — the account row says
           # so, and the transaction history agrees. This account TIES.
           "Fees Balance": 45, "Interest Balance": 0,
           "Principal Balance": 4159.53, "Total Owed": 4204.53,
           "Next Due Date": _d(2025, 5, 1), "NSF / RETURN": 1}
    ),
    _account(
        **{"#": 2, "Vendor": "BC1000", "Provider": "Dr. Grace Hopper", "Acct#": 5002,
           "Status": "CLOSED", "Sub-Status": "PAID", "Days Past Due": 0,
           "Name": "Okafor, Daniel", "Co-Borrower": "Okafor, Ruth",
           "Sales Value": 2000, "Amount Financed": 2000, "Term Months": 12,
           "Rate %": 0.0599, "Regular Payment": 172.13,
           "Payment Frequency": "Bi-Weekly", "Cost of Borrowing": 65.56,
           "First Pmt Date": _d(2024, 2, 1), "Final Pmt Date": _d(2025, 1, 1),
           "DinY": 360, "Origination": _d(2024, 1, 1), "Org. Type": "LOAN-NEW",
           "Payment Amount": 2010.0, "Fees Paid": 0, "Interest Paid": 10.0,
           "Principal Paid": 2000.0, "Fees Balance": 0, "Interest Balance": 0,
           "Principal Balance": 0, "Total Owed": 0, "Next Due Date": "Closed",
           "Close Date": _d(2025, 1, 1), "Close Type": "PdOUT-CUST"}
    ),
    _account(
        **{"#": 3, "Vendor": "AB2000", "Provider": "Dr. Alan Turing", "Acct#": 5003,
           "Status": "OPEN", "Sub-Status": "ACTIVE", "Days Past Due": 34,
           "Name": "Ramsey, Nora", "Amount Financed": 1000, "Term Months": 12,
           "Rate %": 0.1499, "Regular Payment": 90.26,
           "Payment Frequency": "Monthly", "Cost of Borrowing": 83.12,
           "First Pmt Date": _d(2025, 3, 1), "Final Pmt Date": _d(2026, 2, 1),
           "DinY": 365, "Origination": _d(2025, 2, 1), "Org. Type": "LOAN-NEW",
           "Payment Amount": 115.26, "Fees Paid": 25.0, "Interest Paid": 12.5,
           "Principal Paid": 77.76, "Fees Balance": 0, "Interest Balance": 0,
           "Principal Balance": 922.24, "Total Owed": 922.24,
           "Next Due Date": _d(2025, 5, 1)}
    ),
    _account(
        **{"#": 4, "Vendor": "BC1000", "Provider": "Dr. Ada Lovelace", "Acct#": 5004,
           "Status": "VOIDED", "Sub-Status": "VOIDED", "Name": "Doe, Jane",
           "Amount Financed": 900, "Term Months": 12, "Rate %": 0.0999,
           "Payment Frequency": "Monthly", "Origination": _d(2025, 3, 1),
           "Org. Type": "LOAN-VOIDED", "Payment Amount": 0, "Fees Paid": 0,
           "Interest Paid": 0, "Principal Paid": 0, "Fees Balance": 0,
           "Interest Balance": 0, "Principal Balance": 0, "Total Owed": 0,
           "Next Due Date": "Closed", "Close Date": _d(2025, 3, 2),
           "Close Type": "VOIDED"}
    ),
    _account(
        # STATES a zero balance, but its transactions leave $250 outstanding.
        **{"#": 5, "Vendor": "BC1000", "Provider": "Dr. Grace Hopper", "Acct#": 5005,
           "Status": "CLOSED", "Sub-Status": "PAID", "Name": "Bhatt, Priya",
           "Amount Financed": 1000, "Term Months": 12, "Rate %": 0.0599,
           "Regular Payment": 86.07, "Payment Frequency": "Monthly",
           "First Pmt Date": _d(2024, 6, 1), "Final Pmt Date": _d(2025, 5, 1),
           "DinY": 365, "Origination": _d(2024, 5, 1), "Org. Type": "LOAN-NEW",
           "Payment Amount": 750.0, "Fees Paid": 0, "Interest Paid": 0,
           "Principal Paid": 750.0, "Fees Balance": 0, "Interest Balance": 0,
           "Principal Balance": 0, "Total Owed": 0, "Next Due Date": "Closed",
           "Close Date": _d(2025, 5, 1), "Close Type": "PdOUT-CUST"}
    ),
    # The TOTALS row a real export appends: a row count where the account number
    # belongs, and no status. The row_guard must drop it.
    _account(**{"Acct#": 5, "Payment Amount": 3290.0}),
]


# --- Transactions -----------------------------------------------------------
# Note the float noise on 340.47 -> the importer must land 34047 cents exactly.
TRANSACTION_ROWS = [
    # 5001: origination, two autopays, an NSF that returns the second one, a re-pay
    _txn(**{"T#": 1, "Vendor": "BC1000", "Acct #": 5001, "Name": "Ramsey, Nora",
            "Date": _d(2025, 1, 1), "Fee Balance": 0, "Interest Due": 0,
            "Interest Balance": 0, "Principal Balance": 4500, "Total Owed": 4500,
            "Type": "LOAN-NEW", "VP": "O", "Comments": "ORIGINATION: 9.99% - 24 mths"}),
    _txn(**{"T#": 2, "Vendor": "BC1000", "Acct #": 5001, "Name": "Ramsey, Nora",
            "Date": _d(2025, 2, 1), "Payment": 207.36, "Fees Paid": 0,
            "Fee Balance": 0, "Interest Due": 37.13, "Interest Paid": 37.13,
            "Interest Balance": 0, "Principal Paid": 170.23,
            "Principal Balance": 4329.77, "Total Owed": 4329.77,
            "Type": "PMT-AUTOPAY", "VP": "P", "Comments": "Autopayment Received"}),
    _txn(**{"T#": 3, "Vendor": "BC1000", "Acct #": 5001, "Name": "Ramsey, Nora",
            "Date": _d(2025, 3, 1), "Payment": 207.36, "Fees Paid": 0,
            "Fee Balance": 0, "Interest Due": 37.12, "Interest Paid": 37.12,
            "Interest Balance": 0, "Principal Paid": 170.24,
            "Principal Balance": 4159.5300000000002, "Total Owed": 4159.53,
            "Type": "PMT-AUTOPAY", "VP": "P", "Comments": "Autopayment Received"}),
    _txn(**{"T#": 4, "Vendor": "BC1000", "Acct #": 5001, "Name": "Ramsey, Nora",
            "Date": _d(2025, 3, 15), "Payment": -207.36, "Fees Charged": 45,
            "Fees Paid": 0, "Fee Balance": 45, "Interest Paid": -37.12,
            "Interest Balance": 37.12, "Principal Paid": -170.24,
            "Principal Balance": 4329.77, "Total Owed": 4411.89,
            "Type": "NSF/RETURN", "VP": "N", "Comments": "RETURNED: NSF 03-15-2025"}),
    _txn(**{"T#": 5, "Vendor": "BC1000", "Acct #": 5001, "Name": "Ramsey, Nora",
            "Date": _d(2025, 4, 1), "Payment": 207.36, "Fees Paid": 0,
            "Fee Balance": 45, "Interest Due": 37.12, "Interest Paid": 37.12,
            "Interest Balance": 0, "Principal Paid": 170.24,
            "Principal Balance": 4159.53, "Total Owed": 4204.53,
            "Type": "PMT-CUST", "VP": "V", "Comments": "Customer payment"}),
    # 5002: origination + payoff
    _txn(**{"T#": 6, "Vendor": "BC1000", "Acct #": 5002, "Name": "Okafor, Daniel",
            "Date": _d(2024, 1, 1), "Fee Balance": 0, "Interest Due": 0,
            "Interest Balance": 0, "Principal Balance": 2000, "Total Owed": 2000,
            "Type": "LOAN-NEW", "VP": "O", "Comments": "ORIGINATION: 5.99% - 12 mths"}),
    _txn(**{"T#": 7, "Vendor": "BC1000", "Acct #": 5002, "Name": "Okafor, Daniel",
            "Date": _d(2025, 1, 1), "Payment": 2010.0, "Fees Paid": 0,
            "Fee Balance": 0, "Interest Due": 10.0, "Interest Paid": 10.0,
            "Interest Balance": 0, "Principal Paid": 2000.0,
            "Principal Balance": 0, "Total Owed": 0, "Type": "PdOUT-CUST",
            "VP": "W", "Comments": "Paid out in full"}),
    # 5003: origination, a fee adjustment (non-cash), one autopay
    _txn(**{"T#": 8, "Vendor": "AB2000", "Acct #": 5003, "Name": "Ramsey, Nora",
            "Date": _d(2025, 2, 1), "Fee Balance": 0, "Interest Due": 0,
            "Interest Balance": 0, "Principal Balance": 1000, "Total Owed": 1000,
            "Type": "LOAN-NEW", "VP": "O", "Comments": "ORIGINATION: 14.99% - 12 mths"}),
    _txn(**{"T#": 9, "Vendor": "AB2000", "Acct #": 5003, "Name": "Ramsey, Nora",
            "Date": _d(2025, 2, 15), "Payment": 25.0, "Fees Charged": 25,
            "Fees Paid": 25.0, "Fee Balance": 0, "Interest Due": 0,
            "Interest Balance": 0, "Principal Balance": 1000, "Total Owed": 1000,
            "Type": "ADJUST-FEE", "VP": "A",
            "Comments": "Adjustment: waive admin fee"}),
    _txn(**{"T#": 10, "Vendor": "AB2000", "Acct #": 5003, "Name": "Ramsey, Nora",
            "Date": _d(2025, 3, 1), "Payment": 90.26, "Fees Paid": 0,
            "Fee Balance": 0, "Interest Due": 12.5, "Interest Paid": 12.5,
            "Interest Balance": 0, "Principal Paid": 77.76,
            "Principal Balance": 922.24, "Total Owed": 922.24,
            "Type": "PMT-AUTOPAY", "VP": "P", "Comments": "Autopayment Received"}),
    # 5005: origination + ONE payment. Leaves $250 owing, but the account row
    # claims zero — the reconciliation exception.
    _txn(**{"T#": 11, "Vendor": "BC1000", "Acct #": 5005, "Name": "Bhatt, Priya",
            "Date": _d(2024, 5, 1), "Fee Balance": 0, "Interest Due": 0,
            "Interest Balance": 0, "Principal Balance": 1000, "Total Owed": 1000,
            "Type": "LOAN-NEW", "VP": "O", "Comments": "ORIGINATION"}),
    _txn(**{"T#": 12, "Vendor": "BC1000", "Acct #": 5005, "Name": "Bhatt, Priya",
            "Date": _d(2024, 6, 1), "Payment": 750.0, "Fees Paid": 0,
            "Fee Balance": 0, "Interest Due": 0, "Interest Paid": 0,
            "Interest Balance": 0, "Principal Paid": 750.0,
            "Principal Balance": 250.0, "Total Owed": 250.0,
            "Type": "PMT-CUST", "VP": "V", "Comments": "Partial payment"}),
    # The totals row: a row COUNT lands in the Type column.
    _txn(**{"Acct #": 12, "Payment": 3290.0, "Type": 12}),
]


VENDOR_COLUMNS = [
    # (label, vendor-1 value, vendor-2 value)
    ("Vendor Name", "Northside Dental", "Prairie Family Dental"),
    ("Vendor Address", "12 Cascade Ave., Kelowna, BC. V1Y 1A1",
     "88 Prairie Rd., Calgary, AB. T2P 2B2"),
    ("Vendor Email", "accounts@northside.example", "accounts@prairie.example"),
    ("Vendor Phone", "(250) 555-0101", "(403) 555-0202"),
    ("Vendor Start Date", datetime(2022, 1, 1), datetime(2023, 6, 1)),
    ("1st Invoice Date", datetime(2022, 1, 31), datetime(2023, 6, 30)),
    ("Vendor ID", "BC1000", "AB2000"),
    ("Providers", "Dr. Ada Lovelace", "Dr. Alan Turing"),
    (None, "Dr. Grace Hopper", None),
    (None, "AR-General", None),
]


def build_workbook(*, include_vendor_sheet: bool = True) -> InMemoryWorkbook:
    """The synthetic book, ready to hand to ``read_workbook``."""
    return InMemoryWorkbook(build_sheets(include_vendor_sheet=include_vendor_sheet))


def build_sheets(*, include_vendor_sheet: bool = True) -> dict[str, list[list]]:
    """The raw ``{sheet: rows}`` the workbook is made of."""
    accounts = [
        [None] * len(ACCOUNT_HEADER),   # row 1: blank
        ["Account Register"],           # row 2: a title band
        ACCOUNT_HEADER,                 # row 3: the header
        *ACCOUNTS_ROWS,                 # row 4+: data
    ]
    transactions = [
        *[[None] for _ in range(10)],   # rows 1-10: the statement lookup block
        ["Transaction Register"],       # row 11
        [None], [None],                 # rows 12-13
        TRANSACTION_HEADER,             # row 14
        *TRANSACTION_ROWS,              # row 15+
    ]
    sheets: dict[str, list[list]] = {
        "Accounts": accounts, "Transactions": transactions
    }
    if include_vendor_sheet:
        vendors = [[None], [None]]      # rows 1-2 blank; labels start at row 3
        vendors += [list(col) for col in VENDOR_COLUMNS]
        sheets["Vendors"] = vendors
    return sheets


def build_xlsx_bytes(*, include_vendor_sheet: bool = True) -> bytes:
    """The same book as a real .xlsx payload, for exercising the upload path."""
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in build_sheets(include_vendor_sheet=include_vendor_sheet).items():
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append(list(row))
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
