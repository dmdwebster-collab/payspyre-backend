# Settings Part 1 — Feature Inventory

Video: `07__WP_Settings_Part_1` (~49.5 min, 149 frames @ 20s). Environment: Turnkey Lender UAT (`kelowna-dental-uat.turnkey-lender.com`), branded "PaySpyre Financial", platform version footer **PaySpyre Financial - 2026 7.9.0.30**. Narrated by Dave. This part covers the Settings workplace from **Accounts** through **Loan settings → Calendar**. (Decision engine, Notifications, Application process, Security, Appearance, Kelowna settings are deferred to Part 2.)

---

## 1. Settings sections covered in this video (full settings nav tree)

Left nav of the Settings workplace (categories expand to sub-items; right pane shows the selected screen). **Every item visible in nav**, whether or not opened in this part:

- **Accounts** ▸
  - Users *(opened)*
  - Branch offices *(opened — empty)*
- **Company settings** ▸
  - Company information *(opened)*
  - Regional settings *(opened)*
  - Provinces management *(opened)*
- **Integrations** *(opened — single page, multiple sections)*
- **Loan settings** ▸
  - Credit products *(opened — bulk of the video)*
  - Delinquency buckets *(opened)*
  - Calendar *(opened — "Holidays and weekends")*
- **Decision engine** ▸ *(visible, NOT opened in Part 1)*
- **Notifications** ▸ *(visible, NOT opened)*
- **Application process** ▸ *(visible, NOT opened)*
- **Security** ▸ *(visible, NOT opened)*
- **Appearance** ▸ *(visible, NOT opened)*
- **Kelowna settings** *(custom/tenant item, visible, NOT opened)*

Top nav (workplaces) visible throughout: Origination, Risk Evaluation, Underwriting, Servicing, Collection, Reports, Archive, Settings, Tools.

---

## 2. Section-by-section walkthrough (chronological, frame refs)

### 2.1 Accounts → Users (f0001–f0006, ~0:00–2:00)
URL: `/settings/accounts/users/`

List screen:
- **Add user** button; Search box; **"All permissions"** filter dropdown.
- Columns: (avatar), Login, Name, Email, Branch, Actions (edit ✎ / delete 🗑 per row).
- Pagination (« ‹ Page x of 1 › »), record count ("5 record(s)").
- Seeded users visible: Wilson / David Wilson / admin@payspyre.kom; olga / Olga Admin / fgh@test.com; admin / Administrator Initial; Oksana / Ok Sh / test@gm.com; andy.wilson@payspyre.com / Andy Wilson.

**Add user modal** (f0003–f0005):
- Fields (all required *): Login, Password (with show-eye), Email, First name, Last name, Phone. Avatar/photo circle.
- **Permissions** block with note: *"Edit/Reverse repayment transaction, Assignment officer, Branch management, Document verification are roles that just give access to additional features and work only in conjunction with any other role."*
- **Select all / Clear all** buttons.
- Permission checkboxes (19, each with info-tooltip):
  1. Loan origination
  2. Underwriting
  3. Customer management
  4. Blacklist
  5. Export
  6. Assignment officer
  7. Vendor management
  8. Collection
  9. Reports
  10. Archive
  11. Edit/Reverse repayment transaction
  12. Import loans and customers
  13. Branch management
  14. Risk Evaluation
  15. System administration
  16. Loan servicing
  17. Import transactions
  18. Loan migration
  19. Document verification
- OK / Cancel buttons. Users can be edited and deleted from the list.

### 2.2 Accounts → Branch offices (f0007, ~2:00)
URL: `/settings/accounts/branches/`
- Buttons: **+ Add**, Collapse all, Expand all; counter "Branch(es): 0".
- Empty-state banner: "There are no branch offices in the system."
- Not currently used; possible future multi-location/teams feature.

### 2.3 Company settings → Company information (f0009–f0012, ~2:40–3:40)
URL: `/settings/company/info/`
- Fields: **Company name** = "PaySpyre Financial"; **Official company name** = "PaySpyre Financial Inc."; **Brand name** = "PaySpyre"; **Company address** = "2033 Gordon Dr #100, Kelowna, BC, V1Y 3J2"; **Company city** = "Kelowna"; **Company country** = "Canada"; **Company phone** = "1 (877) 729-1973"; **Website** = "https://www.payspyre.com"; **Email** = "support@payspyre.com"; **Lending type** = "Retail Financing - Installment"; **Foundation date** = (empty). **Save changes** button.
- **Logo / icons (tiles)** section: **Company logo** upload (Choose file / Preview; supported: JPG, JPE, BMP, GIF, PNG; height auto-reduced to fit heading) — dark PaySpyre wordmark tile shown; **Favicon** upload (square, min 310x310 px, auto-scaled) — teal sparkle monogram shown.
- Values are pulled into notifications/legal docs (official name → legal documents; company/brand name → payment notifications etc.).

