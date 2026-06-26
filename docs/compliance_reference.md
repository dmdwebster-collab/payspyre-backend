# Compliance Reference (Canada)

Source documents (provided by Dave, 2026-06-25): *Canadian Alternative Lending
Compliance.pdf* and *PaySpyre Credit Application v1.00.pdf*. **Principle (Dave):
PaySpyre will always be 100% compliant with all laws and regulations — development
must adhere to this.** This file is an engineering-facing summary, not legal advice;
the source docs and counsel govern.

## APR / Cost of Borrowing — IMPLEMENTED
- The only allowable APR method in Canada is the **Cost of Borrowing Regulations
  (SOR/2001-104) s.3(1)**: `APR = (C / (T × P)) × 100` — C = interest + fees over the
  term, P = average principal outstanding per interest period (before that period's
  payment, s.3(2)(b)), T = term in years (s.3(2)(c)). s.4: with no cost of borrowing
  other than interest, APR = the annual interest rate.
- Implemented in `app/services/loan_quote.py::compute_apr_bps`. The s.3(2)(a) round-to-
  1/8% option is not applied (unrounded is also compliant). **Wants a final legal/QA
  pass against a reference before go-live.**

## Criminal rate of interest — GUARDRAIL ADDED (flag), enforcement TODO
- **Criminal Code s.347**: Bill C-47 lowered the criminal rate from 60% EAR to **35%
  APR**, in force 2026-01-01. `loan_quote.CRIMINAL_RATE_CAP_BPS = 3500` +
  `exceeds_criminal_rate()`; surfaced on the quote and the terms calculator blocks a
  selection at/over the cap. **TODO: hard enforcement at loan booking/decisioning.**

## Regulatory map (Tier 1 — must have)
1. Provincial consumer-protection legislation (every province).
2. Provincial cost-of-credit disclosure requirements.
3. Criminal Code s.347 interest review.
4. Privacy: PIPEDA (federal) / substantially-similar provincial acts (BC PIPA, Alberta
   PIPA, Quebec). Governs application data, ID docs, income/underwriting data.
5. Marketing: CASL (email/SMS consent + unsubscribe) + Competition Act (no misleading
   claims about rates/approval/savings).

Tier 2 (depends on model): collections rules (provincial collection-agency + consumer-
protection acts), credit-reporting rules (provincial consumer-reporting acts + privacy),
AML/KYC (PCMLTFA — best practice unless a regulated category), electronic contracting.

Excluded per Dave's assumptions: Payday Loans Acts, high-cost-credit licensing, Bank Act,
FINTRAC registration (unless the entity category applies).

Biggest operational risk areas (per the doc): **APR calculation, fees, disclosure
language, consent flows, underwriting-data use, collections communications.**

## Credit application — v1.00 field set (IMPLEMENTED as the manual base)
Personal (first/middle/last name, DOB, email, marital status, dependents, citizenship,
education); Address (residence years/months, street, apt, city, province, postal,
residential status); Additional (SIN, ID-verification type, province of issue, main/alt
phone, car owner, other monthly expenses); Employment/primary income (income type, net
monthly income, next pay date, pay frequency, employer, job title, hire date, work phone
+ ext). Implemented in `ManualApplicationBody`. **Deferred:** co-borrower, document
uploads (ID front/back, photo — need file-storage infra).
