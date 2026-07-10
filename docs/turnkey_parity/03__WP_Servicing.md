# Servicing Workplace — Feature Inventory

Source video: `03__WP_Servicing` (~31 min, 94 frames @ 20s). Narrator: Dave. Environment: Turnkey Lender UAT (`kelowna-dental-uat.turnkey-lender.com/App#/servicing`), branded "PaySpyre Financial", app version 7.9.0.30.

The Servicing workplace houses all loan accounts that are **Active (performing)** or **Approved (awaiting activation)**. It is the post-origination loan-management (LMS) console: payment schedules, actual-transaction ledger, payout quotes, hardship/rescheduling, promises to pay, and the frozen record of the original underwriting decision.

---

## 1. Screen-by-screen walkthrough (chronological, with frame refs)

**f0001–f0002 (0:00–0:40) — Servicing landing / Summary tab (Active account "Raleigh Bailey", loan 5713).**
Top nav: Origination, Risk Evaluation, Underwriting, Servicing (selected), Collection, Archive, Reports, Settings, Tools. Left panel: search box, Filters button, account list (ID, Name, Disbursed date, Amount disbursed, "A" avatar column, "S" status-icon column: green check / red exclamation / calendar icons), pagination ("Page 1 of 2", "30 record(s)"). Right panel: header with photo, name, loan ID 5713, province (British Columbia), "None", vendor/provider "Rock Dental / Dr. Yusef Khademba shi", status chip "Active" (green). Action buttons: Unassign (with dropdown caret), Payment (dropdown), Write off (red). Right side: "Loan virtual date" (editable icon). Tab strip (Active account): Summary, Customer details, Bank details, Payments, Rescheduling, Transactions, Scheduled transactions, Contacts, Risk score, Credit bureau, Documents, Credit history, Promise to pay, Workflow, Flags, Bank statements, Map, Comments. Summary tab shows Contact Information, Loan details, **Loan performance** (new in Servicing), Previous activity, Credit bureau check.

**f0003–f0007 (0:40–2:20) — Approved (not yet active) account "Howard Pacocha", loan 5524.**
Status chip "Approved". Action buttons differ: **Assign to me** (then Unassign), **Activate loan**, **Cancel** (red). Pink alerts bar: "5524 Flag: Second payment missed | Comment:" / "5524 Flag: First payment missed | Comment:" with dismiss ×. Tab strip for Approved account includes **Initial schedule** (instead of Payments/Promise-to-pay ordering of active): Summary, Customer details, Bank details, Initial schedule, Rescheduling, Transactions, Contacts, Risk score, Credit bureau, Documents, Credit history, Promise to pay, Workflow, Flags, Bank statements, Map, Comments. Dave explains the Approved→Active flow: assign to self → "Loan activation" → enter required info → OK → account activates, payments get scheduled. f0006 shows Filters expanded: **All time, All statuses, All branches, All loan types, All assignations, All vendors, All provinces** dropdowns; expanded header shows Credit product code "BC4906-K02", Start date, Repaid amount, Loan amount. Maximum DPD shows 392 on this stale approved account.

**f0008–f0009 (2:20–3:00) — Back to Raleigh Bailey Summary tab, scrolled.**
Sticky mini-header strip: Next auto payment 2026-07-10, Total amount due $262.84, Add-on fee balance $0.00. Loan performance section detail (see fields below). Footer: ISO badge + "PaySpyre Financial - 2026 7.9.0.30".

**f0010 (3:00–3:20) — Bank details tab.**
"Add bank account" button. Bank accounts table: Name, Account number, Routing number, Institution number, Account holder, Type, Source (FlinksService), Actions; a star/default indicator per row (filled star = default payment method, hollow star = not default).

**f0011–f0020 (3:20–6:40) — Payments tab.**
Expanded header (full): Credit product BC5055-B07, Loan amount $16,000.00, Contract date 2026-06-16, Outstanding principal $14,509.62, Interest due $74.25, Installment payment $262.84, Next auto payment 2026-07-10, Next payment due $262.84, Past due amount $0.00, Total amount due $262.84, Account due as of 2026-09-18, Current DPD 0, Amount to move $142.28, Add-on fee balance $0.00. Payments-tab toolbar buttons: **Restructure**, **History**, **Disable Auto-Charges** (with dropdown caret), **Payout amount**. Export button "Excel 2007+" with format dropdown. "Principal: $14,342.28 (i)". Schedule grid (the as-agreed amortization view): #, Date, Total, Principal, Interest, Fee (expandable dropdown), Close date, Status (✔ Paid on time / ⏱ Scheduled), Cash flow (icon per row). Dave: this tab is "what was supposed to happen", not what did; daily-simple-interest engine must recalc off actuals. f0021 shows the "set default bank account" confirm dialog: "Please confirm you are going to set this bank account as default for payments" (Confirm / Cancel) — set-default lives in Bank details.

