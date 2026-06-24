"""Raw bank transaction-analysis engine (P8.x).

Derives underwriting metrics directly from RAW Flinks transaction data,
WITHOUT relying on Flinks' Enrich / Attributes API (inaccurate + expensive).

The output contract is a drop-in replacement for
``app.services.webhooks.translators._derive_bank_metrics`` — see
``analyze_accounts`` which returns exactly:

    {
        "monthly_income_cents": int,
        "nsf_count_90d":        int,
        "account_age_months":   int,
        "avg_balance_cents":    int,
    }

These keys are consumed by ``ReplayBankAdapter`` (replay_adapters.py):
``monthly_income_cents`` -> monthly_income_after_tax_cents,
``avg_balance_cents``    -> balance_current_cents, etc.

Design notes
------------
The hard problem is INCOME. Flinks transaction ``Code`` values are
non-standardized and frequently wrong, so we cannot trust them. Instead we
treat income as a *recurring deposit stream*:

  1. Group credit (money-in) transactions into "streams" keyed by a
     normalized merchant/description token + a rounded amount bucket.
  2. For each stream, look at the GAPS between consecutive deposits. A real
     payroll/benefit stream lands on a regular cadence (weekly ~7d,
     bi-weekly ~14d, semi-monthly ~15d, or monthly ~30d) with amounts that
     are stable (low coefficient of variation).
  3. Exclude one-off transfers / refunds / reversals via description
     heuristics AND by requiring >= MIN_DEPOSITS_PER_STREAM occurrences.
  4. Normalize each qualifying stream to a monthly figure using its detected
     cadence (deposit amount * deposits-per-month) and sum across streams.

Description keywords (payroll / deposit / direct dep) are used as ONE
positive signal that can rescue a borderline stream, but recurrence +
amount-stability is the dominant signal — so we never naively sum every
credit the way the stopgap did.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Window over which we look for income deposits.
INCOME_LOOKBACK_DAYS = 90

# A stream needs at least this many deposits to be considered "recurring".
MIN_DEPOSITS_PER_STREAM = 2

# Cadence buckets: (label, expected_gap_days, tolerance_days, deposits_per_month)
# deposits_per_month is the normalization factor (avg # pay events / month).
_CADENCES: list[tuple[str, float, float, float]] = [
    ("weekly", 7.0, 2.5, 52.0 / 12.0),       # ~4.33 / mo
    ("biweekly", 14.0, 3.5, 26.0 / 12.0),    # ~2.17 / mo
    ("semimonthly", 15.2, 3.5, 2.0),         # exactly 2 / mo
    ("monthly", 30.4, 6.0, 1.0),             # 1 / mo
]

# Max coefficient of variation (stdev/mean) of deposit amounts for a stream to
# count as "stable income". Payroll varies a little (taxes, hours) but not wildly.
MAX_AMOUNT_CV = 0.25

# Amount bucketing: deposits within this fraction of each other are treated as
# the "same" recurring amount when grouping into streams.
AMOUNT_BUCKET_PCT = 0.15

# Max upward step between two same-merchant amount bands to treat as a single
# stream split by a raise/COLA (rather than two unrelated deposit streams).
RAISE_MAX_STEP_PCT = 0.30

# Income keywords (positive signal). Lowercase, matched as substrings of the
# normalized description.
_INCOME_KEYWORDS = (
    "payroll", "salary", "wages", "wage", "direct dep", "directdep",
    "dir dep", "paycheck", "pay chq", "pay cheque", "deposit pay",
    "employer", "adp", "ceridian", "dayforce", "gusto", "remuneration",
    "benefit", "pension", "annuity", "e-transfer payroll",
)

# Non-income keywords (negative signal): transfers, refunds, reversals,
# loan disbursements, etc. that must NOT be counted as income even if recurring.
_NON_INCOME_KEYWORDS = (
    "transfer", "xfer", "tfr", "refund", "reversal", "reversed", "return",
    "chargeback", "interac e-transfer", "internal", "to savings",
    "from savings", "loan advance", "loan disbursement", "cash advance",
    "credit card payment", "cc payment", "atm deposit", "redeposit",
    "interest", "rebate", "cashback", "gift", "casino", "winnings",
    "lottery", "betting",
)

# NSF / overdraft detection. Word-boundary regexes, not naive substring.
_NSF_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bnsf\b",
        r"\bn\.s\.f\.?\b",
        r"non[-\s]?sufficient\s+funds?",
        r"insufficient\s+funds?",
        r"\boverdraft\b",
        r"\boverdrawn\b",
        r"\bod\s+(?:fee|charge|interest)\b",
        r"\bod\s+handling\b",
        r"returned\s+(?:item|cheque|check|payment)",
        r"\b(?:item|cheque|check|payment)\s+returned\b",
        r"\bnsf\s+(?:fee|charge)\b",
        r"dishonou?red",
    )
]


# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------


@dataclass
class _Txn:
    date: date
    description: str  # normalized lowercase
    raw_description: str
    debit_cents: int  # >= 0, money out
    credit_cents: int  # >= 0, money in


@dataclass
class _Stream:
    key: str
    deposits: list[_Txn] = field(default_factory=list)

    @property
    def amounts_cents(self) -> list[int]:
        return [t.credit_cents for t in self.deposits]

    @property
    def dates(self) -> list[date]:
        return sorted(t.date for t in self.deposits)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def parse_flinks_date(value: Any) -> Optional[date]:
    """Flinks emits ``"YYYY-MM-DD"``; some endpoints emit ISO-8601 w/ time.

    Mirrors translators._parse_flinks_date so behavior is identical.
    """
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _to_cents(value: Any) -> int:
    """Flinks reports decimal dollars (e.g. 1234.56). Round to int cents.

    Tolerates strings like ``"1,234.56"`` and missing values.
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(round(float(value) * 100))
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("$", "").strip()
        if not cleaned:
            return 0
        try:
            return int(round(float(cleaned) * 100))
        except ValueError:
            return 0
    return 0


