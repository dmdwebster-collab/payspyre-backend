# The Escalation Boundary — what must stay human

**Layer 5 of the AI-CTO system** (see [`ai_cto_system.md`](./ai_cto_system.md)).

AI does the work across the whole platform — writing code, reviewing PRs, running
scans, drafting evidence. This document draws the **non-automatable line**: the
short list of decisions where a *named human* must personally sign off before the
change takes effect. Everything on this list is enforceable in two ways: by
[`.github/CODEOWNERS`](../../.github/CODEOWNERS) (the high-risk *code paths* can't
merge without a human Code Owner approval) and by process (the *decisions* below
require a named accountable person to say "yes" on the record).

> **Principle.** A human sets this line, and errs conservative. When it is unclear
> whether something belongs here, it belongs here until a human decides otherwise.
> Automation may *prepare* any of these (gather facts, draft the change, lay out
> options) — it may not *decide* or *ship* them.

---

## The "must stay human" list

### 1. Credit-model / decisioning deployment
Deploying or materially changing the credit decision engine (`flow_orchestrator`,
`flow_engine`, scoring/eligibility logic, cut-offs, adverse-action mapping) requires
a **named human Model Approver** sign-off — never an automated merge.

**Why.** [OSFI Guideline E-23 (Model Risk Management)](https://www.osfi-bsif.gc.ca/),
**effective 2027-05-01**, requires federally-relevant credit models to have a named,
accountable human **Model Approver** in the model lifecycle, and imposes **heightened
governance for AI/ML models** specifically (explainability, data quality, ongoing
monitoring, human oversight of the decision). A consumer-credit decision that
adversely affects a person is exactly the class of model E-23 governs. Even before
the in-force date we hold to this standard because bank/sponsor partners expect it.

### 2. Any new money flow or change to disbursement
Introducing a new way money moves, or changing how/when/how-much is disbursed,
refunded, debited, or collected (touching `loan_servicing`, `loan_lifecycle`,
disbursement/payment rails) requires human sign-off.

**Why.** A defect here moves real money incorrectly — over-disbursing, double-debiting,
mis-collecting. There is no clean undo for funds that have left an account, and errors
are directly financial and reputational. AI may implement the change; a human owns
the decision to turn it on.

### 3. Auth / identity / PII-storage architecture changes
Changes to authentication, identity, session/token handling, or **how PII is stored,
encrypted, or transmitted** (e.g. `sin_crypto`, KYC document storage, SIN handling)
require human sign-off.

**Why.** These are the controls a breach would exploit. Insurers require MFA; bank
partners and SOC 2 expect a deliberate, reviewed identity/encryption architecture.
An automated change that quietly weakens key handling or token scope is a worst-case
silent failure — it passes tests and ships a vulnerability.

### 4. Anything regulator-facing or that changes a regulated disclosure
Any change to a **regulated disclosure** — APR, cost of borrowing, total cost of
credit, the consent text in [`config/consent_text/`](../../config/consent_text/) —
or anything submitted to / representing the company to a regulator requires human
sign-off.

**Why.** Cost-of-borrowing and APR disclosures are legally prescribed (federal
Criminal Code interest cap + provincial consumer-credit disclosure rules). A wrong
APR or a weakened consent flow is a compliance violation, not a bug. The
[`loan_quote`](../../app/services/loan_quote.py) APR/cap path and the consent texts
are Code-Owner-gated for this reason.

### 5. Privacy-breach assessment + reporting decisions
Deciding whether a privacy incident is a reportable breach, and what/when/whom to
notify, is a **human** decision — never automated.

**Why.** [PIPEDA](https://www.priv.gc.ca/) requires reporting a breach of security
safeguards to the Privacy Commissioner **and** affected individuals when it creates a
**"real risk of significant harm."** That is a judgement call assigned to the named
privacy-accountable person (PIPEDA Schedule 1, cl. 4.1). Automation may detect and
assemble the facts; a human assesses risk and owns the reporting decision and its
clock.

### 6. Final sign-off before production go-live
The decision to put a new capability (especially anything in 1–5) **into production
where it touches real customers or real money** is a named human's call.

**Why.** Go-live is the moment risk becomes real. The accountable human reviews that
the deterministic gates passed, the adversarial review cleared, and the evidence
exists — then explicitly approves. The pipeline can *block*; only a human can *bless*.

---

## Who holds the pen

These map to the named roles in [`ai_cto_system.md`](./ai_cto_system.md):

| Decision | Accountable human |
|---|---|
| Credit-model / decisioning deploy (1) | **Model Approver** — both founders jointly (OSFI E-23) |
| New money flow / disbursement (2) | **Technical owner of record** (founder-engineer) |
| Auth / identity / PII architecture (3) | **Technical owner of record** |
| Regulated disclosure / regulator-facing (4) | **Privacy/Compliance accountable person** (domain co-founder) |
| Privacy-breach assessment + reporting (5) | **Privacy/Compliance accountable person** (PIPEDA-required) |
| Production go-live sign-off (6) | The relevant owner above for the change in question |

No law, bank/sponsor partner, or insurer we are aware of requires a **full-time CTO**
to hold these — they require a *named accountable human*, which these roles satisfy.

---

## How this is enforced

- **Code paths** → [`.github/CODEOWNERS`](../../.github/CODEOWNERS) + branch protection
  ("require review from Code Owners") block merges to the high-risk files.
- **Scheduled re-checks** → [`.github/workflows/scheduled-audit.yml`](../../.github/workflows/scheduled-audit.yml)
  files an issue if a gate regresses between PRs.
- **Decisions** → recorded sign-off (PR approval / decision log) by the named human
  above before go-live.
