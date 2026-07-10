# Underwriting Workplace — Feature Inventory

Source video: `02__WP_Underwriting` (~29 min, 88 frames @ 20s). Platform: Turnkey Lender UAT (`kelowna-dental-uat.turnkey-lender.com/App#/underwriting`, footer "PaySpyre Financial - 2026 7.9.0.30"). Narrator: Dave (business co-founder). Demo record: applicant "Antonietta Watsica", loan ID 5712, $7,500 / 48 months / 21.99%, status "Waiting for approval", vendor "Kelowna Dental Centre / Dr. Michael Webster".

---

## 1. Screen-by-screen walkthrough (chronological, with frame refs)

**f01–f03 (0:00–1:00) — Underwriting queue + Summary tab.**
Top nav: Origination | Risk Evaluation | Underwriting | Servicing | Collection | Reports | Archive | Settings | Tools. Left panel = application list (search field, Filters button, columns: ID, Name, Amount, Wait, A[ssignee avatar], S[tatus icon]; hover on status/assignee icons reveals tooltips; pagination "Page 1 of 1, 14 record(s)"). Right panel = loan header (name, ID 5712, province British Columbia, "None" branch, vendor/provider "Kelowna Dental Centre / Dr. Michael Webster"), status badge "Waiting for approval" (top right, orange). Action button row: **Unassign (with dropdown caret) | Reject | Bank Verification | Cancel | Send for reprocessing | Approve**. "Loan virtual date" edit control at right. Two alert banners between header and tabs: blue info banner ("David Wilson: Re-scoring caused by a new credit report", dismissible ×) and pink flag banner ("5712 Flag: First payment missed | Comment:", dismissible ×). Tab strip: **Summary, Risk score, Customer details, Bank details, Contacts, Documents, Credit history, Initial schedule, Credit bureau, Workflow, Flags, Bank statements, Map, Comments**.

**f04 (1:00) — Filters expanded.** Clicking Filters reveals filter dropdown row: **All time | Active | All branches | All loan types | All assignations | All vendors | All provinces**.

**f05–f13 (1:20–4:20) — Header expander + Summary tab detail.**
Header expands (chevron) to show: Credit product **BC4906-M04** (with info icon), Loan term 48 months, Start date 2026-05-28, Requested amount $7,500.00, Offered amount $7,500.00, Interest 21.99% per year, First due date 2026-06-15. Summary tab sections:
- *Contact Information*: Full name, Email, Main phone, Province, Alternative phone.
- *Loan details*: Loan ID, Creation date, Requested amount, Offered term, Interest, Installment, System decision (**Manual review**), Credit risk (**High**), APR (22.27%).
- *Previous activity*: Previous loans #, Previous offers #, Outstanding balance, Applied payment holidays, Late payments #, Max DPD, Days past due.
- *Credit bureau check*: Status (Hit), Score (644), Total balance ($59,572.00), Number of defaults (0), Number of bankruptcies (1), Bankruptcy date (2007-12-01).
Dave: wants phone + email promoted to the static header; wants CB summary expanded (open accounts, minimum payments, inquiries, public records/collections).