**f0022–f0025 (7:00–8:20) — Payout amount calculator (modal "Payout calculation").**
Payout date* field with calendar picker (up to ~30 days ahead). Read-only computed panel: Principal balance $14,509.62, Accrued interest $74.25, Future interest $0.00→$87.75, Fees $1.00, Per diem interest $6.75, Future days 0→13, **Total payout amount** $14,584.87→$14,672.62 when date moved 2026-07-07→2026-07-20. Close button. Dave: assumes no intervening payments received; pending payout must NOT suspend scheduled payments; needs disclaimer wording; borrower should only be able to *request* a payout figure (human follows up — payout requests often signal bankruptcy filing); back-end staff need the calculator itself.

**f0026–f0031 (8:20–10:20) — Payments tab continued.**
Discussion of Disable Auto-Charges / re-enable (dropdown caret on the button implies enable/disable options): used when payments return "account closed" / "payer deceased" / blanket stop-payment, to avoid processor dishonour fees; re-enable after new default bank account set.

**f0032–f0043 (10:20–14:20) — Rescheduling tab.**
Shows only "No data to display" (feature unused). Dave's extended hardship monologue (see §5): this tab should be relabeled **Hardship** and hold installment deferments, refinance-balance-only, payment-frequency change, due-date change, temporary **Adjustment of Terms** (interest set aside into non-accruing add-on fees, term extended past product max, temporary rate reduction, auto snap-back at expiry), all gated on borrower e-signed amendment; Write off should move here and be permission-gated.

**f0044–f0047 (14:20–15:40) — Transactions tab (the actual ledger).**
Toolbar: Excel 2007+ export + format dropdown. Grid: Date, Reference #, Transaction, Amount, Method, Operator, Comments/Error, Actions (expand chevron; one row has a red stop/void icon). Rows: 2026-06-01 2:51 PM "Tx Completed" Loan disbursement $16,000.00 Virtual (David Wilson); two 2026-06-01 2:52 PM Automatic charge rows $297.84 and $1,000.00 Direct bank transfer (Raleigh Bailey); 2026-06-26 12:27 PM Automatic charge $297.84 Direct bank transfer (System). Expanded row detail: Creation date, Repayment method "FlinksCapital, 77777, 1111000", Service: **PaymentServiceSimulator**, Repayment mode: Regular payment, Principal: $297.84. Dave: prefers the term **ledger**; first transaction is the loan distribution; payment "modes" accomplish different tasks (covered in a later settings video).

**f0048–f0052 (15:40–17:20) — Payment header button dropdown.**
Options: **Manual payment** and **Charge payment**. Manual = record money already received outside the platform (vendor-location cash/credit-card payment, vendor accommodation adjustments — reduce amount owed when treatment not completed); Charge = initiate a new debit against the borrower's bank account.

**f0053–f0079 (17:20–26:20) — "Submit repayment" modal (manual payment form).**
Fields: Repayment mode* (dropdown — options per Dave: Regular payment, Add-on payment, Special payment, Payoff), Type* (dropdown — per Dave: Cash, Check, Bank transfer/Electronic transfer, Credit card, Adjustment), Reference #* (with info tooltip), Date* (defaults 2026-07-07, calendar), Transaction amount* ($ 262.84 pre-filled), Comments, OK / Cancel. Validation states shown: "Repayment mode is required.", "Type is required.", "Reference # is required." (dropdowns wouldn't render options during recording). Dave specs: Regular = interest first, then principal, then scheduled fees; Add-on targets the non-interest-accruing add-on fee bucket (e.g., $45 NSF); Special = 100% to principal bypassing interest; Payoff = closes account, fixed non-editable amount (all principal + add-on fees + installment fees + accrued interest as of this moment). Type should drive payment-mix metrics/reconciliation. Reference # should auto-populate as unique `vendorID-loanID-transaction#`, each component filterable. Date must split into **effective date** vs **processing date** (backdating limited: not past prior transactions, not into previous months, permission-gated). Write-off must NOT live in the loan header — hardship queue only, user-defined availability.

