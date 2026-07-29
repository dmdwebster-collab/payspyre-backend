"""Declarative source-mapping PROFILES for the portfolio import.

PaySpyre must be able to take on a vendor whose existing loan book lives in any
servicing system. The shape of that export — sheet names, header rows, column
order, status vocabulary, transaction-type vocabulary, money units — differs per
system, but the MEANING does not. So the shape lives in DATA (a profile), not in
code: mapping a new source is a new profile, not a new parser.

A profile is a plain dataclass tree that round-trips to/from JSON
(``to_dict`` / ``from_dict``), so an operator can POST a mapping for a source
this repo has never seen and import it without a deploy.

WHAT A PROFILE DECLARES
-----------------------
* ``accounts`` / ``transactions`` — tabular sheets: which worksheet, which row
  holds the header, which row data starts on, and a canonical-field -> column
  binding. A column binds either by HEADER TEXT (resilient to reordering) or by
  0-based INDEX (for unlabelled exports).
* ``vendors`` — many legacy exports lay vendors out TRANSPOSED (attribute labels
  down one column, one vendor per column). ``TransposedSheetSpec`` maps that
  shape, including a multi-row list block (the provider roster).
* ``status_map`` — source ``STATUS/SUB-STATUS`` -> PaySpyre loan status.
* ``transaction_types`` — the source's transaction taxonomy -> how each type
  behaves (opens the loan, closes it, is cash, is a reversal) and which PaySpyre
  ledger ``txn_type`` it lands on.
* ``money_unit`` / ``rate_unit`` / ``name_format`` — unit + format declarations,
  so the reader never guesses.

NOTHING here touches a database or reads a file; this module is pure data + a
couple of pure lookups. ``portfolio_workbook`` applies a profile to a workbook.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Union

# A column binds by header TEXT (str) or by 0-based INDEX (int).
ColumnBinding = Union[str, int]

MONEY_UNITS = ("dollars", "cents")
RATE_UNITS = ("fraction", "percent", "bps")
NAME_FORMATS = ("last_comma_first", "first_last", "single_field")

#: PaySpyre ledger transaction types (mirrors the platform_loan_txn_type enum).
LEDGER_TXN_TYPES = ("payment", "disbursement", "fee", "adjustment", "reversal")


@dataclass(frozen=True)
class SheetSpec:
    """A conventional row-per-record sheet."""

    sheet: str
    header_row: int          # 1-based row holding the column headers
    first_data_row: int      # 1-based row the data starts on
    #: canonical field name -> header text or 0-based column index
    columns: dict[str, ColumnBinding] = field(default_factory=dict)
    #: fields that MUST resolve to a column, else the profile does not fit the file
    required_columns: tuple[str, ...] = ()
    #: a data row is only real if every one of these fields is non-empty. Legacy
    #: exports append a TOTALS row that carries an id but no status/type — this
    #: is how it gets excluded without a heuristic.
    row_guard: tuple[str, ...] = ()
    #: a data row is only real if each of these fields parses as a string (totals
    #: rows tend to put a row-count integer where a code belongs).
    row_guard_text: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SheetSpec":
        return cls(
            sheet=d["sheet"],
            header_row=int(d["header_row"]),
            first_data_row=int(d["first_data_row"]),
            columns=dict(d.get("columns") or {}),
            required_columns=tuple(d.get("required_columns") or ()),
            row_guard=tuple(d.get("row_guard") or ()),
            row_guard_text=tuple(d.get("row_guard_text") or ()),
        )


@dataclass(frozen=True)
class TransposedSheetSpec:
    """A sheet laid out sideways: attribute labels run DOWN ``label_column``, and
    each entity occupies its own COLUMN from ``first_entity_column`` rightwards.

    ``list_block`` names a field whose values span several consecutive rows
    (the provider roster under a single "Providers" label).
    """

    sheet: str
    label_column: int = 0        # 0-based column holding the attribute labels
    first_entity_column: int = 1  # 0-based column of the first entity
    #: canonical field -> the label text in ``label_column``
    labels: dict[str, str] = field(default_factory=dict)
    #: canonical field -> (label text, how many rows the block spans)
    list_blocks: dict[str, tuple[str, int]] = field(default_factory=dict)
    #: an entity column is only real if this field is non-empty
    entity_guard: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["list_blocks"] = {k: list(v) for k, v in self.list_blocks.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TransposedSheetSpec":
        return cls(
            sheet=d["sheet"],
            label_column=int(d.get("label_column", 0)),
            first_entity_column=int(d.get("first_entity_column", 1)),
            labels=dict(d.get("labels") or {}),
            list_blocks={
                k: (v[0], int(v[1])) for k, v in (d.get("list_blocks") or {}).items()
            },
            entity_guard=d.get("entity_guard", ""),
        )


@dataclass(frozen=True)
class TransactionTypeRule:
    """How one source transaction type behaves.

    The flags are the source system's own semantics (a settings/taxonomy sheet
    usually states them verbatim — see ``LEGACY_SERVICING_V1``); ``ledger_type``
    is where the row lands in PaySpyre's ledger.
    """

    ledger_type: str = "adjustment"   # one of LEDGER_TXN_TYPES
    opens_loan: bool = False          # this row IS the origination event
    closes_loan: bool = False         # this row settles the loan
    is_cash: bool = False             # real money moved (gets a payment receipt row)
    is_reversal: bool = False         # undoes an earlier cash row (NSF/return)
    repayment_mode: Optional[str] = None  # regular | add_on | special | payoff

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TransactionTypeRule":
        return cls(
            ledger_type=d.get("ledger_type", "adjustment"),
            opens_loan=bool(d.get("opens_loan", False)),
            closes_loan=bool(d.get("closes_loan", False)),
            is_cash=bool(d.get("is_cash", False)),
            is_reversal=bool(d.get("is_reversal", False)),
            repayment_mode=d.get("repayment_mode"),
        )


@dataclass(frozen=True)
class PortfolioProfile:
    """A complete description of one source system's export shape."""

    name: str
    description: str = ""
    money_unit: str = "dollars"
    rate_unit: str = "fraction"
    name_format: str = "last_comma_first"
    accounts: Optional[SheetSpec] = None
    transactions: Optional[SheetSpec] = None
    vendors: Optional[TransposedSheetSpec] = None
    #: "STATUS/SUB-STATUS" (upper-cased, whitespace-collapsed) -> PaySpyre status.
    #: A bare "STATUS" key matches when the sub-status is absent/unmapped.
    status_map: dict[str, str] = field(default_factory=dict)
    #: Source statuses that must NOT be imported at all (e.g. voided rows).
    skip_statuses: tuple[str, ...] = ()
    transaction_types: dict[str, TransactionTypeRule] = field(default_factory=dict)
    #: source cadence label -> PaySpyre pricing_config.PaymentFrequency value
    payment_frequency_map: dict[str, str] = field(default_factory=dict)

    # -- validation ---------------------------------------------------------
    def validate(self) -> list[str]:
        """Structural problems with the profile itself (not with a file)."""
        problems: list[str] = []
        if self.money_unit not in MONEY_UNITS:
            problems.append(f"money_unit must be one of {MONEY_UNITS}")
        if self.rate_unit not in RATE_UNITS:
            problems.append(f"rate_unit must be one of {RATE_UNITS}")
        if self.name_format not in NAME_FORMATS:
            problems.append(f"name_format must be one of {NAME_FORMATS}")
        if self.accounts is None:
            problems.append("profile has no 'accounts' sheet spec")
        for src, rule in self.transaction_types.items():
            if rule.ledger_type not in LEDGER_TXN_TYPES:
                problems.append(
                    f"transaction type {src!r}: ledger_type must be one of {LEDGER_TXN_TYPES}"
                )
        return problems

    # -- lookups ------------------------------------------------------------
    def map_status(self, status: Any, sub_status: Any) -> Optional[str]:
        """(status, sub-status) -> PaySpyre loan status, or None if unmapped."""
        s = _norm(status)
        sub = _norm(sub_status)
        if not s:
            return None
        return self.status_map.get(f"{s}/{sub}") or self.status_map.get(s)

    def is_skipped_status(self, status: Any, sub_status: Any) -> bool:
        s, sub = _norm(status), _norm(sub_status)
        return s in self.skip_statuses or f"{s}/{sub}" in self.skip_statuses

    def rule_for(self, txn_type: Any) -> Optional[TransactionTypeRule]:
        return self.transaction_types.get(_norm(txn_type))

    def map_frequency(self, raw: Any) -> Optional[str]:
        return self.payment_frequency_map.get(_norm(raw))

    # -- (de)serialization --------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "money_unit": self.money_unit,
            "rate_unit": self.rate_unit,
            "name_format": self.name_format,
            "accounts": self.accounts.to_dict() if self.accounts else None,
            "transactions": self.transactions.to_dict() if self.transactions else None,
            "vendors": self.vendors.to_dict() if self.vendors else None,
            "status_map": dict(self.status_map),
            "skip_statuses": list(self.skip_statuses),
            "transaction_types": {
                k: v.to_dict() for k, v in self.transaction_types.items()
            },
            "payment_frequency_map": dict(self.payment_frequency_map),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PortfolioProfile":
        return cls(
            name=d["name"],
            description=d.get("description", ""),
            money_unit=d.get("money_unit", "dollars"),
            rate_unit=d.get("rate_unit", "fraction"),
            name_format=d.get("name_format", "last_comma_first"),
            accounts=SheetSpec.from_dict(d["accounts"]) if d.get("accounts") else None,
            transactions=(
                SheetSpec.from_dict(d["transactions"]) if d.get("transactions") else None
            ),
            vendors=(
                TransposedSheetSpec.from_dict(d["vendors"]) if d.get("vendors") else None
            ),
            status_map={_norm(k): v for k, v in (d.get("status_map") or {}).items()},
            skip_statuses=tuple(_norm(s) for s in (d.get("skip_statuses") or ())),
            transaction_types={
                _norm(k): TransactionTypeRule.from_dict(v)
                for k, v in (d.get("transaction_types") or {}).items()
            },
            payment_frequency_map={
                _norm(k): v for k, v in (d.get("payment_frequency_map") or {}).items()
            },
        )


