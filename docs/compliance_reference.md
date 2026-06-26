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

## Consent model (researched 2026-06-26 — primary sources: OPC/PIPEDA, CRTC/CASL, BC BPCPA)
- **Express opt-in** is required for all sensitive data here (medical + financial + bureau +
  bank/income) — implied/bundled consent is not acceptable (OPC Meaningful Consent Guidelines;
  medical info "almost always sensitive", financial "generally extremely sensitive").
- **Granular & purpose-specific.** Integral underwriting purposes (PIPEDA processing, bureau
  pull, income verification, automated decisioning) MAY be required together as a condition of
  financing IF each is enumerated. Our required set (`get_required_consents`: id_verification,
  bank_verification, soft/hard_bureau_pull, automated_decision_making) is correct.
- **CASL marketing consent must be SEPARATE, OPTIONAL, pre-unchecked, and never a condition of
  service** (CRTC opt-in + PIPEDA anti-bundling). Implemented: `marketing_communications`
  consent purpose (registered, versioned, NOT in the required set) + an optional pre-unchecked
  opt-in on the disclosure page, recorded separately, non-blocking. Transactional/servicing
  messages don't need CASL consent.
- **Record each consent BEFORE the action it authorizes** (bureau-pull consent must exist before
  the pull — statutory in BC). Our flow records consents at the disclosure step before
  verifications run. `record_consent_grant` already stores purpose + version + IP + user-agent +
  timestamp via a `consent_granted` event — the auditable record the org must be able to produce.
- **Cost-of-borrowing disclosure**: the binding disclosure (APR, total cost, schedule) must be
  given before the earlier of entering the agreement or making any payment (BC BPCPA s.66(2)).
  An upstream "estimate / not an offer" disclaimer is fine (our terms page) — the FINAL binding
  disclosure must gate the e-sign/agreement step (downstream TODO).
- **KYC documents** (ID front/back, selfie): express consent/notice at the upload step naming the
  KYC vendor; encrypt at rest + in transit; least-privilege access + audit logging (PIPEDA P7);
  purpose-bound retention/auto-purge (P5), reconciled with any FINTRAC/PCMLTFA retention floor.

### Open questions flagged by the research (confirm with counsel before launch)
- **Quebec Law 25** imposes stricter, separately-governed consent (clear/free/informed/specific,
  separate request) + ADM notice & right to human review — the consolidated-consent UX must be
  revisited for QC residents.
- **Automated decision-making**: no explicit PIPEDA consent requirement was verified; Quebec Law 25
  s.12.1 DOES require ADM notice + human-review right. Confirm a distinct ADM disclosure for QC.
- **Province-by-province cost-of-credit disclosure timing** (AB/ON/QC) — only BC s.66 verified.
- **FINTRAC/PCMLTFA retention minimum** (commonly 5 yrs) may impose a floor that interacts with
  PIPEDA's "retain only as long as needed" — confirm whether PaySpyre is a reporting entity.
- Bill C-27 / CPPA could replace PIPEDA and change consent/record-keeping mechanics — verify status.

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