### 2.4 Company settings → Regional settings (f0013, ~4:00)
URL: `/settings/company/regional/`
- **Country of operation*** dropdown = "Canada".
- **Currency symbol*** = "$".
- **Default language*** dropdown = "English".
- Buttons: Save changes / Reset changes.
- Framed as what you'd change for e.g. a US-expansion instance.

### 2.5 Company settings → Provinces management (f0015–f0022, ~4:40–7:20)
URL: `/settings/company/states/`
- Header text: *"Please select the provinces you're going to work in. They will later show up when you create Credit products."* and note: *"*Note, if you operate under government law, you may not need the Provinces Management feature. Consider consulting with your regulator or counsel."*
- Toggle: **Use provinces management** (ON).
- **Provinces** checkbox grid (alphabetical groups, Select all / Clear all): Alberta AB ✓, British Columbia BC ✓, Manitoba MB ✓, New Brunswick NB ✓, Newfoundland and Labrador NL ✓, Nova Scotia NS ✓, Ontario ON ✓, Prince Edward Island PE ✓, **Quebec QC ✗ (unchecked)**, Saskatchewan SK ✓.
- **Territories** (Select all / Clear all): Northwest Territories NT ✓, Nunavut NU ✓, Yukon YT ✓.
- Canada map graphic at right. **Save changes**.
- Today it only gates which provinces appear (sorting/search/credit-product selection). Dave wants it vastly expanded into a per-province compliance rules engine (see §5).

### 2.6 Integrations (f0023–f0032, ~7:20–10:40)
URL: `/settings/integrations/` — one page, section per integration, each with an **on/off toggle**. All shown creds are TEST environment.

1. **Email notifications** (toggle ON)
   - Service* dropdown = **"Built-in server"**; From address* = notifications@payspyre.com; From name* = "PaySpyre Financial".
2. **SMS notifications** (toggle ON)
   - Service* dropdown = **"Simulator"**.
3. **Document signing** (toggle ON)
   - Service* = **"Sign Now"** (SignNow); Client ID*; Client secret*; Account username* = notifications@payspyre.com; Account password* (masked, show-eye); URL* = https://api.signnow.com/; Checkbox: **"Enable embedded signing from the customer's dashboard"** (unchecked).
4. **Bank account verification** (toggle ON)
   - Service* = **"Flinks"**.
   - **When to conduct bank account verification*** dropdown = "Bank verification at the end of the loan applicat…"; checkbox **Score provided data** ✓.
   - Verification expiry (days) = 10; Verification reminder (days) = 3; **Allow customer to skip request** ✓.
   - Customer ID* (GUID); Service URL* = https://kelowna-api.private.fin.ag; Iframe URL* = https://kelowna-iframe.private.fin.ag/v2/; Currency* = CAD; **Depth of account report*** = 90 days; checkboxes: **Test mode** ✓, **Use attributes** ✓, **Log response** ✓.
5. **Credit bureau** (toggle ON) (f0026)
   - Service* = **"Equifax Canada"**.
   - Checkbox **Automatic request** (unchecked, has info tooltip).
   - Fields: Member number*, Security code*, Customer code, Environment* (dropdown, empty), Log request ☐, Log response ☐, Client Id*, Client Secret* (all empty in UAT).
6. **Payment processing** (toggle ON) (f0026)
   - Checkbox **"Use the same service for all operations"** ✓.
   - Service* = **"Zum Rails"**; API Username*; API Password*; **ZumWallet Id Payable** / **ZumWallet Id Receivable** fields (partially below fold).

### 2.7 Loan settings → Credit products — LIST (f0033–f0034, ~10:40)
URL: `/settings/loans/products/`
- **Add credit product** button; Search box (e.g. searching "BC-4906" filters all products for that vendor); toggle **"Show deleted credit products in the filter"**.
- Columns: Name | Allowed loan amount | Allowed loan term | Visible to customer | Loan type | Repayment period | Description | Last edited | Actions (**edit ✎ / copy ⧉ / delete 🗑**).
- Naming convention observed: vendor-code products, one product per amount bracket, e.g.:
  - AB3912-VM1 $500.00–$1,749.99, 3–12 months — "Vegreville Family Dental - CP 1 - Monthly"
  - AB3912-VM2 $1,750.00–$4,999.99, 3–24 mo — CP 2; -VM3 $5,000–$9,999.99, 3–48 mo; -VM4 $10,000–$14,999.99, 3–60 mo; -VM5 $15,000–$20,000, 3–72 mo; AB3912-VMZ $2,500–$10,000, 3–48 mo — "0% CP - Monthly"
  - A84464-M08 $17,500–$19,999.99, 3–84 mo — "New Teeth Today - Monthly Bracket 8"
  - AB5115-MM1…MM5 + MMZ — "Montgomery Dental Centre CP 1–5 / 0% CP"
  - AB9014-01M…05M — "East Village Dental CP1–CP5"