def _norm(v: Any) -> str:
    """Upper-case, whitespace-collapsed key form. ``None`` -> ``''``."""
    if v is None:
        return ""
    return " ".join(str(v).split()).upper()


# ---------------------------------------------------------------------------
# Built-in profile: the spreadsheet-based servicing export PaySpyre's first
# book arrives in (Accounts + Transactions + Vendors/Providers + a Settings
# taxonomy sheet). Column bindings are by HEADER TEXT wherever the export
# labels its columns, so a reordered export still maps.
# ---------------------------------------------------------------------------

_ACCOUNTS = SheetSpec(
    sheet="Accounts",
    header_row=3,
    first_data_row=4,
    columns={
        "vendor_code": "Vendor",
        "provider_name": "Provider",
        "account_number": "Acct#",
        "status": "Status",
        "sub_status": "Sub-Status",
        "days_past_due": "Days Past Due",
        "borrower_name": "Name",
        "co_borrower_name": "Co-Borrower",
        "sales_value": "Sales Value",
        "insurance": "Insurance",
        "down_payment": "Downpayment",
        "amount_financed": "Amount Financed",
        "term_months": "Term Months",
        "annual_rate": "Rate %",
        "regular_payment": "Regular Payment",
        "payment_frequency": "Payment Frequency",
        "cost_of_borrowing": "Cost of Borrowing",
        "first_payment_date": "First Pmt Date",
        "final_payment_date": "Final Pmt Date",
        "days_in_year": "DinY",
        "origination_date": "Origination",
        "origination_type": "Org. Type",
        "payment_amount_total": "Payment Amount",
        "fees_paid": "Fees Paid",
        "interest_paid": "Interest Paid",
        "principal_paid": "Principal Paid",
        "fees_balance": "Fees Balance",
        "interest_balance": "Interest Balance",
        "principal_balance": "Principal Balance",
        "total_owed": "Total Owed",
        "next_due_date": "Next Due Date",
        "last_transaction_date": "Last Tansaction Date",  # sic — the export's spelling
        "last_transaction_type": "Last Transaction Type",
        "close_date": "Close Date",
        "close_type": "Close Type",
        "nsf_return_count": "NSF / RETURN",
        "deferment_count": "Deferment",
    },
    required_columns=("account_number", "status", "borrower_name", "amount_financed"),
    # The export's final row is a TOTALS row: it carries a row count where the
    # account number goes and leaves Status blank.
    row_guard=("account_number", "status"),
)

