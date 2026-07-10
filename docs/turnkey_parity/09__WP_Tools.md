# Tools Workplace — Feature Inventory

Source video: "09__WP_Tools" (~25 min, 75 frames @ 20s). Environment: `kelowna-dental-uat.turnkey-lender.com/App#/tools/...`, branded "PaySpyre Financial", footer version `PaySpyre Financial - 2026 7.9.0.30`, ISO badge in footer.

Dave frames the workplace: "the final workplace is tools... some of the items are tools for the back end staff, this wouldn't be applicable to vendors or to borrowers." Layout mirrors Settings: category list on the left, results on the right.

Top navigation (all workplaces, visible throughout): Origination, Risk Evaluation, Underwriting, Servicing, Collection, Reports, Archive, Settings, Tools.

Tools left-nav categories: **Customer management, Export, Import (expandable), Excel reports, Blacklists, Credit bureau, Loan migration, Vendors (expandable: Vendors / Settings / Report)**.

---

## 1. Screen-by-screen walkthrough (chronological, with frame refs)

### 0:00–2:20 (f0001–f0007) — Customer management: list + customer profile
- URL `/tools/manageCustomers/`. Left pane: customer grid with columns **ID, Login, Full name, Email, Status** (status shown as green person icon). Pagination "Page 1 of 39, 581 record(s)". Toolbar: **Add customer** button, **All statuses** filter dropdown, **Search** box.
- Right pane: selected customer **Royce Heidenreich**, avatar photo, email, status pill **Active** (turns red **Locked** when locked). Action buttons: **Edit, Change login, Reset password, Lock user**.
- Profile tabs: **Customer details, Loans, Flags, Bank details, Documents, Comments**.
- Customer details tab: **Export to PDF** and **Changelog** buttons; Personal information + Address sections with embedded street map (Leaflet / OpenStreetMap, St. John's NL shown for the test record).
- Dave: prefers name displayed "last name, first name" for sorting; sorting by last name matters "for assigning help segments to staff members".
- f0007 (~2:00): **Unlock customer confirmation dialog** — "Are you sure you want to unlock customer?" with **Block reason** shown ("locking"), OK/Close. Demonstrates lock/unlock cycle.

### 2:20–3:00 (f0008–f0009) — Reset password / Change login
- Reset password: for customers who can't find the "reset my password" link — back office initiates reset, "that will send them the applicable notifications in order to reset their password."
- f0009: **Change login modal** — "Are you sure you want to change this user's login?" with Login field (pre-filled with current email), OK/Close. Use case: customer changed email address.

### 3:00–3:40 (f0010) — Edit customer (full credit-application editor)
Edit opens the full default credit application ("everything about the user that is not loan specific... the most recent iteration of what the credit application would be"). Bottom sections visible:
- **Employment information → Primary Income**: Income Type (dropdown, "Retirement / Pension"), Net Monthly Income ($1266), Next pay date (date picker), How Often Are You Paid? (Weekly), Income received from (date). **Add Secondary Income** button.
- **Documents**: Borrower Identification (Front & Back of ID) — "Max 2 files", uploaded download.png; Co-Borrower Identification (Front & Back of ID); generic Documents section; each with **+ Add file**.
- **Photo**: "Please upload your photo" with Change Photo + download/view/delete icon buttons (selfie).
- **Fill with random data** (yellow test/UAT button), OK/Close.

### 3:40–5:40 (f0011–f0017) — Remaining customer tabs
- **Changelog** (narrated): "you can see who changed what and from what to what"; **Export to PDF** available.
- f0012 **Loans tab**: collapsible row per loan — **Loan ID 2165, Loan amount $2,500.00, Loan term: 24 months, Loan status: Rejected** with expander for "additional details about the loan."
- **Flags tab** (narrated): "any flags, we can set flags here."
- f0013 **Bank details tab**: **Add bank account** button; grid columns **Name, Account number, Routing number, Institution number, Account holder, Type, Source** — rows "FlinksCapital / 1111001 / 77777 / 777 / John Doe / None / FlinksService" (star icon = primary). Source column shows accounts arriving from the Flinks bank-connection service.
- f0014 **Documents tab**: customer-level (not loan-specific, not internal) documents both back end AND borrower can see: Borrower Identification (Front & Back of ID), Co-Borrower Identification, Documents; **Show archived files** toggle; + Add file per section.
- f0016 **Comments tab**: **Loan** selector defaulting to "All loans" — aggregates every comment across all of the customer's loans "so that we can review users for potential issues." Comment entries show author (Master User), timestamp, **Loan ID**, status-transition chips (e.g. `ORIGINATION → AUTO PROCESSING`), and free text ("application reviewed and okay to send for approval", "Reviewed, okay for underwriting").
- f0017: status filter dropdown open: **All statuses, Active, Locked, Registration incomplete**. Empty state: "Customer has no comments in any of the loans."

### 5:40–6:40 (f0018–f0020) — Add customer (manual profile creation)
- f0018: `/tools/manageCustomers/create` — step 1 is just **Email** + Save ("essentially bypassing part of the application process").
- f0019–20: full manual default application form (not tied to any active loan application): **Personal information** (First/Middle/Last name, Date of birth (3 dropdowns), Email, Marital status, Number of dependents, Citizenship, Education), **Address** (Resides at years/months, Street, Apartment, City, Province, Postal Code, Residential status), **Additional info** (Social Insurance Number, Type of ID Verification, Province of Issue, Main phone, Alternative phone, Car Owner, Other Monthly Expenses $). **Fill with random data** button. Required-field asterisks throughout.

### 6:40–7:20 (narrated; Export menu) — Export
- Export section allows export of "the loan portfolio, payments export" — Dave: "we should also have a customer export."

### 7:00–7:40 (f0021–f0022) — Import
- URL `/tools/import/customers/`. Import submenu (9 import types): **Customers, Loans, Disbursements, Payments, Write Off Reasons, Cancellation Reasons, Rejection Reasons, Suspicious Phones, Industry Categories**.
- Customers import screen: **Choose file** + **Download template** buttons. Info text: import existing customers via MS Excel file; download template, fill, upload; preview import results to fix errors; "New customers won't be created in the System unless you confirm the import." Important note: "Photos and documents can't be migrated via customer import... customers will be created and assigned status Incomplete... if required fields are empty... the general loan flow will not be applied for them."

### 7:40–13:00 (f0023–f0033) — Excel reports (custom report builder)
- f0023: **Excel reports** list (`/tools/reportExport/`) with **Report builder** button. Stock/custom reports with names + descriptions:
  - **111** (Accounting Cash Flow Vendor.xlsx)
  - **222** (ExportVendors (1).xlsx)
  - **Accounting and customer details** — "A report with the accrued amount on the loan agreement, customer details, and outstanding balance."
  - **Accounting Cash Flow** — "A report with all the transactions of a certain loan. Includes disbursements, repayments, interest, loan's body, and all the fees."
  - **Customer data** — "A report with all borrowers' full name, email, phone, state (if applicable), address and zip code."
  - **Disbursements** — "Disbursements v1.00"
  - **Expected payments** — "A report with the full list of expected payments for all disbursed loans (which haven't been closed yet)."
  - **Loans' data** — "A report with loan applications ID, borrowers full name, loan amount, term, and status."
  - **Payment information** — "A report with data about disbursement and repayment of the loan and payment type (cash, card, etc)."
  - Each row has a run/export action icon.
- f0024–f0029: **Add Excel report dialog** (Report builder): report **name** (required), and independently enable/disable filters:
  - **Branch filter** (All branches) + "Enable this filter for the report" checkbox
  - **Loan Status filter** — multi-select checkboxes: **Pre-Origination, Origination, Reprocessing, Initial payments, Risk Evaluation, Waiting for approval, Approved, Rejected, Soft pull rejected, Active, Past due, Repaid, Written off, Canceled** (Dave reads these as the platform's loan statuses; PaySpyre already has its own list; he notes "we need to add in verification here")
  - **Credit Product filter** (All credit products)
  - **Days past due filter**
  - **Transaction status filter** — **Successful, Failed, In progress, Reversed**
  - **Transaction type filter** (All directions; narrated: disbursement / payment)
  - **Report template** — Choose file (upload custom .xlsx template)
  - **Description** text field; OK/Cancel.
- f0030–f0033: **Report builder management list**: columns **Name, File name (template .xlsx link), Updated (timestamp), Actions (run / edit / delete)**. Template file names: Accounting Cash Flow Vendor.xlsx, ExportVendors (1).xlsx, Accounting and customer details Template.xlsx, Accounting Cash Flow Template.xlsx, ExportCustomers.xlsx, Disbursements_v1.60.xlsx, ExportExpectedPayments.xlsx, ExportLoans.xlsx, Transactions report (new).xlsx.
- Templates use **smart tags** (not merge fields) to pull data points into the report layout.

### 13:00–13:40 (f0034) — Blacklists
- `/tools/blacklist/`. List of blacklist records with created dates and reason ("Testing Blacklist"); select + delete; **Add to blacklist dialog**: "Choose category" dropdown (narrated categories: **name, social insurance number, phone number, email, driver's license**), OK/Close.

### 13:40–16:40 (f0035–f0040) — Credit bureau (Updates for Credit Bureau)
- `/tools/cbUpdates`. Header "Updates for Credit Bureau". Filters: **All credit bureaus**, **All statuses**, Search. Buttons: **Send updates**, **Settings**.
- Grid columns: **Status, Created, Sent, Customer, Loan, Service, Actions** (details + send icons per row). Service column = "Emulator" (UAT stub — no real bureau connected). 24 records, 2 pages.
- f0039 **Settings dialog**: checkbox "**Automatically send updates to the credit bureau**"; **Periodicity** dropdown (shown: "First day of month"; narrated options: first day of every month / every day / custom / immediately when data has changed); **Time** (01:00). OK/Cancel.
- f0040: after clicking Send updates, toast: "**New records will be sent to the Credit Bureau shortly. Please reload the page to update the status.**" — manual/custom update on demand.

### 16:40–17:40 (f0041–f0042) — Loan migration
- `/tools/loanMigration/customer/`. Info banner: "Here you can migrate existing loans to the system. **Create a new customer or find the existing one, then enter loan terms and dates, repayment transactions and other loan events.**"
- Tabs: **New customer / Existing customer**. New-customer tab repeats the full application form (Personal information, Address, Additional info, ... with Fill with random data). Switching tabs with data triggers "There are unsaved changes on this page. Are you sure you want to leave?" (Confirm/Cancel).

### 17:40–19:00 (f0043–f0044) — Vendors: vendor list
- `/tools/vendors/entities/`. **Add vendor** + Search. Grid columns: **Name, Phone, Providers (count), Vendor users (count), Provider users (count)** + row actions **edit / lock / delete** (lock icon appears on some rows; once locked, delete becomes available). 23 record(s), 2 pages.
- Vendor names visible: Vendor1, ABC Dental Clinic, Vegreville Family Dental, Selkirk Dental Clinic, YanaV, Montgomery Dental Centre, Elevation Dental Inc., TEST VENDOR 1, **PaySpyre Financial** (the "I don't have a vendor" default), Dr. N. J. Seddon Dental Corp., Yaletown Dentistry, Arch Dental + Aesthetics, **New Teeth Today**, Parkview Dental, Brightside Dental (+ Kelowna Dental Centre on page 2 or above).
- Vendors submenu: **Vendors, Settings, Report**.

### 19:00–21:00 (f0045–f0058) — Vendor detail: Kelowna Dental Centre
- Tabs: **Vendor details, Providers, Users**.
- **General info**: Name ("Kelowna Dental Centre"), **Id*** ("BC4906" — the unique vendor ID that becomes the prefix of every loan/account number for that vendor), **Industry category*** dropdown, Tax identification number, Phone (250-860-1414).
- f0051 Industry category options (defined in Settings→directories): **Dental Goods & Services, Healthcare - Cosmetic, Healthcare - Vision, Healthcare - Hearing, Healthcare - Medical Weight Loss, Healthcare - Dermatology, Healthcare - Medical, Healthcare - Veterinary**.
- **Address**: Street (2033 Gordon Dr.), Apartment, City (Kelowna), Province (British Columbia), Postal Code (V1Y 3J2), **Locate** button + embedded map pin.
- **Bank accounts**: Add bank account; grid **Bank name, Account number, Routing number, Actions**; row "ABC Bank / 1234567890 / 12345" with star = primary. Purpose per Dave: destination for direct-to-vendor lending disbursements and vendor-initiated disbursement requests of available funds.
- **Contacts**: Add contact person; grid **Name, Position, Phone, Email, Actions (edit/delete)** — "Leanne Wilson / Practice Manager / 2508601414 / leanne.kelownadentalcentre@gmail.kom".
- **Documents**: Choose file upload area (Dave: vendor application + master service agreement should auto-populate here; wants expiry/renewal-date tracking + notifications).
- **Save changes / Back**.

### 21:00–21:40 (f0059–f0063) — Create provider / provider detail
- `/tools/vendors/entities/<id>/stores/create` — "Create provider". Fields: **Name*, Id*, Phone***; **Address** (Street, Apartment, City, Province, Postal Code, Locate); **Bank accounts** with **"Use vendor bank account"** toggle OR Add bank account (validation: "There should be at least one bank account"); **Contacts** (Add contact person; validation "There should be at least one contact"); Save changes/Back.
- f0063 Provider detail **Dr. Trent Orth**: tabs **Provider details / Users**; **Id "4906/3"** — provider ID = vendor ID + unique provider number; loan ID chains from that (vendor → provider → loan). Phone/address copied from vendor; "Use vendor bank account" toggle ON; contact Leanne Wilson (Office Manager). Dave: provider = doctor in dental world, or a **location** for multi-store businesses (own phone/address/bank account possible).

### 21:40–22:20 (f0064–f0066) — Provider users vs vendor users
- f0065 Provider → Users tab: **Create provider user** + search (empty — "We don't really use this. We typically just use vendor users").
- f0066 Vendor → Users tab: **Create vendor user** + search; grid **Login, Full name, Phone, Creation date, Actions (edit/delete)**. 10 record(s): jade@kelowna-dental-centre.ca (Jade MacDonald), hannah@ (Hannah Butcher), keely@ (Keely Horning), bc4906@payspyre.com (PaySpyre Financial), yolanda@ (Yolanda Rodriguez), accounts@ (Tess Foisy), daniela@ (Daniela Taranto), aly@ (Aly Pollock), candacemarie311@gmail.com (Candace Legault), dmdwebster@gmail.com (Michael Webster).

### 22:20–24:00 (f0067–f0071) — Create vendor user + permissions
- Form: **Login*, Password* (masked, eye-reveal), Email*, First name*, Last name*, Phone***; auto-generated avatar (color+icon derived from name letters — visibly changes as name typed, f0069→f0071).
- **Permissions** panel with note: "**NOTE: Assignment officer, Document verification are roles that just give access to additional features and work only in conjunction with any other role.**"
- Permission checkboxes (each with info tooltip): **Loan origination, Loan servicing, Monitoring, Collection, Assignment officer, Vendor management, Archive, Document verification, Export** + **Select all / Clear all** buttons.
- Required-field validation shown (f0071). Dave objects to admins setting initial passwords (see §4).

### 24:00–24:30 (f0072–f0073) — Vendor settings
- `/tools/vendors/settings/`:
  - **Enable vendor selection for a loan** (toggle, on) — "If disabled, customers cannot define vendor and provider for the loan application."
  - **Show vendors on map** (toggle, on) — "Is required for search by Industry and Provider."
  - **Save**.

### 24:30–end (f0074–f0075) — Vendor report
- `/tools/vendors/export/` — "Report". Filters: **All statuses, All time, All vendors** + **Excel 2007+** export button (format dropdown).
- Grid columns: **Loan Id (sortable), Vendor, Provider, Industry category, Loan Amount, Create Date, Disbursement date, Close Date, Loan Status** (status rendered as colored icons: $, prohibited, hourglass, etc.). 560 record(s), 38 pages. "Export details on particular vendor portfolios." Dave: "And that concludes the workplace reviews."

---

## 2. Complete tool catalog

### Customer management (back-office CRM over all borrower profiles)
1. Customer list: search, status filter (All/Active/Locked/Registration incomplete), pagination (581 records)
2. Add customer (email-first, then full manual application form; bypasses online application; not tied to a loan)
3. Lock user / Unlock user (with block reason capture + confirm dialog)
4. Reset password (triggers notification to customer)
5. Change login (e.g., customer changed email)
6. Edit customer — full default credit-application editor (personal, address, additional info, employment/income incl. secondary income, documents, ID front/back, photo/selfie)
7. Export customer profile to PDF
8. Changelog / audit trail (who changed what, from → to)
9. Loans tab (all loans w/ ID, amount, term, status, expandable detail)
10. Flags tab (set/view flags on customer)
11. Bank details tab (add/view bank accounts; Flinks-sourced accounts w/ institution & routing numbers; primary star)
12. Documents tab (borrower-visible shared docs; archived-files toggle; add file)
13. Comments tab (cross-loan comment aggregation, filter by loan, status-transition chips)
14. "Fill with random data" test-data utility (UAT)

### Export
15. Loan portfolio export
16. Payments export
17. (Gap flagged by Dave: no customer export — "we should also have a customer export")

### Import (Excel-template-driven, with preview + confirm)
18. Import Customers (download template → fill → upload → preview errors → confirm; no photos/docs; incomplete records get status Incomplete)
19. Import Loans
20. Import Disbursements
21. Import Payments
22. Import Write Off Reasons
23. Import Cancellation Reasons
24. Import Rejection Reasons
25. Import Suspicious Phones
26. Import Industry Categories

### Excel reports (custom report builder)
27. Prebuilt report library (Accounting and customer details, Accounting Cash Flow, Customer data, Disbursements, Expected payments, Loans' data, Payment information) with one-click run/export
28. Report builder: create named report with independently-enableable filters — Branch, Loan Status (14-status multi-select), Credit Product, Days past due, Transaction status (Successful/Failed/In progress/Reversed), Transaction type (disbursement/payment/all directions)
29. Custom .xlsx report template upload using smart tags (data-point placeholders)
30. Report management (edit / delete / re-run; template file + last-updated tracking)
31. (Gap flagged by Dave: no scheduled/automated report runs or distribution — needed for monthly vendor statements)

### Blacklists
32. Blacklist records list w/ created date + reason; multi-select delete
33. Add to blacklist by category: name, social insurance number, phone number, email, driver's license

### Credit bureau
34. Updates-for-Credit-Bureau queue (per customer+loan record; status/created/sent; service column — Emulator in UAT)
35. Filter by credit bureau / status; search
36. Automatic scheduled sending: on/off, periodicity (first day of month / every day / custom / immediately on change) + time-of-day
37. Manual "Send updates" trigger (batch, async w/ toast)
38. Per-row detail + per-row send actions

### Loan migration
39. Migrate existing loans onto the platform: create new customer OR find existing, then enter loan terms, dates, repayment transactions, and other loan events; unsaved-changes guard

### Vendors (vendor management — "the vendor management version of the customer management")
40. Vendor list (providers/vendor-users/provider-users counts) + search + Add vendor
41. Vendor lock (blocks application submission) → then delete (Dave: delete must remain soft/hidden for historical loans)
42. Vendor details editor: name, unique vendor ID (prefix of all loan/account numbers), industry category (from Settings directories), tax ID, phone, address + geolocate + map
43. Vendor bank accounts (add/edit/remove; primary flag) — for direct-to-vendor disbursements and vendor-requested payouts
44. Vendor contacts (add/edit/delete; name/position/phone/email)
45. Vendor documents upload (vendor application, MSA — Dave wants these auto-populated + expiry/renewal alerts)
46. Providers sub-entity (doctor or location): create/edit w/ compound ID (vendorID/n), own phone/address/bank or "Use vendor bank account" toggle, own contacts; validations (≥1 bank account, ≥1 contact)
47. Provider users (users scoped to one provider only — exists, unused by PaySpyre)
48. Vendor users (access to vendor + all providers): create w/ login/password/email/name/phone; auto-generated identicon avatar
49. Vendor-user permission matrix: Loan origination, Loan servicing, Monitoring, Collection, Assignment officer, Vendor management, Archive, Document verification, Export (+ select all/clear all; two roles are add-on-only)
50. Vendor settings: Enable vendor selection for a loan (borrower-side vendor/provider picker on application); Show vendors on map (required for search by Industry and Provider — feeds marketplace-style vendor map)
51. Vendor report: loan-level portfolio export per vendor/provider (status/time/vendor filters; Excel 2007+ export; loan status icons)

---

## 3. Data fields visible

**Customer list**: ID, Login, Full name, Email, Status (icon).

**Customer — Personal information**: First name, Middle name, Last name, Date of birth, Email, Marital status, Number of dependents, Citizenship, Education.

**Customer — Address**: Resides at (years, months), Street, Apartment, City, Province, Postal Code, Residential status (Rent), Monthly Rent ($1200), map pin.

**Customer — Additional info (create form)**: Social Insurance Number, Type of ID Verification, Province of Issue, Main phone, Alternative phone, Car Owner, Other Monthly Expenses ($).

**Customer — Employment / income**: Income Type (e.g., Retirement/Pension), Net Monthly Income, Next pay date, How Often Are You Paid (Weekly...), Income received from (date), secondary income (repeatable).

**Customer — Documents**: Borrower Identification front & back (max 2 files), Co-Borrower Identification front & back, general Documents, Photo (selfie); file name/size/type; archived flag.

**Customer — Loans tab**: Loan ID, Loan amount, Loan term (months), Loan status.

**Customer — Bank details**: Name, Account number, Routing number, Institution number, Account holder, Type, Source (FlinksService), primary star.

**Comments**: author, timestamp, Loan ID, status transition (from → to), comment text.

**Excel report definition**: name, description, branch, loan statuses (Pre-Origination, Origination, Reprocessing, Initial payments, Risk Evaluation, Waiting for approval, Approved, Rejected, Soft pull rejected, Active, Past due, Repaid, Written off, Canceled), credit product, days past due, transaction status (Successful, Failed, In progress, Reversed), transaction type/direction, template file, updated timestamp.

**Blacklist record**: category (name / SIN / phone / email / driver's license), value, created date, reason.

**Credit bureau update record**: Status, Created, Sent, Customer, Loan, Service; settings: auto-send flag, periodicity, time.

**Vendor**: Name, unique Id (e.g., BC4906), Industry category, Tax identification number, Phone, Street/Apartment/City/Province/Postal Code, bank accounts (bank name, account number, routing number, primary), contacts (name, position, phone, email), documents, counts of providers / vendor users / provider users.

**Industry categories (directory)**: Dental Goods & Services; Healthcare - Cosmetic / Vision / Hearing / Medical Weight Loss / Dermatology / Medical / Veterinary.

**Provider**: Name, compound Id (vendorId/seq, e.g., 4906/3), Phone, Address, Use-vendor-bank-account toggle or own bank accounts, contacts, users.

**Vendor user**: Login, Password, Email, First name, Last name, Phone, avatar, creation date, 9 permission flags.

**Vendor report row**: Loan Id, Vendor, Provider, Industry category, Loan Amount, Create Date, Disbursement date, Close Date, Loan Status.

**ID chaining convention (narrated)**: vendor ID → prefix of provider ID → prefix of loan/account ID; "everything has to have a unique transaction, a unique identifier... so we don't have multiple hits for the same possible transaction."

---

## 4. Dave's editorial comments (verbatim-ish)

**Scope**: "some of the items are tools for the back end staff, this wouldn't be applicable to vendors or to borrowers."

**Name sorting**: "I probably prefer this to be a last name, first name so that we can sort by... last name — that's going to be something that we potentially use for assigning help segments to staff members."

**Password reset**: "if these are calls that they can't get logged in... we can initiate a password reset by clicking this button and that will send them the applicable notifications."

**Customer export gap**: "Export allows us to export data on like the loan portfolio, payments export — **we should also have a customer export**."

**Imports for migration (must-have)**: "**we're going to need something similar to this when we try to import information into the new platform**" and (loan migration) "**we need a way to import users and loans into the new platform and we need to create a way that we can do that efficiently with just uploads of like CSV files or Excel files** with data that is the templates that are generated and filled in."

**Loan statuses**: "we need to add in verification here... those are the loan statuses that are applicable to this platform — that does not mean that they should be what is applicable to our new platform; **we've already provided a list of applicable loan statuses**."

**Report filters gap**: "there should be additional filters here — **there should be like a vendor filter here for all our particular vendors**."

**Scheduled reporting gap (must-have)**: "**one of the things that is missing is automated running — for example if we want a monthly vendor statement to be generated and sent to the vendor, which we do, we should be able to define that and create the template for that report, have it uploaded and then set frequencies for that report to be distributed.**"

**Smart tags / data access**: "they don't use merge fields, they use smart tags... whether we use both in the platform or one or the other... **there needs to be a robust way to access data points and pull data points that are needed into reporting or documentation**."

**Credit bureau (must-have)**: "**this is something that needs to be built into the platform and also needs to be built in with automatic disbursement or activation**... we send credit bureau updates on our loans and their statuses **to Equifax**... **we need to be able to have it in the format that Equifax requires because they do require a specific format and we want it to be triggered automatically**. For example, on 12:01, the first possible second of a new month, the performance on all of the applications is automatically generated and sent to Equifax... We don't want [immediately-on-change], **we want basically first day of every month** and then specify the time, right, just exactly like this, and then we can initiate a custom update if we so choose."

**Vendors area importance**: "**Vendors — this is an important area**... the vendor management version of the customer management."

**Vendor soft delete (must-have)**: "from a deleted vendor, it's still likely going to need to be required to be **held in the background, just hidden**, because of the historical loans and transactions for the vendor that we'll have to have access to — that would be basically deleting them from active use... if they, for example, don't renew the master service agreement."

**Unique IDs (must-have)**: "each vendor is required to have a **unique identification number**. This ensures database integrity and sorting... This vendor ID essentially becomes part of the overall loan account ID... every loan for this vendor would start with their vendor ID... **Everything has to have a unique identifier so that we can make sure that we don't have multiple hits for the same possible transaction.**"

**Vendor bank accounts**: "this would be used for when, if in the future we do direct lending and we do direct-to-vendor lending — this is where disbursements would land... as well as if we're doing a scenario where **vendors can request disbursements themselves of available funds** in the portfolio and what's due to them."

**Vendor onboarding automation (must-have)**: "**we want to automate the vendor onboarding process**, which is a separate conversation about another vendor journey... **vendor application, vendor master service agreement — those should be auto-populated in here when they're generated. We also want to have record of expiry dates on master service agreements or renewal dates so that we can be notified when those are coming up for renewal.**"

**Providers as locations**: "this provider can also mean like location — if we're talking expansion into other industries where it's not doctors but different stores... which may have a different phone number... a different address... a different bank account."

**Provider users**: "**We don't really use this. We typically just use vendor users.** ... in a situation where providers are locations of other umbrella businesses, it may make sense at that point to use provider users."

**Password anti-pattern (must-fix)**: "**I don't really like the setting of user passwords. That should be something that's automatically set and that we do not have access to.** We should be able to institute a password reset, but I don't think we should be the one setting the initial password. That just creates a weird dynamic where... **multiple people have access to that particular user account and that should never be a thing.**"

**Default vendor / marketplace**: "there's going to be instances where we might just have a default vendor that is like 'I don't have a vendor' — that's when we're gathering information for the marketplace to present to vendors... and then **show vendors on a map**."

---

## 5. Integrations / external services referenced

1. **Equifax** — credit bureau metro-style monthly reporting; requires Equifax's specific file format; must be auto-triggered (first-of-month) with manual custom-update option. UAT shows "Emulator" service placeholder.
2. **Flinks** — bank-account aggregation: customer bank details rows sourced "FlinksService" / bank "FlinksCapital" with institution/routing/account numbers and account-holder name.
3. **Leaflet / OpenStreetMap** — embedded address maps + geocode "Locate" on customer, vendor, and provider addresses; also underpins "Show vendors on map" search.
4. **MS Excel (.xlsx)** — import templates (9 entity types), report builder templates with smart tags, "Excel 2007+" exports.
5. **PDF export** — customer profile Export-to-PDF.
6. **Email/notification service** (implied) — password-reset notifications; desired future: scheduled vendor-statement distribution and MSA-renewal alerts.
7. **Turnkey Lender platform itself** — white-labeled as "PaySpyre Financial" at kelowna-dental-uat.turnkey-lender.com, v7.9.0.30.
