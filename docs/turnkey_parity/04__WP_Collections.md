# Collections Workplace — Feature Inventory

Video: 04__WP_Collections (~19 min, 57 frames @ 20s). Narrator: Dave. Platform: Turnkey Lender UAT ("PaySpyre Financial", kelowna-dental-uat.turnkey-lender.com/App#/collection, footer version 2026 7.9.0.30).

---

## 1. Screen-by-screen walkthrough (chronological, with frame refs)

**f0001–f0007 (0:00–2:20) — Collections queue + Summary tab (borrower Abe Mertz, loan 5469).**
Two-pane layout identical to the other workplaces: left = filterable account list, right = loan detail panel. Top nav: Origination, Risk Evaluation, Underwriting, Servicing, Collection (active), Reports, Archive, Settings, Tools. Left list shows 13 records sorted by Outstanding principal and DPD (descending — 775, 760, 283, 151, 145, 142, 128, 46, 44, 9, 8, 2, 0 days). Columns: ID, Name, Outstanding principal, DPD (sortable), P (round "probability-to-collect" badge), A (assignee avatar). Right pane: collapsed static loan header (name, loan ID 5469, "Medium Collectability" tag, British Columbia, "None", vendor "Selkirk Dental Clinic / Dr. Steven Schadinger"), action buttons **Unassign (with dropdown), Payment (dropdown), Write off**, a pink alert banner between header and tabs ("5469 Flag: Second payment missed | Comment:"), "Loan virtual date" edit control (test-server time travel), and 18 tabs. Summary tab shows Contact Information, Loan details, My next action, Loan performance, Previous activity. Dave narrates the modernization need: bulk assignment of accounts to junior/senior collectors, queues by vendor/alpha, and his corrected delinquency buckets (current-month-late / pot-30 / pot-60 / pot-90 / defaults / insolvency).

**f0008 (2:20) — Unassigned account selected (Eugenia Olson, loan 5470, DPD 775).**
Button strip changes to **"Assign to me" (with dropdown)**; Payment and Write off are greyed out/disabled until the account is assigned. Two stacked flags in the pink banner: "5470 Flag: First payment missed | Comment:" and "5470 Flag: Second payment missed | Comment:". Demonstrates the current one-account-at-a-time assignment model Dave calls "just not viable".

**f0009–f0022 (2:40–7:20) — "All past due" filter dropdown opened (held open through the delinquency-bucket narration).**
Dropdown options visible: **All past due; 1 - 30 Past due (days); 31 - 60 Past due (days); 61 - 90 Past due (days); >91 Past due (days); Today's actions; Expiring cards; Defaults; No active payment methods.** While the menu is open Dave explains his month-end reporting bucket model, the insolvency portfolio concept, that "expiring cards doesn't make a lot of sense" for them, and that defaults should replace 120+. At f0023 the menu closes.

**f0024–f0025 (7:40–8:20) — Expanded loan header (loan 5470).**
Chevron expands the static header to the full financial snapshot: Credit product BC5149-R02 (info icon), Loan amount $2,113.50, Contract date 2024-04-23; Installment payment $196.84, Next auto payment 2026-07-23; Outstanding principal $2,113.50, Interest due $933.80; Next payment due $196.84, Past due amount $2,350.08, Total amount due $2,546.92; Account due as of 2024-05-23, Current DPD 775, Amount to move $97.92; Add-on fee balance $0.00. f0025 scrolls the Summary to the bottom: Previous activity (Previous loans #, Previous offers #, Outstanding balance, Applied payment holidays, Late payments #, Max DPD, Days past due) and Credit bureau check (Status, Score, Total balance, Number of defaults, Number of bankruptcies, Bankruptcy date).

**f0026–f0030 (8:20–10:00) — Action plan tab + New action dialog.**
Action plan tab (collections-only tab): **"Action" button**, empty state "No actions". Clicking Action opens **"New action" modal**: Date* (date picker, prefilled 2026-07-07), Action type* (dropdown, required — validation message "Action type is required" shown), Comments (textarea), OK / Close. Dave: action types (check for repayment, call borrower, email borrower…) must be definable in a settings area; these are internal messages reviewed by back-end staff.

**f0031 (10:00–10:20) — Customer details tab.**
Buttons: **Blacklist, Export to PDF, Changelog**. Sections: Personal information (First/Middle/Last name, Date of birth, Email, Marital status, Number of dependents, Citizenship, Education) and Address (Resides at, Street, Apartment, City, Province, Postal Code, Residential status). "Exact same as other workplaces."

**f0032 (10:20–10:40) — Payments tab.**
Buttons: **Restructure, History, Disable Auto-Charges (dropdown), Payout amount**; **Excel 2007+ export (dropdown)**. Totals line: Principal $2,113.50 / Interest $756.52 (info icons). Amortization/schedule table: #, Date, Total, Principal, Interest, Fee (dropdown per row), Close date, Status ("Missed" with info icon), Cash flow (icon button per row). Missed rows highlighted pink. Dave starts to talk about accrual display here, then strikes it ("I probably wouldn't put that in the payments tab").

**f0033–f0034 (10:40–11:20) — Transactions tab (Dave: should be "the ledger tab").**
Excel 2007+ export. Table: Date, Reference #, Transaction, Amount, Method, Operator, Comments/Error, Actions (row expander). One row: 2024-04-22 3:07 PM, Ref 2, "Loan disbursement", $2,113.50, Virtual, System. Dave: on top of the transactions there should be a running summary of current outstanding principal, accrued interest, add-on fees, etc., each click-through-able to its transactions.

**f0035 (11:20–11:40) — Contacts tab (communication log).**
Buttons: **Send Email, Send SMS, + Log message**. Filters: All time, All statuses; **"Show system messages" toggle**. Table: Date, User, Subject/Purpose, Method, Result, Status, Comment, Actions. Visible rows are the automated dunning ladder (User = System, Method = Dashboard notification + Email pairs, green result checks): "90 day(s) past due" / "PaySpyre Test Server - ACCOUNT IN DEFAULT: Loan Past Due 90 Days!"; "60 day(s) past due" / "IMMEDIATE Action Required: Loan Past Due 60 Days!"; "30 day(s) past due" / "Immediate Action Required: Loan Past Due 30 Days!"; "21 day(s) past due" / "Loan Past Due 21 days"; "14 day(s) past due" / "Action Required: Loan Past Due 14 days"; 7-day row partially visible.

**f0036 (11:40–12:00) — Documents tab.**
Sub-tabs: System documents / Loan documents / Personal documents. Empty in this account.

**f0037–f0038 (12:00–12:40) — Credit history tab.**
Table of the borrower's other loans with us: Loan ID (clickable link — Dave wants it to jump to that loan in whatever workplace it sits), Amount ($1,500.00), Term (10 months), Average installment ($38.44), Disbursement date (2024-12-26), Close date (blank = not closed), Max DPD (760), Outstanding balance ($2,174.00), Outstanding principal ($1,500.00), Status (Past due). Dave: "I would also add in there like payment frequency or some minimum terms."

**f0039 (12:40) — Promise to pay dialog (opened from Promise to pay tab).**
Modal "Promise to pay": **Promise to pay amount*** ($ input), **Promise to pay date*** (date input), **Comment*** (required), option **"Do not accrue late fees and late interest"**, and a **"Bad debt"** button visible top-left of the same dialog area (bad-debt marking lives here). Save/Close buttons. Underlying tab shows "+ Add promise to pay" and "No data to display".

**f0040–f0055 (13:00–18:20) — Promise to pay tab + expanded header narration.**
Long static stretch on the Promise to pay tab ("+ Add promise to pay", empty list) while Dave delivers the most important content of the video: the loan-header field semantics — interest due accrues daily; a "fees due" field is missing; principal + interest due + fees due + add-on balance must equal payout amount; NSF fees are triggered fees that belong under add-on balance, not loan fees; "Account due as of" derivation from the amortization schedule; and the "Amount to move" tolerance algorithm (50% threshold moves the due-as-of date forward one month; remainder still owed but account not treated as past due). At f0055 he highlights/selects the "Amount to move: $97.92" field in the header. He commits to providing two Excel files: the account-due-as-of calculation and the amount-to-move algorithm. He also says bad debt and delinquency tracking "in the current circumstances does not work" with daily simple interest.

**f0056 (18:20) — Quick flip to Servicing workplace for comparison.**
URL /App#/servicing, borrower Raleigh Bailey (5713). Servicing list columns: ID, Name, Disbursed, Amount disbursed, A, S (status icons: green check, red exclamation, calendar). Header shows same layout (Current DPD 0, Amount to move $142.28); banners "Transactions suspended: From 2026-07-10 To 2026-07-10" and "5713 Flag: Promise to pay | Comment: Rescheduled payment from July 10th to July at borrower request"; tabs include Bank statements (servicing has it; collections shown here doesn't). Comments feed visible with a David Wilson "Application Submission Checklist" (vendor application review, treatment cost, down payment, amount financed etc.). Point made: servicing queue right-pane = collections queue right-pane.

**f0057 (18:40–end) — Back in Collections, Comments tab.**
Comment feed with "Add your comment…" input; entries show workflow-status chips: David Wilson 2024-04-22 2:08 PM "BANK ACCOUNT VERIFICATION → ORIGINATION — Bank verification has been skipped: testing"; System 2024-04-22 1:49 PM "AUTO PROCESSING → BANK ACCOUNT VERIFICATION — Customer is expected to verify their bank accounts on Customer's Dashboard". Dave: "promise to pay again the same, workflows the same, flags and comments are the same… that concludes our collections review."

---

## 2. Complete feature checklist

Queue / list pane:
- [ ] Collections queue listing all delinquent accounts (default: all accounts listed)
- [ ] Search box over the queue
- [ ] "Filters" master button
- [ ] Filter: All time (date range)
- [ ] Filter: All past due — with options: All past due / 1-30 Past due (days) / 31-60 Past due (days) / 61-90 Past due (days) / >91 Past due (days) / Today's actions / Expiring cards / Defaults / No active payment methods
- [ ] Filter: All branches
- [ ] Filter: All loan types
- [ ] Filter: All assignations (by assigned collector)
- [ ] Filter: All vendors
- [ ] Filter: All provinces
- [ ] Columns: ID, Name, Outstanding principal, DPD (sortable, default sort), P = probability-to-collect score badge, A = assignee avatar
- [ ] Pagination (first/prev/page N of M/next/last) + record count
- [ ] Row selection loads account into right pane

Account actions (header button strip):
- [ ] Assign to me (button + dropdown for assigning to another user)
- [ ] Unassign (button + dropdown) once assigned
- [ ] Payment button (with dropdown; manual payment entry) — disabled until account assigned
- [ ] Write off button — disabled until assigned (Dave: must NOT be here; move to hardship section, permission-gated)
- [ ] Assignment gates actions: Payment/Write off greyed out when account unassigned
- [ ] Loan virtual date editor (test/staging time travel)

Loan header (collapsible):
- [ ] Collapsed identity row: photo, name, loan ID, collectability rating tag (e.g. "Medium Collectability"), province, secondary tag ("None"), vendor/provider ("Selkirk Dental Clinic / Dr. Steven Schadinger")
- [ ] Expandable financial snapshot (chevron) with ~14 live fields (see §3)
- [ ] Info tooltips (i) on calculated fields
- [ ] Alerts/flag banner strip between header and tabs (pink, per-flag lines, dismiss ×, e.g. "First payment missed", "Second payment missed", "Promise to pay" with comment)

Tabs (18): Summary, Action plan, Customer details, Bank details, Payments, Rescheduling, Transactions, Scheduled transactions, Contacts, Risk score, Credit bureau, Documents, Credit history, Map, Promise to pay, Workflow, Flags, Comments
- [ ] Summary tab — same content as Servicing summary (contact info, loan details, my next action, loan performance, previous activity, credit bureau check)
- [ ] Action plan tab (collections-specific): list of planned actions; "Action" button
- [ ] New action dialog: Date* (picker), Action type* (dropdown, required-field validation), Comments, OK/Close
- [ ] Action types must be admin-definable (settings area, add/remove) — e.g. check for repayment, call borrower, email borrower
- [ ] "My next action" block on Summary (Action / Scheduled to / Comments) reflects action plan
- [ ] Customer details tab: Blacklist button, Export to PDF button, Changelog button; personal info + address sections
- [ ] Bank details tab (same as other workplaces)
- [ ] Payments tab: schedule/amortization table with per-row Fee dropdown and Cash-flow icon; missed rows highlighted; Restructure button; History button; Disable Auto-Charges button (dropdown); Payout amount button; Excel 2007+ export; Principal/Interest running totals
- [ ] Rescheduling tab (Dave: should be the hardship tab)
- [ ] Transactions tab: ledger listing (Date, Reference #, Transaction, Amount, Method, Operator, Comments/Error, Actions expander); Excel export. Dave wants running balances (outstanding principal, accrued interest, add-on fees) pinned above, click-through to underlying transactions
- [ ] Scheduled transactions tab (same as servicing)
- [ ] Contacts tab: communication log; Send Email, Send SMS, + Log message (manual contact logging); All time / All statuses filters; Show system messages toggle; result/status ticks per message
- [ ] Automated dunning notifications at 7/14/21/30/60/90 days past due, each sent both as Dashboard notification and Email, logged in Contacts with result status (system-generated)
- [ ] Risk score tab — score frozen at time of credit decision (not current)
- [ ] Credit bureau tab (bureau pull data)
- [ ] Documents tab: System documents / Loan documents / Personal documents sub-tabs
- [ ] Credit history tab: other loans held with lender; Loan ID hyperlinks to that loan's workplace; columns per §3; Dave wants payment frequency + minimum terms added
- [ ] Map tab (borrower geolocation, same as other workplaces)
- [ ] Promise to pay tab: list + "+ Add promise to pay"
- [ ] Promise to pay dialog: amount*, date*, comment*, "Do not accrue late fees and late interest" option, Bad debt marking
- [ ] Bad debt flag (Dave: doesn't work with daily simple interest as-is)
- [ ] Workflow tab (status/workflow history, same as other workplaces)
- [ ] Flags tab (same)
- [ ] Comments tab: threaded internal comments with author, timestamp, and workflow-transition chips (e.g. AUTO PROCESSING → BANK ACCOUNT VERIFICATION); "Add your comment" input
- [ ] Probability-to-collect score computed per account ("how likely the system thinks it is to be collected")
- [ ] Delinquency automation: DPD counting, Current DPD vs Maximum DPD, Account due as of date, Amount to move
- [ ] Write-off capability (existing, to be relocated/permission-gated)
- [ ] Excel export (2007+) on Payments and Transactions; PDF export on Customer details

New-build requirements Dave states that do NOT exist today:
- [ ] Bulk assignment of accounts to collectors (by vendor, by vendor+alpha of last name, by bucket)
- [ ] Per-user queue display ("limit the display of accounts by user")
- [ ] Junior vs senior collector tiers (junior = current-month late / pot-30; senior = deeper delinquency, hardship options)
- [ ] Corrected buckets: Current month late / Pot 30 / Pot 60 / Pot 90 / Defaults (>90) — replacing 1-30/31-60/61-90/>91 and dropping 120+
- [ ] Month-end delinquency reporting snapshot (report status of all accounts at end of each month)
- [ ] Insolvency queue segregated from collection queues (consumer proposal / bankruptcy / credit counseling)
- [ ] Separate insolvency portfolio: write off from main portfolio but keep maintaining; maintenance fee; receive trustee payments and forward recoveries
- [ ] Fees due field in loan header; identity principal + interest due + fees due + add-on balance = payout amount
- [ ] Triggered fees (NSF) live in add-on balance, not loan fees
- [ ] Hardship section housing Write off + Rescheduling, controlled by user permissions
- [ ] Settings-defined action types for action plans
- [ ] Ledger tab with running category balances above transactions
- [ ] Account-due-as-of + amount-to-move algorithms per Dave's Excel files (to be provided)

## 3. Data fields visible (by screen/form)

**Queue list columns:** ID; Name; Outstanding principal; DPD; P (probability-to-collect badge); A (assignee avatar). Footer: "13 record(s)", Page x of y.
Sample rows: 5470 Eugenia Olson $2,113.50 / 775; 5479 Eugenia Olson $1,500.00 / 760; 5485 Milford Klein $9,889.64 / 283; 5501 Ivory Osinski $2,595.77 / 151; 4462 Alexandra Legros $6,275.99 / 145; 5469 Abe Mertz $1,194.82 / 142; 5494 Heloise Ferry $4,367.25 / 128; 5518 John Doe $1,115.39 / 46; 5472 Pauline Braun $4,771.58 / 44; 4459 Travis Langosh $5,000.00 / 9; 5474 Rasheed Konopelski $11,775.21 / 8; 4458 Jaylan Hirthe $3,000.00 / 2; 4460 Alphonso Harvey $429.38 / 0.

**Queue filters:** All time; All past due (1-30 / 31-60 / 61-90 / >91 Past due (days) / Today's actions / Expiring cards / Defaults / No active payment methods); All branches; All loan types; All assignations; All vendors; All provinces; Search.

**Collapsed loan header:** Name; Loan ID; Collectability (Medium Collectability); Province (British Columbia); tag (None); Vendor/Provider (Selkirk Dental Clinic / Dr. Steven Schadinger); Loan virtual date.

**Expanded loan header:** Credit product (BC5149-R02); Loan amount; Contract date; Installment payment; Next auto payment; Outstanding principal; Interest due (accrues daily); Next payment due; Past due amount; Total amount due; Account due as of; Current DPD; Amount to move; Add-on fee balance. (Dave: add Fees due.)

**Flag banner:** "<loanID> Flag: First payment missed | Comment:"; "…Second payment missed | Comment:"; (servicing example) "Flag: Promise to pay | Comment: Rescheduled payment from July 10th to July … at borrower request"; dismiss ×.

**Summary — Contact Information:** Full name; Gender; Email; Main phone; Province; Alternative phone; Passport (Eugenia: 01602); Submitted by (Borrower).
**Summary — Loan details:** Loan ID; Creation date; Amount; Term; Interest (% per year); Contract date; Interest start date; Next payment date; Installment; Credit risk (Medium/Highest); APR.
**Summary — My next action:** Action; Scheduled to; Comments.
**Summary — Loan performance:** Principal paid; Interest paid; Fees paid; Triggered fees paid; Total repayment; Maximum DPD; # of Late payments; # of NSF payments; Installments owed (24.00/$7,176.09); Installments paid (21.47/$6,421.21); Installments due (2.53/$754.88).
**Summary — Previous activity:** Previous loans #; Previous offers #; Outstanding balance; Applied payment holidays; Late payments #; Max DPD; Days past due.
**Summary — Credit bureau check:** Status; Score; Total balance; Number of defaults; Number of bankruptcies; Bankruptcy date.

**New action dialog:** Date*; Action type* (dropdown; "Action type is required" validation); Comments; OK; Close.

**Customer details:** Buttons Blacklist / Export to PDF / Changelog. Personal information: First name; Middle name; Last name; Date of birth; Email; Marital status; Number of dependents; Citizenship; Education. Address: Resides at (7 years, 11 months); Street; Apartment; City; Province; Postal Code; Residential status.

**Payments tab:** Buttons Restructure / History / Disable Auto-Charges / Payout amount; Excel 2007+ export. Totals: Principal; Interest. Table: #; Date; Total; Principal; Interest; Fee (dropdown); Close date; Status (Missed); Cash flow.

**Transactions tab:** Excel 2007+ export. Table: Date; Reference #; Transaction (Loan disbursement); Amount; Method (Virtual); Operator (System); Comments/Error; Actions.

**Contacts tab:** Buttons Send Email / Send SMS / + Log message; filters All time / All statuses; Show system messages toggle. Table: Date; User; Subject/Purpose; Method (Dashboard notification / Email); Result; Status; Comment; Actions. Dunning subjects: "90 day(s) past due", "PaySpyre Test Server - ACCOUNT IN DEFAULT: Loan Past Due 90 Days!", "60 day(s) past due", "IMMEDIATE Action Required: Loan Past Due 60 Days!", "30 day(s) past due", "Immediate Action Required: Loan Past Due 30 Days!", "21 day(s) past due", "14 day(s) past due", "Action Required: Loan Past Due 14 days", 7-day entries.

**Documents tab:** System documents; Loan documents; Personal documents.

**Credit history tab:** Loan ID (link); Amount; Term; Average installment; Disbursement date; Close date; Max DPD; Outstanding balance; Outstanding principal; Status (Past due). (Dave: add payment frequency / minimum terms.)

**Promise to pay dialog:** Promise to pay amount* ($); Promise to pay date*; Comment*; "Do not accrue late fees and late interest" option; Bad debt; save/Close. Tab: "+ Add promise to pay"; "No data to display".

**Comments tab:** Add your comment input; per comment: author, timestamp, workflow chips (BANK ACCOUNT VERIFICATION → ORIGINATION; AUTO PROCESSING → BANK ACCOUNT VERIFICATION), body text.

**Servicing flash (f0056):** list columns ID/Name/Disbursed/Amount disbursed/A/S; banner "Transactions suspended: From <date> To <date>"; extra tab Bank statements.

## 4. Workflow / state machine

**Turnkey's built-in buckets (filter values):** 1-30 / 31-60 / 61-90 / >91 days past due, plus Today's actions, Expiring cards, Defaults, No active payment methods.

**Dave's replacement model (month-end snapshot reporting):**
1. **Current month late** — past due for the current calendar month (1-29 DPD within the month payment was due).
2. **Pot 30** — due previous month; becomes actual 30-day delinquency if unpaid at month-end.
3. **Pot 60** — two months unpaid; at 60 days delinquent it is **reported to the credit bureau**.
4. **Pot 90** — three months unpaid.
5. **Default** — anything past pot 90 (replaces Turnkey's "120+"; "once they get past pot 90 they would just be quote unquote default").
6. **Insolvency** (consumer proposal / bankruptcy / credit counseling) — separated from all other queues. Written off from the main portfolio but kept in a distinct insolvency portfolio: maintenance fee, receive trustee payments, forward recoveries. (Industry norm is returning these to vendors, which Dave wants to avoid losing recovery on.)

**Bucket rollover rule:** classification is evaluated at end of each reporting month; e.g. due July 15 → "current month late" until July 31; on August 1 it becomes pot-30; unpaid through August month-end = reported 30 days late; and so on.

**Account due as of / DPD calculation:** compare collected payments against the amortization schedule; a later payment backfills the earliest missed installment; DPD = today − "account due as of" date. Dave has a separate Excel file with the exact calculation.

**Amount to move (tolerance algorithm, Dave's own):** if borrower pays ≥ tolerance (e.g. 50%) of the past-due amount, the "due as of" date moves forward one month; remainder stays owed and collectable but the account is not treated/reported as past due (e.g. $450 paid of $500 due). Threshold configurable ("whatever percentage we set as our tolerance level"). Second Excel file to come.

**Assignment workflow:** account unassigned → "Assign to me" (or assign via dropdown) → Payment / Write off unlock → "Unassign" available. Dave's target: bulk assignment by vendor / vendor+alpha / bucket, junior collectors on current-month-late & pot-30, senior collectors (hardship powers) beyond.

**Promise to pay:** create promise (amount, date, comment, optional no-late-fee accrual) → tracked on tab → flag raised on header banner ("Flag: Promise to pay"). Bad-debt marking available in same dialog. Dave: bad debt + delinquency tracking don't work with daily simple interest as implemented.

**Write-off:** button in collections header today; must move into a permission-controlled hardship section (with Rescheduling).

**Dunning escalation (automated, observed in Contacts log):** system sends paired Dashboard notification + Email at 7, 14, 21 ("Action Required"), 30 ("Immediate Action Required"), 60 ("IMMEDIATE Action Required"), 90 days ("ACCOUNT IN DEFAULT") past due.

**Action plan loop:** senior staff create dated actions (type + comments) → appear as "My next action" on the summary → executed/reviewed by back-end staff.

## 5. Dave's editorial comments (verbatim-ish)

- "This one in my opinion needs quite a bit of modernization and change in the way that we look at this."
- "We want to be able to kind of limit the display of accounts by user."
- "We need to be able to set or assign accounts on a more bulk status to our staff… junior and more senior collectors… a junior collector is going to deal with something that is maybe current month late or potentially 30 days late and then anything beyond that is going to be somebody that is a more senior collector that is first with hardship options and collection methods on more difficult accounts."
- "Typically the way that I like to assign my collection cues to users is by last name… we'll likely need to do something along the lines of per vendor or per vendor and alpha depending on how big that vendor is."
- "We would have to go in each individual account and assign it to a user and that's just not viable… you'd have to repeat the process, so it's just not sufficient."
- "These are actually not accurate" (Turnkey's 1-30/31-60/61-90/>91 buckets) — "we would have… current month late… pot 30s… pot 60… pot 90 and then a 120 plus."
- "The idea with the collection reporting is that we would report the status of all accounts at the end of a given month."
- "Going into September if we don't collect anything that is going to be a 60 day delinquent account that is reported to the credit bureau."
- "We also need to have a section where we are tracking insolvent accounts whether that be consumer proposal or bankruptcy or credit counseling… essentially going to be separated out of those other collection queues."
- "We would need to come up with a maintenance fee to follow up and maintain bankrupt accounts and receive payments from trustees and forward those payments on… we would essentially write them off of our main portfolio but we also want to have a separate like insolvency portfolio that we are maintaining so that we have another way to get some recovery out of those accounts."
- "We don't really deal with credit cards at the moment so expiring cards doesn't make a lot of sense."
- "Defaults would be kind of when we get to the point where anything is like 90 plus… instead of the 120 plus… once they get past pot 90 they would just be quote unquote default."
- "There's also a probability to collect score on how likely the system thinks it is to be collected and who it's assigned to."
- "We've got a write-off button that should not be there — it should live again in the hardship section that is controlled by user permissions."
- "Action plan is a new tab in collections — allows a senior account rep… to basically advise what the expectations are for that particular account… checking for repayment, calling borrower, email borrower… those action types need to be in a settings area that we can define and add and remove to." / "These are internal messages and internal actions that are going to be reviewed by other back-end staff or ourselves."
- "As principal and interest is accrued and as — actually I probably wouldn't put that in the payments tab, let's strike that for the moment."
- "Rescheduling again should be the hardship tab."
- "Transactions should be the ledger tab. This is where we also need — you should be keeping an ongoing record on top here above the transactions of the current outstanding principal, the accrued interest, the add-on fees… and be able to click into and see the transactions on what has happened for those particular categories."
- "Risk score… is the risk score at time of the credit decision not at present circumstances."
- (Credit history) "I would also add in there like payment frequency or some minimum terms and… this would be a link that we could click on to access that particular loan ID and whatever workplace it was sitting in."
- "One thing here is the bad debt in the current circumstances does not work; the delinquency tracking in the current circumstances does not work with the way that we track daily simple interest."
- "We should also have a fees due because if there is fees that have been accrued, say for example an administration fee, we should have a record of that so that… if we add the principal, the interest due and the fees due and the add-on balances that should equal the payout amount as of the particular date."
- "This interest due is per day so this will change tomorrow… it's keeping track of the accrued interest due on the account as of right now."
- "Those would be loan fees… it would not include NSF fees; NSF fees would be under add-on balance."
- "This might require me to provide you with the calculation… I've got a separate Excel file on the account due as of date — that's what we use to determine the current days past due."
- "The amount to move is another unique algorithm that I developed that allows us leeway to collect a certain portion of a regular installment in order to move this date forward… the amount to move will be 50% or whatever percentage we set as our tolerance level… if they're past due $500 and they can pay $450 we don't want to then continually report the account as past due for 50 bucks… the $50 still remains past due and we still collect it, the system just doesn't treat the account as past due."
- "I'm probably gonna have to give you again another Excel file on how I came up with the algorithm on how to calculate that."
- "The servicing queue… is the same as the collections queue." / "Promise to pay again the same, workflows the same, flags and comments are the same."
- "Okay, and that concludes our collections review."

## 6. Integrations/external services referenced

- **Credit bureau reporting** — 60-day delinquents "reported to the credit bureau"; Credit bureau tab + Credit bureau check fields (score, defaults, bankruptcies). (PaySpyre context: Equifax.)
- **Email delivery** — system dunning emails from "PaySpyre Test Server"; Send Email action in Contacts. (PaySpyre stack: SendGrid.)
- **SMS** — Send SMS action in Contacts. (PaySpyre stack: Twilio.)
- **Dashboard notifications** — in-app borrower notifications paired with each dunning email (implies borrower/customer dashboard).
- **Payment rails / auto-charges** — Next auto payment, Disable Auto-Charges, Payment button, NSF fee concept (implies PAD/EFT processor; PaySpyre stack: Zumrails).
- **Insolvency trustees** — receiving payments from bankruptcy trustees and forwarding recoveries (manual/financial process, future insolvency portfolio).
- **Excel exports** (Excel 2007+ downloads on Payments/Transactions) and **PDF export** (Customer details).
- **Map tab** — geolocation/mapping of borrower address (embedded map service).
- **Dave's Excel models (to be supplied):** (1) account-due-as-of / days-past-due calculation; (2) amount-to-move tolerance algorithm.