_TRANSACTIONS = SheetSpec(
    sheet="Transactions",
    header_row=14,   # rows 1-10 are a per-account statement lookup block
    first_data_row=15,
    columns={
        "transaction_number": "T#",
        "vendor_code": "Vendor",
        "provider_name": "Provider",
        "account_number": "Acct #",
        "annual_rate": "Rate",
        "days_in_year": "DinY",
        "borrower_name": "Name",
        "date": "Date",
        "payment": "Payment",
        "fees_charged": "Fees Charged",
        "fees_paid": "Fees Paid",
        "fees_balance": "Fee Balance",
        "accrued_interest": "Accrued Interest",
        "interest_due": "Interest Due",
        "interest_paid": "Interest Paid",
        "interest_balance": "Interest Balance",
        "principal_paid": "Principal Paid",
        "principal_balance": "Principal Balance",
        "total_owed": "Total Owed",
        "type": "Type",
        "comment": "Comments",
    },
    required_columns=("account_number", "date", "type"),
    row_guard=("account_number", "type"),
    # The totals row puts a row COUNT in the Type column.
    row_guard_text=("type",),
)

_VENDORS = TransposedSheetSpec(
    sheet="Vendors",
    label_column=0,
    first_entity_column=1,
    labels={
        "name": "Vendor Name",
        "address": "Vendor Address",
        "email": "Vendor Email",
        "phone": "Vendor Phone",
        "start_date": "Vendor Start Date",
        "code": "Vendor ID",
    },
    # The provider roster sits under a single "Providers" label and runs down
    # the next rows until the block ends.
    list_blocks={"providers": ("Providers", 10)},
    entity_guard="name",
)

