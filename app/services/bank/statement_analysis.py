"""AI bank-statement analysis v1 (WS-D).

Dave (02__WP_Underwriting.md §5): "Right now, I'm doing that manually, which is
very time-consuming... sometimes there's thousands of transactions that need to
be reviewed... should be something that an AI or a platform that has been
automated can do in a very short period of time."

This module classifies RAW Flinks-style transaction lists into income/expense
categories learned from descriptions, flags **micro-lender usage** as an
explicit risk attribute, and produces a human-visible summary attached to the
application (via the bank_link verification payload) plus a compact
``risk_attributes`` dict consumed by the scorecard / flow engine.

Classifier seam
---------------
Classification goes through the :class:`TransactionClassifier` protocol.
v1 ships :class:`RuleBasedClassifier` (deterministic keyword rules — zero
cost, zero latency, auditable). An LLM-backed classifier later is a drop-in:
implement ``classify()`` and pass it to :func:`analyze_statement`; nothing else
changes. The output contract (categories + risk attributes + summary) is the
stable interface, not the classifier.

PII policy
----------
The output payload (``StatementAnalysis.to_payload``) carries ONLY category
aggregates, counts, and canonical lender names from the known micro-lender
registry — never raw transaction descriptions or counterparty names — so it is
safe to embed in the WORM ``verification_completed`` event payload (Hard Rule
#6; see translators.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional, Protocol

from app.services.bank import transaction_analysis as ta

# Analysis window — mirrors the income engine's lookback.
ANALYSIS_WINDOW_DAYS = 90
_MONTHS_IN_WINDOW = ANALYSIS_WINDOW_DAYS / 30.4

# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

INCOME_CATEGORIES = (
    "payroll_income",
    "government_benefit",
    "pension_income",
    "other_income",
)

EXPENSE_CATEGORIES = (
    "housing",
    "utilities_telecom",
    "groceries",
    "dining",
    "transport_fuel",
    "insurance",
    "debt_payment",
    "micro_lender",
    "subscriptions_entertainment",
    "healthcare",
    "gambling",
    "cash_withdrawal",
    "transfer_out",
    "nsf_and_bank_fees",
    "other_expense",
)

# ---------------------------------------------------------------------------
# Known Canadian micro-lender / payday-lender registry (canonical name →
# match keywords). Registry names are lender businesses (not borrower PII) and
# are the ONLY merchant-ish strings the analysis ever emits.
# ---------------------------------------------------------------------------

MICRO_LENDER_REGISTRY: dict[str, tuple[str, ...]] = {
    "Money Mart": ("money mart", "moneymart"),
    "Cash Money": ("cash money", "cashmoney"),
    "easyfinancial": ("easyfinancial", "easy financial", "goeasy"),
    "Fairstone": ("fairstone",),
    "Mogo": ("mogo",),
    "Speedy Cash": ("speedy cash", "speedycash"),
    "Cash 4 You": ("cash 4 you", "cash4you"),
    "iCash": ("icash",),
    "Nyble": ("nyble",),
    "Bree": ("bree advance", "bree inc"),
    "LendDirect": ("lenddirect", "lend direct"),
    "Captain Cash": ("captain cash", "captaincash"),
    "Cash Store": ("cash store", "cashstore"),
    "Loan Express": ("loan express", "loanexpress"),
    "My Canada Payday": ("my canada payday", "mycanadapayday"),
    "GoDay": ("goday",),
    "Payday (generic)": ("payday loan", "payday ln", "pay day loan", "pd loan"),
}

# ---------------------------------------------------------------------------
# Rule-based keyword tables (lowercase substring match on the normalized
# description). First-match wins in the order below; micro-lender is checked
# before everything else because it doubles as a risk flag.
# ---------------------------------------------------------------------------

_EXPENSE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("housing", (
        "rent", "mortgage", "landlord", "property mgmt", "property management",
        "strata", "condo fee",
    )),
    ("utilities_telecom", (
        "hydro", "fortis", "bc hydro", "enbridge", "utility", "utilities",
        "telus", "rogers", "bell ", "shaw", "fido", "koodo", "freedom mobile",
        "internet", "wireless",
    )),
    ("groceries", (
        "grocery", "groceries", "superstore", "save-on", "save on foods",
        "safeway", "sobeys", "loblaws", "no frills", "nofrills", "walmart",
        "costco", "iga ", "metro ", "food basics", "freshco", "t&t supermarket",
    )),
    ("dining", (
        "restaurant", "mcdonald", "tim hortons", "starbucks", "subway",
        "a&w", "wendy", "burger", "pizza", "doordash", "skip the dishes",
        "skipthedishes", "uber eats", "ubereats", "cafe", "coffee",
    )),
    ("transport_fuel", (
        "petro-can", "petro can", "esso", "shell", "chevron", "husky",
        "gas bar", "fuel", "transit", "uber trip", "uber *trip", "lyft",
        "bc ferries", "parking", "ic bc", "translink",
    )),
    ("insurance", (
        "insurance", "insur ", "sunlife", "sun life", "manulife", "canada life",
        "icbc", "intact", "wawanesa", "belairdirect",
    )),
    ("debt_payment", (
        "loan payment", "loan pmt", "credit card payment", "cc payment",
        "visa payment", "mastercard payment", "amex", "line of credit",
        "loc payment", "auto loan", "car loan", "student loan", "afterpay",
        "affirm", "klarna", "financing",
    )),
    ("subscriptions_entertainment", (
        "netflix", "spotify", "disney", "crave", "prime video", "apple.com",
        "apple bill", "google play", "playstation", "xbox", "cinema",
        "cineplex", "subscription", "patreon", "onlyfans", "gym", "fitness",
    )),
    ("healthcare", (
        "pharmacy", "shoppers drug", "rexall", "dental", "dentist", "clinic",
        "physio", "chiro", "optical", "medical",
    )),
    ("gambling", (
        "casino", "bet365", "betmgm", "draftkings", "playnow", "lotto",
        "lottery", "bclc", "poker", "betting", "sportsbook",
    )),
    ("cash_withdrawal", (
        "atm withdrawal", "abm withdrawal", "cash withdrawal", "atm w/d",
        "withdrawal interac", "cash advance",
    )),
    ("transfer_out", (
        "e-transfer", "etransfer", "e-tfr", "transfer to", "tfr to",
        "wire transfer", "to savings", "xfer",
    )),
    ("nsf_and_bank_fees", (
        "nsf", "overdraft", "monthly fee", "service charge", "service fee",
        "account fee", "overlimit", "interest charge", "od fee",
    )),
)

_INCOME_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("payroll_income", (
        "payroll", "salary", "wages", "wage", "direct dep", "directdep",
        "dir dep", "paycheck", "pay chq", "pay cheque", "employer", "adp",
        "ceridian", "dayforce", "gusto", "remuneration",
    )),
    ("government_benefit", (
        "canada child benefit", "ccb", "gst credit", "gst/hst", "cpp",
        "ei canada", "employment insurance", "canada fed", "canada pro",
        "provincial payment", "climate action", "carbon rebate",
        "social assistance", "disability payment",
    )),
    ("pension_income", ("pension", "annuity", "retirement", "rrif", "oas")),
)


class TransactionClassifier(Protocol):
    """The classifier seam. v1 = keyword rules; an LLM classifier later
    implements the same call and drops in via ``analyze_statement(classifier=…)``."""

    def classify(self, description: str, amount_cents: int, is_credit: bool) -> str:
        """Return a category name from INCOME_CATEGORIES / EXPENSE_CATEGORIES."""
        ...


def match_micro_lender(description: str) -> Optional[str]:
    """Return the canonical registry name if the description matches a known
    micro/payday lender, else None. Input must be a normalized (lowercase)
    description."""
    for canonical, keywords in MICRO_LENDER_REGISTRY.items():
        if any(kw in description for kw in keywords):
            return canonical
    return None


_WS_RE = re.compile(r"\s+")


class RuleBasedClassifier:
    """Deterministic keyword classifier (v1). Categories are learned from
    description substrings; micro-lender matches (either direction) always win
    because they are the explicit risk attribute Dave asked for."""

    def classify(self, description: str, amount_cents: int, is_credit: bool) -> str:
        desc = _WS_RE.sub(" ", (description or "").lower()).strip()
        if match_micro_lender(desc) is not None:
            return "micro_lender"
        if is_credit:
            for category, keywords in _INCOME_RULES:
                if any(kw in desc for kw in keywords):
                    return category
            return "other_income"
        if ta.is_nsf_description(description):
            return "nsf_and_bank_fees"
        for category, keywords in _EXPENSE_RULES:
            if any(kw in desc for kw in keywords):
                return category
        return "other_expense"


@dataclass(frozen=True)
class MicroLenderUsage:
    """The explicit micro-lender risk attribute."""

    flag: bool
    txn_count_90d: int
    inflow_cents: int  # money received FROM micro-lenders (new borrowing)
    outflow_cents: int  # repayments TO micro-lenders
    lenders: list[str] = field(default_factory=list)  # canonical registry names only


@dataclass(frozen=True)
class StatementAnalysis:
    """The full analysis output (aggregates only — no raw descriptions)."""

    window_days: int
    txn_count: int
    monthly_income_cents: int  # recurring-stream engine (verified income)
    monthly_expense_cents: int
    monthly_free_cash_flow_cents: int
    income_by_category_cents: dict[str, int]  # 90d totals
    expense_by_category_cents: dict[str, int]  # 90d totals
    nsf_count_90d: int
    micro_lender: MicroLenderUsage
    summary_lines: list[str]

    @property
    def risk_attributes(self) -> dict[str, Any]:
        """Compact flow-engine / scorecard inputs."""
        return {
            "micro_lender_used": self.micro_lender.flag,
            "micro_lender_txn_count_90d": self.micro_lender.txn_count_90d,
            "monthly_expense_cents": self.monthly_expense_cents,
            "monthly_free_cash_flow_cents": self.monthly_free_cash_flow_cents,
        }

    def to_payload(self) -> dict[str, Any]:
        """JSON-safe, PII-free dict for event payloads / API responses."""
        return {
            "v": 1,
            "window_days": self.window_days,
            "txn_count": self.txn_count,
            "monthly_income_cents": self.monthly_income_cents,
            "monthly_expense_cents": self.monthly_expense_cents,
            "monthly_free_cash_flow_cents": self.monthly_free_cash_flow_cents,
            "income_by_category_cents": self.income_by_category_cents,
            "expense_by_category_cents": self.expense_by_category_cents,
            "nsf_count_90d": self.nsf_count_90d,
            "micro_lender": {
                "flag": self.micro_lender.flag,
                "txn_count_90d": self.micro_lender.txn_count_90d,
                "inflow_cents": self.micro_lender.inflow_cents,
                "outflow_cents": self.micro_lender.outflow_cents,
                "lenders": self.micro_lender.lenders,
            },
            "risk_attributes": self.risk_attributes,
            "summary_lines": self.summary_lines,
        }


def _dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _build_summary(analysis_kwargs: dict[str, Any]) -> list[str]:
    """Human-visible summary — what Dave reads instead of thousands of rows."""
    lines: list[str] = []
    lines.append(
        f"Analyzed {analysis_kwargs['txn_count']} transactions over the last "
        f"{analysis_kwargs['window_days']} days."
    )
    lines.append(
        f"Verified recurring income: {_dollars(analysis_kwargs['monthly_income_cents'])}/mo; "
        f"average spend: {_dollars(analysis_kwargs['monthly_expense_cents'])}/mo; "
        f"free cash flow: {_dollars(analysis_kwargs['monthly_free_cash_flow_cents'])}/mo."
    )
    expenses: dict[str, int] = analysis_kwargs["expense_by_category_cents"]
    top = sorted(
        ((k, v) for k, v in expenses.items() if v > 0 and k != "micro_lender"),
        key=lambda kv: kv[1],
        reverse=True,
    )[:3]
    if top:
        pretty = ", ".join(
            f"{name.replace('_', ' ')} {_dollars(cents)}" for name, cents in top
        )
        lines.append(f"Top spending categories (90d): {pretty}.")
    nsf = analysis_kwargs["nsf_count_90d"]
    if nsf:
        lines.append(f"⚠ {nsf} NSF/overdraft event(s) in the last 90 days.")
    ml: MicroLenderUsage = analysis_kwargs["micro_lender"]
    if ml.flag:
        lenders = ", ".join(ml.lenders) or "unrecognized lender"
        lines.append(
            f"⚠ RISK: micro-lender activity detected — {ml.txn_count_90d} transaction(s) "
            f"with {lenders} (received {_dollars(ml.inflow_cents)}, "
            f"repaid {_dollars(ml.outflow_cents)})."
        )
    else:
        lines.append("No micro-lender / payday-lender activity detected.")
    return lines


def analyze_statement(
    accounts: list[dict[str, Any]],
    *,
    today: Optional[date] = None,
    classifier: Optional[TransactionClassifier] = None,
) -> StatementAnalysis:
    """Analyze raw Flinks ``Accounts[]`` into the WS-D statement analysis.

    ``today`` is injectable for deterministic tests; ``classifier`` is the LLM
    seam (defaults to the rule-based v1).
    """
    if today is None:
        today = datetime.now(timezone.utc).date()
    if classifier is None:
        classifier = RuleBasedClassifier()

    # Reuse the hardened income/NSF/age/balance engine for the base metrics —
    # identical numbers to what the flow engine's bank verification sees.
    base = ta.analyze_accounts(accounts, today=today)
    txns = ta._extract_txns(accounts)  # noqa: SLF001 — same-package parsing helper
    window = [t for t in txns if 0 <= (today - t.date).days <= ANALYSIS_WINDOW_DAYS]

    income_totals = {c: 0 for c in INCOME_CATEGORIES}
    expense_totals = {c: 0 for c in EXPENSE_CATEGORIES}
    ml_count = 0
    ml_in = 0
    ml_out = 0
    ml_lenders: set[str] = set()

    for t in window:
        is_credit = t.credit_cents > 0
        amount = t.credit_cents if is_credit else t.debit_cents
        if amount <= 0:
            continue
        category = classifier.classify(t.raw_description, amount, is_credit)
        if category == "micro_lender":
            ml_count += 1
            canonical = match_micro_lender(t.description)
            if canonical:
                ml_lenders.add(canonical)
            if is_credit:
                ml_in += amount
                # New borrowing is NOT income; track it only under the risk flag.
            else:
                ml_out += amount
                expense_totals["micro_lender"] += amount
            continue
        if is_credit:
            if category not in income_totals:
                category = "other_income"
            income_totals[category] += amount
        else:
            if category not in expense_totals:
                category = "other_expense"
            expense_totals[category] += amount

    total_expense_90d = sum(expense_totals.values())
    monthly_expense = int(round(total_expense_90d / _MONTHS_IN_WINDOW)) if window else 0
    monthly_income = base["monthly_income_cents"]
    free_cash_flow = monthly_income - monthly_expense

    micro = MicroLenderUsage(
        flag=ml_count > 0,
        txn_count_90d=ml_count,
        inflow_cents=ml_in,
        outflow_cents=ml_out,
        lenders=sorted(ml_lenders),
    )

    kwargs: dict[str, Any] = {
        "window_days": ANALYSIS_WINDOW_DAYS,
        "txn_count": len(window),
        "monthly_income_cents": monthly_income,
        "monthly_expense_cents": monthly_expense,
        "monthly_free_cash_flow_cents": free_cash_flow,
        "income_by_category_cents": income_totals,
        "expense_by_category_cents": expense_totals,
        "nsf_count_90d": base["nsf_count_90d"],
        "micro_lender": micro,
    }
    return StatementAnalysis(summary_lines=_build_summary(kwargs), **kwargs)