_WS_RE = re.compile(r"\s+")
_NUM_TAIL_RE = re.compile(r"[\d#*]{2,}")  # ref / acct numbers, masked digits


def _normalize_description(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    text = raw.lower().strip()
    # collapse runs of digits / masking chars to a single token so that
    # "PAYROLL DEP 0012345" and "PAYROLL DEP 0012999" group together.
    text = _NUM_TAIL_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def _merchant_token(description: str) -> str:
    """A coarse merchant key: first ~4 alpha words of the normalized desc."""
    words = [w for w in re.findall(r"[a-z]+", description) if len(w) > 1]
    return " ".join(words[:4])


def _has_keyword(description: str, keywords: tuple[str, ...]) -> bool:
    return any(kw in description for kw in keywords)


def is_nsf_description(raw: Any) -> bool:
    """True if a description denotes an NSF / overdraft / returned-item event."""
    if not isinstance(raw, str):
        return False
    return any(p.search(raw) for p in _NSF_PATTERNS)


# ---------------------------------------------------------------------------
# Transaction extraction
# ---------------------------------------------------------------------------


def _extract_txns(accounts: list[dict[str, Any]]) -> list[_Txn]:
    txns: list[_Txn] = []
    for account in accounts or []:
        if not isinstance(account, dict):
            continue
        for raw in account.get("Transactions") or []:
            if not isinstance(raw, dict):
                continue
            d = parse_flinks_date(raw.get("Date"))
            if d is None:
                continue
            raw_desc = raw.get("Description") or ""
            txns.append(
                _Txn(
                    date=d,
                    description=_normalize_description(raw_desc),
                    raw_description=raw_desc if isinstance(raw_desc, str) else "",
                    debit_cents=_to_cents(raw.get("Debit")),
                    credit_cents=_to_cents(raw.get("Credit")),
                )
            )
    return txns


# ---------------------------------------------------------------------------
# Metric: account age
# ---------------------------------------------------------------------------


# Account-level metadata keys that may carry the date the account was OPENED.
# Flinks' raw schema is inconsistent across institutions/endpoints, so we scan a
# small set of plausible names (top-level and nested under ``Detail``). Using a
# real open date avoids understating age from the 90-day transaction span — a
# borderline-thin file is otherwise penalized for our short lookback window.
_ACCOUNT_OPEN_DATE_KEYS = (
    "OpeningDate", "OpenDate", "DateOpened", "OpenedDate", "AccountOpenDate",
    "CreatedDate", "CreationDate",
)


def _account_open_date(accounts: list[dict[str, Any]]) -> Optional[date]:
    """Earliest account open-date from account metadata, if any institution
    reports one. Returns ``None`` when no usable metadata date is present.
    """
    earliest: Optional[date] = None
    for account in accounts or []:
        if not isinstance(account, dict):
            continue
        sources: list[dict[str, Any]] = [account]
        detail = account.get("Detail")
        if isinstance(detail, dict):
            sources.append(detail)
        for src in sources:
            for key in _ACCOUNT_OPEN_DATE_KEYS:
                d = parse_flinks_date(src.get(key))
                if d is not None and (earliest is None or d < earliest):
                    earliest = d
    return earliest


def _account_age_months(
    txns: list[_Txn],
    today: date,
    accounts: Optional[list[dict[str, Any]]] = None,
) -> int:
    # Prefer a real open date from account metadata; it does not understate age
    # the way the 90-day transaction span does. Fall back to the earliest
    # observed transaction when no metadata date is available.
    open_date = _account_open_date(accounts or [])
    earliest: Optional[date] = open_date
    if txns:
        txn_earliest = min(t.date for t in txns)
        # Guard against an implausible/future metadata date by taking whichever
        # genuinely earlier date better reflects the account's true age.
        if earliest is None or txn_earliest < earliest:
            earliest = txn_earliest
    if earliest is None:
        return 0
    return max(0, (today - earliest).days // 30)


# ---------------------------------------------------------------------------
# Metric: balance
# ---------------------------------------------------------------------------


def _avg_balance_cents(accounts: list[dict[str, Any]]) -> int:
    """Sum of ``Accounts[].Balance.Current`` across accounts (cents).

    Matches the stopgap's proxy for "what's in the account right now".
    """
    total = 0
    for account in accounts or []:
        if not isinstance(account, dict):
            continue
        balance = account.get("Balance") or {}
        if isinstance(balance, dict):
            total += _to_cents(balance.get("Current"))
    return total


# ---------------------------------------------------------------------------
# Metric: NSF count (trailing 90d)
# ---------------------------------------------------------------------------


def _nsf_count_90d(txns: list[_Txn], today: date) -> int:
    count = 0
    for t in txns:
        days_ago = (today - t.date).days
        if 0 <= days_ago <= 90 and is_nsf_description(t.raw_description):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Metric: monthly income via recurring-stream detection
# ---------------------------------------------------------------------------


def _bucket_amount(cents: int) -> int:
    """Round an amount to a bucket so similar deposits group together."""
    if cents <= 0:
        return 0
    # bucket width scales with amount (AMOUNT_BUCKET_PCT) but min $25.
    width = max(2500, int(cents * AMOUNT_BUCKET_PCT))
    return (cents // width) * width


def _build_streams(credits: list[_Txn]) -> list[_Stream]:
    streams: dict[str, _Stream] = {}
    for t in credits:
        key = f"{_merchant_token(t.description)}|{_bucket_amount(t.credit_cents)}"
        stream = streams.get(key)
        if stream is None:
            stream = _Stream(key=key)
            streams[key] = stream
        stream.deposits.append(t)
    return _merge_raise_buckets(list(streams.values()))


def _merge_raise_buckets(streams: list[_Stream]) -> list[_Stream]:
    """Re-join streams that the amount bucketing fragmented across a pay raise.

    A raise (e.g. $1,500 -> $1,650 biweekly) can split one real payroll stream
    into two amount buckets, leaving each with too few deposits to qualify — so
    the raise silently drops the borrower's income to zero. We coalesce streams
    that share a merchant token and whose deposit amounts step monotonically up
    (or down) by a modest amount, so the combined stream is evaluated as one.

    Only same-merchant streams are merged, and only when the amount gap between
    the two bands is small (a plausible raise/COLA, not two unrelated deposits),
    keeping the merge conservative.
    """
    # Group fragments by merchant token (the part of the key before "|").
    by_merchant: dict[str, list[_Stream]] = {}
    for s in streams:
        merchant = s.key.rsplit("|", 1)[0]
        by_merchant.setdefault(merchant, []).append(s)

    merged: list[_Stream] = []
    for merchant, group in by_merchant.items():
        if len(group) == 1:
            merged.append(group[0])
            continue
        # An empty merchant token is the catch-all for description-less /
        # non-alpha deposits; never coalesce those — they are unrelated.
        if not merchant:
            merged.extend(group)
            continue
        # Order fragments by their typical amount; merge neighbours whose means
        # differ by at most RAISE_MAX_STEP_PCT (a realistic raise band).
        group.sort(key=lambda s: statistics.fmean(s.amounts_cents))
        combined = _Stream(key=group[0].key)
        combined.deposits.extend(group[0].deposits)
        prev_mean = statistics.fmean(group[0].amounts_cents)
        for frag in group[1:]:
            frag_mean = statistics.fmean(frag.amounts_cents)
            if prev_mean > 0 and (frag_mean - prev_mean) / prev_mean <= RAISE_MAX_STEP_PCT:
                combined.deposits.extend(frag.deposits)
            else:
                merged.append(combined)
                combined = _Stream(key=frag.key)
                combined.deposits.extend(frag.deposits)
            prev_mean = frag_mean
        merged.append(combined)
    return merged


def _classify_cadence(median_gap: float) -> Optional[tuple[str, float]]:
    """Return (label, deposits_per_month) if median_gap matches a cadence."""
    best: Optional[tuple[str, float]] = None
    best_dist = None
    for label, expected, tol, per_month in _CADENCES:
        dist = abs(median_gap - expected)
        if dist <= tol and (best_dist is None or dist < best_dist):
            best = (label, per_month)
            best_dist = dist
    return best


def _stream_is_non_income(stream: _Stream) -> bool:
    """A stream is excluded if the MAJORITY of its deposits look like
    transfers / refunds / reversals (negative keywords) and none carry a
    positive income keyword.
    """
    neg = sum(
        1 for t in stream.deposits if _has_keyword(t.description, _NON_INCOME_KEYWORDS)
    )
    pos = sum(
        1 for t in stream.deposits if _has_keyword(t.description, _INCOME_KEYWORDS)
    )
    if pos > 0:
        return False
    return neg * 2 >= len(stream.deposits)


def _qualify_deposits(deposits: list[_Txn]) -> int:
    """Monthly income for a homogeneous set of deposits, or 0 if not qualifying.

    Operates on a single amount band — callers handle stream-level exclusion and
    raise-splitting before invoking this.
    """
    if len(deposits) < MIN_DEPOSITS_PER_STREAM:
        return 0

    amounts = [t.credit_cents for t in deposits]
    mean_amt = statistics.fmean(amounts)
    if mean_amt <= 0:
        return 0

    # Amount stability (coefficient of variation). Single-amount streams have CV 0.
    if len(amounts) >= 2:
        cv = statistics.pstdev(amounts) / mean_amt
    else:
        cv = 0.0

    has_income_kw = any(_has_keyword(t.description, _INCOME_KEYWORDS) for t in deposits)

    # Cadence from gaps between consecutive deposits.
    dts = sorted(t.date for t in deposits)
    gaps = [(dts[i] - dts[i - 1]).days for i in range(1, len(dts))]
    gaps = [g for g in gaps if g > 0]

    cadence: Optional[tuple[str, float]] = None
    if gaps:
        median_gap = statistics.median(gaps)
        cadence = _classify_cadence(median_gap)

    # Decision: qualify as income if EITHER
    #   (a) we found a regular cadence and amounts are stable, OR
    #   (b) amounts are stable AND a positive income keyword is present
    #       (rescues a 2-deposit stream whose single gap is borderline), OR
    #   (c) strong keyword signal with >=2 deposits even if cadence is noisy.
    qualifies = False
    per_month = 1.0

    # (a) regular cadence + stable amounts. An UNKEYWORDED stream additionally needs
    #     >= 3 deposits (>= 2 corroborating gaps), so a single coincidental gap
    #     between two one-off transfers (e.g. two similar e-transfers 14 days apart
    #     that dodge the negative-keyword filter) can't masquerade as biweekly income
    #     and inflate the income estimate. A positive income keyword still qualifies a
    #     2-deposit stream.
    if cadence is not None and cv <= MAX_AMOUNT_CV and (has_income_kw or len(deposits) >= 3):
        qualifies = True
        per_month = cadence[1]
    elif has_income_kw and cv <= MAX_AMOUNT_CV:
        qualifies = True
        # Infer cadence from median gap even if outside tight tolerance.
        per_month = _infer_per_month_from_gaps(gaps)
    elif has_income_kw and len(deposits) >= 3:
        qualifies = True
        per_month = _infer_per_month_from_gaps(gaps)

    if not qualifies:
        return 0

    return int(round(mean_amt * per_month))


def _recent_amount_band(deposits: list[_Txn]) -> list[_Txn]:
    """Deposits whose amount is within AMOUNT_BUCKET_PCT of the most recent one.

    After a raise, a merged stream spans two amount levels. Underwriting cares
    about CURRENT income, so we isolate the band around the latest deposit — and
    using the latest level (whether the step was up or down) keeps the estimate
    conservative relative to summing or averaging across the step.
    """
    if not deposits:
        return []
    latest = max(deposits, key=lambda t: t.date)
    ref = latest.credit_cents
    if ref <= 0:
        return []
    lo, hi = ref * (1.0 - AMOUNT_BUCKET_PCT), ref * (1.0 + AMOUNT_BUCKET_PCT)
    return [t for t in deposits if lo <= t.credit_cents <= hi]


def _stream_monthly_income_cents(stream: _Stream) -> int:
    """Monthly income contribution of a stream, or 0 if not qualifying income."""
    deposits = stream.deposits
    if len(deposits) < MIN_DEPOSITS_PER_STREAM:
        return 0
    if _stream_is_non_income(stream):
        return 0

    # First try the stream as a whole (stable, single-amount payroll).
    income = _qualify_deposits(deposits)
    if income > 0:
        return income

    # Fallback: a pay raise can leave a merged stream's amounts too dispersed to
    # qualify (high CV), even though the underlying payroll is real. Re-evaluate
    # the most recent amount band on its own so a raise doesn't drop income to 0.
    recent = _recent_amount_band(deposits)
    if len(recent) < len(deposits):
        return _qualify_deposits(recent)
    return 0


def _infer_per_month_from_gaps(gaps: list[int]) -> float:
    """Fallback cadence normalization from raw median gap in days."""
    if not gaps:
        return 1.0
    median_gap = statistics.median(gaps)
    if median_gap <= 0:
        return 1.0
    return 30.4 / median_gap


def _monthly_income_cents(txns: list[_Txn], today: date) -> int:
    cutoff_days = INCOME_LOOKBACK_DAYS
    credits = [
        t
        for t in txns
        if t.credit_cents > 0 and 0 <= (today - t.date).days <= cutoff_days
    ]
    if not credits:
        return 0

    streams = _build_streams(credits)
    total = 0
    for stream in streams:
        total += _stream_monthly_income_cents(stream)
    return total


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def analyze_accounts(
    accounts: list[dict[str, Any]],
    *,
    today: Optional[date] = None,
) -> dict[str, int]:
    """Derive underwriting metrics from raw Flinks ``Accounts[]`` data.

    Drop-in replacement for translators._derive_bank_metrics — returns the
    same four keys consumed by ReplayBankAdapter:

        monthly_income_cents, nsf_count_90d, account_age_months, avg_balance_cents

    ``today`` is injectable for deterministic testing; defaults to UTC today.
    """
    if today is None:
        today = datetime.now(timezone.utc).date()

    txns = _extract_txns(accounts)

    return {
        "monthly_income_cents": _monthly_income_cents(txns, today),
        "nsf_count_90d": _nsf_count_90d(txns, today),
        "account_age_months": _account_age_months(txns, today, accounts),
        "avg_balance_cents": _avg_balance_cents(accounts),
    }
