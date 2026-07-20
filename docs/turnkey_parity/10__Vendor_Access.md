# Vendor Access — Feature Inventory

Source video: "10 — Vendor Access" (~21 min, 63 frames @ 20s). Narrator: Dave. This video is unusual among the series: it is less a demo of an existing "vendor portal" and more **Dave's requirements spec for the PaySpyre vendor portal**, narrated while driving the Turnkey Lender *admin backend* (kelowna-dental-uat.turnkey-lender.com, branded "PaySpyre Financial", footer version 7.9.0.30). His framing: *"a vendor portal is going to be access to the backend, just heavily modified so that they have limited vision and capabilities."* Every screen shown is the full back-office view; the narration specifies which pieces vendors get, which get masked, and which get removed.

Top nav visible throughout: **Origination | Risk Evaluation | Underwriting | Servicing | Collection | Reports | Archive | Settings | Tools** (plus notification bell, language globe, user menu).

---

## 1. Screen-by-screen walkthrough (chronological, with frame refs)

### A. Origination workplace — application queue + application detail (f0001–f0005, ~0:00–1:40)
URL: `/App#/origination`.
- **Left panel (queue list)**: "+ New application" button, keyword Search, "Filters" button, filter chips: All time / All (status) / All branches / All loan types / All assignations / **Kelowna Dental Centre** (vendor filter) / All providers / All provinces. Columns: **ID, Name, Amount, Created, Wait (days), A (assignee avatar), S (status icon)**. Pagination (Page 1 of 2, 26 records).
- **Right panel (selected application #5593 "New Test Middle Doe")**: header with photo, ID, province (British Columbia), branch (None), **Vendor / Provider pair: "Kelowna Dental Centre / Dr. Michael Webster"**, status chip "Origination", action buttons **Assign to me (dropdown), Cancel, Reset the appraisal**, and "Loan virtual date" (test-time control).
- Pink alert banner: "5593 Flag: First payment missed | Comment:" (x2) — flags surface at top of the account.
- **Origination-stage tabs**: Customer details, Co-Borrower, Bank details, Summary, Initial schedule, Workflow, Contacts, Documents, Credit history, Flags, Credit bureau, Comments.
- Customer details tab content (buttons: Edit, **Export to PDF**, Changelog): Personal information, Address (with inline validation error "The provided address is not found"), Additional info incl. Social Insurance Number and Type of ID Verification (see §3 for full field list).
- Narration here: why applications must be vendor-created (PaySpyre doesn't know if the patient should get $500 or $5,000 — the vendor arranged that), and the current clunky flow: originations workplace → new application → pick province & credit product → amounts and terms.

### B. New application form (f0006–f0017, ~1:40–5:40)
URL: `/App#/origination/newLoan`. Title "New application". This is the intake form a vendor-style user fills today:
- **Card 1 — product/pricing**: Province* (dropdown, "British Columbia"), Credit product* (dropdown, "AB4464-M08", info tooltip), Loan amount* ($ 17500), Term* (unit dropdown "Month" + number 3), Interest* (17.99 [%] [per Year]) — editable rate, Vendor (typeahead "Enter vendor name or id" → resolves to "Kelowna Dental Centre" with green checkmark; **Clear** button), Provider (dependent typeahead, enabled after vendor chosen).
- **Card 2 — dates**: Start date* with calendar datepicker (July 2026 shown); checkbox **"Use custom first due date"** → reveals Custom first due date* field (2026-08-07 chosen after start date 2026-07-16). Dave's example: treatment scheduled the 15th, loan starts the 16th, first payment arranged for Aug 7.
- **Card 3 — computed preview** (recalculates live): "Repayment period: Monthly", "Approximate payment: $6,012.54", equation strip **"$17,500.00 Principal + $534.62 Interest + $43.00 Commission = $18,077.62 Total (19.54% APR)"**, and a full **payment schedule preview table** (#, Date, Total, Principal, Interest, Fee — fee column has per-row dropdowns, e.g. $41.00 first period then $1.00).
- Buttons: **Next** (proceeds to applicant details) / Cancel. Note: form clears the schedule preview while required fields are incomplete (f0009–f0017 show the short state).
- Narration: province + credit product selection "shouldn't be a thing" for vendors (auto by their province, list of their products); vendor field must be pre-filled from login; provider must remain selectable (multiple providers per vendor); replace scattered inputs with the slider+input "new experience"; vendor may adjust interest **within allowed limits** if permitted; after this step the vendor initiates borrower verifications (ID, bank, credit-bureau consent) sent by **text and email** — Dave recounts a real integration he tested that day: text link → driver's licence front/back photo + selfie → instant ID verify → second text link → bank verification, all "in just a few minutes"; results must populate the backend.

### C. Return to Origination queue (f0018, ~5:40)
Brief transition through the origination list (same queue as A) before switching workplaces.

### D. Underwriting workplace (f0019–f0026, ~6:00–8:40)
URL: `/App#/underwriting`.
- Left queue: Search, Filters; columns ID, Name, Amount, Wait, A, S (14 records). Status icons: green shield (approved/verified), orange padlock (waiting), green check.
- Selected: **Antonietta Watsica #5712, $7,500** — status "Waiting for approval". Vendor line: Kelowna Dental Centre / Dr. Michael Webster.
- **Underwriter action buttons: Unassign (dropdown), Reject, Bank Verification, Cancel, Send for reprocessing, Approve.**
- Blue note: "David Wilson: Re-scoring caused by a new credit report". Pink flag: "5712 Flag: First payment missed | Comment:".
- **Underwriting tabs**: Summary, Risk score, Customer details, Bank details, Contacts, Documents, Credit history, Initial schedule, Credit bureau, Workflow, Flags, Bank statements, Map, Comments.
- **Summary tab** sections: Contact Information (name, email, main phone, province, alternative phone); Loan details (Loan ID, Creation date, Requested amount $7,500.00, Offered term 48 months, Interest 21.99%/yr, Installment $235.63, **System decision: Manual review**, **Credit risk: High**, APR 22.27%); Previous activity (previous loans #, previous offers #, outstanding balance, applied payment holidays, late payments #, Max DPD, days past due); **Credit bureau check** (Status: Hit, Score: 644, Total balance $59,572.00, Number of defaults, Number of bankruptcies: 1, Bankruptcy date 2007-12-01).
- Narration (key policy): vendors get **no underwriting abilities/buttons except one: "Request reprocessing"** — a request for PaySpyre to send the deal back; while in underwriting the deal is locked ("we're reviewing the deal as submitted"). In the future automated flow the app goes borrower-verification → auto-decision and "would not appear to land in underwriting whatsoever"; automated **rejections get routed to a human underwriter instead of hard-rejecting**, and that occurrence must be communicated to the vendor; approvals land in Servicing as "approved pending activation".

### E. Servicing workplace — account list + account detail (f0027–f0044, ~8:40–14:40)
URL: `/App#/servicing`.
- Left list: Search, Filters, "…" menu; columns **ID, Name, Disbursed (date), Amount disbursed, A, S** (30 records). Status icons incl. red exclamation (past due), green check (paid/ok), calendar (scheduled).
- Selected: **Raleigh Bailey #5713**, $16,000 disbursed 2026-06-01, status chip **Active**, vendor line **"Rock Dental / Dr. Yusef Khadembashi"** (a second vendor — demonstrates cross-vendor visibility the portal must remove).
- Account action buttons: **Unassign (dropdown), Payment (dropdown), Write off**.
- Banners: "Transactions suspended: From 2026-07-10 To 2026-07-10"; "5713 Flag: Promise to pay | Comment: Rescheduled payment from july 10th to july at borrower request".
- **Servicing tabs (the "tabs on the right hand side within the accounts" Dave triages one by one)**: Summary, Customer details, Bank details, Payments, Rescheduling, Transactions, Scheduled transactions, Contacts, Risk score, Credit bureau, Documents, Credit history, Promise to pay, Workflow, Flags, Bank statements, Map, Comments.

Tab-by-tab as shown:
1. **Summary** (f0028, f0037–f0038): Contact Information; Loan details (Loan ID 5713, Creation date 2026-06-01, Amount $16,000.00, Term 36 months, Interest 16.99%/yr, Contract date 2026-06-16, Interest start date 2026-06-16, Next payment date 2026-07-10, Installment $262.84, System decision: Manual review, Credit risk: Medium, APR 17.37%, Applied payment holidays 0); **Loan performance** (Principal paid $1,672.22, Interest paid $150.30, Fees paid $36.00, Triggered fees paid $0.00, Total repayment $1,858.52, Maximum DPD 0, # of Late payments 0, # of NSF payments 0, Installments owed 1.00/$261.84, Installments paid 6.96/$1,822.52, Installments due 0.00/$0.00); Previous activity; Credit bureau check.
2. **Bank details** (f0029–f0032): **"Add bank account" button**; Bank accounts table: Name (FlinksCapital), **Account number (1111000 / 1111001 — fully visible today)**, Routing number (77777), Institution number (777), Account holder (John Doe), Type, **Source: FlinksService**, Actions; star marks the primary/payment account. Dave highlights the account number (f0031) while explaining masking.
3. **Comments** (f0033): free-text "Add your comment…" box; threaded log mixing **system status-transition entries** (e.g. "BANK ACCOUNT VERIFICATION → WAITING FOR APPROVAL / Bank Scoring: Refer"; "AUTO PROCESSING → BANK ACCOUNT VERIFICATION / Customer is expected to verify their bank accounts on Customer's Dashboard") and human entries — notably David Wilson's 16-point **"Application Submission Checklist"** (see §3) with Edit/Delete controls. This is the channel Dave wants vendor communication to flow through.
4. **Credit history** (f0034): "No credit history" (empty state).
5. **Bank statements** (f0035): "Available reports" dropdown (Most recent report - 2026-06-01 2:42 PM); sub-tabs **Bank Accounts / Attributes / Raw report**; account selector; per-account detail: Title "Another Business Account", Account number, Category: Credits, Type: Chequing, Currency: CAD, RequestId, Transit number 77777, Institution number 777, Overdraft limit, Balance current $42.00, Balance available $84.00, Balance limit; Holder name "Mo'e Money Inc.", holder address (Montreal QC), Email biz@flinks.com, Phone 514-123-4567; full **bank transaction feed** table (Date, Balance, Debit, Credit, Description e.g. TrxAnq@Cr38.05). This is raw Flinks bank-verification output — vendors must NOT see it.
6. **Scheduled transactions** (f0036, f0041): **Custom transactions** table (#, Status, Effective date 2026-07-15, Amount $500.00, User David Wilson, Actions: edit/delete/revert) with green "+" add button; **Suspended transactions** table (#, Status, Start date 2026-07-10, End date 2026-07-10, User, Actions) with "+" add button. (Matches the "Transactions suspended" banner.)
7. **Payments** (f0039): buttons **Restructure, History, Disable Auto-Charges (dropdown), Payout amount** (payout/settlement calculator); **Excel 2007+ export**; "Principal: $14,079.28" outstanding; schedule table (#, Date, Total, Principal, Interest, Fee (per-row dropdown), Close date, **Status: "Paid on time" / "Scheduled"**, Cash flow detail icon per row).
8. **Transactions** (f0040): Excel export; ledger table (Date, Reference # (tx hash/id), Transaction type: **Loan disbursement / Automatic charge**, Amount, Method: **Virtual / Direct bank transfer**, Operator (David Wilson / Raleigh Bailey / System), Comments/Error, delete + expand actions). Expanded row reveals: Creation date, **Repayment method: FlinksCapital, 77777, 1111000** (full bank info), **Service: PaymentServiceSimulator**, Repayment mode: Regular payment, Principal $181.84, Interest $81.00 split.
9. **Contacts** (f0042): buttons **Send Email, Send SMS, + Log message**; "Show system messages" toggle; filters (All time, All statuses); communications log table (Date, User=System, Subject/Purpose, Method: **Email / Dashboard notification**, Result, Status ✓, Comment, Actions). Visible automated messages: "PaySpyre Financial - Payment Received", "PTP reminder", "PaySpyre Financial - Promise to Pay Recorded", "7 day(s) before payment", "Payment Reminder: Autopay in 7 Days", "2 day(s) before payment", "Payment Reminder: Due in less than 48 hours", "Loan disbursement".
10. **Documents** (f0043): sub-tabs **System documents / Loan documents / Personal documents**; System documents shows generated PDFs: **Loan agreement (2026-06-01), Terms & Conditions, Privacy policy**.
11. **Promise to pay** (f0044): "+ Add promise to pay" button; PTP table (Start date 2026-07-07, PTP date 2026-07-15, PTP amount $500.00, Comments "Rescheduled payment from july 10th to july at borrower request", User David Wilson, Paid date, Cancel date, edit/delete actions).
- Tabs named but not opened on camera: Rescheduling (Dave: "the hardship which is in this as rescheduling"), Risk score, Credit bureau, Workflow, Flags, Map, Customer details.
- Narration across E: vendor visibility must be **portfolio-scoped to that vendor only**; bank details masked to last digits; no add/modify bank account; no payout calculator or disabled-charges buttons but yes to seeing upcoming/scheduled payments; transactions visible in reduced "accounting" form only; no Contacts tab (no sending email/SMS on PaySpyre's behalf) — vendor comms go through Comments; no Risk score / Credit bureau / Bank statements / Scheduled transactions / Rescheduling(hardship); Documents OK; Credit history OK only if limited to that vendor's accounts; Promise to pay, Workflow, Flags, Map, Comments all fine.

### F. Reports — Business performance dashboard (f0045–f0061, ~14:40–20:20)
URL: `/App#/portfolio/dashboard/`. Left rail report sections: **Business performance, Portfolio, Operational, Risks, Scoring, Underwriting**. Global "Comparison period: Monthly" selector.
Widgets on Business performance:
- **Geographical report**: Leaflet/OpenStreetMap heat map, metric filter "Processed Applications".
- **Portfolio report** KPI tiles with sparklines + deltas: Portfolio size $706.31k (−$2.02k), Disbursed amount 0.00 (−$16.00k), Repaid Amount $2.42k (−$1.05k), Profit $405.25 (+$65.58), Profit/Portfolio 0.00.
- **Risk report** donut: Early payments, Paid on time $2.42k, Paid late, Bad debt $3.23k, At risk $3.23k, Losses.
- **Collection report**: Bad debt $3.23k (trend chart), Paid late, Write-offs, Debt roll rate $49.00; **Bad debt Collectability** bar (Low 0.00% / Medium 100.00% / High 0.00%).
- **Performance report**: Number of processed applications (Number/Amount toggle, trend line).
- **Top write-off reason** grid (Number/Amount toggle): Bankruptcy, Insolvency, Liquidation, Unenforceable, The debt is too old, Abscond, Uneconomical to Collect, Uncollectible.
- **Time series report**: metric dropdown ("Number of Processed Applications") over time.
- Narration: vendors should land on a **performance dashboard on login** with "all of the relevant metrics for their particular portfolio"; Business performance / Portfolio / Operational / even Risks are fine to expose; **Scoring and Underwriting reports are not**; keep it to "the big sexy numbers… vendors are not finance minded"; loan-performance/interest-income info should be re-expressed relative to what's been paid out to the vendor. This section also carries the entire **vendor disbursement/self-serve payout requirement** (see §2/§4) — month-to-date collections, amount due to vendor, amount available to request, 4-business-day clearing holdback, free monthly auto-disbursement + fee-charged extra disbursements, instant processing via the payment-processor integration so PaySpyre isn't an intermediary.

### G. Settings — Users (f0062–f0063, ~20:20–end)
URL: `/App#/settings/accounts/users`.
- Settings left rail: **Accounts (Users, Branch offices), Company settings, Integrations, Loan settings, Decision engine, Notifications, Application process, Security, Appearance, Kelowna settings**.
- Users screen: **"Add user"** button, search, "All permissions" filter; table (Login, Name, Email, Branch, edit/delete actions) listing admin users (David Wilson, Olga Admin, Administrator Initial, Ok Sh, Andy Wilson).
- Narration: vendors get **Archive access for their own accounts**, but **no Settings and no Tools** — "if they need to add something then we would do it in the back end. So that's the summary for vendors."

---

## 2. Complete feature checklist (vendor portal capability matrix)

Legend: ✅ = vendor gets it; 🟡 = gets it modified/limited; ❌ = admin-only, hide from vendor.

**Application creation (Origination)**
- ✅ Create new application from the portal (originations queue access).
- 🟡 Province & credit product: auto-resolved from the vendor's own province/product list — "shouldn't be a thing" they pick.
- ✅ Enter loan amount and term; 🟡 UI should be slider + input field ("like the new experience we've created"), not scattered inputs.
- 🟡 Interest rate: editable only "within their allowed limits" if the vendor has rate authority.
- 🟡 Vendor field: pre-filled from login (never typed); ✅ Provider: selectable (multiple providers per vendor, e.g. multiple dentists).
- ✅ Set start date (align to treatment date) and custom first payment due date agreed with the patient.
- ✅ Live payment preview: repayment period, approximate payment, Principal + Interest + Commission = Total (APR), full amortization schedule preview.
- ✅ Trigger borrower verification steps: **ID verification, bank account verification, credit bureau authorization — delivered by SMS text and email**; results auto-populate the backend.
- ✅ Search/filter/paginate their own application queue (time, status, loan type, provider filters); ❌ branch/vendor/province filters beyond their own.

**Underwriting stage**
- 🟡 Read-only visibility of deals in underwriting (status "Waiting for approval", system decision, etc.).
- ✅ Single button: **"Request reprocessing"** (asks PaySpyre to send the app back to them for edits).
- ❌ Approve / Reject / Bank Verification / Cancel / Send for reprocessing (the underwriter versions) / Unassign — none of the underwriter buttons.
- ❌ Editing applications while in underwriting (deal locked as submitted).
- ✅ Notification of decisions: approval communicated (can be automated system notification in Comments); auto-rejections silently rerouted to human review, with that occurrence communicated to the vendor.

**Servicing (post-approval loan accounts)**
- ✅ Access to Servicing workplace, **scoped strictly to their own portfolio** ("they should not have visibility to any other vendors or any other accounts").
- ✅ See account status, flags, banners (suspensions, PTP flags) on their loans.
- ✅ Summary tab — with the caveat that P&L-style figures should be restated relative to amounts paid out to the vendor.
- 🟡 Bank details: view-only, masked — bank name + last few digits of account number; routing similar; institution number fully blocked. Example use-case: patient walks in asking "which account are my payments from?" → "Flinks Capital, ending in 000".
- ❌ Add bank account / change which bank account payments come from (PaySpyre's responsibility).
- 🟡 Payments tab: see upcoming & scheduled payments and statuses; ❌ Payout-amount calculator, Disable Auto-Charges, Restructure, "some of the other buttons".
- 🟡 Transactions: see that payments occurred — "the accounting of that payment and the method… reduced information"; ❌ expanding to full banking info / service-provider (processor) details.
- ❌ Scheduled transactions (creating custom/suspended transactions).
- ❌ Rescheduling (hardship) — "we would handle all of that stuff".
- ❌ Contacts tab — vendors must not send emails/SMS or log messages "on our behalf"; vendor communication happens in the **Comments tab** (✅).
- ❌ Risk score (proprietary in-house info). ❌ Credit bureau. ❌ Bank statements (Flinks raw data).
- 🟡 Credit history: only history of that individual **with that vendor**.
- ✅ Documents (loan agreement, T&C, privacy policy, loan/personal docs).
- ✅ Promise to pay. ✅ Workflow. ✅ Flags. ✅ Map. ✅ Comments.
- ❌ Write off, Payment (manual) action buttons implied admin-only.

**Collections**
- ✅ Visibility into Collections for their own accounts (tab shown in nav; not demoed).

**Reporting / dashboard**
- ✅ Landing page = concise **performance dashboard** with "all of the really meaningful metrics… big sexy numbers", vendor-scoped.
- ✅ Business performance, Portfolio, Operational, Risks report sections. ❌ Scoring, ❌ Underwriting report sections.
- 🟡 Loan performance / interest income restated "in a way that was relevant to just them", net of amounts paid out to the vendor.

**Disbursements to vendor (new build — Dave's "best case scenario", doesn't exist in Turnkey)**
- ✅ Page (dashboard section or separate "transaction/disbursements page within the performance reporting") showing: month-to-date collections on their portfolio; amount due to the vendor assuming collections clear; **amount currently available to request as a disbursement**.
- ✅ Vendor-initiated disbursement request ("vendors can control when they get their funds").
- ✅ Automated monthly auto-disbursement of any undispersed balance — the one free transfer per month; additional intra-month disbursements carry a **transaction cost**.
- ✅ **4-business-day clearing holdback**: portions collected within the last 4 business days are not yet releasable (protects against returned payments; avoids clawing funds back from vendors).
- ✅ Request triggers **instant processing through the payment-processor integration** — no manual PaySpyre step; vendor is paid directly by the processor, keeping PaySpyre from acting "as a quote-unquote intermediary" (legal positioning).

**Admin & housekeeping**
- ✅ Archive: access to their own archived accounts.
- ❌ Settings (users, company, integrations, loan settings, decision engine, notifications, application process, security, appearance). ❌ Tools. "If they need to add something then we would do it in the back end."
- (Implicit) vendor users are branch/portfolio-scoped logins of the same backend.

---

## 3. Data fields visible

**Origination queue / servicing list columns**: ID, Name, Amount, Created, Wait (days) | Servicing: ID, Name, Disbursed (date), Amount disbursed; Assignee avatar; Status icon.

**New application intake form** (`/origination/newLoan`):
- Province* (dropdown)
- Credit product* (dropdown, coded e.g. "AB4464-M08", info tooltip)
- Loan amount* ($)
- Term* (unit dropdown Month + integer)
- Interest* (% per Year, editable)
- Vendor (typeahead by name or id; validated ✓; Clear button)
- Provider (typeahead, vendor-dependent)
- Start date* (datepicker)
- Use custom first due date (checkbox) → Custom first due date* (datepicker)
- Computed: Repayment period, Approximate payment, Principal, Interest $, Commission $, Total $, APR %, schedule rows (#, Date, Total, Principal, Interest, Fee-with-dropdown)

**Customer details tab (application record)**:
- Personal information: First name, Middle name, Last name, Date of birth, Email, Marital status, Number of dependents, Citizenship, Education
- Address: Resides at (years, months), Street, Apartment, City, Province, Postal Code, Residential status (+ address-validation warning)
- Additional info: **Social Insurance Number**, Type of ID Verification
- Record actions: Edit, Export to PDF, Changelog
- Separate **Co-Borrower** tab exists at origination.

**Summary tab (underwriting & servicing)**:
- Contact Information: Full name, Email, Main phone, Province, Alternative phone
- Loan details: Loan ID, Creation date, Requested/Amount, Offered term/Term, Interest %/yr, Contract date, Interest start date, Next payment date, Installment, System decision (Manual review), Credit risk (High/Medium), APR, Applied payment holidays
- Loan performance (servicing): Principal paid, Interest paid, Fees paid, Triggered fees paid, Total repayment, Maximum DPD, # of Late payments, # of NSF payments, Installments owed / paid / due (count/$ pairs)
- Previous activity: Previous loans #, Previous offers #, Outstanding balance, Applied payment holidays, Late payments #, Max DPD, Days past due
- Credit bureau check: Status (Hit), Score (e.g. 644), Total balance, Number of defaults, Number of bankruptcies, Bankruptcy date

**Bank details tab**: account Name (FlinksCapital), Account number, Routing number, Institution number, Account holder, Type, Source (FlinksService), primary-star, Actions; "Add bank account" button.

**Bank statements tab (Flinks report)**: report picker (timestamped); sub-tabs Bank Accounts / Attributes / Raw report; Title, Account number, Category, Type (Chequing), Currency (CAD), RequestId, Transit number, Institution number, Overdraft limit, Balance current, Balance available, Balance limit; Holder name/address/email/phone; transaction rows (Date, Balance, Debit, Credit, Description).

**Payments tab**: Principal outstanding; rows: #, Date, Total, Principal, Interest, Fee, Close date, Status (Paid on time / Scheduled), Cash-flow detail.

**Transactions tab**: Date, Reference #, Transaction (Loan disbursement / Automatic charge), Amount, Method (Virtual / Direct bank transfer), Operator, Comments/Error; expanded: Creation date, Repayment method (bank + numbers), Service (PaymentServiceSimulator), Repayment mode, Principal/Interest split.

**Scheduled transactions**: Custom (Status, Effective date, Amount, User) and Suspended (Status, Start date, End date, User), add/edit/delete/revert.

**Contacts tab**: Date, User, Subject/Purpose, Method (Email / Dashboard notification), Result, Status, Comment; Send Email / Send SMS / Log message; system-message toggle.

**Promise to pay**: Start date, PTP date, PTP amount, Comments, User, Paid date, Cancel date.

**Documents**: System documents (Loan agreement, Terms & Conditions, Privacy policy) / Loan documents / Personal documents.

**Comments — David Wilson's "Application Submission Checklist"** (de-facto vendor intake data model; verbatim from f0033):
1. Vendor Application Review: Correct applicant & co-applicant: Yes; Loan amount submitted is correct: Yes; Contract start date is correct: Yes; Interest rate is correct: Yes; Application good to be decisioned: Yes
2. Treatment Date: 2026-06-15
3. Treatment Provider: Dr. Khadembashi
4. Treatment Cost: $25,000.00
5. LESS: Est. Insurance: $1,500.00
6. EQUALS: Est. Co-Pay: $23,500.00
7. LESS: Down payment: $7,500.00
8. EQUALS: New Treatment Financed: $16,000.00
9. ADD: Renewal of Current Account: $0.00
10. EQUALS: Amount Financed: $16,000.00
11. Preferred Payment Amount: $260/bi-weekly
12. Preferred Payment Frequency: Bi-weekly
13. Preferred 1st Payment Date: 2026-06-25
14. Additional Offers: N/A
15. Alt Phone# Name & Relationship: June, Mother
16. Additional Notes: Advised has bad credit, concerned about approval

**Reports dashboard metrics**: Portfolio size, Disbursed amount, Repaid amount, Profit, Profit/Portfolio (each with sparkline + period delta); Early payments / Paid on time / Paid late / Bad debt / At risk / Losses; Bad debt, Paid late, Write-offs, Debt roll rate, Bad debt collectability (Low/Med/High); Number of processed applications; write-off reasons (Bankruptcy, Insolvency, Liquidation, Unenforceable, Debt too old, Abscond, Uneconomical to collect, Uncollectible); time-series by selectable metric; geographic heat map; comparison period selector.

**Settings→Users**: Login, Name, Email, Branch, permissions filter, add/edit/delete.

---

## 4. Workflow (vendor-side application lifecycle as shown/spec'd)

1. **Point of sale**: vendor and patient agree on treatment and financing amount (this is why PaySpyre can't originate blind — "PaySpyre doesn't know if that patient is supposed to apply for $500 or $5,000").
2. **Vendor creates application** in the portal: (today) originations → New application → province → credit product → amount/term/rate → vendor/provider → start date → custom first due date; (target) product auto-selected, vendor pre-filled, slider UX, provider picked, dates aligned to treatment schedule and the patient-agreed first payment date.
3. **Borrower verification kick-off**: vendor initiates ID verification, bank account verification, and credit bureau authorization; links sent to the borrower via **SMS + email**; borrower completes photo-ID + selfie and bank login in minutes; data lands in the backend (Bank statements/Flinks, credit bureau tabs).
4. **Decisioning**:
   - Target automated path: system auto-decisions; the deal never visibly sits in underwriting.
   - Approval → account appears in **Servicing** as "approved pending activation"; approval communicated to vendor (system notification in Comments acceptable).
   - Auto-rejection → NOT hard-rejected; silently escalated to a **human underwriter** for review; the vendor is informed this occurred.
   - Manual path (current): deal sits in Underwriting "Waiting for approval"; vendor sees it read-only; only lever is **Request reprocessing** to pull it back for changes.
5. **Servicing/active loan**: vendor monitors their own portfolio — status, upcoming/scheduled payments, completed transactions (reduced detail), flags, PTPs, workflow, documents, comments. Hardship/rescheduling, restructure, write-off, bank-account changes, custom transactions all stay with PaySpyre.
6. **Collections**: overdue accounts visible to vendor (their portfolio only).
7. **Vendor gets paid**: collections accumulate on the vendor's portfolio → dashboard shows MTD collected / due to vendor / **available now** (net of the 4-business-day clearing holdback) → free automated monthly disbursement, or vendor requests an extra disbursement (fee applies) → request fires straight through the payment-processor integration for instant, direct processor→vendor payout.
8. **Archive**: closed accounts remain visible to the vendor (own accounts only).

---

## 5. Dave's editorial comments (verbatim-ish)

Portal architecture
- "A vendor portal is going to be access to the backend, just heavily modified so that they have limited vision and capabilities."
- "Payspire doesn't know if that patient or borrower is supposed to apply for $500 or $5,000 in financing… it may be something that the vendor has communicated with the patient directly about."

Application creation
- "Click on their province and credit products, **which shouldn't be a thing** because they're going to be in the same province and they're going to have a list of credit products — **so we want that part automated**."
- "If they have ability to affect the interest rates, then they would be able to change it **within their allowed limits**."
- "They shouldn't have to enter a vendor or provider because they are logged in as a vendor… **that should already be filled out**. Now providers will be something that they do have to select because it may involve multiple different providers."
- "Probably a lot similar to the way that we've created the new experience… **more like a sliding bar with an input field rather than just scattered input fields**."
- "They would initiate the applicants to have their ID verified, bank account verified and authorize the credit bureau. **Those are things that we would like to be able to be sent out by text as well as email.**"
- "I personally did an ID verification through an integration and a bank verification today… I was pretty much instantly verified… completed both of those steps in just a few minutes. **So that's the type of thing that we're looking to bring into [PaySpyre]** and have that information populated into the back end."

Underwriting
- "They do not need access to underwriting… **they shouldn't have any of the abilities or buttons. They should have one button that says 'request reprocessing'** which essentially is their request for us to send it back to them."
- "When something is in underwriting **we don't want vendors to be making changes to applications** because the deal is being reviewed as submitted."
- "In an automated underwriting process… **it would not appear to land in underwriting whatsoever, but we will need to have some sort of way to communicate a pass and fail**."
- "If an automated response is an automated rejection then **instead of being hard rejected it would get submitted to a human underwriter for review, and that occurrence needs to be communicated to the vendor in some way**."
- "If it's an approval then it would just end up in the servicing workplace as an **approved pending activation** account."

Scoping & privacy
- "**All of that vendor access needs to be just for that vendor — they should not have visibility to any other vendors or any other accounts outside of their own separate portfolio.**"
- "Bank details should have the majority of the information **blocked out by say stars** and just have the last few digits available… 'your account is with Flinks Capital and the account number ends in 000'… Same with the routing number; the institution number could be fully blocked out… **we don't need to make it easy**."
- "**Vendors should not be able to add bank accounts or modify which bank account the payments come out of.** That's going to be something that ultimately we end up being responsible for."
- "The risk score information — **that's proprietary in-house information**… They do not need to see the full credit information for the applicants."
- "Credit history should be **limited to only credit history with that vendor**."
- "Bank statements — the actual information from the bank verification — **they should not have access to**."
- "The hardship, which is in this as rescheduling, **they shouldn't have access to either. We would handle all of that stuff.**"

Servicing tab triage (rapid-fire)
- "The payments screen: to calculate a payout **they should not have** — disable charges or some of the other buttons; **they should be able to see which payments are upcoming and scheduled**."
- "Transactions is fine — they can see what transactions have taken place — **but they should not be able to click on it and see the full banking information or the service provider information… just basically the accounting of that payment and the method, just some reduced information**."
- "Scheduled transactions — again, **no**."
- "**They shouldn't have access to the contacts tab because we don't want them to send out emails and SMS texts or log messages on our behalf.** Typically all of the vendor communication goes through the… comments tab."
- "Risk score — no. Credit bureau — no. **Documents is fine.** Credit history — if it's limited to that vendor, fine. **Promise to pay is fine. Workflow is fine. Flags is fine.** Bank statements — no. **Map is fine and comments is fine.**"
- Summary tab: "It's fine to give them access [but] **I would prefer if loan performance… interest income and things like that was communicated to the vendors in a way that was relevant to just them** and took into consideration the amount that has been paid out."

Dashboard & reports
- "We would like them to be **met with a dashboard when they log in — kind of like a performance dashboard with all of the relevant metrics for their particular portfolio**."
- "They should have business performance, portfolio, operational, even risks — **but scoring and underwriting they shouldn't have access to**."
- "**A really concise dashboard with all of the really meaningful metrics would probably suffice**… we need to keep this simple because **vendors are not finance minded. We want this to be the big sexy numbers that paint the picture** and get away from all of the tedious minute intricate details."

Disbursements (the big new ask)
- "**The best case scenario that I would love to have is a place for them to see what is currently available or what has been collected on their portfolio that has not been dispersed to the vendor.**"
- "What payments have been collected for that vendor month to date, what is the amount that is due to the vendor…, and **what amount is available for the vendor to request as a disbursement — so that the vendors can control when they get their funds**."
- "It's an automated monthly process… **the one time monthly is free, but if they want additional disbursements in the month then that would be an additional cost**."
- "**Take away any of the payments that were made within the last four business days**… that allows us to ensure that funds released to vendors are fully cleared and are not going to be returned, so we don't have to try to collect funds back from a vendor."
- "When they do that, **I'd like the integration with the payment processor to be instantly processed so that we don't have to manually process that. That takes some of the legalities away from us** because we're not then acting as a quote-unquote intermediary — the vendor is directly getting funds from the payment processor."

Archive / settings / tools
- "Archive — they should have access to, again, their particular accounts. **Settings — they don't need access to. Tools — they don't need access to.** We don't really want them to be adding anything; if they need to add something then we would do it in the back end. **So that's the summary for vendors.**"

---

## 6. Integrations / external services referenced

- **Flinks** — bank account verification & bank statement aggregation (visible as "FlinksCapital" bank accounts, "FlinksService" source, biz@flinks.com in report data; "Bank Scoring: Refer" in system comments; "Flink's capital" in narration).
- **Credit bureau** — credit bureau check with score/defaults/bankruptcies (Equifax in PaySpyre's real stack); borrower "authorize the credit bureau" consent step.
- **ID verification service** — SMS-link photo-ID (licence front/back) + selfie flow Dave personally tested that day (vendor unnamed; the pattern to replicate).
- **SMS + Email delivery** — verification links and automated borrower notifications (Contacts tab shows Email + Dashboard-notification methods; PaySpyre stack: Twilio/SendGrid).
- **Payment processor** — "PaymentServiceSimulator" in the transaction detail (Turnkey's mock); Dave requires instant vendor-disbursement processing via the real processor integration (PaySpyre stack: Zumrails) with direct processor→vendor payout.
- **Turnkey Lender** itself — the platform being documented/replaced (kelowna-dental-uat.turnkey-lender.com, v7.9.0.30), with Leaflet/OpenStreetMap powering the geographic report.
- **Excel export** (Excel 2007+ format) on Payments and Transactions tabs; **Export to PDF** on customer details; generated **PDF loan documents** (loan agreement, T&C, privacy policy).
