# Settings Part 2 — Feature Inventory

Source: `08__WP_Settings_Part_2` (~55.5 min, 167 frames @ 20s). Environment: `kelowna-dental-uat.turnkey-lender.com/App#/settings/...`, footer "PaySpyre Financial - 2026 7.9.0.30" (Turnkey Lender UAT, white-labeled "PaySpyre Financial"). Narrator: Dave. Part 1 ended at Loan Settings > Calendar; this part covers everything from the Decision Engine to the end of Settings.

Frame↔time: frame N starts at (N−1)×20s. e.g. f0030 ≈ 9:40.

---

## 1. Settings sections covered in this video (full settings nav tree)

Left nav (visible throughout, f0001 etc.):

- **Accounts** (>) — not opened in this video (Part 1)
- **Company settings** (>) — not opened (Part 1)
- **Integrations** — not opened (Part 1)
- **Loan settings** (>) — Credit products / Delinquency buckets / **Calendar** (Part 1 ended here; f0001 shows the Calendar "Holidays and weekends" screen: month grid July 2026 with weekends highlighted, Year dropdown = 2026, "Add new holiday" button, prev/next month arrows)
- **Decision engine** (>) — sub-items: **Decision rules**, **Scorecards** ← covered
- **Notifications** (>) — sub-items: **General system notifications**, **Due date reminders**, **Email template**, **Custom messages design** ← covered
- **Application process** (>) — sub-items: **Application form**, **Application flow**, **Dictionaries**, **Disclaimer**, **System documents**, **Personal & Loan documents**, **Co-applicant** ← covered
- **Security** (>) — sub-items: **API clients**, **Audit trail** ← covered
- **Appearance** (>) — sub-items: **Appearance** (Color Themes), **Field settings** ← covered
- **Kelowna settings** (custom item, no children) — Past due settings ← covered

Top main nav (workplaces, context): Origination, Risk Evaluation, Underwriting, Servicing, Collection, Reports, Archive, Settings, Tools.

---

## 2. Section-by-section walkthrough (chronological, with frame refs)

### 2.0 Carryover: Loan settings > Calendar (f0001, 0:00)
"Holidays and weekends" calendar (July 2026), weekend columns tinted, Year selector (2026), **Add new holiday** button, Reset + month arrows. Dave only uses it as the jump-off: "In the previous settings video we ended off at Loan Settings Calendar."

### 2.1 Decision engine > Decision rules (f0003–f0026, ~0:40–8:30)
URL `/settings/decision/rules/`. One long page of rule groups. Every rule row = enable **checkbox** + rule name + description + optional numeric parameter field(s) + **action dropdown** on the right (visible value everywhere: **"Manual review"**; per Dave the options are *manual review, alter, reject, no action (just records)*). Page has Save changes / Reset changes (per transcript).

**Fraud prevention rules** (f0003–f0009):
- **Open sanctions database** ✓ — 4 sub-checks each with its own action dropdown (all "Manual review"):
  - *Suspicious name*: borrower's full name found in sanctions list
  - *Name and dob (YYYY)*: full name + year of birth found in sanctions list
  - *Name and dob (MM/YYYY)*: full name + partial DOB (month+year)
  - *Sanctioned borrower*: full name + full date of birth found on sanctions list
- **Blacklisted** ✓ Manual review — checks Full name, Social Insurance Number, Main phone, Alternative phone, Work phone, Email, Driver's license against internal blacklists
- **Main phone** ✓ Manual review — main phone already used by another borrower
- **Suspicious age** ✓ — Max age: **100** — Manual review
- **Suspicious phone number** ✓ Manual review — phone appears in suspicious-phones list (fed by the Suspicious phones dictionary)
- **Driver's license** ✓ Manual review — driver ID unique in database
- **Social Insurance Number** ✓ Manual review — SIN unique in database