**f0074–f0080 (24:20–26:40) — Scheduled transactions tab.**
Sections: **Custom transactions** (grid: #, Status, Effective date, Amount, User, Actions; green + add button) and **Suspended transactions** (grid: #, Status, Start date, End date, User, Actions; red + add button). f0074–f0076: **Suspend transaction** modal — Payment date (multi-select dropdown "None selected" listing upcoming scheduled dates e.g. ☐ 2026-07-10), Transaction suspension Start date* / End date*, Comment* ("Borrower requested suspend payment and process on 07-15-202…"), Suspend / Close buttons. f0077–f0078: **Add transaction** modal — Date* (2026-07-15), Amount* ($500), Repayment mode* (dropdown; selecting "Regular payment" shows helper "Regular payments (off of due dates)"), Comment* ("Borrower request, authorization obtained"), Add / Close. f0079–f0080: tab now shows Custom transaction row (2026-07-15, $500.00, David Wilson) and Suspended transaction row (2026-07-10 → 2026-07-10, David Wilson), each with edit / delete / history action icons; pink banner appears: "Transactions suspended: From 2026-07-10 To 2026-07-10" with dismiss ×.

**f0081 (26:40) — Transaction change history modal.**
Columns: Action date, Effective date, Action ("Transaction created"), Amount ($500.00), Comment ("Borrower request, authorization obtained"), User (David Wilson). Close.

**f0082 (27:00) — Contacts tab.**
Buttons: Send Email, Send SMS, + Log message. Filters: All time, All statuses; toggle "Show system messages". Grid: Date, User, Subject/Purpose, Method (Email / Dashboard notification), Result, Status (✔), Comment, Actions (detail icon). Visible notification history: "7 day(s) before payment", "Payment Reminder: Autopay in 7 Days", "Payment Received", "2 day(s) before payment", "Payment Reminder: Due in less than 48 hours", "Loan disbursement", "Rock Dental Loan Approved & Activated", "Loan Agreement Signed", "Approved", "Loan Offer Accepted - Signing Required", "Loan offer confirmation", "Pre-Approved Loan Offer - Action Required". Sortable by time and status.

**f0083–f0086 (27:20–28:40) — Risk score tab (frozen underwriting record).**
Check pipeline across top with green checks: Application Check, Anti-fraud Check, Geolocation, Web Activity/Behavior Check, Cyber Security Check, Credit Bureau, ID Verification, Bank Account Check. Decision Summary: Creditworthiness gauge ("Good"), System decision: Manual review, Underwriter decision: Approve, Decision maker: David Wilson, dates, "Approved" stamp. Creditworthiness breakdown cards: Application check (Scoring, Manual review, gauge 522, Probability of default 1.9%, Risk level Medium, Odds (Good:Bad) 51:1, Risk segment "Acceptable client", Recommended decision text), Anti-fraud Check (Business rules, Auto approve, 7), Geolocation (Business rules, Auto approve, 2), Credit bureau check (Scoring, gauge 2), Cyber security check (Threat level, error: "Something went wrong when we tried to pull the required information from the data provider. Please, contact technical support."), Web Activity (Business rules, Auto approve, 5), Bank account check (Scoring, Manual review, gauge 330, Probability of default 12%, Risk level Highest, Odds 7:1, Risk segment "Strongly Restricted client", recommendation "detailed client check"). Business rules section: filter "All matched", Expand all / Collapse all; groups: Application check rules (expanded: Result / Name / Comment — "Citizenship (i) — The borrower is Non-Resident"), Bank provider rules, Credit bureau rules. Rule-count chips (matched/warn counts: 11|1, 6|2, 5, 14|3). Dave: this must be **hard-coded immutable** — a snapshot of decision-time data (e.g., later bank re-verification must not change the recorded bank score); used for post-mortems on bad loans (e.g., non-resident skipped town).

**f0088–f0090 (29:00–29:40) — Credit bureau tab.**
Buttons: **Hard pull**, **Soft pull**; "Available reports:" dropdown ("2026-06-01 2:49 PM - Hard"). Summary: Full name, Score 2, Check date, Status Hit, Number of defaults 0, Number of bankruptcies 0, Age 1970, Active accounts 0, Thin file No, Inquiries in the last 6 months 2, Total balance $0.00, OFAC Alert No. Liabilities' overview table: Type (Credit card, Mortgages, Auto loan, Payday, Other, Total) × Open accounts / Max DPD / Total balance. "Characteristics" section below. Dave: one report should be labeled "used in underwriting"; new pulls (e.g., hard pull when hunting a delinquent) create new entries to diff (delinquency up? inquiries up? bankruptcy? new public records? new fraud alert?).

**f0091–f0092 (30:00–30:20) — Promise to pay tab.**
"+ Add promise to pay" button; "No data to display" initially. f0093 modal **Promise to pay**: Bad debt ($ 0), Promise to pay amount* ($ 500), Promise to pay date* (2026-07-15, calendar), Comment* ("Rescheduled payment from July 10th to July a…"), toggle "**Do not accrue late fees and late interest**", Save changes / Close.

**f0093–f0094 (30:20–31:20) — PTP saved; Flags tab.**
After save: new pink alert "5713 Flag: Promise to pay | Comment: Rescheduled payment from July 10th to July at borrower request"; PTP grid: Start date, PTP date, PTP amount, Comments, User, Paid date, Cancel date, Actions (edit / delete). Borrower is notified automatically. Flags tab (f0094): **Customer flags** (Enable flag + Flags history buttons, "No flags") and **Loan flags** (Enable flag, Flags history; grid: Flag name "Promise to pay", Enablement date 2026-07-07, Initiated by System, Expiry date, Actions: view/edit/delete). Dave closes: Workflow ("flow"), Flags, Bank statements, Map, Comments tabs are the same as prior workplaces; review ends.

---

## 2. Complete feature checklist

### Workplace shell
- [ ] Top-level nav: Origination, Risk Evaluation, Underwriting, Servicing, Collection, Archive, Reports, Settings, Tools
- [ ] Servicing queue = accounts in **Active** or **Approved** status only
- [ ] Left list: search box; Filters toggle exposing dropdowns: All time, All statuses, All branches, All loan types, All assignations, All vendors, All provinces
- [ ] List columns: ID (sortable), Name, Disbursed date, Amount disbursed, avatar, status icon (paid-OK check / problem exclamation / scheduled calendar)
- [ ] Pagination (first/prev/page N of M/next/last) + record count
- [ ] Account header: photo, name, loan ID, province, vendor/provider ("Rock Dental / Dr. Yusef Khademba shi"), status chip (Active green / Approved)
- [ ] Expandable header (chevron) with full loan financial snapshot (see §3)
- [ ] Alerts bar (pink) with per-account flag messages, dismissible ×
- [ ] "Loan virtual date" control (test/time-travel utility)
- [ ] Per-status action buttons: Approved → Assign to me / Activate loan / Cancel; Active → Unassign (dropdown), Payment (dropdown: Manual payment / Charge payment), Write off
- [ ] Assignment model: must assign account to self before activation

### Summary tab
- [ ] Contact Information block
- [ ] Loan details block
- [ ] **Loan performance** block (servicing-only addition): principal/interest/fees paid, triggered fees (NSF etc.), total repayment, max DPD, # late payments, # NSF payments, installments owed/paid/due
- [ ] Previous activity block
- [ ] Credit bureau check mini-block

### Bank details tab
- [ ] Bank accounts table (Name, Account #, Routing #, Institution #, Holder, Type, Source, Actions)
- [ ] Add bank account
- [ ] Star toggle = set default payment method, with confirm dialog ("set this bank account as default for payments")
- [ ] Source shows aggregator origin (FlinksService)

### Payments tab (as-scheduled view)
- [ ] Amortization schedule grid: #, Date, Total, Principal, Interest, Fee (expandable), Close date, Status (Paid on time / Scheduled), per-row Cash flow detail
- [ ] Restructure button
- [ ] History button
- [ ] **Disable Auto-Charges / re-enable** (dropdown) — kill switch for scheduled debits
- [ ] **Payout amount** button → Payout calculation modal (date up to 30 days out; principal, accrued interest, future interest, fees, per-diem, future days, total payout; recalculates on date change; does NOT assume interim payments)
- [ ] Excel export with format selector
- [ ] Principal running total display
- [ ] Requirement (Dave): calculation engine driven by **what actually happened** (daily simple interest; late payment = extra per-diem interest), schedule is only the default plan

### Rescheduling tab (to become "Hardship")
- [ ] Present but unused ("No data to display")
- [ ] Required replacement scope: installment deferment (move installment to end of contract; advances "due as of" date so account not shown in arrears), refinance balance only, change payment frequency, change due dates, temporary Adjustment of Terms (park accrued interest into non-accruing add-on fees; extend term beyond product max; temporary rate reduction; expiry date with automatic snap-back), each requiring borrower-signed amendment before taking effect
- [ ] Write-off relocated here; whole tab permission-gated (junior staff excluded)

### Transactions tab (ledger of actuals)
- [ ] Grid: Date, Reference #, Transaction (Loan disbursement / Automatic charge / …), Amount, Method (Virtual / Direct bank transfer), Operator (user or System), Comments/Error, Actions
- [ ] Expandable row detail: creation date, repayment method (institution, transit, account), service (PaymentServiceSimulator), repayment mode, principal split
- [ ] Void/stop indicator on a transaction row
- [ ] Excel export
- [ ] First ledger entry = loan disbursement
- [ ] Requirement: treat as immutable ledger; reference # auto-generated unique `vendor-loan-txn#`, each part filterable; two dates (effective + processing) with permission-limited, bounded backdating

### Manual/Charge payment (Submit repayment modal)
- [ ] Payment dropdown in header: Manual payment (record externally received funds / adjustments) vs Charge payment (initiate new bank debit)
- [ ] Fields: Repayment mode*, Type*, Reference #* (tooltip), Date* (calendar), Transaction amount* (prefilled with installment), Comments; OK/Cancel; required-field validation messages
- [ ] Repayment modes: Regular (interest→principal→fees), Add-on (targets non-accruing fee bucket), Special (100% principal), Payoff (system-computed, non-editable, closes account)
- [ ] Types: Cash, Check, Bank/Electronic transfer, Credit card, Adjustment — each recorded for payment-mix metrics & reconciliation

### Scheduled transactions tab
- [ ] Custom transactions section: add (+), grid (#, Status, Effective date, Amount, User, Actions: edit/delete/history)
- [ ] Suspended transactions section: add (+), grid (#, Status, Start date, End date, User, Actions)
- [ ] Suspend transaction modal: multi-select of upcoming payment dates, suspension start/end dates, mandatory comment → suspends only the selected scheduled transaction(s)
- [ ] Add transaction modal: date, amount, repayment mode (with helper text "Regular payments (off of due dates)"), mandatory comment
- [ ] Use cases: one-off due-date move (suspend 10th, custom on 15th); pull a missed installment to a promised future date; edit/delete custom transactions
- [ ] Transaction change history modal (audit: action date, effective date, action, amount, comment, user)
- [ ] Banner alert on account when transactions are suspended (with date range)

### Contacts tab
- [ ] Log of all outbound comms: Date, User (System), Subject/Purpose, Method (Email / Dashboard notification / SMS), Result, Status, Comment, per-row detail
- [ ] Send Email / Send SMS / Log message (manual note) actions
- [ ] Filters: time, status; "Show system messages" toggle; sortable
- [ ] Automated lifecycle notifications evidenced: pre-payment reminders (7-day, 48-hour/2-day), autopay-in-7-days, payment received, loan disbursement, approved & activated, agreement signed, offer accepted/signing required, offer confirmation, pre-approved offer action required

### Risk score tab (decision snapshot)
- [ ] Check pipeline: Application, Anti-fraud, Geolocation, Web Activity/Behavior, Cyber Security, Credit Bureau, ID Verification, Bank Account
- [ ] Decision Summary: creditworthiness gauge, system decision (Manual review), underwriter decision (Approve), decision maker, timestamps, Approved stamp
- [ ] Per-check scorecards: score gauges, probability of default, risk level, odds, risk segment, recommended decision, auto-approve vs manual-review labels, business-rule match counts, provider-error surface
- [ ] Business rules drill-down (Application check rules / Bank provider rules / Credit bureau rules; matched-rule filter; expand/collapse; rule rows: Result, Name, Comment — e.g., Citizenship: Non-Resident)
- [ ] Requirement: **immutable** — never updated by later account changes (new bank verification must not overwrite decision-time bank score)

### Credit bureau tab
- [ ] Hard pull / Soft pull buttons (on-demand re-pull, e.g., locating delinquent borrower)
- [ ] Available reports dropdown (timestamped, labeled Hard/Soft) — multiple stored reports; requirement: label which report underwrote the loan; diff across reports (delinquency, inquiries, bankruptcy, public records, fraud alerts)
- [ ] Report render: Summary (score, hit status, defaults, bankruptcies, age, active accounts, thin file, inquiries 6-mo, total balance, OFAC alert), Liabilities overview by type (open accounts / max DPD / balance), Characteristics section

### Promise to pay tab
- [ ] Add promise to pay modal: Bad debt $, PTP amount*, PTP date*, Comment*, toggle "Do not accrue late fees and late interest", Save/Close
- [ ] PTP grid: Start date, PTP date, PTP amount, Comments, User, Paid date, Cancel date, edit/delete
- [ ] Saving raises a "Promise to pay" loan flag + alert banner + borrower notification
- [ ] Used with rescheduled payments and heavily in the collections queue as a record of borrower promises

### Flags tab
- [ ] Customer flags section (Enable flag, Flags history)
- [ ] Loan flags section (Enable flag, Flags history; grid: Flag name, Enablement date, Initiated by, Expiry date, actions)
- [ ] System-initiated flags (Promise to pay, payment-missed flags feeding alerts bar)

### Other tabs (unchanged from other workplaces per Dave)
- [ ] Customer details, Documents, Credit history, Workflow (status-stage flow), Bank statements, Map, Comments
- [ ] Initial schedule tab (Approved accounts only)

---

## 3. Data fields visible (by screen)

**Account list:** ID; Name; Disbursed; Amount disbursed; status icons.

**Header (collapsed):** Name; loan ID; province; secondary tag ("None"); Vendor/Provider; Status (Active/Approved).
**Header (expanded):** Credit product (BC5055-B07 / BC4906-K02, with tooltip); Loan amount; Contract date; Start date; Repaid amount; Outstanding principal; Interest due; Installment payment; Next auto payment; Next payment due; Past due amount; Total amount due; Account due as of (date); Current DPD; Amount to move; Add-on fee balance; Loan virtual date.

**Summary — Contact Information:** Full name; Email; Main phone; Province; Alternative phone.
**Summary — Loan details:** Loan ID; Creation date; Amount; Term (36 months); Interest (16.99% / 23.99% per year); Contract date; Interest start date; Next payment date; Installment; System decision (Manual review); Credit risk (Low/Medium); APR (17.37% / 24.77%); Applied payment holidays.
**Summary — Loan performance:** Principal paid; Interest paid; Fees paid; Triggered fees paid; Total repayment; Maximum DPD (i); # of Late payments; # of NSF payments; Installments owed (1.00/$261.84); Installments paid (5.96/$1,559.68); Installments due (0.00/$0.00).
**Summary — Previous activity:** Previous loans #; Previous offers #; Outstanding balance; Applied payment holidays; Late payments #; Max DPD; Days past due.
**Summary — Credit bureau check:** Status (Hit); Score; Total balance; Number of defaults; Number of bankruptcies; Bankruptcy date.

**Bank details:** Name (FlinksCapital); Account number; Routing number; Institution number; Account holder; Type; Source (FlinksService); default-star; Actions.

**Payments grid:** #; Date; Total; Principal; Interest; Fee; Close date; Status; Cash flow. Toolbar principal total.
**Payout calculation modal:** Payout date*; Principal balance; Accrued interest; Future interest; Fees; Per diem interest; Future days; Total payout amount.

**Transactions grid:** Date(+time); Reference #; Transaction type; Amount; Method; Operator; Comments/Error; expanded: Creation date; Repayment method (bank triplet); Service; Repayment mode; Principal.

**Submit repayment modal:** Repayment mode*; Type*; Reference #*; Date*; Transaction amount*; Comments.

**Suspend transaction modal:** Payment date (multi-select of scheduled dates); Start date*; End date*; Comment*.
**Add transaction modal:** Date*; Amount*; Repayment mode*; Comment*.
**Scheduled transactions grids:** Custom (#, Status, Effective date, Amount, User, Actions); Suspended (#, Status, Start date, End date, User, Actions).
**Transaction change history:** Action date; Effective date; Action; Amount; Comment; User.

**Contacts grid:** Date; User; Subject/Purpose; Method; Result; Status; Comment; Actions; filters (time, statuses); Show system messages toggle.

**Risk score:** check names ×8; Decision Summary (Creditworthiness gauge label, System decision, Underwriter decision, Decision maker, dates); per-card: score value; Probability of default; Risk level; Odds (Good:Bad); Risk segment; Recommended decision; Business-rule counts; rule rows (Result, Name, Comment).

**Credit bureau:** Available reports (timestamp + Hard/Soft); Summary (Full name; Score; Check date; Status; Number of defaults; Number of bankruptcies; Age; Active accounts; Thin file; Inquiries in the last 6 months; Total balance; OFAC Alert); Liabilities table (Credit card / Mortgages / Auto loan / Payday / Other / Total × Open accounts / Max DPD / Total balance); Characteristics.

**Promise to pay modal:** Bad debt; Promise to pay amount*; Promise to pay date*; Comment*; "Do not accrue late fees and late interest" toggle.
**PTP grid:** Start date; PTP date; PTP amount; Comments; User; Paid date; Cancel date.

**Flags:** Flag name; Enablement date; Initiated by; Expiry date.

---

## 4. Workflow / state machine

**Statuses in Servicing:** `Approved` (awaiting activation) and `Active` (performing). Other statuses live in other workplaces (Origination/Underwriting upstream; Collection for delinquent; Archive for closed).

**Approved → Active:** operator assigns account to self → clicks "Activate loan" → fills required activation info → OK → account becomes Active; payment schedule is generated; auto-charges begin processing. `Cancel` available while Approved (terminates without activation).

**Active account lifecycle mechanics:**
- Scheduled auto-charges fire per amortization schedule (Payments tab = plan; Transactions tab = actuals).
- Daily simple interest: a late actual payment accrues per-diem interest for the extra days; engine must always compute from actual events, never force actuals into the original amortization.
- Late/NSF events generate **triggered fees** into an **add-on fee balance** that does not accrue interest; targeted by Add-on repayment mode.
- Auto-charges can be disabled (dead account codes, blanket stop payments) and re-enabled (e.g., new default bank account) — Disable Auto-Charges control.
- Scheduled-transaction surgery: suspend individual scheduled payment(s) (date range + comment) → create custom transaction(s) on new date/amount/mode → optionally attach Promise to pay (flag + borrower notification + optional "no late fees/late interest" waiver). Custom transactions editable/deletable with full change history.
- Payment application order (Regular mode): interest → principal → scheduled fees. Special mode: principal only. Payoff mode: computes fixed total (principal + add-on fees + installment fees + accrued interest) and closes the account.
- Payout quote (no state change): date-forward calculation up to 30 days; pending payout does NOT suspend scheduled payments; interim payments adjust the figure.
- Write off: terminal treatment, to be restricted to hardship queue with permissions.
- Hardship (to build): deferment moves an installment to contract end and rolls "account due as of" forward one installment (keeps account out of arrears); Adjustment of Terms = park interest in add-on bucket + extend term + reduce rate, all temporary with expiry snap-back to original contract terms; every hardship change requires a borrower-signed amendment BEFORE back-end change.
- Flags: system raises loan flags (payment missed, promise to pay) that surface in the alerts bar; flags have enablement/expiry dates and history.
- Workflow tab records progression through status stages (same as other workplaces).

---

## 5. Dave's editorial comments (verbatim-ish, in order)

1. "In order to move the approved status, we would first assign it to ourselves, and then we would click ["Loan activation"], put in the required information, hit OK, and that would activate the account… payments would be scheduled."
2. "With daily simple interest, it's not necessarily what was scheduled that matters, it's what actually occurred… **the system needs to have a calculation engine that looks at what actually happened** to calculate the interest and the fees… this payments section is kind of like the defaults — this is what was supposed to have happened… not necessarily this is what happened."
3. "This would be where we could **disable automatic payments or auto charges**… key if somebody has returned a payment with a code account closed or payer deceased… continuing to try to pull is just going to incur dishonored or failed transaction fees from our payment processor. There's no point."
4. "We could also… re-enable them once, for example, we got new bank account information… **we can set whatever bank account we want as the default payment method**" (Bank details).
5. Payout calculator: "can be used up to 30 days into the future… it would NOT automatically assume that payments are received… we would then have to have in the notification something along the lines of **'pending payout does not suspend future scheduled payments until the payout is processed'**."
6. "**This particular function typically is handled by a human** but we do want to have this function somewhat available to individuals to request. Unsure if we want it right on their dashboard or if they just submit a request… **sometimes a payout amount is an indicator they're looking to file for bankruptcy**… probably something we would want them to be able to submit a request for… **but not give them the actual information**, and have a human follow up."
7. "**Rescheduling — this is something that isn't used or active in the platform. We need something that is more aptly labeled as HARDSHIP**… installment deferments, potentially refinancing the balance only, changing the payment frequency, changing due dates — anything that is going to trigger the need to send authorization to the borrower."
8. "**We can't unilaterally in the back end just change those things without getting the borrower to first sign legal documentation** that they are agreeing to the amendment to the loan agreement."
9. Adjustment of terms: "in situations of pretty extreme hardship… any interest that would be due, we set that aside into add-on fees that do not accrue interest… extend the term temporarily out beyond the current maximum… temporary reduction in interest rate… activate that with a new agreement that this is a temporary measure with an expiration date, and then the terms **automatically snap back** to what they were under the original loan agreement."
10. "[Hardship] functionality **likely needs more in-depth discussion and explanation**… deferments… move the account due-as-of date forward one installment and allow us to accommodate that request **without the account showing in arrears**."
11. Transactions: "this is more of what actually occurred… kind of a ledger of the account — in fact **I prefer the term ledger here; it should be treated as such**."
12. Manual vs charge: "manual would be when we are processing things manually and not instituting the payment from their bank account… the vendor has agreed to reduce the amount owed as an accommodation… or a payment received at the vendor's location by credit card or cash — we don't then charge that money again. A charge payment is… a new transaction against the bank account to collect those funds."
13. Repayment modes: "regular… first interest is cleared, then principal… add-on payments target the add-on section… where any fees that don't incur interest live — NSF fees as an example… a special payment is designed to go 100 percent to principal… payoff is designed to close the account and calculate a fixed amount that is **not editable**."
14. Types: "cash, check, bank transfer or electronic transfer, credit card and adjustment… **each one would be recorded in the back end so we could get metrics**… and use that for reconciliation."
15. Reference #: "**should be auto-populated… a unique number… encompass the vendor, the loan ID and the transaction number**… we could enter vendor ID - loan ID - transaction number and search by that… each component filterable… **we want to be able to sort things quite robustly in the back end in order to have a reporting system driven by a database of transactions**."
16. Dates: "**there actually needs to be two dates here — the effective date and the processing date**… effective date can be backdated but **there need to be limitations**: we can't backdate past previous transactions and we shouldn't backdate into previous months… by default effective and processing should be the same, with the ability **limited by user permission**." (Includes the real customer-service anecdote of honoring a Friday payment processed the next business day.)
17. "**Write-off should actually not live in the loan header and should only be in the hardship queue or section and should not be available to everybody — the hardship tab should be user-defined availability so that junior staff members can't write off accounts or do adjustment of terms.**"
18. Scheduled transactions: "**this is actually a very, very important section**. We can suspend upcoming transactions… create a new custom transaction on the 15th for whatever amount and repayment mode we want… say someone is behind and says 'that June 26 payment — I can make it July 10th', we can add a transaction on July 10th to process that past-due payment. We can edit these, look at the transaction history, and delete them."
19. Risk score: "essentially a record of the underwriting information used to decision the account… say this account goes bad and we're wondering why the heck did we approve this… **it allows us to keep a record of our credit decisions and the metrics that led to them, and this should be hard-coded to NOT change with any future account changes** — a new bank verification wouldn't update this bank account score at all."
20. Credit bureau: "there's different reports… **one should be labeled as what was used in the underwriting**; if we needed to do a new check because we were trying to find a delinquent account, we would do a new hard pull and that creates a new entry so we can see the differences — has delinquency increased, inquiries increased, a bankruptcy, new public records, a new fraud alert."
21. Promise to pay: "really doesn't need to live in servicing — BUT if we're actively changing a future payment date… we would follow that up with a promise to pay… that adds a new flag and **also sends a notification to that borrower**… a lot of the time it's used in the collections queue as a record of the promises made by borrowers."
22. "Contacts… this is an active performing account so there's a lot more notifications being sent out; we can sort by time and statuses."
23. "Flow is the same… flags the same, bank statements the same, map the same, comments the same. That concludes the servicing workplace review."

---

## 6. Integrations / external services referenced

- **Flinks** — bank account aggregation: bank accounts sourced "FlinksService"; account name "FlinksCapital"; repayment method carries Flinks-derived institution/transit/account; bank re-verification flow mentioned.
- **Payment processor / EFT rail** — "Direct bank transfer" auto-charges; dishonored/failed-transaction fees charged by "our payment processor"; NSF return codes ("account closed", "payer deceased", stop payments) drive Disable Auto-Charges. (Shown in UAT as **PaymentServiceSimulator**.)
- **Credit bureau** — hard/soft pulls, stored timestamped reports, score/liabilities/OFAC alert (Equifax-style Canadian bureau data).
- **E-signature / legal-document service** — required for all hardship amendments ("sign legal documentation… amendment to the loan agreement") and referenced in notifications ("Loan Agreement Signed", "Loan Offer Accepted - Signing Required").
- **Email + SMS + dashboard notifications** — Contacts tab: system emails, dashboard notifications, Send SMS capability; automated reminder cadence (7-day, 48-hour, autopay-in-7-days, payment received, disbursement, approval, signing).
- **Risk-decisioning data providers** — anti-fraud, geolocation, web activity/behavior, cyber-security threat feed (one provider erroring in UAT), ID verification, bank account scoring.
- **Excel export** (Excel 2007+ and alternate formats) on Payments and Transactions grids.
- ISO-badge footer (compliance branding), Turnkey Lender platform v7.9.0.30.
