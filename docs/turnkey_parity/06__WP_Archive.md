# Archive Workplace — Feature Inventory

Source video: `06__WP_Archive` (~3.5 min). Turnkey Lender UAT instance: `kelowna-dental-uat.turnkey-lender.com/App#/archive`, branded "PaySpyre Financial", footer version `2026 7.9.0.30`. Narrator: Dave.

The Archive is the terminal workplace: every non-active account/application (cancelled, rejected, written off, fully repaid, expired) lands here as a read-only historical record. Layout mirrors Origination/Underwriting/Servicing/Collection: search + filters + account list on the left, selected-account detail (loan header + tab strip) on the right.

## 1. Walkthrough with frame refs

- **f0001–f0004 (0:00–1:00)** — Archive queue loaded, record ID 5711 "John J Doe" selected, **Documents tab** open. Red status banner top-right: **"Loan offer expired"**. Left list shows a mix of statuses (red hourglass, trash icon, red X, green shield icons in the S column). Documents tab shows sub-tabs *System documents / Loan documents / Personal documents*; System documents lists **Loan agreement (On demand)**, **Terms & Conditions**, **Privacy policy** as document cards. Dave narrates what the Archive holds (cancelled / rejected / written-off / fully-repaid) and that the layout matches the other workplaces.
- **f0005 (~1:20)** — **Summary tab**: Contact Information, Loan details, Previous activity, Credit bureau check blocks (fields listed in §3). Dave notes Summary content depends on how far the record got: paid-off accounts look like Servicing; rejected applications look like Origination.
- **f0006 (~1:40)** — **Bank details tab**: empty, with an **"Add bank account"** button (present even in Archive). Dave: "Summary details is the same, so is bank details."
- **f0007 (~2:00)** — **Risk score tab**: full decision-engine snapshot — 8-step check pipeline (Application Check, Anti-fraud Check, Geolocation, Web Activity/Behavior Check, Cyber Security Check, Credit Bureau, ID Verification, Bank Account Check), Decision Summary (creditworthiness gauge = "Confident", System decision = Manual review, Underwriter decision = Approve by "David Willson", green **APPROVED** stamp, decision timestamps 2026-05-13 5:05 PM / 5:07 PM), and a Creditworthiness breakdown card grid (scorecard 585 gauge, anti-fraud business rules, geolocation auto-approve, cyber-security error card, bank account check, credit bureau check). Dave stresses this is frozen **as of the credit decision** — "It does not update if they do a new application… This is a historical record of why and how we made the decision."
- **f0008 (~2:20)** — **Payments tab**: amortization/repayment schedule with **Excel export** button. Row 1 status **Missed** (red highlight, 2026-06-01, $917.25 incl. $51 fee), remaining rows **Scheduled**; per-row Cash flow icon. Dave: payments are "same as to that particular application… just for these particular accounts."
- **f0009–f0010 (~2:40–3:00)** — **Actions tab**: shows **"No actions"** (no operations available on this archived record). Dave rattles off the remaining tabs — hardship, transactions/ledger, scheduled transactions, credit bureau, actions, workflow, promise to pay, flags — "all essentially the same as they would be in the other queues."
- **f0011 (~3:20)** — Cursor hovers the status icon on the selected list row while Dave explains the left-list columns (ID, name, amount, close date, assignee, status) and enumerates archive statuses: "offer expired, bank verification expired, cancelled, rejected, repaid, et cetera." Wrap-up: "That concludes the archive review."

## 2. Feature checklist

### What gets archived (per narration)
- Cancelled applications/accounts
- Rejected applications
- Written-off loans
- Fully repaid ("repaid as agreed") loans
- Expired records: **offer expired**, **bank verification expired**
- i.e., all closed applications AND closed loan accounts, with full detail retained

### Archive statuses observed (list "S" column + banner)
- Loan offer expired (red hourglass icon; banner on selected record)
- Bank verification expired
- Cancelled (red X icon)
- Rejected (trash-can style red icon)
- Repaid (green shield/check icon)
- "et cetera" — Dave implies more terminal statuses exist

### List / queue features (left panel)
- Free-text **Search** box + **Filters** toggle
- Filter dropdowns: **All time / All statuses / All branches / All loan types / All assignations / All vendors / All provinces**
- Sortable columns: **ID** (sorted), Name, Amount, Close date, **A** (assignee avatar), **S** (status icon)
- Pagination: page N **of 30**, **437 record(s)** total, first/prev/next/last controls

### Record-level actions
- **Unassign** button (with dropdown) in loan header — assignment can still be changed on archived records
- **Loan virtual date** (edit icon) in header — virtual-date/time-travel control still present
- **Add bank account** button on Bank details tab (present but pointless in archive context)
- **Excel export** of payment schedule (Payments tab)
- **Actions tab: "No actions"** — no operational actions (disburse, reschedule, etc.) are offered on this archived record; effectively read-only
- No explicit **restore/reactivate** function was shown or narrated in this video