- All rows: Loan type = "Daily simple interest", Repayment period = "Monthly".

### 2.8 Add credit product — header + tabs (f0035–f0044)
Modal "Add credit product":
- **Name*** (required); **Provinces management** multi-select ("All selected"; Select all / Select none / Reset; search; checklist of the 13 provinces/territories enabled in §2.5); **Visible to customer** checkbox (✓ default) — controls whether borrowers can select the product in the vendor's calculator/application; **Description** textarea.
- **Tab strip (10 tabs)**: Schedule building | Use grace period | Use payment holiday | Due dates | Due date seasons | Payoff | Documents | Disbursement options | Approval | Repayment modes.
- Footer: **Use advanced mode** link (bottom-left), OK / Cancel.

### 2.9 Tab 1 — Schedule building (f0041–f0090)
**Payment schedule** block:
- **Loan type*** dropdown. Default = "Annuity (installment based)"; Dave switches to **"Daily simple interest"** (their always-used calc method). (Multiple calculation methods exist in the dropdown.)
- **Repayment period*** dropdown = Monthly (also Bi-weekly shown selected later; options include daily/weekly/bi-weekly/semi-monthly/monthly/bi-monthly per narration).
- **Late grace days*** = Day | 0 — days past due before a late fee is charged (if enabled).
- **Enable customizable equal payments** checkbox (unused).
- **Use variable interest rate** checkbox — when OFF: single **Interest*** = [Percent ▾] value % per [Month/Year ▾] (default shown 48%/Year — "this will never be 48 percent"). When ON (f0049–f0059): fields become **Interest*** (default rate; set 19.99 % per Year), **Min interest rate*** (9.99%), **Max interest rate*** (23.99%), and **"Who can edit the fee"** multi-select = Back-Office users ✓ / Customers ☐ (Dave wants a third: Vendors — see §5).
- **Calculation basis*** = "Remaining principal" (interest on unpaid principal, daily simple interest).
- **Loan phase*** multi-select = Grace period / Main period (default "Grace period, Main period"; Dave sets Main period only — 99.9% of cases).

**Loan offer constraints**:
- **Min amount*** ($1 → set 1000), **Max amount*** ($1000 → set 10000), **Min term*** ([Month ▾] 12), **Max term*** ([Year ▾] 3 → changed to Month 48). Inline validation: "Min term must be smaller or equal to 3 Month" / "Max term must be bigger or equal to 12 Month" (f0061). Dave: terms must always be months, never years.

**Commissions (fees)** block — Dave: should be renamed "Fees":
- **Add commission ▾** dropdown — full fee-type list (f0069): **Administration fee, Disbursement fee, Down payment, Late fee, Late fee (unpaid due), NSF, Origination fee, Past due interest, Payment holiday interest fee, Payoff fee, Pre-approval fee, Pre-disbursement fee, Repayment fee, Sales tax**.
- Fee grid: Name | Rate/Amount | Calculation basis | Actions (edit/delete). Demo product ends with: Administration fee $1.00; NSF $45.00; Origination fee $25.00.
- **Commission modal** (per fee) (f0063–f0072):
  - Name*; **Use variable fee value** checkbox (when ON, adds **Who can edit the fee** dropdown = Back-Office users, plus **Min value*** $ / **Max value*** $).
  - **Fee value***: [Amount ▾ | $ | value] — switching calc basis to "Amount" removes the %-basis options (other calculation bases exist for % fees).
  - **Accrual mode*** = "Always in equal payment" (Administration fee $1 is baked into each scheduled payment).
  - **Loan phase*** multi-select = Grace period / Main period (NSF set to both — an NSF during a grace period still charges).
  - **Payoff settings**: **General: by date*** dropdown = "All"; **General: future*** = 0 %.
  - Origination fee modal variant (f0071): Name, Fee value (Amount $25), Loan phase, Payoff settings — charged on the first installment, sized by amount financed.
  - OK / Close.