**f14–f39 (4:20–13:00) — Risk score tab (the "completely new" tab vs originations).**
Top: horizontal pipeline of check nodes, all with green check circles: **Application Check → Anti-fraud Check → Geolocation → Web Activity / Behavior Check → Cyber Security Check → Credit Bureau → ID Verification → Bank Account Check**.
- *Decision Summary* card: Creditworthiness color scale (5 segments red→green with marker on **Poor**); System decision: **Manual review**; text "The System recommends additional loan application review by an Underwriter. To make a final decision on the loan, review the Creditworthiness details below."; Date 2026-05-26 6:11 PM.
- *Creditworthiness breakdown* cards:
  - **Application check — Scoring** (Manual review): gauge 416/1000; Probability of default 3.7%; Risk level High; Odds (Good:Bad) 26:1; Risk segment "Restricted client"; Recommended decision text ("This range is characterized by acceptable exposure to the credit risk. A careful analysis of the loan application and additional information is recommended."). Business rules footer: Manual review, 10 green / 2 yellow.
  - **Anti-fraud Check — Business rules** (Auto approve): 7 green.
  - **Geolocation — Business rules** (Auto approve): 2 green.
  - **Credit bureau check — Scoring**: gauge 644/1000 (banded 579/669/739/799). Business rules footer: Manual review, 2 green / 2 yellow.
  - **Cyber security check — Threat level**: error state "Something went wrong when we tried to pull the required information from the data provider. Please, contact technical support." (pink card).
  - **Web Activity — Business rules** (Auto approve): 5 green.
  - **Bank account check — Scoring** (Manual review): gauge 330/1000; Probability of default 12%; Risk level Highest; Odds (Good:Bad) 7:1; Risk segment "Strongly Restricted client"; Recommended decision "Strongly restricted client. System recommends making a detailed client check." Business rules footer: Manual review, 15 green / 2 yellow.
- *Business rules* section (filter dropdown "All matched", Expand all / Collapse all), accordion groups with Result | Name | Comment columns:
  - **Application check rules**: Employment (yellow) — "The borrower has no official employment"; DTI (Debt to Income Ratio) (yellow) — "DTI ratio is 61.55%".
  - **Bank provider rules**: Average Monthly Free Cash Flow (yellow) — "Average Monthly Free Cash Flow is much less than minimal income (37.12)"; Income (Loan Application) vs average total inflow (yellow) — "The customer has income 1919.00 less than his average total inflow 3154.20."
  - **Credit bureau rules**: Bankruptcy check (yellow) — "The number of bankruptcies in the CB report exceeds 0"; Credit bureau score (yellow) — "Credit bureau score is below 660".
Dave narrates his 5-level risk-rank model (excellent/good/average/weak/poor), bankruptcy policy, and heavy criticism of this scoring design (see §5).

**f40 (13:00) — Customer details tab.** Buttons: **Blacklist | Export to PDF | Changelog**. Sections: *Personal information* (First/Middle/Last name, Date of birth, Email, Marital status, Number of dependents, Citizenship, Education), *Address* (Resides at "10 years, 10 months", Street, Apartment, City, Province, Postal Code, Residential status "Own", Monthly Mortgage Payment $900) + embedded street map (Leaflet/OpenStreetMap) with pin and zoom (+/−). Dave: same as originations; notes Blacklist button prevents future applications; map zooms to street address (fake data here).

**f41 (13:20) — Bank details tab.** "Add bank account" button. *Bank accounts* table: Name (FlinksCapital ×2, star = primary), Account number, Routing number, Institution number, Account holder, Type, Source ("FlinksService"), Actions. Same as originations.

**f42–f45 (13:40–15:00) — Contacts tab (communication log).** Buttons: **Send Email | Send SMS | + Log message**. Filters: All time, All statuses; toggle **"Show system messages"**. Table: Date | User | Subject/Purpose | Method | Result | Status (green check) | Comment | Actions (view-message icon). Rows shown (all User=System): "Waiting for a decision" (Dashboard notification), "PaySpyre Financial - Loan Application Submitted" (Email), "Bank Account Verification Request" (Dashboard notification), "PaySpyre Financial - Bank Account Verification Required" (Email), "PaySpyre Financial Registration Completed" (Email). Click icon opens the exact message body. Dave: this full-content comms log is critical for legal defensibility (court/collections evidence).

**f46 (15:00) — Initial schedule tab.** Buttons: **Change terms | Change due dates**; Excel export (Excel 2007+ dropdown); "Principal: $0.00" indicator. Amortization table: # | Date | Total | Principal | Interest | Fee (editable dropdown per row, e.g. $21.00, $1.00) | Close date | Status (row 1 **Missed**, red; others **Scheduled**, green) | Cash flow (detail icon per row). Installment $235.63/mo over 48 rows. Dave: Change-due-dates feature unnecessary (just change terms/first payment date); rails against 4-credit-products-per-bracket for payment frequencies — wants frequency selection inside one bracketed credit product.

