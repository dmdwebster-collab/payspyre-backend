# Dunning & Reminder Policy — Decisions Needed (for Dave)

**Status:** the notification automation engine is built and tested. The only thing
blocking the payment-reminder / overdue ("dunning") piece is **business policy** —
the questions below. Each has a recommended default so you can just confirm or
tweak; I'll wire whatever you pick.

**Context:** the system already sends approval emails automatically, and declined
applicants already get the adverse-action notice. This is purely about reminding
borrowers around their **payment due dates** once a loan is active.

These are policy choices, not technical ones — I'm not making them. Mark each
answer inline and send back.

---

## 1. Reminder cadence (before the due date)

When do we proactively remind a borrower that a payment is coming up?

| Option | What it means |
|---|---|
| **A (recommended)** | One reminder **3 days before** the due date |
| B | Two reminders: **5 days** and **1 day** before |
| C | One reminder **1 day before** only |
| D | No pre-due reminders (only act once overdue) |

**Your answer:** ____  (default: A)

*Affects:* how many reminder emails/texts go out per installment.

---

## 2. Overdue escalation schedule (after a missed payment)

Once a payment is late, how often do we follow up, and for how long?

| Option | Follow-ups sent at |
|---|---|
| **A (recommended)** | Day **+1**, **+7**, **+14** past due (3 notices, then stop and hand to manual collections) |
| B | Day **+1** and **+10** (2 notices) |
| C | Day **+3**, **+15**, **+30** |
| D | Day +1 only |

**Your answer:** ____  (default: A)

*Affects:* dunning frequency + when a loan stops auto-chasing and becomes a
manual collections case. Note: the loan is already auto-flagged `delinquent` by
the nightly aging job regardless of this choice — this is only about *messaging*.

---

## 3. Late-fee policy

Do we charge a late fee, and does the reminder mention it?

| Option | What it means |
|---|---|
| **A (recommended for launch)** | **No late fee.** Reminders omit any fee language. Simplest; revisit post-launch. |
| B | Flat fee (e.g. **$25**) applied at day +X. Reminders state the fee + when it applies. |
| C | Percentage fee (e.g. **5%** of the payment). |

**Your answer:** ____  (default: A)
**If B/C — amount and the day it applies:** ____

*Affects:* email/SMS copy **and** whether the loan ledger (`loan_servicing`) needs
to actually post a fee — option A is purely cosmetic; B/C is a real money change
that's a bigger build. Flag if you want fees, and confirm it's permitted under
the lending licence/agreement terms.

---

## 4. Channel policy

Which channels for which message?

| Message | Recommended | Alternatives |
|---|---|---|
| Pre-due reminder | **Email only** | Email + SMS |
| Overdue notices | **Email + SMS** | Email only |

**Your answer (reminders):** ____  (default: email only)
**Your answer (overdue):** ____  (default: email + SMS)

*Affects:* SMS has consent/opt-out obligations (we already honour STOP/opt-outs
automatically), and SMS costs per message. Email is free-ish and lower-risk.

---

## 5. Quiet hours / send time

What local time should reminders go out? (Avoids 3am texts.)

- **Recommended:** send the daily batch at **9:00 AM** in the borrower's timezone
  (or a single fixed time if we don't reliably have timezone — confirm: do we
  collect borrower timezone? If not, default to **Pacific** since we're BC-based).

**Your answer:** ____  (default: 9 AM, Pacific if timezone unknown)

---

## 6. "Pay now" link destination

Reminders include a **Make Payment** button. Where should it go?

- The borrower portal exists (magic-link login) but **"pay now" / online payment
  is not built yet** (it's on the borrower-portal backlog).
- **Recommended interim:** link to the borrower portal account page (where they can
  see the balance and contact support), and add real online payment when that
  feature ships.

**Your answer:** ____  (default: portal account page for now)

---

## Out of scope here (separate decisions, flagged for awareness)

- **Adverse-action legal wording** — the decline notice currently uses *US*
  ECOA/FCRA boilerplate but we're a *Canadian* lender. Counsel needs to confirm
  the correct BC/PIPEDA wording before launch. (Engineering note already in the
  code.)
- **"Under review" email** — needs a stated review turnaround SLA (e.g. "decision
  within 2 business days") before we can send an honest under-review email.
  **Your SLA:** ____ business days.

---

### Once you've answered

Send this back and I'll build the dunning job to match — it's a small, contained
piece on top of the engine that's already in place. Estimated ~2 days after the
answers land.