**Application check rules** (f0009–f0021):
- **Citizenship** ✓ Manual review — borrower is a Canadian resident
- **DTI (Debt to Income Ratio)** ✓ N: **50** — Manual review (Dave's worked example: rule = hard ceiling / manual-review trigger; the *value* of DTI is scored separately in the scorecard bins)
- **Employment since (date)** ✓ N: **6** — Manual review (employed with company for too short a period)
- **Employment** ✓ Manual review — borrower has official employment
- **Derivatives, depending on customer choice of income** ✓ N: **6** — Manual review (insurance/retirement income, or self-employed for too short)
- **Net income** ✓ Limit: **1500** — Manual review (net income not less than minimum set value)
- **PTI (Monthly Payment to Net Income Ratio)** ✓ N: **20** — Manual review
- **Age by the end of a loan period** ✓ Male: **65**, Female: **65** — Manual review (age at end of loan term must not exceed set value)
- **Residence at the registration address** ✓ Min months: **6** — Manual review
- **Number of active loans** ✓ Max active loans: **2** — Manual review
- **Delinquency check** ✓ Minor DPD: **14**, Major DPD: **30** — separate action dropdowns for Minor and Major (both Manual review)
- **Minimal age** ✓ Minimal age: **19** — Manual review

**Geolocation** (f0024, ~7:40):
- **IP address lookup distance** ✓ Range: **150** — Manual review (distance between borrower geolocation and coordinates from IP address lookup)
- **Geolocation** ✓ Range: **150** — Manual review (distance between application-form address and current geolocation)

**Network security rules** (f0024):
- **Network threat level** ✓ — two action dropdowns: Medium Threat Level: Manual review; High Threat Level: Manual review
- **Proxy usage** ✓ Manual review — borrower using proxy server to apply
- **Tor system usage** ✓ Manual review — borrower using Tor

**Web activity/behavior rules** (f0024):
- **Application details pasted from the clipboard** ✓ Manual review ("last symbol of the filled fields is a space")
- **Replacement of attachments** ✓ Replacements: **6** — Manual review (attached docs removed/replaced too many times)
- **Suspected copy/paste form filling or abnormally fast typing** ✓ Time(min): **3** — Manual review
- **Suspected irresponsible behavior** ✓ Manual review (immediate selection of maximum loan term and loan amount)
- **Too doubtful about the loan term** ✓ Number of changes: **6** — Manual review (loan term changed too many times)

**Bank provider rules** (f0024, f0023, ~7:20–7:40) — driven by bank-statement/IBV data:
- **No credit transactions** ✓ Number of Days: **24** — Manual review
- **No debit transactions** ✓ Number of Days: **24** — Manual review
- **Nontypical payment behavior** ✓ Number of cases: **3** — Manual review (expenses exceeded portfolio average size by 2.5x too many times in last 3 months)
- **Negative balance** ✓ Number of Days: **30** — Manual review (too long period with negative balance)
- **Account Age in Days** ✓ Min Account Age: **60** — Manual review
- **Active Days in the Account (Last 90 Days)** ✓ Minimum Active Days: **20** — Manual review
- **Sum of Child Support Income - Government (90 Days)** ☐ (disabled)
- **Average Monthly Employer Income** ✓ Min Monthly Employer Income: **1500** — Manual review
- **Average Monthly Expenditure** ☐ (disabled)
- **Average Monthly Free Cash Flow** ✓ Min Monthly Free Cash Flow: **100** — Manual review
- **Average Monthly Government Income** ☐ (disabled)
- **Average Monthly Non-Employer Income** ☐ (disabled)
- **Average Monthly Telecom Payments** ✓ Maximum Telecom Payments: **250** — Manual review
- **Average Monthly Utility Payments** ✓ Maximum Utility Payments: **250** — Manual review
- **Count of NSF Fees (Last 90 Days)** ✓ Number of NSF fee: **0** — Manual review
- **Count of Stop-Payment Fees (Last 90 Days)** ✓ Number of Stop-Payment fee: **0** — Manual review
- **Micro-Loan Payment** ✓ Maximum Micro-Loan Payment: **0** — Manual review
- **Check borrower's employed income** ✓ Manual review (borrower is employed AND last-month Primary Employer Income=0 and Secondary Employer Income=0 and Other Employer Income=0)
- **Income (Loan Application) vs average total inflow** ✓ Manual review (income from loan application < 80% of last-3-month average total inflow)
- **Average total inflow vs minimal income** ✓ Manual review (last-3-month avg total inflow < 80% of minimal income)
- **Average total outflow vs minimal living cost** ✓ Manual review (last-3-month avg total outflow < 80% of minimal living cost)

**Credit bureau rules** (f0023–f0025):
- **Bankruptcy check** ✓ Max bankruptcies: **0** — Manual review
- **Defaults check** ✓ Max defaults: **1** — Manual review
- **Credit bureau inquiries** ✓ Minor: **10**, Major: **15** — separate Minor/Major action dropdowns (Manual review)
- **Credit bureau score** ✓ Second score threshold: **660**, First score threshold: **600** (each with ⓘ tooltip) — separate Second/First action dropdowns (Manual review)

**Co-applicant anti-fraud check** (f0025):
- **Co-applicants. Blacklisted** ✓ Manual review (co-applicant's phone numbers, email, full name, ID in internal blacklists)
- **Co-applicants. Suspicious age** ✓ Max age: **100** — Manual review
- **Co-applicants. Suspicious phone number** ✓ Manual review

**Co-applicant application check** (f0025):
- **Co-applicants. Minimal Age** ✓ Minimal age: **19** — Manual review

**Co-applicant credit bureau rules** (f0025–f0026):
- **Bankruptcy check** ✓ Max bankruptcies: **0** — Manual review (list continues below fold; mirrors borrower credit-bureau rule set)

### 2.2 Decision engine > Scorecards (f0027–f0044, ~8:40–14:20)
URL `/settings/decision/scorecards/`. Two scorecard tiles: **Application Check** and **Bank Account Check** (Dave calls this a "fragmented approach" — see §5). Per scorecard:
- **"Use custom scorecard"** button
- Tabs: **Scoring characteristics** | **Risk segments**
- **Risk segments** tab (f0027–f0031) — editable table, columns: Name | Risk level | Score range | PD | Expected distribution | Odds | Recommendations | System decision | Actions (edit pencil per row):
  | Name | Risk level | Score range | PD | Expected distribution | Odds | System decision |
  |---|---|---|---|---|---|---|
  | Target client | Low | 560–1000 | 0.58% | 13.37% | 160:1 | Manual review |
  | Acceptable client | Medium | 460–559 | 1.9% | 21.02% | 51:1 | Manual review |
  | Restricted client | High | 219–459 | 3.7% | 41.1% | 25:1 | Manual review |
  | Strongly Restricted client | Highest | 142–218 | 6.4% | 11.45% | 14:1 | Manual review |
  | Rejected clients | Default | 0–141 | 9.2% | 13.06% | 9:1 | Manual review |
  Recommendation strings per row (e.g. "The credit risk of potential borrowers in this risk group is very low", "...rejection of the loan application is recommended"). **Save changes / Reset** buttons.
- **Scoring characteristics** tab (f0033–f0044): shows placeholder graphic + message: **"The built-in scorecard is used. You can't edit characteristics of the built-in scorecard. However it's still possible to change risk segments configuration."** (i.e., the vendor's proprietary scorecard internals are a black box — Dave: "we don't have a lot of visibility into what these are.")

### 2.3 Notifications > General system notifications (f0045–f0081, ~14:40–27:00)
URL `/settings/notifications/general/`. **Audience tabs across the top: Customers | Co-applicants | Back-office users | Vendor users** (same option set per audience, independently configurable). Each event row: enable checkbox + name + trigger description + channel icon buttons on the right (email ✉, SMS 📱, dashboard/bell, and a "CC"-style icon); each group has **Enable / Disable** (group-level) buttons; page footer has **Enable all / Disable all** (f0079). Default: all enabled notifications go to the borrower dashboard (not configurable off); the 3 channels are dashboard, email, SMS.

Event groups & rows (Customers tab; ✓ = enabled in this install):
- **General system notifications**: Welcome message ✓ (user completed registration); Customer is registered from back office ✓ (send link for first log in and password set-up); Password reset (greyed; dashboard-icon only — triggered from Login page / Customer Management)
- **Loan status notifications**: Approved ✓; Rejected ✓; Soft pull rejected ☐ (credit bureau ran soft pull and didn't confirm borrower eligibility); Waiting for a decision ✓ (submitted for approval, to be processed by Underwriter); Loan disbursement ✓ (amount transferred to user's bank account); Written off ✓; Full repayment ✓; Canceled ✓; Loan offer confirmation ✓ (customer asked to accept the loan offer); Loan offer expired ✓; Loan restructured ☐; Awaiting initial payment ☐ (initial fee expected from customer); Initial payment is expired ☐
- **Payments**: Partial payment ☐; Full payment ☐; Payment is greater than the scheduled amount ☐; Special payment ☐ (repayment used to cover special fees); **Payment received ✓**; Card expired ☐; **Notice of NSF payment error ✓** (payment failed – NSF); **Notice of payment error ✓** (payment failed – not NSF; stop-payment/account-closed class errors); Upcoming card expiration ☐; Initial payment is paid partly ☐; Initial payment is paid in full ☐; Repayment transaction reversed ☐ (reversed from back-office); Repayment transaction edited ☐
- **Promise to pay**: PTP reminder ✓ (sent when User adds new PTP); PTP past due ✓ (no payment received by promise-to-pay date)
- **Payment holiday**: Payment holiday approved ✓; Payment holiday rejected ✓ (by loan manager)
- **Loan agreement**: Loan agreement signed ✓ (by Customer); Agreement signature reminder ✓ (recurring, every few days while signature pending); Agreement signature expired ✓ (signature period expired and loan closed)
- **Bank providers**: Bank Account Verification Request ✓ (loan approved; customer required to verify bank account); Bank account verification reminder ✓ (recurring every several days while pending); Bank account verification expired ✓ (period expired and loan closed)

**Per-notification editor** (Welcome message f0053–f0063; Rejected f0067–f0069):
- **Send email** toggle; **Preview** button
- **Email subject** field with **+ merge-field picker** (e.g. `Welcome to \*|CompanyName|*`, `\*|CompanyName|* - Application Rejection`)
- **Email body** rich/markdown editor (toolbar: H1 H2 H3, B, I, lists, undo/redo, + merge fields). Welcome body includes merge fields `\*|CompanyName|*`, `\*|EmailLogin|*`, CompanyWebsiteUrl link, Forgot Password link. Rejected body: `Vendor: \*|Vendor|*`, `Application #: \*|LoanId|*`, `Rejection Reason: \*|RejectionReason|*`, re-apply link
- **Attachments** multi-select dropdown (Select all / Select none / Reset, search): options **Loan agreement, Terms & Conditions, Privacy policy, Loan statements, Amortization Schedule** (f0057)
- Right side: info box "You can define a custom signature or greeting section for this email template. If custom greeting or signature section is filled, it overrides the corresponding global value." + **Greeting** editor (e.g. `Greetings \*|FullName|*, thank you for registering with \*|...`) + **Signature** editor
- **Send SMS** toggle → **SMS template** picker field (required when SMS on; Dave objects to it being a separate template, see §5). Rejected editor also shows **Send dashboard notification** toggle and **"Use same texts as for Email"** checkbox
- Buttons: **Save changes / Reset changes / Go back**
- **Email preview modal** (f0059): branded render — dark header with PaySpyre sparkle logo, teal box (Email Subject / subject line / date), greeting, body, standardized signature "If you encounter any problems please reach out to us. Regards, Customer Support Team, PaySpyre Financial Inc. (Test Server), support@payspyre.com, Finance, Your way. www.payspyre.com"

### 2.4 Notifications > Due date reminders (f0083–f0093, ~27:20–31:00)
URL `/settings/notifications/due/`. List screen with **Add reminder** button; each row has enable checkbox, channel icons, edit and **delete** (red) buttons.
- **Next payments**: `7 day(s) before payment`, `2 day(s) before payment`
- **Past due**: `7 day(s) past due`, `14 day(s) past due`, `21 day(s) past due`, `30 day(s) past due`, `60 day(s) past due`, `90 day(s) past due`
**Add/edit reminder form** (`/settings/notifications/due/add/reminder`, f0083–f0088): radio **Past due reminder** ⓘ / **Next payment reminder** ⓘ; for next-payment: checkbox **"Always send this reminder for upcoming due dates"** ⓘ (sends regardless of loan state — e.g. past-due borrowers still get "your next payment is due Friday"; Dave enabled it on all); **Offset days** field (required; helper text "Notification will be sent when the loan is past due for 7 day(s)"); **Send email** toggle → same subject/body/attachments/greeting/signature editor block (validation: "Email body cannot be empty"); **Send SMS** toggle → SMS template picker; Save changes / Reset changes / Go back. Preview modal same branded format (f0087).

### 2.5 Notifications > Email template (global) (f0095, ~31:20)
URL `/settings/notifications/template/`. "Here you can define global settings for all email notifications and due date reminders. If 'Greetings' or 'Signature' section is filled in, it will be used for all emails, unless the corresponding section is overridden for a specific notification type."
- **Greeting** editor: `Greetings, \*|CustomerName|*.`
- **Signature** editor: "If you encounter any problems please reach out to us.<br> Regards,<br> Customer Support Team<br> PaySpyre Financial Inc. (Test Server)<br> <a href="mailto:\*|SupportEmail1|*">\*|SupportEmail1|*</a><br> Finance, Your way.<br> www.payspyre.com"
- **Edit master template (HTML)** toggle (exposes the outer HTML wrapper — the teal box/header layout); **Preview**; **Save changes**

### 2.6 Notifications > Custom messages design (f0097–f0106, ~32:00–35:00)
URL `/settings/notifications/customTemplates/email`. "Back-office users can send custom messages to clients directly from their TurnKey Lender workplace. Here you can manage the design and the default contents of custom emails sent by employees."
- Tabs: **Email templates | SMS templates**
- Email templates table (Name | Subject | Actions edit/delete): **60 Past Due** ("Account Past Due 60 Days"), **Mergefield testing**, **Loan Statement**, **Vendor Email** (`\*|Vendor|* - \*|LoanId|*`)
- **Add template** button; **Edit master template (HTML)** toggle

### 2.7 Application process > Application form (f0107–f0115, ~35:20–38:00)
URL `/settings/process/form/`. **Application form editor**: **Add new field** button + search box; custom-fields grid currently "No data to display"; below, built-in **Photo** section with enable toggle (ON), fields: Name = "Photo", **Is required?** dropdown = No, Description = "Please upload your photo." Save changes / Reset changes. (Dave's plan: default/short/long application variants assignable per credit product — see §5.)

### 2.8 Application process > Application flow (f0117–f0128, ~38:40–42:20)
URL `/settings/process/applicationFlow/`:
- **Require origination confirmation** toggle ON — "If selected, new loan applications created by the borrowers will be placed in the Origination workplace. If not selected, auto-processing will take place and the applications will appear in Underwriting."
- **Use loan offer confirmation** toggle ON — **"How many days till loan offer expires"** = **30**
- **Multiple loan offers created by Underwriter** checkbox ✓ — **"Max number of offers"** = **3**
- **Save changes**

### 2.9 Application process > Dictionaries (f0129–f0136, ~42:40–45:00)
URL `/settings/process/dictionaries/`. Table of editable lookup lists (Caption | edit action): **Countries, Industry category, Loan cancellation reasons, Loan rejection reasons, Loan write-off reasons, Suspicious phones**.
- **Loan rejection reasons** list (11 items, paginated; visible behind dialogs): Unemployed/Bankruptcies(?), Inferior Credit Score Below Minimum Requirements, Credit Score Below Minimum Requirements, Blacklisted, Max active loans, Borrower Cancellation, **Ability to Pay Below Minimum Requirements**, Application Error, Non-Resident
- **Edit item** dialog (f0131): Title* ⓘ = "Ability to Pay Below Minimum Requirements", **Code*** ⓘ = "A03" (unique DB code), Description* ⓘ; OK/Close
- **Add new item** dialog (f0130): same Title/Code/Description with required-field validation
- Suspicious phones dictionary = the feed for the fraud-rule suspicious-phone check (add numbers found in the wild)

### 2.10 Application process > Disclaimer (f0137–f0140, ~45:20–46:20)
URL `/settings/process/disclaimer/`:
- **"Require disclaimer confirmation to submit the application"** toggle ON
- **Disclaimer text** rich editor ("Please check the box below to certify.")
- **Confirmations** table (Name | Actions): one row "Submit" (edit/delete) + **+ New Field** button (multiple checkbox confirmations supported)
- **Documents** — "Documents that will be used from the 'Documents' section": **Terms & Conditions ✓** ("A legal agreement between a lender and a customer... must agree to abide by..."), **Privacy policy ✓** ("A statement or a legal document that discloses some or all of the ways a lender uses customer's data...")
- **Preview**, **Save changes / Reset changes**

### 2.11 Application process > System documents (f0141–f0144, ~46:40–47:40)
URL `/settings/process/documents/`. First part = **Loan agreement** templates: "A contract between a borrower and a lender which regulates the mutual promises made by each party... **Every Credit Product can have its own loan agreement template** to support its specific details."
- **Merge fields** button (top-left) → **Merge fields dialog** (f0142): search, **Export to Word**, Collapse all / Expand all; tabs **Scalar Fields | Table Fields**; grouped field list with descriptions, e.g. *BorrowerSSN* ("Borrowers SSN. Information from the Application form"), *CardHint* (last digits of card), *CustomerName* (full name of the customer who owns this loan application), *ExpirationDate* (card expiration date); **General** group: *BaseAppPath* (base-path part of application URL for building links), *CompanyAddress*, *CompanyCity*, *CompanyCountry*, *CompanyLogo* (absolute URL to logo uploaded in System > Company Settings)… (long scrollable list)
- Template table (Title | Description | File name | Updated | Actions: upload/download, history/restore, preview-eye, edit, delete). Active default marked ✓. Rows visible:
  - **Loan Agreement — Default Template** (PaySpyre - Loan Agreement, 2024-11-06)
  - **BC1180** v10.75 - Elevation Dental (2026-04-26); **BC5055** v10.75 - Rock Dental; **BC2724** v10.75 - Arch Dental + Aesthetics; **BC8187** v10.75 - Brightside Dental; **BC6669** v10.75 - Yaletown Dentistry; **AB4464** v10.75 - New Teeth Today; **BC1555** v10.75 - Parkview Dental; **Lreview** 10.75 for Lreview; **BC4906-1075** v10.75 KDC Test; **BC4906-551** v10.50 Special Use; **BC4906-510** 510 Test; **AB9014** v10.25 - East Village Dental; **AB3912** v10.25 - Vegreville Family Dental; **AB5115** v10.25 - Montgomery Dental Centre; **BC4906-BiWkly** v10.20 - Kelowna Dental Centre - Bi-Weekly Payments; **BC6786** v10.00 - E Pro Dental; **BC5149** v10.00 - Selkirk Dental Centre; **BC4440** v10.00 - Dawson Creek Dental Centre; **BC9876** v10.00 - Urban Smiles Colwood; **BC3509** v10.00 - Summerland Dental Centre (+ more below fold)
  - → i.e. per-vendor(clinic)/per-use-case agreement template versioning, keyed by vendor codes (AB/BC prefix = province)
- Further down the page (narrated, ~47:30): **Terms and conditions** default template, **Privacy policy**, templates for **loan/account statements** and **amortization schedules**

### 2.12 Application process > Personal & Loan documents (f0145–f0148, ~48:00–49:00)
URL `/settings/process/doc-management/`. Defines the contents of the borrower-facing "Personal documents" and "Loan documents" sub-tabs.
- **Personal documents** table — columns: Title | Collect during the application | Required | Verification | Internal | Max files | Exp. period | actions (edit/delete):
  - *Borrower Identification (Front & Back of ID)* — Collect: Yes, Required: No, Verification: No, Internal: No, Max files: ≤2
  - *Co-Borrower Identification (Front & Back of ID)* — Yes / No / No / No / ≤2
  - *Documents* — Yes / No / No / No / – / –
  - **+ Add document**
- **Loan documents** table (same columns): *Documents* — Collect: No, Required: No, Verification: No, **Internal: Yes** (back-office only) — **+ Add document**
- **Add document dialog** (f0147): Title*, Description (rich editor), checkboxes: **Limit number of files**, **Collect during the application process**, **Internal** (only back-end staff can see), **Verification is needed**, **Custom file types**, **Max upload file size**; OK/Close

### 2.13 Application process > Co-applicant (f0149, ~49:20)
URL `/settings/process/coApplicant/`:
- **Use Co-applicant** toggle ON ⓘ
- **Label*** ⓘ = **"Co-Borrower"** (customizable display label)
- **Credit bureau check** toggle OFF ⓘ
- **Co-applicant is required** toggle OFF ⓘ
- **Send copy of loan-specific notifications to the Co-applicant** toggle ON ⓘ
- **Save changes**
(These exact values are echoed in the Audit-trail details modal: `IsEnabled: true, Label: "Co-Borrower", CreditBureauCheckIsEnabled: false, Mandatory: false, SendMailCopy: true, IsSupported: true`.)

### 2.14 Security > API clients (f0151–f0153, ~50:00)
URL `/settings/security/api/`. **Add API client** button. Table (Name | API Keys | Permissions | Actions edit/delete):
- **Turnkey Lender Client** — 1 key — Permissions: Front-office, Management, Payment options
- **Taid** — 1 key — Front-office, Management, Payment options
(Programmable API access for outside connections; Dave doesn't expect to need inbound API clients at launch — integrations to be hard-coded against an Integrations settings tab.)

### 2.15 Security > Audit trail (f0154–f0159, ~51:00–53:00)
URL `/settings/security/audit/`. Columns: **Time | Action | Target | Target details | User's name | IP address | API Client | Actions** (details button per row). Toolbar: time-range filter dropdown (**All time, Today, Last 3 days, This week, This month, Custom**), **Search** box, **Settings** button.
- Sample rows: `2026-07-08 6:09:18 PM — SaveSettings — Co Applicant — David Wilson — 24.71.240.94`; many `SaveNotification` / `EnableDisableNotification — Notification Settings` rows (every toggle click recorded individually)
- **Audit trail details modal** (f0155): "Here you can see parameters and results of the action... displayed in a raw form and may require additional knowledge about application data structures..." — shows the raw **Request** object (JSON tree; the Co-applicant example above). Dave: raw "code speak," wants human-readable before/after diffs.
- **Audit trail settings modal** (f0157): **Retention period (days)*** = **90**; Save/Close

### 2.16 Appearance > Appearance (Color Themes) (f0161–f0162, ~53:20)
URL `/settings/appearance/themes/`. "Here you can select one of the pre-defined color themes for the application or create a custom one to fit your brand requirements."
- **Pre-defined themes** (each with light+dark thumbnails, Apply / Preview): **Light, Dark, Soft Blue Light, Soft Blue Dark, Breeze Light, Breeze Dark**
- **Custom themes**: **"PaySpyre Test Server"** — *Currently applied* ✓ (Edit / Rename / Delete) + **Create new custom theme** tile

### 2.17 Appearance > Field settings (f0163–f0164, ~54:00)
URL `/settings/appearance/definitions/`. Platform-wide terminology renaming (singular/plural pairs):
- **Vendor*** = "Vendor" / **Vendors*** = "Vendors"
- **Store*** = **"Provider"** / **Stores*** = **"Providers"** (dental industry: a Vendor's "stores" are relabeled Providers/locations)
- **Save / Cancel**

### 2.18 Kelowna settings (f0165–f0167, ~54:40–end)
URL `/settings/kelowna/` — a custom (bespoke) settings module for this tenant. **Past due settings**: single field **"Installment movement tolerance %"** = **50** + **Save**. Meaning: if a borrower pays ≥50% of an installment, the due date moves forward — with algorithm caveats (only if account is up to date; behavior changes if behind or already used once). Dave: this really belongs in the **hardship credit product settings** as "amount to move," and he'll supply the exact algorithm ("it's not as intuitive as it seems"). Video ends: "okay so that finishes off the settings."

---

## 3. Complete feature checklist

Decision engine
- [ ] Decision rules page: per-rule enable checkbox, per-rule configurable numeric thresholds, per-rule outcome dropdown (Manual review / alter / reject / no action-record only), Save/Reset
- [ ] Rule groups: Fraud prevention; Application check; Geolocation; Network security; Web activity/behavior; Bank provider (IBV-driven); Credit bureau; Co-applicant anti-fraud; Co-applicant application check; Co-applicant credit bureau
- [ ] Sanctions screening w/ 4 name/DOB match tiers; internal blacklist matching across 7 identity fields; cross-borrower uniqueness checks (phone, driver's license, SIN)
- [ ] Financial eligibility: DTI cap, PTI cap, net income floor, employment tenure, active-loan cap, delinquency DPD tiers (minor/major), age min & age-at-maturity max (by gender), residence tenure, citizenship/residency
- [ ] Bank-statement analytics rules (22 rules: transaction activity, account age, income vs stated income, cash flow, NSF/stop-payment counts, micro-loan detection, telecom/utility spend, negative-balance days)
- [ ] Credit bureau rules: bankruptcies, defaults, inquiry counts (minor/major), dual score thresholds (600/660) w/ separate outcomes
- [ ] Scorecards: two models (Application Check, Bank Account Check); built-in (locked) scoring characteristics; custom scorecard option; editable risk segments (5 bands with score range, PD, expected distribution, odds, recommendation, system decision)
- [ ] PaySpyre requirement (Dave): bin-based additive scoring on VERIFIED data only, modular (application + bank + optional bureau), natural min/max — NO clamping (see §5)

Notifications
- [ ] 4 audience channels-tabs: Customers, Co-applicants, Back-office users, Vendor users — independently configurable
- [ ] 3 delivery channels: dashboard (always-on when enabled), email, SMS — per-event channel toggles; group Enable/Disable; page Enable all/Disable all
- [ ] ~40 event triggers across 7 groups (general system, loan status ×13, payments ×13, promise-to-pay ×2, payment holiday ×2, loan agreement ×3, bank providers ×3)
- [ ] Per-event template editor: subject + rich body with merge fields, attachments picker (5 system docs), custom greeting/signature overrides, SMS template, dashboard toggle, "use same text as email", branded preview
- [ ] Global email template: global greeting + signature, master HTML template editing, preview
- [ ] Due date reminders: unlimited add/delete; next-payment (offset before due, "always send" flag) and past-due (offset after due) types; per-reminder email/SMS; defaults 7/2 before + 7/14/21/30/60/90 past due
- [ ] Custom messages design: ad-hoc email + SMS template library for back-office staff to send manually (60 Past Due, Loan Statement, Vendor Email, etc.); master HTML template
- [ ] PaySpyre additions wanted: vendor-scoped send-templates (e.g. account statement) with restricted access; standardized **comment templates** for vendors (treatment date/cost/insurance/copay/down payment/amount financed); merge-field system across notifications+agreements; combined verification-request notification (ID+bank+bureau in one email, possibly 3 SMS)

Application process
- [ ] Application form editor: add custom fields, search, per-field name/required/description, photo section toggle
- [ ] Application flow: origination confirmation toggle (origination queue vs auto→underwriting); loan offer confirmation + expiry days (30); multiple offers by underwriter + max count (3)
- [ ] Dictionaries (dropdown feeders): countries, industry category, cancellation/rejection/write-off reasons, suspicious phones; items = title + unique code + description; add/edit/delete
- [ ] Disclaimer: require-confirmation toggle, rich disclaimer text, multiple confirmation checkboxes, linked T&C + privacy-policy docs, preview
- [ ] System documents: per-credit-product loan agreement templates; per-vendor template library w/ versioning, file upload/download/preview/history/delete; merge-fields browser (scalar + table fields, Export to Word); T&C, privacy policy, statement & amortization-schedule templates
- [ ] Personal & Loan documents: configurable document types w/ collect-during-application, required, verification-needed, internal-only, max files, expiration period, custom file types, max upload size
- [ ] Co-applicant: enable, custom label ("Co-Borrower"), bureau-check toggle, required toggle, CC loan notifications toggle

Security
- [ ] API clients: named clients, key counts, permission scopes (Front-office / Management / Payment options), add/edit/delete
- [ ] Audit trail: every action logged w/ timestamp, action, target, target details, user, IP, API client; per-entry raw request/response details; time-range filter + search; retention period setting (90 days)
- [ ] PaySpyre additions wanted: automated backup/export of audit logs (e.g. PDF archive at digital-retention expiry); human-readable change diffs

Appearance / misc
- [ ] Color themes: 6 predefined + custom themes w/ apply/preview/edit/rename/delete (custom "PaySpyre Test Server" applied)
- [ ] Field settings: global relabeling of Vendor/Store terminology (Store→Provider) — PaySpyre may need per-vendor/per-industry wording
- [ ] Loan settings > Calendar: holidays & weekends calendar per year + add-holiday (carryover)
- [ ] Kelowna settings (custom): installment movement tolerance % (50) — partial-payment due-date movement for hardship product
- [ ] PaySpyre additions wanted: vendor inventory/catalog (e.g. tire & rim shop SKUs) pull-through into loan agreements; widget appearance customization per vendor

---

## 4. Workflow / rule configuration details (exact parameters)

### 4.1 Decision-rule parameter values (as configured in this tenant)
| Rule | Parameter(s) | Value | Outcome |
|---|---|---|---|
| Suspicious age (borrower & co-app) | Max age | 100 | Manual review |
| DTI | N (max %) | 50 | Manual review |
| Employment since | N (months) | 6 | Manual review |
| Derivatives/income choice | N | 6 | Manual review |
| Net income | Limit (min $) | 1500 | Manual review |
| PTI | N (max %) | 20 | Manual review |
| Age at end of loan | Male / Female | 65 / 65 | Manual review |
| Residence at address | Min months | 6 | Manual review |
| Active loans | Max | 2 | Manual review |
| Delinquency | Minor DPD / Major DPD | 14 / 30 | Manual review / Manual review |
| Minimal age (borrower & co-app) | Min age | 19 | Manual review |
| IP lookup distance | Range | 150 | Manual review |
| Geolocation distance | Range | 150 | Manual review |
| Network threat | Medium / High | — | Manual review / Manual review |
| Attachment replacements | Count | 6 | Manual review |
| Fast form fill | Time (min) | 3 | Manual review |
| Loan-term changes | Count | 6 | Manual review |
| No credit transactions | Days | 24 | Manual review |
| No debit transactions | Days | 24 | Manual review |
| Nontypical payment behavior | Cases | 3 | Manual review |
| Negative balance | Days | 30 | Manual review |
| Account age | Min days | 60 | Manual review |
| Active days (90d) | Min days | 20 | Manual review |
| Avg monthly employer income | Min $ | 1500 | Manual review |
| Avg monthly free cash flow | Min $ | 100 | Manual review |
| Telecom payments | Max $ | 250 | Manual review |
| Utility payments | Max $ | 250 | Manual review |
| NSF fees (90d) | Max count | 0 | Manual review |
| Stop-payment fees (90d) | Max count | 0 | Manual review |
| Micro-loan payment | Max | 0 | Manual review |
| Income vs avg inflow | <80% rule | fixed | Manual review |
| Inflow vs minimal income | <80% rule | fixed | Manual review |
| Outflow vs minimal living cost | <80% rule | fixed | Manual review |
| Bankruptcies (borrower & co-app) | Max | 0 | Manual review |
| Defaults | Max | 1 | Manual review |
| Bureau inquiries (6 mo) | Minor / Major | 10 / 15 | Manual review / Manual review |
| Bureau score | First / Second threshold | 600 / 660 | Manual review / Manual review |
Disabled rules: Sum of Child Support Income (90d), Avg Monthly Expenditure, Avg Monthly Government Income, Avg Monthly Non-Employer Income.

### 4.2 Scorecard / risk-rank model
- Two scorecards: **Application Check** + **Bank Account Check**; each toggleable to a custom scorecard; built-in characteristics locked (black box).
- Risk segments (editable, incl. system decision per band): Target 560–1000 (PD 0.58%, 160:1) / Acceptable 460–559 (1.9%, 51:1) / Restricted 219–459 (3.7%, 25:1) / Strongly Restricted 142–218 (6.4%, 14:1) / Rejected 0–141 (9.2%, 9:1); all bands currently → Manual review.
- Dave's target design: rules = eligibility/hard-fail + manual-review thresholds; scorecard = scoring of actual values (e.g. DTI 20% falls in a bin worth X points). **Bin system**: standardized base score per metric ± bin adjustments; total = natural sum; min/max emerge from the bins; **no clamping to a forced range** (a prior AI iteration clamped and thereby trivialized metrics — rejected). Modular ranges to compensate when no credit bureau is required (vendor/product-dependent), so application+bank and application+bank+bureau models have different min/max. Score only after ID + bank verification (+ bureau if applicable) — never score unverified statements ($15k claimed vs $5k real). Risk segments map to PaySpyre's labels: **Excellent / Good / Average / Weak / Poor-Fail**, each with its own credit limits; deals above a segment's limit route to manual underwriting.

### 4.3 Application flow parameters
- Require origination confirmation: **ON** (borrower-created apps land in Origination; off = auto-process into Underwriting)
- Use loan offer confirmation: **ON**, offer expiry **30 days** (Dave: always keep — borrower must accept critical loan details before e-sign invitation is issued, saves per-envelope e-signature integration cost)
- Multiple loan offers by Underwriter: **enabled, max 3** (Dave: unresolved how to expose in an automated/customer-facing journey without undermining vendor-approved amounts)

### 4.4 Templates & documents
- Per-credit-product loan agreements; per-vendor versioned template library (v10.00→v10.75 across ~20 dental clinics incl. New Teeth Today, KDC, plus special-use/bi-weekly variants).
- Merge-field engine: scalar + table fields with descriptions and Word export (BorrowerSSN, CustomerName, CardHint, ExpirationDate, BaseAppPath, CompanyAddress/City/Country/Logo, Vendor, LoanId, RejectionReason, FullName, CustomerName, SupportEmail, CompanyName, EmailLogin, CompanyWebsiteUrl…). Dave: "not all will be applicable — we need a workable series of merge fields... so correct information is pulled into notifications, loan agreements, other documentation."
- Notification attachments: Loan agreement, Terms & Conditions, Privacy policy, Loan statements, Amortization Schedule.
- Document collection config (personal vs loan docs) w/ verification, internal-only, expiry, file-type and size limits; borrower vs co-borrower ID both collected (front & back, ≤2 files).
- Due-date reminder cadence: −7, −2 (before) and +7, +14, +21, +30, +60, +90 (past due); regulatory context: borrowers waive the 10-day PAD pre-notification in the pre-authorized debit agreement, but weekly-frequency loans will double-fire reminders — monitor; consider frequency-based include/exclude.

### 4.5 Security & audit
- API client permission scopes: Front-office, Management, Payment options.
- Audit retention 90 days; every settings toggle individually logged (user, IP, timestamp, raw request); Dave wants PDF/log archival at digital-retention expiry + readable diffs "from what to what" for malicious-change traceability.

---

## 5. Dave's editorial comments (verbatim-ish)

Decision engine
- "We **need to have a system where we can have something like this** with the decision rules... that allow us to modify or adjust the values."
- "We can set what the system will do... **a manual review, alter, reject, no action just so it records**."
- Phone sharing across borrowers: "obviously very suspect."
- User profiles vs loan accounts: "**a database of user profiles and a database of loan accounts**... every individual only has one user profile."
- Rules vs scorecards: "decision rules... lay the guidelines... **hard pass fails that aren't potentially scored but are going to be eligibility concerns**... thresholds for when things should be manually reviewed versus auto approved... the scorecard... take whatever the actual information in the application is and score it."
- "It's possible that somebody may have an excellent risk rank... but because... they're blacklisted they would not qualify — **two separate checks that work in tandem**."
- "It's quite intricate and **needs a great degree of care**... we need to make sure that this part is done correctly."
- Bin system: "a **standardized score for a particular metric** and then based on the actual data points... plus or minus... **that's still a system that I want enabled**. The last iteration... was getting **consistently worse each generation** so we need to **throw that out and start over**."
- Scorecards visibility: "we **don't have a lot of visibility** into what these are" (built-in scorecard).
- Risk segments: "in our use case... **excellent, good, average, weak and poor/fail** — each... will have different available credit limits... if the deal exceeds those credit limits... **manual underwriting for review**."
- Two scorecards = "a **fragmented approach** — there should definitely be **two distinct models**."
- "The problem with the application scorecard... it's **basing scoring on unverified information which is never good**. If someone says they make $15,000 but they actually only make $5,000... we want our decisioning and scoring on **verified information**... **verifications completed before scoring happens** — ID and bank account verified and if applicable a credit bureau."
- Modular: "if we have vendors or credit products that **do not require credit reports** then the scoring range needs to compensate... **kind of modular**... different low and high ends of the potential score range."
- **"I don't want any clamping or artificial jamming into a predetermined range."** Clamping "**trivializes a bunch of the metrics**... anything beyond that amount doesn't impact the score and thus doesn't matter... **that is not a good way to do things**. It should be a score from a minimum X to maximum Y... addition/subtraction of the appropriate bins."

Notifications
- "This is a **very robust notification system**." "All notifications are sent to dashboards... not configurable to not send... the three channels are dashboard, email and text."
- "These can **evolve over time** or we can make new ones for different scenarios... **all of these events have triggers**."
- Merge fields: "**merge fields or a similar way needs to be developed** so that these notifications can be populated with... a user's name... their loan ID number etc... pulled into notifications, loan agreements, other documentation."
- SMS UX complaint: "**it would be helpful if we didn't have to go outside of this notification to create a separate SMS notification** — it should be just send SMS with a checkbox 'send the same SMS message as the email'; if not checked and SMS is on, an SMS message creation should be just below — **not a selection of a template**."
- Payments: "a lot of these **aren't presently used because the system doesn't trigger with daily simple interest properly**... partial payments, larger payments — **we don't really need those; we just need a payment received or initiated notification** that details what the payment is and any outstanding or past due amount."
- NSF vs payment error: returned payments "not been as a result of NSF... **stop payment or account closed**... the wording on NSF payments is different."
- "**Payment holidays is not going to be a thing** — that's going to be more about hardship options: deferments, reschedules, adjustment of terms."
- Verification requests: "**shouldn't necessarily be three separate notifications** (ID request, bank verification, credit report)... **should be one notification explaining what needs to be done**... may need to be three separate SMS... for now leave them as three, look at a combined scenario."
- Due-date reminders: "we can add or subtract as many of these as we want — **I like that**." PAD caveat: "borrowers are **waiving their pre-notification requirements within our pre-authorized debit agreement** (technically 10 days advance notice)... on a weekly frequency each reminder triggers every week so **we may just need to monitor that**."
- Vendor templates: "the ability for **vendors to use specific templates**... e.g. send an account statement... but we would **not want the vendor to be able to send whatever templates they wanted**."
- Comment templates: "we would want **comment templates for vendors**... **treatment date, treatment total/cost, estimated insurance, estimated copay, down payment, amount financed — essentially the math of how we arrived at the amount financed**... loan accounts are over seven years... if there's no record... it becomes **lost to time**... it would **standardize the information format**."

Application process
- "What I would like is a **standardized default credit application that is always used unless something else is selected**... a **short application form** for certain situations... perhaps a **longer form**... assign those loan applications within the **credit product creator**... 'this uses the default application or the short application or a customized application'... the vast majority will likely just be default."
- Application flow / marketplace: "there's going to be some additional options within here because **we're going to have a marketplace** — we need to figure out how the application flows into marketplace, how applications are sent to vendors... if the application was created... from the **widget on their website** it would land in originations **with a notification** for them... new application to review."
- Loan offer confirmation: "**I don't foresee a scenario where I wouldn't want this to happen**... presented with the critical details of the loan to review and accept before getting issued a loan agreement invitation... one of the reasons is **cost savings** — every e-signature send **counts towards our usage of that integration**."
- Multiple offers: "needs to be handled in a way that it **doesn't undermine what the vendor has agreed to**... all of a sudden that loan is ten thousand dollars short... **not 100% sure how or if we should implement that on a customer-facing level**."
- Inventory: "we need to find a way for vendors to have **an inventory of items**... a **tire and rim shop**... we need to communicate within the loan agreement the **specific item being purchased**... in the vendor section... an option to have an inventory that we may or may not use to **pull into agreements**."
- Co-applicant: "the co-applicant and co-borrower situation in the current platform is **not great**. What I'd like is... the **same process for the borrower and the co-borrower** — two separate and unique user profiles with the **exact same information requirements for both parties**."

Security / audit / appearance / misc
- API clients: "**don't really know or think that we need that, at least not at launch**... integrations should be handled within the integration section and hard coded in the background... I don't think there's going to be any API calls to us from other applications."
- "**Audit trail is extremely important** and also needs to come with an **automated backup or report feature**... everything's time stamped, every action recorded, the target... user name... IP..."
- "This is **code speak** — it would be nice if code speak was displayed as **actual changes**."
- Retention: "when a digital list meets its expiry... look at **creating a log in PDF format**... we just need to make sure we have **visibility to the changes made within the system**... so that if **nefarious things happen we can track the source and what was changed from what to what** to backtrack any malicious changes."
- Appearance: "something that **I don't think we really need to go over** — apart from potentially having a way for **vendors to customize the way the widget looks on their website** to match their color scheme... a separate widget options thing."
- Field settings: "some default wording on some fields — **store we call provider for dental**... could be location/provider... as we expand... you might find a need to modify this wording **on a larger scale per vendor**."
- Kelowna settings: "**actually something that should be sitting in the hardship credit product settings** — the amount to move... if a borrower pays 50% of an installment it will move the due date forward... **only if the account is up to date** and changes if behind or used once already — **I'll have to provide you with the specific algorithms and calculations** for amount to move because it's **not as intuitive as it seems**."

---

## 6. Integrations / external services referenced

- **E-signature integration** (SignNow in the real stack) — loan-agreement signature requests, signature reminders/expiry notifications; **per-send usage cost** is why loan-offer confirmation gates agreement issuance.
- **Credit bureau** (Equifax in PaySpyre's stack) — soft pull for eligibility ("Soft pull rejected" event), hard-pull scores (dual thresholds 600/660), bankruptcy/defaults/inquiries rules, co-applicant bureau option, "credit bureau request" notification (to be combined with ID+bank ask).
- **Bank providers / bank account verification (IBV)** (Flinks-class) — Bank Account Verification Request/reminder/expired notifications; whole "Bank provider rules" group and "Bank Account Check" scorecard run on aggregated bank-statement data.
- **ID verification** — referenced as a verification step preceding scoring (notification triple: ID / bank / bureau).
- **Payment processor** — payment received / NSF / non-NSF payment errors (stop payment, account closed), card-expiry events, repayment reversals (Zumrails in PaySpyre's stack).
- **SMS gateway** — SMS channel + SMS template system (Twilio in PaySpyre's stack).
- **Email delivery** — branded HTML master template, global greeting/signature (SendGrid in PaySpyre's stack); support address support@payspyre.com.
- **Open sanctions database** — sanctions-list name/DOB screening in fraud rules.
- **IP geolocation / network-threat intelligence** — IP lookup distance, network threat level, proxy/Tor detection.
- **API clients** — inbound API access ("Turnkey Lender Client", "Taid") with scoped permissions; Dave deprioritizes for launch.
- **Website widget** (payspyre.com/vendor sites) — widget-originated applications should land in Origination with vendor notification; vendor-branded widget theming flagged as a future appearance option.
- **Word export** — merge-fields list exportable to Word for template authoring.