#: Source (STATUS/SUB-STATUS) -> PaySpyre ``platform_loan_status``.
_STATUS_MAP = {
    "OPEN/ACTIVE": "active",
    "OPEN/": "active",
    "CLOSED/PAID": "paid_off",
    "CLOSED/RENEWED": "paid_off",      # settled by a new (separate) loan
    "CLOSED/RBO": "paid_off",          # re-bought-out: settled
    "CLOSED/WRITE OFF": "charged_off",
    "CLOSED/TRANSFER": "cancelled",
    "CLOSED/SMALL BALANCE": "charged_off",
}

#: The source's own transaction taxonomy, with its stated OPEN/CLOSE event flags.
_TXN_TYPES = {
    # --- cash in -----------------------------------------------------------
    "PMT-AUTOPAY": TransactionTypeRule("payment", is_cash=True, repayment_mode="regular"),
    "PMT-CUST": TransactionTypeRule("payment", is_cash=True, repayment_mode="regular"),
    "PMT-INS": TransactionTypeRule("payment", is_cash=True, repayment_mode="regular"),
    # --- payoffs (close events) -------------------------------------------
    "PDOUT-AUTOPAY": TransactionTypeRule("payment", closes_loan=True, is_cash=True, repayment_mode="payoff"),
    "PDOUT-CUST": TransactionTypeRule("payment", closes_loan=True, is_cash=True, repayment_mode="payoff"),
    "PDOUT-INS": TransactionTypeRule("payment", closes_loan=True, is_cash=True, repayment_mode="payoff"),
    "PDOUT-RENEWAL": TransactionTypeRule("payment", closes_loan=True, is_cash=True, repayment_mode="payoff"),
    "PDOUT-RBO": TransactionTypeRule("payment", closes_loan=True, is_cash=True, repayment_mode="payoff"),
    # --- originations (open events) ---------------------------------------
    "LOAN-NEW": TransactionTypeRule("disbursement", opens_loan=True),
    "LOAN-RENEWAL": TransactionTypeRule("disbursement", opens_loan=True),
    "LOAN-RBO": TransactionTypeRule("disbursement", opens_loan=True),
    "LOAN-VOIDED": TransactionTypeRule("adjustment"),
    # --- returns -----------------------------------------------------------
    "NSF/RETURN": TransactionTypeRule("reversal", is_reversal=True),
    # --- non-cash corrections ---------------------------------------------
    "ADJUST-INT": TransactionTypeRule("adjustment"),
    "ADJUST-PRI": TransactionTypeRule("adjustment"),
    "ADJUST-FEE": TransactionTypeRule("adjustment"),
    "WROFF-INT": TransactionTypeRule("adjustment"),
    "WROFF-PRI": TransactionTypeRule("adjustment", closes_loan=True),
    "WROFF-FEE": TransactionTypeRule("adjustment"),
    "WROFF-SMBAL": TransactionTypeRule("adjustment", closes_loan=True),
    "TRANSFER-INT": TransactionTypeRule("adjustment"),
    "TRANSFER-PRI": TransactionTypeRule("adjustment", closes_loan=True),
    "TRANSFER-FEE": TransactionTypeRule("adjustment"),
    "DEFERMENT": TransactionTypeRule("adjustment"),
    "REFUND": TransactionTypeRule("adjustment"),
    "VOIDED": TransactionTypeRule("adjustment"),
    "SYSTEM-NULL": TransactionTypeRule("adjustment"),
    "INSOLVENCY": TransactionTypeRule("adjustment"),
}