- Info banner after ≥1 fee: *"Now that you've added at least one fee to the credit product, you can customize repayment priority, i.e. change the order in which accruals will be paid. The repayment priority is applied to each individual installment."*
- **Manage accrual priority** button → "Accrual Priority" dialog (f0083): ordered list w/ up-down arrows: 1 Origination fee, 2 Administration fee, 3 Interest, 4 Principal, 5 NSF (NSF last because it's a triggered fee; origination first as one-time first-payment fee).
- **Manage repayment priority** button → "Priority" dialog (f0085/f0098): *"Reorder the priority in which accrued sums are paid within an installment."* Final order demoed: 1 Interest, 2 Principal, 3 Origination fee, 4 Administration fee, 5 NSF (items appear/disappear as fees are added/removed).
- **APR read-out** (bottom of tab, f0077–f0081): e.g. **"APR (20.32% - 26.69%)"** with Monthly repayment; switching Repayment period to **Bi-weekly recalculates to "APR (20.56% - 29.57%)"** — because the $1 per-payment admin fee compounds more often. Canadian-requirement APR, min–max range.

### 2.10 Tab 2 — Use grace period (f0087–f0091)
- Toggle **Use grace period** (ON to reveal): **Grace period term*** = [Installment(s)] 1.
- Use case: promotional "no payments / no interest for 60–90 days" products only; no current accounts use it.

### 2.11 Tab 3 — Use payment holiday (f0089–f0097)
- Toggle **Use payment holiday** (ON): **Buffer time period*** = Day | 1 (notice required before a reschedule request); **Max shifting term*** = Day | 10 (max days a payment can be moved); **Maximum allowed payment holidays for loan*** = 5.
- Dave: naming is "horrendous" — this is really *borrower payment-reschedule requests* and should become a full **Hardship tab** (reschedule → deferment → adjustment of terms), with per-option borrower visibility and human review when a shift crosses month-end (vendor-distribution impact).

### 2.12 Tab 4 — Due dates (f0099–f0107)
- **Use change start date** toggle (ON) → **Default shift (days)*** = Day | 1 (application on the 7th ⇒ default start date the 8th; also enables custom start dates to align with future-dated treatment).
- **Use change first due date** toggle (ON) → **Min (days)*** = Day | 1; **Max (days)*** = Day | 45 (align first payment with paydays; "typically always use"; Dave would make min 3 days realistically; max is arbitrary/changeable).
- **Enable date rolling** toggle (OFF) — caption: *"Shift due dates if they fall on a holiday (Business Calendar required)."* Not enabled; moot once Canada's Real-Time Rail launches.

### 2.13 Tab 5 — Due date seasons (f0109)
- **Use due date seasons** toggle; **Add season** button; "No data to display" grid.
- For seasonal-employment borrowers (on-season/off-season date ranges baked into the agreement — a mini adjustment-of-terms). Dave: too many unknowns (APR correctness, dates may not match actual season) — **leave out for now**.

### 2.14 Tab 6 — Payoff (f0111–f0113)
- **Use payoff grace period** toggle (OFF) with info tooltip.
- Grid: **Fee name | General: by date | General: future** —
  - Administration fee | All | 0.00%
  - Interest | All | 0.00%
  - NSF | All | **100.00%**
  - Origination fee | All | 0.00%
  - Principal | All | **100.00%**
- Semantics: on early payoff, future interest cannot be collected (0%), but outstanding NSF fees and principal must be 100% collected; future admin/origination fees are not generated.

### 2.15 Tab 7 — Documents (f0115–f0117)
- **Use default templates** button (top-right).
- **Loan agreement*** dropdown = "Loan Agreement" — picks which loan-agreement template (from the created template list) this product uses. Used to give **each vendor its own pre-signed loan agreement** (vendor + lender pre-sign; only the borrower signs; agreements only attach to approved applications so a pre-signed doc can't be downloaded and misused).

### 2.16 Tab 8 — Disbursement options (f0119–f0122)
- **Multiple disbursements (credit line)** toggle (demoed ON then OFF) — future interest for revolving/credit-line max, needs new legal agreements first.
- **Disbursement type*** dropdown, options seen: **Virtual disbursement** (current default — vendor provides the capital, no funds transfer; 100% of current business), **Disbursement to the customer** (direct lending — funds to borrower), **Disbursement to the vendor** (lender finances patient but pays clinic directly).

### 2.17 Tab 9 — Approval (f0123–f0126)
- Single toggle: **Use two level approval** (OFF; info tooltip). Would add a second approval step (e.g. big-ticket $60,000 accounts where the vendor must sign in and a designated vendor user approves); requires selecting who has approval authority for that vendor. Not currently used.

### 2.18 Tab 10 — Repayment modes (f0126–f0144)
Default new-product grid (f0126): Name | Description | Availability | Actions (reorder ▲▼ / edit / delete; star = default):
- **Regular payment** — "A common way to allocate the payment is suggested by default" — Back-Office users, Customers
- **Initial payment** — "available by default and has only one step: all the funds are used to pay initial payments" — Back-Office users, Customers
- **Payoff** — "available by default only to complete pay off the loan as of the current date" — Back-Office users, Customers

**Add repayment mode** dialog (f0129–f0137):
- Name* (with Reset), Description.
- **Availability** multi-select = Back-Office users / Customers (customer-visible ⇒ selectable on borrower dashboard).
- **Max payment amount*** dropdown — options seen: **Sum of special fees**, **Sum of postponed fees** (i.e., cap any transaction at the outstanding fee total; a plain limit for dashboard payments also referenced).
- **Due date mode*** dropdown = None selected / "On due date, Out of due date" (availability on vs off the due date).
- **Select a step** dropdown + **Add step** button (a mode = ordered steps targeting specific buckets). Step options observed with helper text:
  - **Pay future principal (apply on the future due date)** — "Cover the principal of the future installments. If the next step is 'Recalculate current installment', the interest stays the same." (under *future installments*)
  - **Pay initial fees** — "Cover the fees that should be paid before disbursement (downpayment, pre-qualification fees etc.)" (under *initial payments*)
  - **Pay postponed fees** — "Cover postponed fees due as of the repayment date. Funds are allocated according to the payment priority order." 
  - Narration: step groups exist for **current installment, future installments, initial payments, missed installments, payoff, side fees**.
- Validation: "At least one step has to be added." OK / Close.
- Demo: builds an **"Add-on Payment"** mode (Back-Office users + Customers, Max = Sum of postponed fees, On due date + Out of due date, step = Pay postponed fees) to let borrowers pay off NSF/late fees from their dashboard.

**Repayment modes on a real product** (Edit credit product AB3912-VM1, f0139–f0144): Automatic payment ("Automatic payment (used only for auto-charges)" — no availability, not user-accessible, always runs on schedule), Regular Payment ("Regular payments on due dates only"), Regular payment ("Regular payment out of due dates"), Add-on payments ("Add-on payments on NSF and Late fees out of the schedule"), Special payment ("Principal only payment" — Back-Office users only), Initial payment (edit shows step "Pay initial fees"; not really used — down payments are handled by vendors), Payoff.

Bottom of tab:
- **Loan closure settings*** dropdown = **"Immediately once there is no debt on a loan"** (zero balance includes fees, not just principal).
- **Future installments recalculation mode*** dropdown = **"Keep initial installment payment and reduce number of future installments"** (installments stay level; first payment can be slightly higher from origination fee, last slightly lower).

### 2.19 Loan settings → Delinquency buckets (f0143–f0146, ~47:20)
URL: `/settings/loans/pastdue/`
- Note: *"These intervals are applied to the Collection workplace (DPD filters) and to some reports. To create an interval like '91+', leave the 'maximum' value of this interval blank."*
- Rows: Default checkbox | Past due period (days): **1–30, 31–60, 61–90, 91–(blank) [Default ✓]**. Save changes / Reset changes.
- Dave's desired labels: current month late = "due current month"; 30s = previous month; 60s = two months ago; 90s = three months ago; **90+ = default (classified as default)**.

### 2.20 Loan settings → Calendar (f0145–f0149, ~48:00)
URL: `/settings/loans/calendar/` — "Holidays and weekends"
- Month grid (July 2026; weekends tinted), **Year** dropdown (2026), **Add new holiday** button, prev/next month arrows.
- Dave: should auto-populate **Canadian statutory holidays**; banks don't process electronic payments on stat holidays, so today he *manually* notifies every borrower whose due date lands on one that processing may be delayed (avoiding perceived missed payments). May change when Real-Time Rail arrives.
- Video ends: "I'm going to stop this video here; there'll be a second part of settings."

---

## 3. Complete feature checklist

Back-office user management:
- [ ] Back-office user CRUD (login, password, email, first/last name, phone, avatar)
- [ ] Granular permission flags (19 listed in §2.1) with select-all/clear-all + "modifier role" concept
- [ ] Permission-based filter + search on user list; edit/delete users
- [ ] NEW (Dave): hardship-section access permission (collections & servicing)
- [ ] NEW (Dave): backdate-payments permission
- [ ] NEW (Dave): underwriting **override** permission (approve exceptions on direct lending)
- [ ] Branch offices (add/collapse/expand; unused today, future multi-location)

Company/regional:
- [ ] Company info: company name / official name / brand name (used in different doc & notification contexts), address, city, country, phone, website, email, lending type, foundation date
- [ ] Logo + favicon upload with format/size rules
- [ ] NEW (Dave): multiple phone numbers, multiple emails, multiple websites (per-campaign landing pages)
- [ ] Regional: country of operation, currency symbol, default language (per-instance for e.g. US expansion)
- [ ] Provinces management: on/off toggle; province + territory checklists (QC excluded today); feeds credit-product province pick-lists
- [ ] NEW (Dave): per-province regulatory guardrails (see §4/§5)

Integrations (all toggleable, credential fields editable in-app, not hard-coded):
- [ ] Email (built-in server; from address/name) — pluggable service dropdown
- [ ] SMS (Simulator service option)
- [ ] Document signing (SignNow: client id/secret, account, URL, embedded-signing toggle)
- [ ] Bank verification (Flinks: trigger point, score data, expiry/reminder days, skip option, customer id, service/iframe URLs, currency, report depth, test mode, use attributes, log response)
- [ ] Credit bureau (Equifax Canada: member number, security code, customer code, environment, client id/secret, log request/response, automatic-request option)
- [ ] Payment processing (Zum Rails: API username/password, ZumWallet payable/receivable ids, "same service for all operations")
- [ ] NEW (Dave): full **simulator mode per integration** that returns realistic simulated results for end-to-end testing before go-live
- [ ] NEW (Dave): swap providers without code changes (e.g. Flinks → Zumrails bank verification); no sharing creds with AI/devs

Credit products:
- [ ] Product list: search, add, edit, copy, delete, show-deleted filter; columns incl. amount range, term range, visibility, loan type, repayment period
- [ ] Product header: name, province multi-select, visible-to-customer, description
- [ ] NEW (Dave): **vendor multi-select** on product (which vendors a product belongs to); province driven by vendor location (maybe borrower too — TBD)
- [ ] Schedule building: loan/calc type (daily simple interest), repayment period, late grace days, customizable equal payments, variable rate (default/min/max), who-can-edit-rate, calculation basis (remaining principal), loan phase (grace/main)
- [ ] NEW (Dave): repayment frequencies as **checkboxes** (weekly, biweekly, semi-monthly, monthly) selectable per product; fees adjustable per repayment period
- [ ] Loan offer constraints: min/max amount, min/max term (months only)
- [ ] Fees ("Commissions"): 14 fee types; fixed or % basis; variable fee w/ min-max + editor roles; accrual mode; loan-phase scoping; payoff (by date/future %) per fee
- [ ] NEW (Dave): variable-fee option "**varies by payment frequency**" (a $1/pmt fee distorts APR at weekly/biweekly)
- [ ] Accrual priority ordering dialog; Repayment (within-installment) priority ordering dialog
- [ ] APR range auto-calc per Canadian rules; recalculates per repayment period
- [ ] NEW (Dave): APR display expanded to **every enabled payment frequency** (bi-weekly APR, monthly APR, …) and validated against per-province maximums (multi-province ⇒ lowest max wins); block product creation that breaches high-cost-credit thresholds
- [ ] Grace period (term in installments) — promo use only
- [ ] Payment holiday (buffer days, max shift, max holidays per loan) → to be rebuilt as **Hardship options** per product (reschedule / deferment / term adjustment; borrower-visibility flags; month-end-crossing needs human review)
- [ ] Due dates: change-start-date (default shift), change-first-due-date (min/max days), date rolling on holidays
- [ ] Due date seasons (seasonal employment) — parked
- [ ] Payoff settings grid (per fee: by-date scope, future %)
- [ ] Documents: per-product loan-agreement template selection; default templates; pre-signed vendor agreements gated to approved apps
- [ ] Disbursement: multiple-disbursement/credit-line toggle (future); disbursement type = virtual / to customer / to vendor
- [ ] Approval: two-level approval toggle (vendor-side approver) — unused
- [ ] Repayment modes: default Automatic/Regular(on/off due date)/Add-on/Special(principal-only)/Initial/Payoff; custom modes w/ availability, max-amount rule, due-date mode, ordered steps (current/future/initial/missed/payoff/side-fee buckets)
- [ ] Loan closure setting (close at zero total debt incl. fees)
- [ ] Future-installment recalculation mode (keep payment, reduce count)
- [ ] Advanced mode link on product editor (not explored)

Servicing-adjacent settings:
- [ ] Delinquency buckets (1-30/31-60/61-90/91+; default flag; drives Collection DPD filters & reports); 90+ = default classification
- [ ] Business calendar (holidays & weekends; add holiday; per-year) — NEW (Dave): auto-populate Canadian stat holidays + auto-notify borrowers with due dates on bank holidays

---

## 4. Workflow / rule configuration details (exact parameters)

**Credit product parameter set (Schedule building)** — demo values used:
- Loan type: Daily simple interest (default option was "Annuity (installment based)")
- Repayment period: Monthly (later Bi-weekly to show APR shift)
- Late grace days: Day 0
- Use variable interest rate: ✓ → Interest 19.99 % per Year; Min 9.99 %; Max 23.99 %; Who can edit the fee: Back-Office users (options: Back-Office users, Customers)
- Calculation basis: Remaining principal; Loan phase: Main period
- Constraints: Min $1,000 / Max $10,000; Term 12–48 months
- Fees: Administration fee $1.00 (Always in equal payment, Main period); NSF $45.00 (Grace+Main); Origination fee $25.00 (first installment)
- Accrual priority: Origination fee → Administration fee → Interest → Principal → NSF
- Repayment priority: Interest → Principal → Origination fee → Administration fee → NSF
- APR result: Monthly 20.32%–26.69%; Bi-weekly 20.56%–29.57%
- Payoff: future Interest 0%, future Admin 0%, future Origination 0%, NSF 100%, Principal 100%
- Payment holiday: buffer 1 day, max shift 10 days, max 5 per loan
- Due dates: default start shift 1 day; first due date min 1 / max 45 days; date rolling OFF
- Closure: "Immediately once there is no debt on a loan"; Recalc: "Keep initial installment payment and reduce number of future installments"

**Compliance rules Dave wants encoded per province** (currently absent): communication windows & frequency caps (per provincial Consumer Protection Acts), required disclosures, **max APR before high-cost-credit licensing** (varies by province; they operate under high-cost lending licenses), lending-license requirements (e.g. **Saskatchewan requires a license for alternative lenders regardless**; others follow the Criminal Code under the APR cap), language requirements (Quebec). Product creation must be blocked if any enabled province's cap would be breached; multi-province products validate against the **lowest** maximum.

**Late fees legal note**: unclear whether late fees are lawful or count as default charges limited to NSF-only under many CPAs (payday-loan legacy). Currently **no late fees are used**; needs counsel clarification.

**Scoring/decision rules**: not in this part — the **Decision engine** nav item exists but is Part 2 material. (Only scoring-adjacent item here: Flinks "Score provided data" checkbox.)

---

## 5. Dave's editorial comments (verbatim-ish)

Users/permissions:
- "This is not an exhaustive list of the permissions that I would like to set but to start this is likely going to be sufficient."
- "The one permissions that I would add is the access to the hardship sections in the collections and servicing and the ability to backdate payments and… the ability to process overrides… we need an underwriting override permission."
- Branch offices: "not something that we presently use… could be something that we look at adding in the future."

Company settings:
- "I would like the ability to add additional phone numbers and additional emails… and additional websites if we want to have landing pages for specific things."
- "Lending type and foundation date is more just informational."

Provinces:
- "This needs to be vastly expanded… we need to be able to set specific regulation targets or requirements… so that the system will ensure compliance."
- "We should take a look at each province… is there a lending license requirement? Is there a language requirement like there would be in Quebec?"
- "We can't add a credit product that would inadvertently take us into a position where we are non-compliant."

Integrations:
- "I don't want these integrations hard-coded into the back end where they can't be adjusted or moved."
- "If we have a contract with Flinks and Zumrails comes… and says we now do bank verifications, which they do, we will give you a better rate — we want to be able to just implement the changes."
- "We don't want to have to provide password and login information to AI. We just want to be able to put it in the platform and have it be safe and secure."
- "There should be a simulator selection so that we can get full simulated result… something that actually works… to test things before they go live."

Credit products:
- "This is incredibly detailed and incredibly important."
- "We do need province selection… the province is based on where the vendor is, not necessarily where the borrower is. We'll have to confirm whether we need to consider both."
- "We also need a vendor selection here… a list of vendors… which vendors this particular credit product is associated with."
- "We are always going to be daily simple interest."
- "The repayment method — this should be a checkbox… weekly, biweekly, semi-monthly, monthly… I don't know why they have bi-monthly; bi-monthly and semi-monthly are the same thing."
- "The fees need to be adjustable per repayment period."
- Late fees: "there's some confusion around if late fees are legal or if they constitute a default charge… currently we don't use late fees."
- "We don't need to enable customize equal payments."
- "This will never be 48 percent." (default interest)
- Who can edit rate: "We would want three check boxes here: back end users which are PaySpyre staff members, vendors… and customers… Most of the time this is just going to be back end users, probably 95 percent of the time."
- "Commissions — this should be changed to fees."
- "One of the variables should be 'varies by payment frequency' because… this $1 has a much larger effect on the overall APR [bi-weekly] than a monthly payment does."
- "Disbursement fees we don't charge." / "NSF fees — another fee that we typically charge… if [a grace-period payment] was returned we would still want to charge the NSF fee." / "Origination fee is a fee that we charge… charged on the first installment."
- APR: "This APR needs to be expanded to show all the payment methods — bi-weekly APR, monthly APR — and reviewed to make sure each one does not exceed the maximum that has been set… if multiple provinces are selected, it doesn't exceed the lowest maximum value."
- Grace period: "The only time we would actually use this is… promotional accounts, for example no payments, no interest for 60 days… All of our current accounts do not make use of the grace period."
- Payment holiday: "It's not a holiday — the wording is horrendous… what this really should be is a hardship tab."
- "The problem with making hardship options too visible to borrowers is people will use them more and abuse them more… we want the capability to help people with legitimate hardships but we don't want some hardship options readily understood or available."
- Due dates: "We typically always use this… default delay between start date and application date is typically one day." / "Especially in dentistry we're approving applications for future dated treatment and we want to align start dates with treatment dates." / "Use change first payment date — something we typically always use… minimum one day… realistically make that three days… maximum 45… allows us to align payment schedules with pay days."
- "We don't enable rolling due dates and that won't be a thing when the real-time rail system gets released anyway."
- Due date seasons: "I would leave this out for now — there's too much unknowns with this one."
- Payoff: "Just because interest is calculated over a four-year term, if they pay it in 24 months we cannot make them pay the future interest — that's zero. However NSF fees… 100% collected; principal 100% collected."
- Documents: "Particularly used when we have a separate loan agreement for each vendor… the vendor and we have already pre-signed them and they're only available to approved applications… so somebody couldn't download a pre-signed agreement, sign it and claim it was approved."
- Disbursement: "We do definitely at some point want to look at credit line or a revolving maximum limit — there's a lot of legal documentation before we can do that." / "A hundred percent of what we do currently is virtual disbursements — the vendor is essentially the one providing the capital for the loans."
- Approval: "This would be like… a $60,000 loan account, if the vendor wanted to have a specific user approve those deals… This is not something that we currently use at all."
- Repayment modes: "This is overly complicated, but essentially what we need to do is create a system where we can have multiple repayment modes that target specific things… this 'add-on payment' to target fees, to only reduce the add-on fees."
- "This automatic payment mode is designed to only be used for auto charges and not be accessible to anybody… always happen on the date and schedule."
- "Special payment which is designed to be a principal-only payment."
- "Initial payments is not really a thing we need to worry about — we don't really collect down payments; the vendors [do]."
- Closure: "Immediately once there's no debt on the loan… that's not just principal, that also includes fees."
- Recalc: "The installments in between will all be the same… the initial payment might be a little higher because of the origination fee and the final payment might be a little lower."
- Delinquency buckets: "We would want the label to be current-month late being 'due current month', 30s to 'previous month', 60s 'two months ago', 90s 'three months ago', and then 90-day-plus which would be also classified as default."
- Calendar: "This would be great if it populated Canadian holidays… right now banks don't process electronic payments on statutory holidays, so manually I go in and send all of our borrowers that have due dates on that day a notification… so we can avoid missed payments."
- Close: "I'm going to stop this video here — there'll be a second part of settings because this is getting to be too long."

---

## 6. Integrations / external services referenced

| Service | Role | Status in UAT |
|---|---|---|
| **Built-in email server** | Email notifications (From: notifications@payspyre.com / "PaySpyre Financial") | ON |
| **SMS Simulator** | SMS notifications (service dropdown; real provider TBD — Twilio in PaySpyre stack) | ON (Simulator) |
| **SignNow** | Document/e-signature signing (client id/secret, account user/pass, api.signnow.com; optional embedded signing in customer dashboard) | ON, test creds |
| **Flinks** | Bank account verification + transaction data (trigger at end of loan application; score provided data; 90-day report depth; CAD; test mode; kelowna-api/kelowna-iframe .private.fin.ag URLs) | ON, test |
| **Equifax Canada** | Credit bureau (member number/security code/customer code/environment/client id+secret; optional automatic request) | ON, creds empty |
| **Zum Rails** | Payment processing (API user/pass; ZumWallet Id Payable & Receivable; one service for all operations) | ON, test creds |
| **Zumrails as bank-verifier** | Mentioned as potential Flinks replacement ("they do bank verifications… better rate") | hypothetical |
| **Real-Time Rail (Payments Canada)** | Future payment rail; will obsolete date-rolling & holiday-delay workarounds | future |
| Canadian statutory holiday data | Desired auto-populate source for business calendar | wishlist |

Note for PaySpyre parity: each integration must be toggleable, credentialed via settings UI (never hard-coded), provider-swappable per category, and offer a true **simulator mode** for pre-live end-to-end testing.
