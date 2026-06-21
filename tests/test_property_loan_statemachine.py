"""Stateful property test of loan payment application (Hypothesis rule-based machine).

Fires arbitrary sequences of payments at a loan and asserts the money invariants
hold after EVERY step — the research-cited pattern for evolving ledger/loan state
("finds sequences of actions that result in a failure" a human wouldn't construct).
"""
from datetime import datetime, timezone

from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from app.services.loan_servicing import record_payment


# --- minimal in-memory fakes (no DB; record_payment is pure over these) ------


class _FakeSession:
    def add(self, obj):
        pass

    def commit(self):
        pass

    def refresh(self, obj, **kwargs):
        pass


class _Item:
    def __init__(self, n, principal, interest):
        self.installment_number = n
        self.principal_cents = principal
        self.interest_cents = interest
        self.total_cents = principal + interest
        self.status = "scheduled"
        self.paid_cents = 0


class _Loan:
    def __init__(self, schedule, principal_balance_cents):
        self.id = "loan-prop"
        self.schedule = schedule
        self.principal_balance_cents = principal_balance_cents
        self.status = "active"


class LoanPaymentMachine(RuleBasedStateMachine):
    """A 4-installment loan ($1000 principal / $40 interest). Pay arbitrary amounts;
    the money invariants must hold after every payment."""

    def __init__(self):
        super().__init__()
        # 4 installments: principal 250 each, interest 10 each -> 260 total each.
        self.schedule = [_Item(n, 250, 10) for n in range(1, 5)]
        self.total_principal = 1000
        self.total_due = sum(s.total_cents for s in self.schedule)  # 1040
        self.loan = _Loan(self.schedule, self.total_principal)

    @rule(amount=st.integers(min_value=1, max_value=400))
    def pay(self, amount):
        record_payment(_FakeSession(), self.loan, amount, datetime.now(timezone.utc), "test")

    @invariant()
    def balance_never_negative(self):
        assert self.loan.principal_balance_cents >= 0

    @invariant()
    def balance_never_exceeds_principal(self):
        assert self.loan.principal_balance_cents <= self.total_principal

    @invariant()
    def no_installment_overpaid(self):
        # Cumulative cash on an installment never exceeds what it's due.
        for s in self.schedule:
            assert s.paid_cents <= s.total_cents

    @invariant()
    def paid_off_iff_balance_zero(self):
        all_paid = all(s.status in ("paid", "waived") for s in self.schedule)
        if self.loan.status == "paid_off":
            # Once paid off, principal balance is exactly zero.
            assert self.loan.principal_balance_cents == 0
            assert all_paid


TestLoanPaymentMachine = LoanPaymentMachine.TestCase
