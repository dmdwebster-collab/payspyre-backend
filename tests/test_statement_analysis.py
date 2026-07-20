"""Pure unit tests for WS-D AI bank-statement analysis v1.

No DB, no network. Covers income/expense categorization, the micro-lender risk
flag, the human-visible summary, the PII-free payload contract, and the
classifier seam.
"""
from __future__ import annotations

from datetime import date, timedelta

from app.services.bank.statement_analysis import (
    RuleBasedClassifier,
    StatementAnalysis,
    analyze_statement,
    match_micro_lender,
)

TODAY = date(2026, 7, 20)


def _txn(days_ago: int, description: str, *, credit=0.0, debit=0.0):
    d = TODAY - timedelta(days=days_ago)
    return {"Date": d.isoformat(), "Description": description,
            "Credit": credit, "Debit": debit}


def _accounts(txns):
    return [{"Balance": {"Current": 1500.0}, "Transactions": txns}]


def test_classifier_categorizes_expenses_and_income():
    c = RuleBasedClassifier()
    assert c.classify("PAYROLL DEP ACME CORP", 200000, True) == "payroll_income"
    assert c.classify("RENT PAYMENT LANDLORD", 150000, False) == "housing"
    assert c.classify("NETFLIX.COM", 1699, False) == "subscriptions_entertainment"
    assert c.classify("SHELL GAS BAR", 8000, False) == "transport_fuel"
    assert c.classify("SOME RANDOM MERCHANT", 5000, False) == "other_expense"
    assert c.classify("MYSTERY DEPOSIT", 5000, True) == "other_income"


def test_micro_lender_registry_match():
    assert match_micro_lender("money mart advance") == "Money Mart"
    assert match_micro_lender("easyfinancial loan pmt") == "easyfinancial"
    assert match_micro_lender("cash money store 123") == "Cash Money"
    assert match_micro_lender("regular grocery store") is None


def test_micro_lender_flag_and_counts():
    txns = [
        _txn(10, "MONEY MART LOAN ADVANCE", credit=500.0),   # borrowed
        _txn(5, "MONEY MART REPAYMENT", debit=120.0),        # repaid
        _txn(3, "iCash disbursement", credit=300.0),
        _txn(40, "PAYROLL ACME", credit=2000.0),
        _txn(26, "PAYROLL ACME", credit=2000.0),
        _txn(12, "PAYROLL ACME", credit=2000.0),
    ]
    analysis = analyze_statement(_accounts(txns), today=TODAY)
    assert analysis.micro_lender.flag is True
    assert analysis.micro_lender.txn_count_90d == 3
    assert analysis.micro_lender.inflow_cents == 80000  # 500 + 300
    assert analysis.micro_lender.outflow_cents == 12000
    assert "Money Mart" in analysis.micro_lender.lenders
    assert "iCash" in analysis.micro_lender.lenders
    # Micro-lender inflow is NOT counted as income.
    assert "micro_lender" not in analysis.income_by_category_cents


def test_no_micro_lender_activity():
    txns = [
        _txn(40, "PAYROLL ACME CORP", credit=2500.0),
        _txn(26, "PAYROLL ACME CORP", credit=2500.0),
        _txn(12, "PAYROLL ACME CORP", credit=2500.0),
        _txn(8, "SAVE ON FOODS GROCERY", debit=180.0),
    ]
    analysis = analyze_statement(_accounts(txns), today=TODAY)
    assert analysis.micro_lender.flag is False
    assert analysis.micro_lender.txn_count_90d == 0
    assert analysis.risk_attributes["micro_lender_used"] is False


def test_risk_attributes_and_free_cash_flow():
    txns = [
        _txn(40, "PAYROLL ACME", credit=3000.0),
        _txn(26, "PAYROLL ACME", credit=3000.0),
        _txn(12, "PAYROLL ACME", credit=3000.0),
        _txn(30, "RENT", debit=1500.0),
        _txn(15, "GROCERY SUPERSTORE", debit=600.0),
    ]
    analysis = analyze_statement(_accounts(txns), today=TODAY)
    assert analysis.monthly_income_cents > 0
    assert analysis.monthly_expense_cents > 0
    ra = analysis.risk_attributes
    assert ra["monthly_free_cash_flow_cents"] == (
        analysis.monthly_income_cents - analysis.monthly_expense_cents
    )


def test_summary_is_human_visible_and_flags_micro_lender():
    txns = [
        _txn(10, "CASH MONEY PAYDAY ADVANCE", credit=400.0),
        _txn(40, "PAYROLL X", credit=2000.0),
        _txn(26, "PAYROLL X", credit=2000.0),
        _txn(12, "PAYROLL X", credit=2000.0),
    ]
    analysis = analyze_statement(_accounts(txns), today=TODAY)
    joined = " ".join(analysis.summary_lines)
    assert "micro-lender" in joined.lower()
    assert "Cash Money" in joined


def test_payload_is_pii_free_and_json_safe():
    import json

    txns = [
        _txn(10, "MONEY MART - JOHN SMITH ACCT 998877", credit=500.0),
        _txn(40, "PAYROLL EMPLOYER 123", credit=2000.0),
    ]
    analysis = analyze_statement(_accounts(txns), today=TODAY)
    payload = analysis.to_payload()
    serialized = json.dumps(payload)  # must be JSON-safe for the event log
    # Only the canonical registry name appears — never the raw description PII.
    assert "JOHN SMITH" not in serialized
    assert "998877" not in serialized
    assert payload["micro_lender"]["lenders"] == ["Money Mart"]


def test_nsf_counted():
    txns = [
        _txn(20, "NSF FEE", debit=45.0),
        _txn(50, "OVERDRAFT HANDLING", debit=5.0),
    ]
    analysis = analyze_statement(_accounts(txns), today=TODAY)
    assert analysis.nsf_count_90d == 2


def test_empty_accounts():
    analysis = analyze_statement([], today=TODAY)
    assert isinstance(analysis, StatementAnalysis)
    assert analysis.txn_count == 0
    assert analysis.micro_lender.flag is False


def test_custom_classifier_seam():
    class AlwaysHousing:
        def classify(self, description, amount_cents, is_credit):
            return "payroll_income" if is_credit else "housing"

    txns = [_txn(5, "anything", debit=100.0), _txn(6, "anything", credit=100.0)]
    analysis = analyze_statement(_accounts(txns), today=TODAY, classifier=AlwaysHousing())
    assert analysis.expense_by_category_cents["housing"] == 10000