### Retention / historical-record behavior
- Risk score, credit history, and payments are **frozen snapshots as of that application's credit decision**; they do not update with future applications by the same customer
- Archive is the system of record for "why and how we made the decision" (audit/adverse-action trail)
- All tabs from active workplaces are retained: Summary, Customer details, Bank details, Documents, Contacts, Risk score, Credit history, Payments, Rescheduling, Transactions (ledger), Scheduled transactions, Credit bureau, Actions, Workflow, Promise to pay, Flags, Map, Comments (Dave also says "Hardship would be the same")
- Summary content is polymorphic: servicing-grade detail for funded/repaid loans vs origination-grade detail for rejected applications

## 3. Data fields visible

### Loan header (selected record 5711, John J Doe)
- Photo/avatar, full name, ID 5711, province (British Columbia), "None" (segment/label), vendor/branch: **Kelowna Dental Centre / Dr. Michael Webster**
- Status banner: "Loan offer expired"
- Credit product: **BC4906-M13** (info tooltip)
- Repaid amount: $0.00 · Outstanding balance: $52,084.81
- Loan amount: $40,000.00 · Loan term: 60 months · First due date: 2026-06-01
- Monthly payment: – · Interest: 10.99% per year · Start date: 2026-05-15

### Summary tab
- Contact Information: Full name, Email (JohnDoe434@48934.kom), Main phone, Alternative phone, Province
- Loan details: Loan ID, Creation date (2026-05-13), Amount, Term, Interest, Installment ($867.25), System decision (Manual review), Credit risk (Low), **APR (11.03%)**
- Previous activity: Previous loans #, Previous offers #, Outstanding balance, Applied payment holidays, Late payments #, Max DPD, Days past due
- Credit bureau check: Status, Score, Total balance, Number of defaults, Number of bankruptcies, Bankruptcy date (all "–" here)

### Risk score tab
- Check pipeline steps: Application / Anti-fraud / Geolocation / Web Activity-Behavior / Cyber Security / Credit Bureau / ID Verification / Bank Account
- Decision Summary: creditworthiness gauge band ("Confident"), System decision, Underwriter decision + decision maker name + timestamps, APPROVED stamp
- Application check card: score **585** gauge (scale ~0–1000), Probability of default 0.58%, Risk level Low, Odds (Good:Bad) 160:1, Risk segment "Target client", Recommended decision text
- Anti-fraud card: business-rule counts (5 / 2); Geolocation card: Auto approve, rule count 2
- Cyber security card: threat-level error — "Something went wrong when we tried to pull the required information from the data provider. Please contact technical support."
- Bank account check card: "As soon as the bank account details are added, the System will score this data and display the results here."

### Payments tab
- Columns: #, Date, Total, Principal, Interest, Fee, Close date, Status, Cash flow
- Statuses: **Missed** (red row), **Scheduled**; monthly installments $867.25, missed row $917.25 with $51.00 fee; Principal rollup $0.00

### Documents tab
- Sub-tabs: System documents / Loan documents / Personal documents
- System documents: Loan agreement (On demand), Terms & Conditions, Privacy policy

### List columns
- ID, Name, Amount, Close date, Assignee avatar, Status icon; record count + pagination

## 4. Dave's editorial comments (verbatim-ish)

This video is pure documentation — **no "we need" must-haves and no "skip this" cuts**. His substantive points:

- "This workplace is where any accounts that are not active are housed… any that are cancelled, any that are rejected, any that are written off, as well as any that are just fully repaid, as agreed."
- "Essentially, these are either closed applications or closed accounts, and all of the information is housed here."
- "We've got a similar setup to the originations, underwriting, servicing, and collection [queues] — a search function and filter function, an account list on the left… the display of the account selection [on the right], with the standard loan header and… the different tabs below."
- "If it was a paid account, it would have the amount of information that would be in the servicing workplace. If it's just an application that was rejected, it would have the similar information that was in the originations workplace."
- Risk score: "same as of the credit decision at the time of that particular application or loan account. **It does not update if they do a new application or whatever. This is a historical record of why and how we made the decision.**"
- Payments: "again, same as to that particular application. It does not update… with future applications; this is just for these particular accounts."
- "Hardship would be the same, transactions or ledger would be the same, scheduled transactions, credit bureau actions, workflow, promise to pay, flags — all of these are essentially the same as they would be in the other queues."
- Left list: "the ID, the name, the amount, the close date, who the account is assigned to, and the status of the account — so why it's sitting there. In this case: offer expired, bank verification expired, cancelled, rejected, repaid, et cetera."
- "That concludes the archive review."

Implicit requirement for PaySpyre: closed/terminal records must retain the complete decision-time snapshot (risk score, credit-bureau pull, payment history, documents, workflow trail) immutably, with the same detail views as active records, filterable by terminal status.

## 5. Integrations referenced

No external vendors named aloud; integrations appear via the frozen risk-score pipeline and UI:

- **Credit bureau** pull (check step + Credit bureau tab + Summary bureau block — score, defaults, bankruptcies)
- **Bank account check / bank statement scoring** (check step; "bank verification expired" as an archive status implies a bank-verification provider, IBV/Flinks-equivalent)
- **Anti-fraud rules engine** (Turnkey decision-engine module)
- **Geolocation check** (auto-approve rules)
- **Web activity / behavior check** (device/behavioral signal module)
- **Cyber security check** — third-party "data provider" explicitly referenced in the error card ("tried to pull the required information from the data provider")
- **ID verification** (pipeline step)
- **Excel export** (payments schedule download)
- E-sign implied by "Loan agreement (On demand)" system document generation