**f47–f53 (15:20–17:40) — "Edit offer details" modal (Change terms).** Fields: Province* (dropdown, British Columbia), Credit product* (dropdown BC4906-M04, info icon), Loan amount* ($7500), Term* (unit dropdown "Month" + 48), Interest* (21.99 % per Year), Vendor (Kelowna Dental Centre), Provider (Dr. Michael Webster, with **Clear** button), Start date* (date picker 2026-05-28), checkbox **"Use custom first due date"** + Custom first due date* (2026-06-15). OK / Close.

**f54–f56 (17:40–18:40) — Credit bureau tab.** Buttons: **Hard pull | Soft pull**; "Available reports:" dropdown ("2026-05-26 6:11 PM - Hard"). Sections:
- *Summary*: Full name, Score 644, Check date, Status Hit, Number of defaults 0, Number of bankruptcies 1, Age 1967, Active accounts 10, Thin file No, Inquiries in the last 6 months 2, Total balance $59,572.00, OFAC Alert No.
- *Liabilities' overview* table: Type (Credit card, Mortgages, Auto loan, Payday, Other, Total) × Open accounts | Max DPD | Total balance.
- *Characteristics*: Active accounts 10, Accounts in last 6 months 0, Closed accounts 15, Worst current DPD 30, Worst closed DPD 90, Current defaults 0, Closed defaults 0, Current bankruptcies 1, Bankruptcy date 2007-12-01, Inquiries last 3 months 0 / last 6 months 2, Fraud alert No, Number of credit cards with exceeded limit 0, Residence type Unknown.
- *Liabilities* table: Created | Loan type | Limit | Balance | Days Past Due | Status (Closed) | Last update | Account type (Individual).
Dave: whole tab needs redesign — interactive sorting by tradeline type, editable trade lines, consolidation click, inquiries/public records/bankruptcies listings missing.

**f57–f63 (18:40–21:00) — Workflow tab (status audit trail).** Table: Date | Previous status | New status | Comments | User. Rows: Pre-origination→Origination (David Wilson); Origination→Auto processing (David Wilson); Auto processing→Bank account verification ("Customer is expected to verify their bank accounts on Customer's Dashboard.") (System); Bank account verification→Waiting for approval ("Bank Scoring: Refer") (System); Waiting for approval→Auto processing ("Re-scoring caused by a new credit report") (David Wilson); Auto processing→Waiting for approval (System). Dave describes his target state machine and verification-matching doctrine (see §4/§5).

**f64 (21:00) — Map tab.** Description text: "Analyze the geolocations of the borrower collected when they apply for a loan and log into the System. The Heat Map is based on Risk Level values for all loan applications registered in the System. It shows how your customers are grouped by their postal addresses. It also shows all the saved coordinates for the current customer." Three selector columns with info icons: **GPS Location** (Last ▾), **Postal address** (Current ▾), **IP Address** (Current loan ▾). World heat map (Leaflet/OSM) with risk-colored blobs + pin.

**f65–f66, f71–f74, f82–f84, f88 — Comments tab.** "Add your comment…" input; chronological feed; each entry = user (or System) + timestamp + status-transition chips (e.g. WAITING FOR APPROVAL → AUTO PROCESSING) + comment text. System auto-comments on every status move with timestamp. Entries mirror the Workflow rows.

**f67–f69 (22:00–22:40) — "Reject loan" modal.** Rejection reason* multi-select dropdown ("None selected"), checkbox list: **Fraud, Duplicated, Unemployed / No Income, Internal Credit Score Below Minimum Requirements, Credit Score Below Minimum Requirements, Blacklisted, Many active loans, Borrower Cancellation, Ability to Pay Below Minimum Requirements, Application Error, Non-Resident**. Comments rich-text editor (H1 H2 H3 B I lists undo/redo). "+ Add user notification" button (e.g. notify vendor). OK / Cancel. Reasons are maintained in a Settings directory (add/remove).

