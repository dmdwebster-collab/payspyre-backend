# Originations Workplace — Feature Inventory

Source video: `01__WP_Originations` (~44 min, 132 frames @ 20s). Environment: Turnkey Lender UAT (`kelowna-dental-uat.turnkey-lender.com/App#/origination`), branded "PaySpyre Financial", version footer `PaySpyre Financial - 2026 7.9.0.30`. Narrated by Dave to document required functionality for the PaySpyre rebuild.

Purpose of the workplace (Dave's definition): "This is where all applications that are pending, being created, incomplete — applications where the information has not been either verified or has not been submitted for underwriting review."

---

## 1. Screen-by-screen walkthrough (chronological, with frame refs)

### 1.1 Global chrome + Originations list (f0001–f0009, ~0:00–3:00)
- Top navigation bar of **workplaces**: `Origination | Risk Evaluation | Underwriting | Servicing | Collection | Reports | Archive | Settings | Tools` — "Each workplace has its own specific function and reason for being."
- Right side of top bar: notification bell (with red badge), globe/language menu, user/profile menu.
- Two-pane layout: **left = queue/navigation panel**, **right = detail panel** that populates when a row is clicked.
- Left panel top: `+ New application` button, free-text Search box, `Filters` toggle button.
- Filter row (revealed by Filters button, f0003): dropdowns `All time`, `All` (status), `All branches`, `All loan types` ("these would be the different credit products currently in the system"), `All assignations`, `All vendors`, `All provinces`. When an app assigned to a specific vendor is selected, a **provider filter** also appears (`All providers`, f0062).
- Vendor filter dropdown open at f0007 — vendor list: All vendors, ABC Dental Clinic, Arch Dental + Aesthetics, Brightside Dental, Dawson Creek Dental Centre, Dr. N. J. Seddon Dental Corp., E Pro Dental, East Village Dental, Elevation Dental Inc., Kelowna Dental Centre, Montgomery Dental Centre, New Teeth Today, Parkview Dental, PaySpyre Financial…
- Queue grid columns: `ID` (sortable), `Name`, `Amount`, `Created` (date), `Wait` (time in stage, e.g. 11d/27d), `A` (assignment avatar — who it's assigned to), `S` (status icon, green). Row highlight for selected.
- Pagination footer: first/prev `Page [1] of 7` next/last, `105 record(s)`.

### 1.2 Loan header (persistent top of right pane) (f0005, f0009–f0011)
- Profile photo of applicant (compared manually against uploaded ID for identity verification — "will hopefully be automated through an integration").
- Name (`David Test6261`), application/loan ID (`5738`), Province (`British Columbia`), Branch (`None`), Vendor / Provider (`TEST VENDOR 2 / TV2 - Provider 2`), status chip top-right (`Origination`).
- Expandable second header row: `Credit product: TV2000-B01` (with info tooltip), `Loan term: 12 months`, `Requested amount: $2,000.00`, `Offered amount: $2,000.00`, `Interest: 21.99% per year`, `Start date: 2026-06-27` (after offer edit also shows `First due date`).
- `Loan virtual date` control with edit icon (top right; test/simulation date tool).
- Header action buttons: `Assign to me` (with dropdown arrow → assign to another user if you have permission), `Cancel`, `Send for approval`. Actions are disabled until the file is assigned to you — "to do anything in the account we have to first assign it to ourself because everything is time-stamped and user-stamped for security log purposes... who did what when." After assignment button becomes `Unassign` and Cancel / Send for approval enable (f0015).
- **Cancel loan dialog** (f0013): required `Cancel reason` dropdown ("reasons are set in the directory in the settings"), rich-text `Comment` (H1/H2/H3, bold, italic, lists, undo/redo), `+ Add user notification` button, OK/Cancel.
- `Send for approval` — sends the application onward (to underwriting/approval) with notes.

### 1.3 Detail tab strip (f0015)
Tabs: `Customer details | Co-Borrower | Bank details | Summary | Initial schedule | Workflow | Contacts | Documents | Credit history | Flags | Bank statements | Credit bureau | Comments`.

### 1.4 Customer details tab — read view (f0001, f0017–f0021)
- Action buttons: `Edit`, `Export to PDF`, `Changelog` ("who made what changes and when and why... important for tracking fraud").
- **Personal information**: First name, Middle name, Last name, Date of birth, Email, Marital status, Number of dependents, Citizenship, Education.
- **Address**: Resides at (X years, Y months), Street, Apartment, City, Province, Postal Code, Residential status (Own), Monthly Mortgage Payment ($2500) — plus an embedded **Leaflet/OpenStreetMap map** with a pin at the address.
- **Additional info**: Social Insurance Number (masked/blank — optional), Type of ID Verification (Driver's License), Driver's License number, Province of Issue, Main phone, Alternative phone, Car Owner (Financing), Monthly Car Payment ($500), Other Monthly Expenses ($750).
- **Employment information → Primary Income**: Income Type (Employed), Net Monthly Income ($13,350), Next pay date, How Often Are You Paid? (Every 2 weeks), Employer (XYZ Corp), Job title (Admin), Hire date (2020-04-18), Work phone.
- **Photo** section: applicant's uploaded selfie/photo.

### 1.5 Customer details — Edit dialog (the manual application form) (f0023–f0047, ~7:20–15:40)
- Modal `Customer details` with **`Fill with random data`** button (yellow) — "testing environment only, wouldn't be in live, but useful for a testing environment."
- Red asterisk = mandatory; no asterisk = optional (e.g. Middle name, SIN, Apartment, Car Owner).
- Personal information: First/Middle/Last name; DOB as 3 dropdowns (Year/Month/Day); Email; Marital status (dropdown); Number of dependents; Citizenship (dropdown); Education (dropdown).
- Address: Resides-at `years` + `months` dropdowns; Street; Apartment; City; Province (dropdown); Postal Code; Residential status dropdown (options per transcript: Own single family, Own strata, Own mobile home, Renter, Lives with parents/guardian); conditional payment field — label switches between `Monthly Mortgage Payment` / `Monthly Rent Payment` and disappears (rent-free) when "living with parents/guardian". "Anything under one year prompts a previous address to be populated" (only current stipulation; Dave wants 3 years history).
- Additional info: SIN (masked input, optional — "legally cannot be required... only reason is accuracy on credit bureau data"); Type of ID Verification dropdown (Driver's License, Government photo ID, Passport, Permanent Residence Card — "controlled by a directory in the back end... we can add and take away"); conditional ID-number field renames per type (passport number, PR card number, DL number) + Province of Issue; Main phone; Alternative phone; Car Owner dropdown (own outright / financing / leasing / no car); Monthly Car Payment; Other Monthly Expenses (required).
- Employment information — Primary Income: Income Type dropdown (Employed, Self-employed, Employment Insurance/Disability, Retirement/Pension, Student, Other); **fields change per income type** (f0038 shows `Employment Insurance / Disability` selected → fields become Net Monthly Income, Next pay date, How Often Are You Paid?, `Benefit start date`; Employed shows Employer/Job title/Hire date/Work phone + ext).
- Validation examples seen: `Next pay date` must be a future date within 30 days (`Date should be greater than 2026-07-07` error at f0048); term/amount caps in offer dialog; `Income Type is required`.
- `Add Secondary Income` button → **Secondary Income** block (Income Type, Net Monthly Income, Next pay date, How Often Are You Paid?) + `Delete Secondary Income` button. Mirrors primary income choices.
- **Documents (inside the form)**: `Borrower Identification (Front & Back of ID)` upload (Max 2 files, thumbnails of example DL front/back); `Co-Borrower Identification (Front & Back of ID)` upload (Max 2 files); generic `Documents` upload; **Photo** upload (`Please upload your photo`, `Change Photo` button, with download/view/delete icons).

### 1.6 Co-Borrower tab (f0048–f0049)
- Empty state: "The co-applicant has not been added." + `Add` button.
- **Co-Borrower dialog** (f0049): First name*, Middle name, Last name*, Date of birth* (3 dropdowns), Email*, Social Insurance Number, Phone*, Relationship* (dropdown); Address: Street*, Apartment, City*, Province*, Postal Code*. OK/Close.
- Note: far less data than borrower — Dave wants co-borrowers completely redefined (separate full file, linked by ID/email).

### 1.7 Bank details tab (f0051–f0052)
- `Add bank account` button; **Bank accounts grid**: star icon (primary/default account selector), Name (FlinksCapital), Account number, Routing number, Institution number, Account holder, Type, Source (`FlinksService`), Actions column.
- **Add a new bank account dialog** (f0052): Bank name*, Financial institution number*, ABA (Routing Number)*, Account number*, Account type* (dropdown). Manual add "requires manual review of documentation to ensure the individual... is the owner of the account and has authority to authorize the account as a payment source"; normally accounts come from the **Flinks** instant bank verification integration.

### 1.8 Summary tab (mentioned ~17:30)
- In originations, on a new account, it is "just a summary of the contact details and the loan details" (read-only recap).

### 1.9 Initial schedule tab (f0053–f0059, ~17:30–19:40)
- `Change terms` button; `Excel 2007+` export button (with dropdown for format); `Principal: $0.00` indicator (with info icon).
- Amortization grid columns: `#`, `Date`, `Total`, `Principal`, `Interest`, `Fee` (dropdown per row expands fee breakdown), `Close date`, `Status` (Scheduled with clock icon), `Cash flow` (icon button per row → cash-flow view of interest, origination fees, administration fees, principal — how accrued/applied).
- **Edit offer details dialog** (via Change terms, f0057): Province* (dropdown), Credit product* (dropdown, with info icon), Loan amount* ($, stepper), Term* (unit dropdown `Month` + number), Interest* (% per Year), Vendor (text/lookup), Provider (with `Clear` button), Start date* (date picker), `Use custom first due date` checkbox → reveals `Custom first due date` date field (f0058-59).
- **Credit-product parameter enforcement**: entering 2500 on a product capped at $2,499.99 raises `This field must be at most 2499.99`; term 18 on a 12-month product raises `Term must be smaller or equal to 12 Month` — "makes sure that any of the credit terms are enforced per credit product."
- After valid changes, dialog shows recalculated offer: repayment frequency from product (bi-weekly), approximate installment (103.10), breakdown principal/interest/commission/total and **APR "calculated using the Canadian method"**, plus updated amortization schedule preview list (f0059: #, Date, Total, Principal, Interest, Fee); OK locks changes in. Header then shows new Offered amount ($2,400), Interest 20.99%, Start date 2026-07-13, First due date 2026-07-17.

### 1.10 Workflow tab (f0061–f0062)
- Shows status-transition audit: columns `Date`, `Previous status`, `New status`, `Comments`, `User`.
- Test app shows "No transitions" (created via widget integration, not recognized); a real example (Telly Cruickshank #5567, f0062) shows `2025-11-13 2:12 PM | Pre-origination → Origination | Telly Cruickshank`. "Pre-origination = application not fully completed; Origination = a completed application."
- f0062 also reveals a **pink flag banner** at top of the detail pane: `5567 Flag: Second payment missed | Comment:` / `5567 Flag: First payment missed | Comment:` — flags surface as banners on the account.

### 1.11 Contacts tab (f0063–f0069)
- Buttons: `Send Email`, `Send SMS`, `+ Log message`. Filters: `All time`, `All statuses`. Toggle: `Show system messages`.
- Communication log grid: `Date`, `User` (System), `Subject/Purpose` (Welcome to PaySpyre Financial!), `Method` (Email), `Result`, `Status` (checkmark), `Comment`, `Actions` (view icon; delete for logged messages).
- **Compose email dialog** (f0064): To (avatar + name), `Preview` link, `Use Template` link (templates set up in back end), Email address + `CC` button, Subject* (+ merge-field `+` button), rich-text editor (H1/H2/H3, B, I, lists, undo/redo, +), **Attachments**: Files (`Choose file`), `Loan documents` dropdown (select generated loan docs to forward), `Comment` (internal note on why sent), Send/Cancel.
- **Compose SMS dialog** (f0065): To, `Use Template`, Phone, plain editor, Comment, Send/Cancel.
- **Log message dialog** (f0067): info banner "You can add interactions you had with the client outside the System... help all the staff members stay up-to-date"; Date*, Method* (Phone…), Purpose*, Result* (dropdown), Comment. Used to "log any collection efforts or contacts — they call, they ask for a payout as of particular dates."
- Logged entries can be deleted (red delete icon, f0069). System automatic notifications (welcome email) appear in the log; **email body shows `<Hidden for privacy purposes>`** (f0069 detail dialog: Status: Success, Email, Copy, Email subject, Date) because it contains login info.

### 1.12 Documents tab (f0071–f0084, ~23:20–28:00)
- Three sub-tabs: `System documents | Loan documents | Personal documents`; `Show archived files` toggle.
- **System documents** = generated loan documentation package (3 documents; viewer shows "1 of 3" with Prev/Next + Download):
  1. **Loan agreement** (7-page PDF, f0073–f0081): parties block (TEST VENDOR 2 = "Vendor", PaySpyre Financial Inc. = "Administrator", collectively the "Lender"; Borrower + Co-Borrower blocks with name/DOB/address/phone/email); ACCOUNT NUMBER, DATE OF AGREEMENT, INTEREST START DATE, PAYMENT FREQUENCY (Bi-weekly), FIRST INSTALLMENT, FINAL INSTALLMENT; table: Amount Financed (Principal), Annual Interest Rate (%), Bi-weekly Payment(s), Loan Term (Months), Total of Payments, Cost of Borrowing; Whereas clauses (Vendor provides Dental Goods & Services; Administrator provides lending platform/admin, credit review, ID verification, payment processing, collection); numbered terms: INTEREST, ANNUAL PERCENTAGE RATE (22.98% APR), PAYMENTS, FEES (ref. attached Fee Schedule), PREPAYMENT (prepay without penalty; partial prepayment does not excuse later scheduled payments), CONTACT DETAILS, PRE-AUTHORIZED DEBIT AGREEMENT, INDEPENDENT LEGAL ADVICE, INTERPRETATION, TIMING, CHOICE OF LAW (Province of British Columbia), COUNTERPARTS (electronic transmissions/e-copies valid); signature blocks Vendor / Administrator / Borrower with **e-signature merge coding for the SignNow integration** ("this is showing some of the e-signature coding for our integration with sign-now"). "All of these documents are set up as templates in the back end with merge fields inserted... so the system knows to pull that particular merge field into the document from the application."
  2. **Amortization schedule** (doc 2 of 3).
  3. **Fee schedule** (doc 3 of 3).
  4. **Pre-Authorized Debit ("PAD") Agreement** (page 7 of the agreement package, f0081): Account Number, Vendor; Payor Information (name, physical address, apartment/unit, city, prov, postal code, telephone, email); Bank Account Information (Institution Number (3 digits), Transit Number (5 digits), Account Number); PAD Details — individual/business checkbox, initial payment amount+date, recurring payment amount + Bi-weekly frequency per amortization schedule, "PAD Agreement will be administered by the third-party payment processor **Zum Rails**", waiver of pre-notification, sporadic PADs authorization clause.
- Terms & conditions and privacy policy "typically go out with the initial welcome email; they have access on their dashboard."
- **Loan documents** sub-tab (f0083): internal-only additional docs ("letter of employment, pay stub, notice of assessment"); label `Internal`; empty state `No documents`; `+ Add file`. "Visible to just the back-end staff."
- **Personal documents** sub-tab (f0084): what the borrower uploaded — `Borrower Identification (Front & Back of ID)` (2 files max, thumbnails), `Co-Borrower Identification (Front & Back of ID)`, generic `Documents`; each with `+ Add file`; visible to back-end staff **and** the borrower.

### 1.13 Credit history tab (f0085)
- "Credit history with PaySpyre and any former accounts... and the performance of those former accounts." New borrower shows `No credit history`.

### 1.14 Flags tab (f0086–f0088)
- Two sections: **Customer flags** and **Loan flags**, each with `Enable flag` + `Flags history` buttons and `No flags` empty state.
- **Enable flag dialog**: confirm text "Are you sure that you want to enable this option?", Flag* dropdown, optional `Expiry date` checkbox+date, Comment* (why the flag was set), Save changes/Close.
- Defined customer flags (from directory in Settings): **Do not contact, Deceased account, Bankrupt account** — all suppress automatic notifications. Selecting a flag shows metadata: `Type: Manual`, `Raise/Lower trigger description: This flag is used to suppress notifications on customer's request.`, `System impact: Suppress notifications` (f0088).
- Loan flags defined in back end with particular effects (e.g., `First payment missed`, `Second payment missed` seen as banner on account 5567, f0062).

### 1.15 Bank statements tab (Flinks data) (f0089–f0119, ~29:20–39:40)
- Header: `Most recent report - 2026-06-26 3:19 PM`. Three sub-tabs: `Bank Accounts | Attributes | Raw report`.
- **Bank Accounts** sub-tab: account picker dropdown (e.g., `Chequing CAD`, `Another Business Account` — every account associated with the online login used, not necessarily all accounts owned). Account panel fields: Title, Account number, Category (Operations/Credits), Type (Chequing), Currency (CAD), RequestId; Transit number, Institution number, Overdraft limit, Balance current, Balance available, Balance limit (credit limit if credit account); Holder name, Holder address, Email, Phone number. Used to "verify the person that completed the application... owns the bank account... and has authority to bind the account and authorize payments; also to verify name, address, email and phone number."
- Transaction grid: `#`, `Date`, `Balance` (running), `Debit`, `Credit`, `Description`. "All transactions are separated into debit or credit — credits increase the balance and debits decrease it on bank accounts, vice versa on credit accounts."
- **Attributes** sub-tab: four collapsible categories — `Income`, `Risks`, `Expenses`, `Fraud Detection`. Dave: "This particular area does not work very well, it's poorly designed and needs to be updated."
  - **Fraud Detection** (f0097): Account age in Days (91), Active Days in the Account (Last 90 Days) (91), Overall Activity Trend (CONSISTENT), Debit Activity Trend, Credit Activity Trend, Is Employer Income Detected? (NONE DETECTED), Average Monthly Recurring Payments ($0.00 — "never seems to work, always reports zero").
  - **Expenses** (f0099): Loan Payments — Sum of Loan Payments ($114.9), Average Monthly Loan Payments, Average Monthly Mortgage Payments, Average Monthly Student Loan Payments, Average Monthly Auto-Loan Payments, Average Monthly Micro-Loan Payments, Average Monthly Other Loan Payments; Committed Bill Payments — Average Monthly Utility Payments, Average Monthly Telecom Payments. (All $0.00 — relies on institutions using correct transaction codes; "no regulations... most use a miscellaneous code"; PaySpyre must "build a system that learns to identify the expenses based on the description, who instituted it, and the code when correct.")
  - **Risks** (f0107): Account Risk Overview — Minimum Balance Detected (Last 90 Days), Average Monthly Free Cash Flow (-$37.44; debits minus credits averaged over 3 months — "very inaccurate/incomplete measure", includes e-transfers from mom & dad), Days with a Negative Balance, Balance Trend (CONSISTENT); Additional Risk Analysis — Count of NSF Fees (Last 90 Days), Micro-Lender Name (Unknown — "should be expanded to identify the monthly amount used with micro lenders; significant risk factor"), Count of Stop-Payment Fees (Last 90 Days), Average Monthly Expenditure ($1,721.97).
  - **Income** (f0113–f0117): Employer Income — Id (GUID), Employer Name (NONE_DETECTED), Average Monthly Employer Income, Sum of Employer Income (Total), Employer Income Trend, Total Deposits Trend (CONSISTENT); 13-month History table (Current month … 12 months ago) with Count and Sum columns. Non-Employer Income — Average Monthly Non-Employer Income ("not useful — anything not identified as employer or government"; bounced-payment reversals counted as 'income'). Government Income — Average Monthly Government Income, Sum of Government Income (Last 90 Days), Sum of Employment Insurance (90 Days), Sum of WSIB (90 Days), Sum of Social Assistance (90 Days), Sum of Pension (90 Days), Sum of Child Support (90 Days), Sum of Other Government (90 Days) + History table ("government is fairly good with coding — WSIB, EI, pensions show up correctly"). Other Income Attributes — History table with `Sum of Total Credits` per month, expandable rows ("essentially just a list of all credits — not something you can count on as income").
- **Raw report** sub-tab (f0119): "press a button to get the raw data — unfiltered and unattributed information."

### 1.16 Credit bureau tab (f0121–f0129, ~40:00–43:00)
- Buttons: `Hard pull`, `Soft pull`; empty state `No reports available to show`. Pulls may already be initiated automatically by the application flow, but "we need to have confirmations because there are regulations around credit bureaus — express and explicit permission — because doing so has the potential to impact the individual's credit score." A confirm step precedes the pull.
- After pull: `Available reports` dropdown (e.g., `2026-07-07 4:44 PM - Hard`) / label `Report received on 2026-07-07 4:44 PM Type: Soft` — reports are stored and selectable by timestamp/type.
- **Summary section**: Full name, Score (550), Check date, Status (Hit), Number of defaults (2), Number of bankruptcies (0), Age (1989), Active accounts (6), Thin file (No), Inquiries in the last 6 months (2), Total balance ($9,408.00), OFAC Alert (No).
- **Liabilities' overview table**: rows Credit card, Mortgages, Auto loan, Payday, Other, Total; columns Open accounts, Max DPD, Total balance.
- **Characteristics**: Active accounts, Accounts in last 6 months, Closed accounts, Worst current DPD (90), Worst closed DPD, Current defaults, Closed defaults, Current bankruptcies, Bankruptcy date, Inquiries in the last 3 months, Inquiries in the last 6 months, Fraud alert (No), Number of credit cards with exceeded limit, Residence type (Unknown).
- **Liabilities (trade lines) table**: Created, Loan type, Limit, Balance, Days Past Due, Status (Open), Last update, Account type (Individual). Hard vs soft pull main difference = "the actual trade lines that are listed."
- Dave's gap list for the rebuild: liabilities should be proper **trade lines** with open date, closed date, type (installment/revolving), limits, balance, delinquency status (R0–R9 where R9 = written off), last-updated date, and the critically missing **reported minimum monthly payment** (to auto-populate financial capacity review); trade lines should be **clickable/editable** — mark "to be paid off" (consolidation lending → generates a cheque/transaction payable to that creditor and removes its minimum payment from capacity calc), edit balance after payout confirmation, and gross up bi-weekly minimum payments to monthly for capacity review.

### 1.17 Comments tab (f0131)
- Rich-text comment composer (avatar, H1/H2/H3, B, I, lists, undo/redo), `Send`/`Cancel`, `+ Add user notification`, `+ Add file`. "Where we communicate back and forth with vendors — anything we add here is visible to both the back-end staff and the vendors."

---

## 2. Complete feature checklist

Navigation & layout
- [ ] Workplace top-nav: Origination, Risk Evaluation, Underwriting, Servicing, Collection, Reports, Archive, Settings, Tools
- [ ] Notification bell w/ badge; language/globe menu; user menu
- [ ] Two-pane master/detail layout (queue left, record right)
- [ ] `+ New application` (manual application creation)
- [ ] Free-text search over queue
- [ ] Filters toggle; filters: time range, status, branch, loan type (credit product), assignation (user), vendor, provider (contextual), province
- [ ] Sortable queue columns: ID, Name, Amount, Created, Wait (time-in-stage), Assigned-to avatar, Status icon
- [ ] Pagination (first/prev/page-of-N/next/last) + total record count
- [ ] Loan virtual date (test-clock) control

Loan header
- [ ] Applicant profile photo in header (used for manual ID-vs-photo comparison)
- [ ] Persistent key info: name, ID, province, branch, vendor, provider, credit product (w/ tooltip), loan term, requested amount, offered amount, interest rate, start date, first due date, status chip
- [ ] Collapsible header detail row (chevron expand/collapse)
- [ ] Assign to me / Unassign; assign to another user (permission-gated)
- [ ] All actions gated on assignment; every action user- and time-stamped (security/audit log)
- [ ] Cancel loan w/ required reason (directory-driven) + rich-text comment + optional user notification
- [ ] Send for approval (with notes) — pushes to next stage
- [ ] Flag banners surfaced at top of record (e.g., payment-missed flags)

Customer details
- [ ] Read view + Edit (full application form) + Export to PDF + Changelog (field-level change audit: who/what/when/why)
- [ ] "Fill with random data" (test env only)
- [ ] Required-field indicator (red asterisk); optional fields unmarked
- [ ] Conditional fields: mortgage vs rent vs none by residential status; previous address prompt if <1 year at address; ID-number/province fields per ID type; income fields per income type; former employer wanted if hire date <6 months (Dave ask)
- [ ] Directory-driven dropdowns (ID types, income types, education, marital status, etc.) editable in Settings
- [ ] Secondary income add/delete (Dave: allow N income sources, 2 usually sufficient)
- [ ] Field validations: next pay date future & within 30 days; product-driven amount/term caps
- [ ] Embedded map (Leaflet/OpenStreetMap) geocoding the address
- [ ] ID document uploads (borrower front/back, co-borrower front/back, max 2 files each), misc documents, applicant photo upload/change/delete

Co-borrower
- [ ] Add co-borrower dialog (basic identity + address + relationship)
- [ ] (Rebuild directive) separate full file per co-applicant, linked by ID/email; identical tabs/docs/photo as borrower

Bank details
- [ ] Bank account list w/ primary-account star, account/routing/institution numbers, holder, type, source
- [ ] Add bank account manually (bank name, institution #, routing #, account #, account type) — requires manual ownership verification
- [ ] Automatic account ingestion via Flinks IBV

Summary
- [ ] Read-only summary of contact + loan details

Initial schedule / offer management
- [ ] Amortization schedule grid (date, total, principal, interest, fee w/ per-row breakdown, close date, status, cash flow)
- [ ] Per-payment cash-flow drill-in (interest, origination fee, admin fee, principal accrual/application)
- [ ] Export schedule to Excel (format selector)
- [ ] Change terms dialog: amount, term, rate, province, credit product, vendor, provider (clearable), start date, custom first due date
- [ ] Hard enforcement of credit-product min/max amount and term
- [ ] Recalculated offer preview: installment, principal/interest/commission/total, APR (Canadian method), full schedule preview; OK to commit

Workflow
- [ ] Status-transition audit trail: date, previous status, new status, comments, user

Contacts (communications)
- [ ] Send email: templates, preview, CC, subject merge-fields, rich text, file attachments, attach generated loan documents, internal comment
- [ ] Send SMS: templates, message, internal comment
- [ ] Log offline contact: date, method, purpose, result, comment; deletable
- [ ] Communication log w/ time+status filters, Show system messages toggle, delivery status/result, per-entry detail view
- [ ] System automatic notifications recorded in log (welcome email); sensitive bodies hidden for privacy
- [ ] Email/SMS templates managed in back end (Settings)

Documents
- [ ] Template-driven document generation with merge fields (loan agreement, amortization schedule, fee schedule, PAD agreement)
- [ ] SignNow e-signature field coding embedded in templates
- [ ] Multi-doc viewer (1 of N, prev/next, zoom, download, print)
- [ ] Three visibility-scoped stores: System documents (generated), Loan documents (staff-only uploads), Personal documents (borrower-visible/borrower-uploaded)
- [ ] Show archived files toggle; add file to each store
- [ ] T&C + privacy policy sent with welcome email and available on borrower dashboard

Credit history
- [ ] Internal (PaySpyre) prior-account history and performance

Flags
- [ ] Customer flags and Loan flags (separate)
- [ ] Directory-defined flags w/ type (manual/auto), trigger description, system impact (e.g., suppress notifications)
- [ ] Enable flag w/ optional expiry date + required comment; flags history view
- [ ] Notification suppression for Do-not-contact / Deceased / Bankrupt
- [ ] Flag banners on account header

Bank statements (Flinks)
- [ ] Report timestamp; multiple accounts per login; account metadata panel
- [ ] Transaction list w/ running balance, debit/credit, description
- [ ] Attributes: Income / Risks / Expenses / Fraud Detection accordions w/ 13-month history tables
- [ ] Raw report (unfiltered data) on demand
- [ ] (Rebuild directive) ML/rules engine to classify expenses & income from descriptions, not just transaction codes; micro-lender monthly usage quantification

Credit bureau
- [ ] Soft pull & hard pull w/ explicit-consent confirmation step
- [ ] Stored report history selectable by timestamp + pull type
- [ ] Summary, liabilities overview by type, characteristics, liabilities/trade-line table
- [ ] (Rebuild directives) full trade-line detail incl. reported minimum monthly payment; editable trade lines; pay-off checkbox driving consolidation disbursement + capacity-calc exclusion; monthly grossing-up of payment frequencies

Comments
- [ ] Vendor-visible comment thread w/ rich text, attachments, user notifications

---

## 3. Data fields visible (by screen/form)

### Queue (left panel)
ID; Name; Amount; Created; Wait; A (assignee avatar); S (status icon); Page; record count.

### Filters
All time; All (status); All branches; All loan types; All assignations; All vendors; All providers (contextual); All provinces.

### Loan header
Photo; Name; ID (5738); Province; Branch (None); Vendor / Provider; Status (Origination); Credit product (TV2000-B01); Loan term (12 months); Requested amount; Offered amount; Interest (% per year); Start date; First due date; Loan virtual date.

### Customer details — Personal information
First name; Middle name; Last name; Date of birth (Y/M/D); Email; Marital status; Number of dependents; Citizenship; Education.

### Customer details — Address
Resides at (years, months); Street; Apartment; City; Province; Postal Code; Residential status (Own / Own single family / Own strata / Own mobile home / Renter / Lives with parents-guardian); Monthly Mortgage Payment ↔ Monthly Rent Payment (conditional); map pin.

### Customer details — Additional info
Social Insurance Number (optional, masked); Type of ID Verification (Driver's License / Government photo ID / Passport / Permanent Residence Card); Driver's License (or Passport number / PR card number per type); Province of Issue; Main phone; Alternative phone; Car Owner (outright / financing / leasing / none); Monthly Car Payment; Other Monthly Expenses.

### Customer details — Employment information (Primary & Secondary Income)
Income Type (Employed / Self-employed / Employment Insurance–Disability / Retirement Pension / Student / Other); Net Monthly Income; Next pay date; How Often Are You Paid? (Every 2 weeks…); Employer; Job title; Hire date; Work phone + ext; Benefit start date (EI/Disability variant).

### Customer details — Documents & Photo
Borrower Identification Front & Back (max 2 files); Co-Borrower Identification Front & Back (max 2); Documents (misc); Photo.

### Co-Borrower dialog
First name; Middle name; Last name; Date of birth; Email; Social Insurance Number; Phone; Relationship; Street; Apartment; City; Province; Postal Code.

### Bank details
Star (primary); Name; Account number; Routing number; Institution number; Account holder; Type; Source; Add dialog: Bank name; Financial institution number; ABA (Routing Number); Account number; Account type.

### Initial schedule / Edit offer
#; Date; Total; Principal; Interest; Fee; Close date; Status; Cash flow. Dialog: Province; Credit product; Loan amount; Term (unit + value); Interest (% per Year); Vendor; Provider (+Clear); Start date; Use custom first due date + Custom first due date. Result: installment; principal; interest; commission; total; APR (Canadian method); Principal indicator; Excel export.

### Workflow
Date; Previous status; New status; Comments; User.

### Contacts
Date; User; Subject/Purpose; Method; Result; Status; Comment; Actions. Email dialog: To; Preview; Use Template; Email; CC; Subject; body; Files; Loan documents; Comment. SMS dialog: To; Use Template; Phone; body; Comment. Log dialog: Date; Method; Purpose; Result; Comment. Email detail: Status; Email; Copy; Email subject; Date; body (may be privacy-hidden).

### Loan agreement (merge fields observed)
Vendor name/address; Administrator (PaySpyre Financial Inc.) address; Borrower name/DOB/address/phone/email; Co-Borrower block; Account number; Date of agreement; Interest start date; Payment frequency; First installment (amount+date); Final installment (amount+date); Amount Financed; Annual Interest Rate %; Bi-weekly Payment(s); Loan Term (Months); Total of Payments; Cost of Borrowing; APR; final payment date; governing province.

### PAD agreement
Account number; Vendor; Payor name/address/unit/city/prov/postal/phone/email; Institution Number (3 digits); Transit Number (5 digits); Account Number; individual/business; initial payment amount+date; recurring amount + frequency; processor (Zum Rails).

### Flags
Flag; Type; Raise/Lower trigger description; System impact; Expiry date; Comment. Defined: Do not contact; Deceased account; Bankrupt account; (loan) First payment missed; Second payment missed.

### Bank statements — account & transactions
Title; Account number; Category; Type; Currency; RequestId; Transit number; Institution number; Overdraft limit; Balance current; Balance available; Balance limit; Holder name; Holder address; Email; Phone number; transaction #; Date; Balance; Debit; Credit; Description; Most recent report timestamp.

### Bank statements — attributes
Fraud: Account age in Days; Active Days (Last 90); Overall/Debit/Credit Activity Trend; Is Employer Income Detected; Average Monthly Recurring Payments. Expenses: Sum of Loan Payments; Avg Monthly Loan/Mortgage/Student-Loan/Auto-Loan/Micro-Loan/Other-Loan Payments; Avg Monthly Utility Payments; Avg Monthly Telecom Payments. Risks: Minimum Balance Detected (90d); Avg Monthly Free Cash Flow; Days with Negative Balance; Balance Trend; NSF count (90d); Micro-Lender Name; Stop-Payment fee count (90d); Average Monthly Expenditure. Income: Employer Income Id/Name/Avg Monthly/Sum/Trend; Total Deposits Trend; 13-month Count/Sum history; Avg Monthly Non-Employer Income; Government income (Avg Monthly; Sums 90d: Government, EI, WSIB, Social Assistance, Pension, Child Support, Other Government) + history; Other Income Attributes (Sum of Total Credits per month).

### Credit bureau
Full name; Score; Check date; Status (Hit); Number of defaults; Number of bankruptcies; Age; Active accounts; Thin file; Inquiries last 6 months; Total balance; OFAC Alert. Liabilities overview: Type (Credit card/Mortgages/Auto loan/Payday/Other/Total); Open accounts; Max DPD; Total balance. Characteristics: Active accounts; Accounts in last 6 months; Closed accounts; Worst current DPD; Worst closed DPD; Current defaults; Closed defaults; Current bankruptcies; Bankruptcy date; Inquiries last 3 months; Inquiries last 6 months; Fraud alert; # credit cards with exceeded limit; Residence type. Liabilities: Created; Loan type; Limit; Balance; Days Past Due; Status; Last update; Account type. Available reports (timestamp + Hard/Soft).

---

## 4. Workflow / state machine

Statuses shown or described:
- **Pre-origination** — application started but not fully completed.
- **Origination** — completed application, in this workplace; information not yet verified / not yet submitted for underwriting. (Status chip in header; also the workplace itself.)
- **Sent for approval** — via `Send for approval` action, moves the file onward (to Underwriting/Risk Evaluation workplaces per the top nav).
- **Cancelled** — via `Cancel` action with directory-driven reason + comment.

Transitions observed/described:
- Pre-origination → Origination (auto on application completion; logged in Workflow tab with user + timestamp).
- Origination → Sent for approval (manual, requires assignment; with notes).
- Origination → Cancelled (manual, reason required).
- Assignment sub-state: Unassigned → Assigned-to-me (→ reassignable to other users with permission). All record actions blocked until assigned; all transitions time- and user-stamped.
- Payment rows have their own status lifecycle starting at `Scheduled` (with Close date once settled).
- Flags act as overlay states (customer-level & loan-level) with system impacts (suppress notifications) and optional expiry.
- Widget-created applications may skip transition logging ("test application for new widget integration so it doesn't recognize that") — a gap to handle in the rebuild.

---

## 5. Dave's editorial comments (verbatim-ish)

Must-haves / build requirements:
- "I'll go through and narrate some of the workplaces and **critical functions that we need**."
- Header: "**I would suggest** that also **it's important** to be here is **critical contact details like an email and phone number, perhaps right under the name**."
- "There's also a **profile picture** for the individual, which is **important to have** because we compare the picture and the profile to the ID... **that will hopefully be automated through an integration**."
- "Everything is **time stamped and user stamped for security log purposes**, so we know **who did what when**."
- Changelog: "**That's important for tracking fraud** or ensuring that we have up-to-date information."
- "Fill with random data... **wouldn't be something that would be in live, but would be useful for a testing environment**."
- "**Typically we would like to have at least three years' residence history.** Currently the only stipulation is under one year prompts a previous address."
- "Social insurance number **legally cannot be required. We have to make that optional.** The only reason we would seek it is accuracy on credit bureau data."
- "This drop-down list is **controlled by a directory in the back end in the settings. We can add and take away from this.**"
- Other monthly expenses: "**People always vastly underestimate this. It's very poorly explained.**" (Should capture non-discretionary obligations: credit cards, loans, LOCs, phone, internet, insurance.)
- Employment type list: "**In this case, this list is not sufficient.**" — "Employed, really we are looking at **full-time, part-time, seasonally, self-employed**." / "**Employment insurance is not a valid source of income... I wouldn't even have that.**" / "**Disability which is a permanent disability would be valid. Retirement/pension is valid.**" / "**Student is not a valid income source so I would remove that.**" / "Other could be rental income, investment income."
- "We should basically have **additional income source buttons. It doesn't necessarily need to stop at two but two should be hopefully sufficient.**"
- "We'd also probably want a **former employer if they are under six months** of the hire date... **ideally we want three years of both address and employment information** to look at stability factors... five jobs in the last three years vastly reduces ability to predict repayment over a five-year term."
- Co-borrowers: "**In the new platform we need to completely redefine how co-borrowers or co-applicants work** — a **completely separate file for each individual** and pick who the co-borrower is based on **an ID number or an email**." / "There would basically be a **separate tab for borrower and co-borrower with the exact same information for both**, documents upload and a photo."
- Bank details: "Typically this is done through the **Flinks integration for the instant bank verification**; adding manually **requires manual review** of documentation to ensure the individual is the owner of the account and has the authority to authorize it as a payment source."
- Offer terms: "**All of these fields are locked in by the credit product... this makes sure that any of the credit terms are enforced per credit product.**"
- "The APR which is **calculated using the Canadian method**."
- Contacts/log: "This is typically where we would **log any collection efforts or any contacts**... anything that we want to record."
- Welcome message "**hidden for privacy purposes** — includes some information about login."
- Documents: "All of these documents are **set up as templates in the back end with merge fields**... this is showing some of the **e-signature coding for our integration with SignNow**."
- Document visibility: "**Loan documents are visible to just the back-end staff, personal documents to back-end staff and the borrower.**"
- Flags: "If it's do not contact, this would also **suppress any automatic notifications**, same with deceased... bankrupt... it would **not be appropriate to have automated emails go out**."
- Bank-statement attributes: "**This particular area does not work very well, it's poorly designed and needs to be updated.**"
- "This **recurring payments thing never seems to work, it always typically reports zero**."
- Expenses attributes: "**Very very poorly utilized... typically reports all zeros**... relies on the institution... to have initiated the correct transaction code. **There are no regulations or legal requirements to make sure these codes are accurate**... that's why this is insufficient and doesn't represent actuals. **So we need to build a system that learns to identify the expenses based on the information available, primarily on description, who instituted it, and also checking if they happen to use the correct code.**"
- Free cash flow: "**A very inaccurate or incomplete measure** — would include e-transfers from random individuals; mom and dad could send money."
- "**The micro lender [attribute] should also be expanded to identify the amount used on a monthly basis with micro lenders. It's a significant risk factor.**"
- Non-employer income: "**Very not useful**... a returned payment would be identified as quote-unquote income."
- Government income: "The government is **fairly good with coding**... WSIB or EI or other pensions will show up in the correct area."
- All-other-income: "**Not really something that you can count on as income... to be used towards an underwriting institution.**"
- Credit bureau consent: "**We really need to have confirmations because there are regulations around credit bureaus. We need to have express and explicit permission** to access credit reports because doing so will have the potential to impact that individual's credit score."
- Credit bureau data: "**In this case, this is not sufficient for what we would want to use it for.**" — wants proper **trade lines** (open date, closed date, installment/revolving type, limits, balance, delinquency R0–R9, last updated) and "**what's critically missing here is the reported minimum monthly payment. We would like to use that minimum monthly payment to populate their minimum payment requirements for financial capacity review purposes.**"
- "**We would also want to be able to click into each one of these trade lines to edit**... if we do **direct lending in the future and we're offering consolidation loans**, clicking one off would make a **transaction payable to that particular creditor** and mean the minimum payment is **no longer used in the financial capacity report**... also edit the balance if we confirm the payout is higher or lower... and **gross up** a bi-weekly minimum payment to monthly for the capacity review."
- Comments tab: "Anything we add here would be **visible to both the back-end staff and the vendors**."

Ignorable / test-only notes:
- The example application was "a test application for new widget integration so it doesn't recognize" workflow transitions.
- "Fill with random data" — testing environments only.
- EI and Student income options — remove.

---

## 6. Integrations / external services referenced

1. **Flinks** — instant bank verification (IBV); source of Bank details accounts (`FlinksService`) and the whole Bank statements tab (accounts, transactions, income/risk/expense/fraud attributes, raw report). Also used as identity/ownership verification source (name, address, email, phone).
2. **SignNow** ("sign-now") — e-signature; signature-field merge coding embedded in generated loan-document templates.
3. **Zum Rails** — third-party payment processor named in the PAD agreement ("The PAD Agreement will be administered by the third-party payment processor Zum Rails").
4. **Credit bureau** (Equifax-style; unnamed on screen) — soft pull / hard pull, scores, trade lines/liabilities, OFAC alert; regulatory consent requirements emphasized.
5. **Email service** — system emails (welcome email w/ login info), template emails, notification suppression via flags.
6. **SMS service** — templated/free-text SMS from Contacts tab.
7. **Leaflet / OpenStreetMap** — embedded address map in Customer details.
8. **Excel export** — amortization schedule export (Excel 2007+ and other formats).
9. **PDF generation/viewer** — Export customer details to PDF; multi-page document viewer with download/print.
10. **Widget intake** (PaySpyre's own website pre-qual widget) — referenced as the source of the test application (created outside normal flow; workflow transitions not logged).
11. **Future/aspirational**: automated ID-verification integration (photo-vs-ID); direct-lending consolidation disbursements to third-party creditors.