_FREQUENCY_MAP = {
    "MONTHLY": "monthly",
    "SEMI-MONTHLY": "semi_monthly",
    "SEMI MONTHLY": "semi_monthly",
    "BI-WEEKLY": "bi_weekly",
    "BIWEEKLY": "bi_weekly",
    "BI WEEKLY": "bi_weekly",
    "WEEKLY": "weekly",
}

LEGACY_SERVICING_V1 = PortfolioProfile(
    name="legacy_servicing_v1",
    description=(
        "Spreadsheet loan-book export with an Accounts sheet (one row per loan), "
        "a Transactions sheet (one row per posted event, carrying its own "
        "fees/interest/principal allocation and running balances), a transposed "
        "Vendors sheet whose provider roster sits in a labelled row block, and a "
        "Settings sheet declaring the transaction taxonomy."
    ),
    money_unit="dollars",
    rate_unit="fraction",
    name_format="last_comma_first",
    accounts=_ACCOUNTS,
    transactions=_TRANSACTIONS,
    vendors=_VENDORS,
    status_map=_STATUS_MAP,
    skip_statuses=("VOIDED", "VOIDED/VOIDED"),
    transaction_types=_TXN_TYPES,
    payment_frequency_map=_FREQUENCY_MAP,
)

#: Built-in profiles, addressable by name. A source this repo has never seen is
#: mapped by POSTing a profile dict (``PortfolioProfile.from_dict``) — no deploy.
PROFILES: dict[str, PortfolioProfile] = {
    LEGACY_SERVICING_V1.name: LEGACY_SERVICING_V1,
}

DEFAULT_PROFILE_NAME = LEGACY_SERVICING_V1.name


def get_profile(name: Optional[str] = None) -> PortfolioProfile:
    key = name or DEFAULT_PROFILE_NAME
    try:
        return PROFILES[key]
    except KeyError:
        raise KeyError(
            f"unknown portfolio profile {key!r}; known: {', '.join(sorted(PROFILES))}"
        ) from None