**f70 (23:00) — Bank Verification confirm dialog.** "Bank account was verified at 2026-05-26. Are you sure you want to send this loan for Bank Verification?" Confirm / Cancel. Dave: request goes to the borrower's dashboard for the applicant to initiate/complete — consent cannot be bypassed.

**f75–f77 (24:40–25:20) — "Cancel loan" modal.** Cancel reason* dropdown (required-field validation shown in red "This field is required"; options per narration: customer request, no documents, unable to contact, application error — dropdown didn't render on video), Comment rich-text editor, "+ Add user notification", OK / Cancel. Cancellation = non-credit-decision termination, distinct from rejection.

**f78–f81 (25:40–26:40) — "Sending for reprocessing" modal.** Rich-text comment editor, "+ Add user notification", Confirm / Cancel. Sends the file back to the Originations workplace for vendor/back-office rework (changed terms, more info).

**f85–f87 (28:00–28:40) — "Create Loan Offers" modal (Approve flow).** Per-offer block: Province*, Credit product*, Loan amount*, Term* (unit+value), Interest* (% per Year), Vendor, Provider (+Clear), Start date*, "Use custom first due date" checkbox + Custom first due date*; delete-offer icon per block; **"+ Add Offer"** to stack multiple offers; OK / Cancel. On OK the offer(s) go to the borrower dashboard for review & acceptance.

---

## 2. Complete feature checklist

### Queue (left panel)
- [ ] Application list scoped to underwriting-stage statuses
- [ ] Search field over applications
- [ ] Filters button expanding filter bar: time range, active/status, branch, loan type, assignation, vendor, province
- [ ] Columns: ID (sortable), Name, Amount, Wait (days in queue, e.g. "41d"), Assignee avatar, Status icon
- [ ] Hover tooltip on status icon (status name) and assignee avatar (user name)
- [ ] Row selection loads application in right panel
- [ ] Pagination (first/prev/page N of M/next/last) + record count

### Loan header (right panel)
- [ ] Standard header: applicant name, loan ID, province, branch, vendor/provider chain
- [ ] Prominent status badge (e.g. "Waiting for approval")
- [ ] Expand/collapse detail strip: credit product code (+info tooltip), loan term, start date, requested amount, offered amount, interest, first due date
- [ ] "Loan virtual date" control (test-time travel)
- [ ] DAVE ADD: main phone + email pinned in static header

### Action buttons (underwriting-specific)
- [ ] Unassign (with dropdown for assign-to)
- [ ] Reject (modal: reason directory multi-select + rich-text comment + add user notification)
- [ ] Bank Verification (confirm dialog; warns if already verified with date; request lands on borrower dashboard for applicant-initiated completion)
- [ ] (per narration) Credit bureau request button — sends soft-pull authorization to borrower dashboard
- [ ] Cancel (modal: reason directory + comment + user notification; non-credit-decision closure)
- [ ] Send for reprocessing (modal: comment + user notification; returns file to Originations for vendor rework — segregation of duties)
- [ ] Approve → Create Loan Offers modal, multi-offer support ("+ Add Offer"), offers sent to borrower dashboard for acceptance

### Alerts zone (between header and tabs)
- [ ] Info banners (user + message, e.g. re-scoring note), dismissible
- [ ] Flag banners (e.g. "First payment missed | Comment:"), dismissible, color-coded

### Tabs
- [ ] Summary: contact info, loan details (incl. system decision, credit risk, APR), previous activity, credit-bureau mini-summary
- [ ] Risk score: check pipeline, decision summary w/ creditworthiness scale, per-check scorecards (gauge, PD%, risk level, odds, risk segment, recommended decision), matched business-rules accordion w/ green/yellow counts, All-matched filter, expand/collapse all
- [ ] Customer details: personal info + address + street map; Blacklist, Export to PDF, Changelog buttons
- [ ] Bank details: bank accounts table (source-tagged, e.g. FlinksService), primary-account star, Add bank account
- [ ] Contacts: full communication log (email/SMS/dashboard notification), send email/SMS, log manual message, filters, show-system-messages toggle, view exact sent message body
- [ ] Documents (stated: same as originations)
- [ ] Credit history (stated: same as originations)
- [ ] Initial schedule: amortization table w/ per-row fee editing, missed/scheduled statuses, cash-flow drill-in, Excel export, Change terms modal, Change due dates modal
- [ ] Credit bureau: Hard pull / Soft pull buttons, historical report picker, summary/liabilities overview/characteristics/liabilities tradeline table
- [ ] Workflow: status-transition audit (date, previous status, new status, comment, user)
- [ ] Flags (stated: same as originations)
- [ ] Bank statements (stated: same as originations)
- [ ] Map: risk heat map + per-customer GPS / postal address / IP address coordinate selectors
- [ ] Comments: free comment input + auto system comments on every status transition (timestamped, with from→to chips)

### Automation behaviors shown/described
- [ ] Auto-processing status: system verifies application data automatically
- [ ] Auto re-scoring triggered by new credit report
- [ ] System-generated decision recommendation (Auto approve / Manual review / fail) per check and overall
- [ ] Automatic system comments + notifications at each status change (registration, bank-verification request, waiting-for-decision, etc.)
- [ ] Rejection/cancellation reason directories configurable in Settings
- [ ] Underwriting decision rules / scorecards configured in back-end Settings

### Dave's build-deltas (new requirements, not in Turnkey)
- [ ] Phone/email in static header
- [ ] Expanded CB summary: open accounts, minimum payments, inquiry count, public records/collections count+value
- [ ] 5-tier risk rank (excellent/good/average/weak/poor); poor = auto-fail; tier drives limit + rate (worse tier → lower limit, higher rate)
- [ ] Bankruptcy hard-fail rule + policy: discharged ≥2 years + re-established credit to qualify
- [ ] No auto-reject on failed rules: route to human underwriter (vendor override scenario, e.g. $1k residual on $8k treatment)
- [ ] Application scoring must use VERIFIED bank-verification data (income/expenses/cash flow), not self-reported; AI to replace Dave's manual line-by-line transaction review (thousands of tx)
- [ ] Bank-account scoring redesign (current one "very poor", always low)
- [ ] FICO = one input to internal score, not the end-all
- [ ] Credit bureau tab redesign: sort by credit cards/mortgages/payday loans, editable trade lines, consolidation click, show inquiries/public records/bankruptcies
- [ ] One credit product per amount bracket with weekly/biweekly/semi-monthly/monthly frequency selection inside (not 4 products per bracket)
- [ ] Drop change-due-dates (use change terms/first payment date)
- [ ] Standardized, regulation-compliant, non-discriminatory, defensible rejection reasons + notifications
- [ ] Verification cross-match engine: bank verification + ID verification + credit bureau vs application, all vs each other; any mismatch → mandatory human intervention (never ignored, never auto-rejected)
- [ ] Auto risk score/decision/offer generated only after all verifications match
- [ ] Full-content communication archive (subject + body + method + timestamp + status) for legal evidence
- [ ] Consent-first data pulls: borrower authorizes bureau/bank pulls from their dashboard; only pre-defined loan-agreement clause allows collections-time re-pull

---

## 3. Data fields visible (by screen/form)

**Queue list:** ID; Name; Amount; Wait (days); Assignee; Status. Filters: time, active/status, branch, loan type, assignation, vendor, province.

**Loan header:** Applicant name; Loan ID; Province; Branch (None); Vendor / Provider; Status badge; Credit product (BC4906-M04); Loan term; Start date; Requested amount; Offered amount; Interest (% per year); First due date; Loan virtual date.

**Summary tab:**
- Contact Information: Full name; Email; Main phone; Province; Alternative phone.
- Loan details: Loan ID; Creation date; Requested amount; Offered term; Interest; Installment; System decision; Credit risk; APR.
- Previous activity: Previous loans #; Previous offers #; Outstanding balance; Applied payment holidays; Late payments #; Max DPD; Days past due.
- Credit bureau check: Status; Score; Total balance; Number of defaults; Number of bankruptcies; Bankruptcy date.

**Risk score tab:** Check names (Application, Anti-fraud, Geolocation, Web Activity/Behavior, Cyber Security, Credit Bureau, ID Verification, Bank Account); System decision; Creditworthiness tier; decision date/time. Per scoring card: Score gauge (0–1000 w/ band thresholds); Probability of default %; Risk level; Odds (Good:Bad); Risk segment; Recommended decision text; per-card rule outcome (Auto approve / Manual review) + green/yellow rule counts. Business rules rows: Result; Name; Comment — Employment; DTI (Debt to Income Ratio); Average Monthly Free Cash Flow; Income (Loan Application) vs average total inflow; Bankruptcy check; Credit bureau score.

**Customer details tab:** First/Middle/Last name; Date of birth; Email; Marital status; Number of dependents; Citizenship; Education; Resides at (duration); Street; Apartment; City; Province; Postal Code; Residential status; Monthly Mortgage Payment; map coordinates.

**Bank details tab:** Bank account Name; Account number; Routing number; Institution number; Account holder; Type; Source; primary flag; Actions.

**Contacts tab:** Date; User; Subject/Purpose; Method (Email / Dashboard notification / SMS); Result; Status; Comment; message body (via icon); filters (time, status); Show system messages toggle.

**Initial schedule tab:** #; Date; Total; Principal; Interest; Fee (editable); Close date; Status (Missed/Scheduled); Cash flow; Principal remaining; installment amount; Excel export format.

**Edit offer details / Create Loan Offers modals:** Province*; Credit product*; Loan amount*; Term* (unit + count); Interest* (% per Year); Vendor; Provider (+Clear); Start date*; Use custom first due date (checkbox); Custom first due date*.

**Credit bureau tab:** Full name; Score; Check date; Status; Number of defaults; Number of bankruptcies; Age (birth year); Active accounts; Thin file; Inquiries last 6 months; Total balance; OFAC Alert. Liabilities overview: Type (Credit card/Mortgages/Auto loan/Payday/Other/Total); Open accounts; Max DPD; Total balance. Characteristics: Active accounts; Accounts in last 6 months; Closed accounts; Worst current DPD; Worst closed DPD; Current defaults; Closed defaults; Current bankruptcies; Bankruptcy date; Inquiries last 3/6 months; Fraud alert; Number of credit cards with exceeded limit; Residence type. Liabilities table: Created; Loan type; Limit; Balance; Days Past Due; Status; Last update; Account type. Report picker: timestamp + Hard/Soft.

**Workflow tab:** Date; Previous status; New status; Comments; User.

**Map tab:** GPS Location (Last/…); Postal address (Current/…); IP Address (Current loan/…); risk-level heat map.

**Comments tab:** author; timestamp; from-status → to-status chips; comment text; comment input.

**Reject loan modal:** Rejection reason* (Fraud; Duplicated; Unemployed / No Income; Internal Credit Score Below Minimum Requirements; Credit Score Below Minimum Requirements; Blacklisted; Many active loans; Borrower Cancellation; Ability to Pay Below Minimum Requirements; Application Error; Non-Resident); Comments (rich text); user notification target.

**Cancel loan modal:** Cancel reason* (per narration: customer request; no documents; unable to contact; application error); Comment; user notification. Required-field validation.

---

## 4. Workflow / state machine

**Statuses observed (Workflow tab + chips):** Pre-origination → Origination → Auto processing → Bank account verification → Waiting for approval (→ Auto processing again on re-score → Waiting for approval). Schedule-row states: Scheduled / Missed. Loan-list statuses shown as icons (approved check, waiting lock, etc.).

**Transitions shown:**
1. Pre-origination → Origination (user)
2. Origination → Auto processing (user or auto) — system verifies all info
3. Auto processing → Bank account verification (system; borrower must verify accounts on their dashboard)
4. Bank account verification → Waiting for approval (system; "Bank Scoring: Refer")
5. Waiting for approval → Auto processing (user-triggered re-score on new credit report)
6. Auto processing → Waiting for approval (system)

**Underwriter decision fan-out from Waiting for approval:**
- **Approve** → Create Loan Offers (1..n offers) → sent to borrower dashboard for review/acceptance
- **Reject** → credit decision, reason-coded, notifications (adverse action)
- **Cancel** → non-credit closure (customer request / no docs / unable to contact / application error)
- **Send for reprocessing** → back to Originations for vendor amendment/resubmission (PaySpyre never edits vendor applications — segregation of duties)
- **Bank Verification / Credit bureau request** → consent request pushed to borrower dashboard; process loops through verification statuses again

**Decisioning model (as-is):** 8 checks (application scoring, anti-fraud rules, geolocation, web activity/behavior, cyber security, credit bureau, ID verification, bank account scoring) feed a composite creditworthiness tier + system decision (Auto approve / Manual review / decline). Rules produce green (pass) / yellow (manual-review trigger) results with threshold comments. Scorecards output score, PD%, odds, risk segment, recommended decision.

**Dave's target model:** AI/platform does the majority of underwriting via decision rules set in Settings. Sequence: verify everything first (bank verification + ID verification + credit bureau cross-matched against the application and each other) → only when concurrent/accurate does the system generate automatic risk score + decision + offer. Any discrepancy (even a misspelled name) → mandatory human review — never ignored, never auto-rejected. 5 risk tiers (excellent/good/average/weak/poor); poor auto-fails; weak is the minimum qualifying tier; higher tier ⇒ higher limit + lower rate; lower tier ⇒ lower limit + higher rate. Hard-fail overlays (e.g. bankruptcy: must be discharged 2+ years with re-established credit) still route to a human underwriter because a vendor may grant an override approval (e.g. financing $1,000 residual of an $8,000 treatment). Rejection ≠ cancellation (credit decision vs administrative closure).

---

## 5. Dave's editorial comments (verbatim-ish)

**Automation vision**
- "The idea would be that the majority of the underwriting is automated through AI and the platform and through... underwriting decision rules that would be set in the settings in the back end."
- "Once the information is verified... then and only then should the system generate an automatic risk score and decision and offer."
- "If anything doesn't match in the verification process, it needs human intervention. It cannot be ignored. It cannot be subverted. It shouldn't also be rejected. We need to have human oversight."
- "We do need to verify any discrepancies and put those discrepancies to rest before continuing."

**Header/summary**
- "I'd like to add the phone number, main phone number and the email to the header... it's just easier to have that information readily available in a static place."
- "This credit bureau check stuff is insufficient for what we would want to do, we would want to redesign that to encompass more information... we would want to include the number of open accounts, the minimum payments on the open accounts, number of inquiries on the credit report, the number and value of any public records or collections."

**Risk tiers / policy**
- "We're going to have five levels of credit worthiness... excellent, good, average, weak and poor. Anything that would be poor would essentially be an automatic fail."
- "As the categories or the credit risks increase so do the credit limits available... the worse the risk ranking is... the lower we should limit the amount... and give it to them at a higher rate to compensate."
- "Bankruptcy would be a situation where there would be a hard fail... a former bankrupt needs to be... discharged for a minimum of two years with established credit in order to be considered."
- "Pretty much anything that does not qualify should not immediately be rejected. It should be sent to a human underwriter for review because we might have vendors that are willing to provide an override approval."

**Scoring criticism (must-fix)**
- "This risk scoring system is not one that I particularly like or agree with. One of the main faults is this application scoring check is based off of self-reported and not verified information and is not pulling in actual data from the bank verification on income and expenses and cash flow."
- "Right now, I'm doing that manually, which is very time-consuming... sometimes there's thousands of transactions that need to be reviewed... should be something that an AI or a platform that has been automated can do in a very short period of time."
- "Antifraud and geolocation are fine."
- "Credit bureau, we would want to take into consideration the credit score or the FICO score, but that would be one part of our internal scoring system and not the end-all be-all."
- "The bank account scoring in this is very poor and is not very accurate. It's typically always low and I don't really agree with how it's defined. We need to evolve this part of our platform to be more modern and encompass more automation in a broader sense of metrics."

**Communications log (must-have)**
- "We can click on this icon to see the exact message that was sent, which is critical to continue to keep an ongoing account of what has been sent."
- "If we end up in a situation where we have to take a borrower to court, we need to have very, very detailed communication records... It is a very important thing that we keep an ongoing log and record of all communications, not just the subject and purpose, but the actual communication itself."

**Schedule / credit products**
- "We don't really need to have a change in due dates section. If we needed to change due dates, we should just change the terms and change the first payment date."
- "In order to have weekly, biweekly, semi-monthly and monthly payment frequencies, we have to have four instances or four different credit products for the same loan size bracket, which is completely ridiculous. We should be able to just have a frequency selection within [one credit product]."
- "We don't want to have a loan, for example, for $1,000 that is over 84 months at a really low interest rate. That is silly... those are some of the reasons why we need to have a bracketed system that goes by amount."
- "That needs to be incorporated into the credit product creation process."

**Credit bureau tab (must-fix)**
- "This needs to be redesigned and to be much more interactive. We need to be able to sort by credit cards, sort by mortgages, sort by payday loans. We need to be able to edit trade lines. We need to be able to click for consolidation. There's no inquiries listed. There's no public records listed. There's no bankruptcies listed... I think [it] is very poorly designed and not really functional for what we're looking to do."

**Rejection / compliance**
- "We need to standardize rejection reasons and notifications to make sure they're compliant with all regulations... and disclosures and make sure that they are not discriminatory and defensible."
- "The borrowers don't dictate whether we approve or reject something... we just need to be able to have the latitude in order to be able to legally, defensively reject an application."
- "These are added in a directory in the settings so we can add and remove particular rejection reasons."

**Consent / privacy**
- "Any bank verification or any credit bureau request... goes to the borrower's dashboard to be initiated and completed by the applicant. We can never subvert or get around the applicant's approval."
- "For collections purposes, we may want to verify address, verify employment... by refreshing a credit report. But we need to be able to specify our rights to do that within the loan agreement... it's a measure that wouldn't be used a lot."

**Reprocessing / segregation of duties**
- "PaySpyre needs to keep a segregation of duties. We don't amend vendor applications for vendors on their behalf... if it needs to change, we send it back to the vendor to be changed by the vendor... so that it can never come back on us that a change was made and we weren't authorized to do so."

**Offers**
- "We can create multiple different offers if that's something that has been indicated as a need... someone is thinking about putting variable amounts down and they want to review the disclosure for different terms of different options."
- "Once we wanted to move forward with an offer and approve it, we would click OK and that offer would be sent to the borrower's dashboard for review and acceptance."

---

## 6. Integrations / external services referenced

- **Credit bureau** (hard pull + soft pull; FICO score; report history; OFAC alert field — bureau unnamed but PaySpyre plan = Equifax Canada). Borrower-consented pulls via dashboard; contractual re-pull for collections.
- **Bank account verification** — "FlinksService" is the account source in Bank details (Flinks); bank-account scoring + bank statements + cash-flow rules derived from it. Applicant-initiated via borrower dashboard.
- **ID verification** service (pipeline node; part of cross-match doctrine).
- **Anti-fraud / cyber security / web-activity-behavior data providers** (cyber security check showed a provider-pull failure state).
- **Geolocation services** — GPS, postal-address geocoding, IP address capture; Leaflet + OpenStreetMap maps (street map + risk heat map).
- **Email + SMS gateways** and **borrower dashboard notifications** (contact log methods; PaySpyre stack = SendGrid/Twilio).
- **Excel export** (Excel 2007+ format) on schedule; **Export to PDF** on customer details.
- **Borrower dashboard** (portal) as the consent/acceptance surface for verifications and offers.
