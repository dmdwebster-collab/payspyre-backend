"""Unit tests for the raw bank transaction-analysis engine.

Pure synthetic fixtures — NO database, NO network. All cases pin ``today`` so
the trailing-90d / 30d windows are deterministic.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.bank.transaction_analysis import (
    analyze_accounts,
    is_nsf_description,
)

TODAY = date(2026, 6, 19)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _txn(d: date, desc: str, *, credit=None, debit=None):
    t = {"Date": d.strftime("%Y-%m-%d"), "Description": desc}
    if credit is not None:
        t["Credit"] = credit
    if debit is not None:
        t["Debit"] = debit
    return t


def _account(transactions, *, current_balance=None):
    acct = {"Transactions": transactions}
    if current_balance is not None:
        acct["Balance"] = {"Current": current_balance}
    return acct


def _biweekly_dates(n, *, end=TODAY, step_days=14):
    """n deposit dates ending near `end`, spaced step_days apart."""
    return [end - timedelta(days=step_days * i) for i in range(n)]


# ---------------------------------------------------------------------------
# Clean bi-weekly payroll
# ---------------------------------------------------------------------------


def test_clean_biweekly_payroll_is_monthly_income():
    # $1,500.00 net every 2 weeks => ~2.17 deposits/mo => ~$3,250/mo.
    txns = [
        _txn(d, "PAYROLL DEP ACME CORP 0099", credit=1500.00)
        for d in _biweekly_dates(7)  # ~90 days of history
    ]
    accounts = [_account(txns, current_balance=2400.00)]

    out = analyze_accounts(accounts, today=TODAY)

    # 1500 * (26/12) = 3250.00 dollars => 325000 cents. Allow small rounding band.
    assert 315000 <= out["monthly_income_cents"] <= 335000
    assert out["nsf_count_90d"] == 0
    assert out["avg_balance_cents"] == 240000
    # earliest deposit ~84 days ago => ~2 months of age.
    assert out["account_age_months"] >= 2


def test_semimonthly_payroll_normalizes_to_two_per_month():
    # $2,000 on the 1st and 15th each month => exactly 2/mo => $4,000/mo.
    dates = []
    for m in (4, 5, 6):
        dates.append(date(2026, m, 1))
        dates.append(date(2026, m, 15))
    txns = [_txn(d, "DIRECT DEPOSIT EMPLOYER", credit=2000.00) for d in dates]
    out = analyze_accounts([_account(txns)], today=TODAY)
    # ~2 deposits/mo of $2,000. Bi-weekly vs semi-monthly cadence is inherently
    # fuzzy for 1st/15th schedules, so accept either normalization (~$4,000-4,350).
    assert 380000 <= out["monthly_income_cents"] <= 440000


# ---------------------------------------------------------------------------
# Transfers / refunds must NOT count as income
# ---------------------------------------------------------------------------


def test_transfers_and_refunds_excluded_from_income():
    txns = []
    # recurring internal transfers (same amount, regular cadence) — NOT income.
    for d in _biweekly_dates(6):
        txns.append(_txn(d, "TRANSFER FROM SAVINGS", credit=800.00))
    # a couple of one-off refunds — NOT income.
    txns.append(_txn(TODAY - timedelta(days=10), "REFUND AMAZON.CA", credit=59.99))
    txns.append(_txn(TODAY - timedelta(days=40), "REVERSAL POS PURCHASE", credit=120.00))
    # interac e-transfer in (treated as transfer) — NOT income.
    txns.append(_txn(TODAY - timedelta(days=5), "INTERAC E-TRANSFER FROM JOHN", credit=300.00))

    out = analyze_accounts([_account(txns)], today=TODAY)
    assert out["monthly_income_cents"] == 0


def test_real_payroll_survives_alongside_transfers():
    txns = []
    # genuine payroll stream
    for d in _biweekly_dates(6):
        txns.append(_txn(d, "ADP PAYROLL DEPOSIT", credit=1800.00))
    # noise: transfers + refunds that must be ignored
    for d in _biweekly_dates(6, step_days=14):
        txns.append(_txn(d - timedelta(days=3), "TRANSFER TO SAVINGS", credit=500.00))
    txns.append(_txn(TODAY - timedelta(days=8), "REFUND RETAILER", credit=45.00))

    out = analyze_accounts([_account(txns)], today=TODAY)
    # ~1800 * 26/12 = 3900/mo, transfers/refunds excluded.
    assert 370000 <= out["monthly_income_cents"] <= 410000


# ---------------------------------------------------------------------------
# NSF detection
# ---------------------------------------------------------------------------


def test_nsf_events_counted_robustly():
    txns = [
        _txn(TODAY - timedelta(days=5), "NSF FEE", debit=48.00),
        _txn(TODAY - timedelta(days=20), "Returned Item - Insufficient Funds", debit=48.00),
        _txn(TODAY - timedelta(days=50), "OVERDRAFT HANDLING CHARGE", debit=5.00),
        _txn(TODAY - timedelta(days=70), "Non-Sufficient Funds NSF", debit=48.00),
        # NOT nsf — must not match naive substring like "transfer" containing nothing
        _txn(TODAY - timedelta(days=3), "GROCERY STORE PURCHASE", debit=82.10),
        # "nsfw"-style false positive guard: word boundary required
        _txn(TODAY - timedelta(days=4), "TRANSFER NSFNET LABS", debit=10.00),
    ]
    out = analyze_accounts([_account(txns)], today=TODAY)
    assert out["nsf_count_90d"] == 4


def test_nsf_outside_90d_not_counted():
    txns = [
        _txn(TODAY - timedelta(days=120), "NSF FEE", debit=48.00),
        _txn(TODAY - timedelta(days=10), "NSF FEE", debit=48.00),
    ]
    out = analyze_accounts([_account(txns)], today=TODAY)
    assert out["nsf_count_90d"] == 1


def test_is_nsf_description_word_boundaries():
    assert is_nsf_description("NSF FEE")
    assert is_nsf_description("insufficient funds")
    assert is_nsf_description("Overdraft Interest")
    assert is_nsf_description("cheque returned")
    assert not is_nsf_description("NSFNET research")  # not a standalone NSF word
    assert not is_nsf_description("regular purchase")


# ---------------------------------------------------------------------------
# Irregular income
# ---------------------------------------------------------------------------


def test_irregular_gig_income_with_keyword_still_estimated():
    # Gig deposits: same employer keyword, stable-ish amounts, irregular gaps.
    days = [3, 12, 25, 41, 55, 70]
    txns = [
        _txn(TODAY - timedelta(days=d), "PAYROLL UBER DRIVER PARTNERS", credit=900.00)
        for d in days
    ]
    out = analyze_accounts([_account(txns)], today=TODAY)
    # Should produce *some* positive monthly income (keyword + stable amount),
    # roughly in the ballpark of 6 deposits of ~$900 over ~3 months.
    assert out["monthly_income_cents"] > 100000


def test_irregular_unlabeled_credits_not_treated_as_income():
    # Random one-off credits, no recurrence, no keyword, varied amounts.
    txns = [
        _txn(TODAY - timedelta(days=4), "E-TRANSFER FROM FRIEND", credit=200.00),
        _txn(TODAY - timedelta(days=33), "CASH DEPOSIT ATM", credit=540.00),
        _txn(TODAY - timedelta(days=61), "MISC CREDIT", credit=75.00),
    ]
    out = analyze_accounts([_account(txns)], today=TODAY)
    assert out["monthly_income_cents"] == 0


def test_two_unkeyworded_deposits_not_treated_as_income():
    # A coincidental pair: two similar deposits 14 days apart with NO income keyword.
    # A single gap is not enough corroboration — must NOT register as biweekly income.
    txns = [_txn(d, "ACME WIDGETS 8842", credit=1200.00) for d in _biweekly_dates(2)]
    out = analyze_accounts([_account(txns)], today=TODAY)
    assert out["monthly_income_cents"] == 0


def test_three_unkeyworded_recurring_deposits_count_as_income():
    # Three deposits on a stable biweekly cadence (2 corroborating gaps) qualify as
    # income even without a keyword.
    txns = [_txn(d, "ACME WIDGETS 8842", credit=1200.00) for d in _biweekly_dates(3)]
    out = analyze_accounts([_account(txns)], today=TODAY)
    assert out["monthly_income_cents"] > 0


def test_two_keyworded_deposits_still_count_as_income():
    # A positive income keyword still rescues a 2-deposit stream.
    txns = [_txn(d, "PAYROLL DEPOSIT EMPLOYER", credit=1200.00) for d in _biweekly_dates(2)]
    out = analyze_accounts([_account(txns)], today=TODAY)
    assert out["monthly_income_cents"] > 0


# ---------------------------------------------------------------------------
# Empty / short history
# ---------------------------------------------------------------------------


def test_empty_accounts():
    out = analyze_accounts([], today=TODAY)
    assert out == {
        "monthly_income_cents": 0,
        "nsf_count_90d": 0,
        "account_age_months": 0,
        "avg_balance_cents": 0,
    }


def test_no_transactions_but_balance():
    out = analyze_accounts([_account([], current_balance=1000.00)], today=TODAY)
    assert out["avg_balance_cents"] == 100000
    assert out["monthly_income_cents"] == 0
    assert out["account_age_months"] == 0


def test_single_deposit_not_recurring_income():
    # One lone payroll-looking deposit cannot establish a recurring stream.
    txns = [_txn(TODAY - timedelta(days=5), "PAYROLL DEP", credit=2000.00)]
    out = analyze_accounts([_account(txns)], today=TODAY)
    assert out["monthly_income_cents"] == 0


# ---------------------------------------------------------------------------
# Output contract / shape
# ---------------------------------------------------------------------------


def test_output_keys_match_adapter_contract():
    out = analyze_accounts([_account([])], today=TODAY)
    assert set(out.keys()) == {
        "monthly_income_cents",
        "nsf_count_90d",
        "account_age_months",
        "avg_balance_cents",
    }
    assert all(isinstance(v, int) for v in out.values())


def test_multi_account_balance_and_income_aggregate():
    payroll = [_txn(d, "PAYROLL CO", credit=1000.00) for d in _biweekly_dates(6)]
    acct1 = _account(payroll, current_balance=500.00)
    acct2 = _account([], current_balance=1500.00)
    out = analyze_accounts([acct1, acct2], today=TODAY)
    assert out["avg_balance_cents"] == 200000
    assert out["monthly_income_cents"] > 0


def test_string_dollar_amounts_parsed():
    txns = [_txn(d, "PAYROLL CO", credit="1,250.50") for d in _biweekly_dates(6)]
    out = analyze_accounts([_account(txns, current_balance="3,000.00")], today=TODAY)
    assert out["avg_balance_cents"] == 300000
    assert out["monthly_income_cents"] > 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
