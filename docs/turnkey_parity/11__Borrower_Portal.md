# Borrower Portal — Feature Inventory

Source video: "11 — Borrower Portal" (~14.5 min, 44 frames @ 20s). Narrator: Dave.
System shown: Turnkey Lender borrower-facing dashboard, white-labeled as **PaySpyre Financial** at
`kelowna-dental-uat.turnkey-lender.com` (UAT). Footer version string: "PaySpyre Financial - 2026 7.9.0.30".
Two demo borrowers are shown: **David Test6261** (loan in Origination/under-review) and **Raleigh Bailey**
(loan Active/approved).

---

## 1. Screen-by-screen walkthrough (chronological, with frame refs)

### 1.1 Login screen (f0001, 0:00)
- URL: `kelowna-dental-uat.turnkey-lender.com/Account/Login`
- PaySpyre logo + brand teal; split layout (form left, lifestyle hero photo right).
- Fields: email (`testing+6261@payspyre.com`), password (masked).
- Buttons/links: **Log in**, **Forgot your password?**
- Globe icon = language selector (Dave: "eight languages selection").
- Footer: ISO-style certification badges + version "PaySpyre Financial - 2026 7.9.0.30".

### 1.2 Post-login dashboard — loan in "Origination" (f0002–f0003, 0:20–0:40)
- Layout per Dave: left = navigation menu, right = content pane; top bar = logout (under user menu),
  notification bell, language selector.
- Left nav items (this account): **David Test6261** (personal details, labeled with the user's name),
  **Bank Details**, **New loan**, **Active loans** (expandable; shows "Loan ID: 5738, $2,400.00 / 12 months").
- Persistent status banner (top, teal): "Your loan request 5738, created on 2026-06-26 **has been submitted
  for approval**. An account representative may contact you to clarify some information if necessary. Thank
  you for your patience." — Dave later calls this the "message dialog box at the top that updates as their
  applications or accounts go through different stages."
- Page header: "Loan ID: 5738" with a red **Cancel** button (borrower can cancel a pending application).
- Info strip: "Your loan application is still under review. You will be notified once the review process is
  completed."
- Loan summary bar (collapsible chevron): Loan amount **$2,400.00** | Term **12 months** | Status
  **Origination** | Vendor/provider **TEST VENDOR 2/TV2 - Provider 2**.
- Sub-tabs: **Schedule** | **Customer documents**.
- Schedule tab = amortization table: columns **Date / Principal / Interest / Fee / Total**; bi-weekly rows
  (2026-07-17 → 2027+); first row $96.58 / $5.52 / $6.00 / $108.10; subsequent rows ~$103.10 with $1.00 fee;
  each Fee cell has its own expand dropdown (fee breakdown).

### 1.3 Personal details (f0004–f0013, 1:00–4:00)
- Header: "Personal details — Here you can change your main personal details".
- Profile picture with pencil/edit overlay.
- **Take a photo modal** (f0006): live webcam capture with crop frame, zoom slider, "Choose a file from
  gallery" alternative, **Apply this photo** / **Cancel**.
- Displayed fields (pencil icon = borrower-editable inline):
  - Full name: David Test6261 (display only)
  - Email: testing+6261@payspyre.com (editable)
  - Main phone: (555) 456-4755 (editable)
  - Alternative phone: (555) 466-7541 (editable)
  - Address: 132 ABC Rd, Kelowna, BC, V1W 5A5 (editable)
  - Employment information: "Employed, Employer: XYZ Corp" (editable)
- **Documents** section on same page:
  - "Borrower Identification (Front & Back of ID)" — "Please upload photos of the Borrower's ID (Front &
    Back of ID - Max 2 Files)"; uploaded files shown as thumbnail cards with filename + size
    (EXAMPLE-DL-BC_Br-Back.png 373.3 kB; EXAMPLE-DL-BC_Br-1Front.png 464.5 kB) — downloadable/viewable.
  - "Co-Borrower Identification (Front & Back of ID)" — Max 2 files, **+ Add file**.
  - "Documents" (generic bucket) — **+ Add file**.
- **Password** section: **Change password** button.

### 1.4 Bank Details (f0014–f0020, 4:20–6:20)
- Two linked bank-account cards (sourced via Flinks test bank "FlinksCapital"):
  - "FlinksCapital Chequing US [USD] Operations/Chequing" — ABA (Routing Number): 77777, Account: 1111001.
  - "FlinksCapital Chequing CAD [CAD] Operations/Chequing" — tagged *(default for payments)* — ABA 77777,
    Account: 1111000.
- Star icon toggles which account is the default payment account (filled star = default).
- Small card icon + a delete-style icon appear on each card (TKL allows removal; Dave explicitly wants
  delete/add disabled — see §5).
- **Add new card** button (Dave: should be replaced with a controlled "add payment method" flow).

### 1.5 New loan (f0021–f0023, 6:40–7:20)
- Header: "New loan — You may apply for a new loan."
- 3-step wizard (logged-in variant): **1 Select loan terms → 2 Select vendor → 3 Add co-applicant**.
- Step 1 controls:
  - Province dropdown (Alberta shown).
  - Credit product dropdown (placeholder: "Enter Prov. then Promo Code to Continue" — product gated by
    province + promo code).
  - Loan amount: slider + numeric input, range $500.00–$20,000.00 ($1,000 shown).
  - Interest: slider + %, range 9.99%–23.99% (19.99% shown).
  - Term: unit dropdown (Month) + value, slider 3–84 months.
  - Computed: Repayment period: Monthly; Approximate payment: $344.74.
  - Promo code input + **Confirm promo code** button.
  - Right summary panel: Principal $1,000.00 + Interest $34.22 = **Total $1,034.22**, **APR 20.25%**.
  - **Next** button.

### 1.6 Active loan detail — pending account, Customer documents tab (f0024–f0032, 7:40–10:20)
- Back on Loan ID 5738; **Customer documents** sub-tab shows the SAME three buckets as Personal details
  (Borrower ID front/back, Co-Borrower ID, generic Documents) with Add file buttons — i.e., today it just
  mirrors the uploaded personal ID docs, which Dave says is wrong (see §5: should be loan agreement, T&Cs,
  privacy policy).

### 1.7 Logged-out public application landing (f0033–f0034, 10:40–11:00)
- URL: `kelowna-dental-uat.turnkey-lender.com/Lending#/terms` — "Welcome!" page with **Log in** top right.
- Public origination wizard shows **5 steps**: 1 Select loan terms → 2 Select vendor → 3 Create an account →
  4 Fill application form → 5 Verify bank account.
- Same terms calculator (province, credit product, amount/interest/term sliders, promo code, payment
  summary with Principal/Interest/Total/APR) + **Apply now** button.

### 1.8 Approved/Active account — Raleigh Bailey (f0035–f0038, 11:20–12:20)
- Banner text changes with lifecycle stage: "Thank you for using our services! Please, make timely payments
  as per your loan agreement and get in touch in case you have any questions."
- Loan ID: 5713. Summary bar: Loan amount **$16,000.00** | Term **36 months** | Status **Active** |
  Vendor/provider **Rock Dental/Dr. Yusef Khadembashi**.
- Next-payment widget: Payment date **2026-07-10** (green), Payment amount **$262.84**, "Repayment in **2**"
  (countdown, highlighted green).
- **Make payment** button.
- Sub-tabs now: **Schedule | Transactions | Customer documents** (Transactions tab = what actually
  processed; appears only on approved/active accounts).
- Schedule tab extras: toggle **Initial schedule** (view original vs current schedule) and toggle
  **Show closed payments**; rows have a Status column ("Scheduled" with star icon), Date, Principal
  ($167.34), Interest ($94.50), Fee ($1.00 with dropdown), Total ($262.84, green).
- Notification bell shows a red badge/count for this user.

### 1.9 Make a payment modal (f0039–f0043, 12:40–14:20)
- Title: "Make payment"; "Please choose an option:" dropdown with three modes:
  1. **Regular payment** — helper text: "Regular payments (off of due dates)". Transaction amount
     pre-filled with installment $262.84 but **editable**.
  2. **Add-on payments** — helper text: "Add-on payments - NSF, Origination, Administration, Repayment,
     Late (Out of the schedule Fee payments)". Amount starts at 0; inline validation "This field must be
     at least 0.01" (red).
  3. **Payoff** — helper text: "This mode is available by default only to complete pay off the loan as of
     the current date". Amount **$14,590.62** system-calculated, **not editable** (principal + accrued
     interest + accrued fees as of now).
- Payment method line (read-only): "FlinksCapital, 77777, 1111000" — the default bank account; not
  selectable in the modal.
- Buttons: **Submit** / **Cancel**. Dave submits a regular payment ("They did submit and that would process
  their payment").

### 1.10 Logout (f0044, 14:20)
- User-name dropdown (top right) → **Log out**.

---

## 2. Complete feature checklist

### Authentication & account security
- [ ] Email + password login (branded login page).
- [ ] Forgot-password self-service link.
- [ ] Logout via user menu.
- [ ] Change password (Personal details page).
- [ ] Language selector (~8 languages) on login page and in-app top bar.
- [ ] NEW-BUILD REQUIREMENT (Dave): two-factor authentication (authenticator app, SMS code, or email
      code — method TBD); "bank-level" security posture.

### Global chrome
- [ ] Left navigation: Personal details (shown as user's name), Bank Details, New loan, Active loans
      (expandable list of loans w/ ID + amount/term).
- [ ] Top bar: notification bell (with unread badge), language globe, user menu (logout).
- [ ] Lifecycle status banner at top that changes text per application/account stage (submitted-for-approval
      message vs active-account thank-you message). Dave flags this as a "good thing."
- [ ] Footer: compliance badges + platform version.

### Personal details / profile
- [ ] Profile picture upload: webcam capture w/ crop+zoom, or choose file from gallery.
- [ ] View full name (read-only).
- [ ] Edit email, main phone, alternative phone, address, employment info (inline pencil edits).
- [ ] NEW-BUILD REQUIREMENT: address/employer edits must be versioned — new record becomes "current",
      old becomes "former", with effective dates (not overwrite-in-place).
- [ ] ID document uploads: Borrower ID front/back (max 2), Co-Borrower ID front/back (max 2), generic
      Documents bucket; thumbnails w/ filename + size; existing files viewable/downloadable.
- [ ] NEW-BUILD REQUIREMENT: existing ID images must be blurred/inaccessible (no download of stored ID);
      upload-only, with an ongoing back-office file of new + former IDs per account.

### Bank details / payment methods
- [ ] List connected bank accounts (institution, currency [USD/CAD], account type, routing/ABA, account #).
- [ ] Default-for-payments designation (star toggle) — borrower can switch which connected account is the
      default debit account.
- [ ] Add new card button (TKL native).
- [ ] Delete-account affordance visible on cards (TKL native).
- [ ] NEW-BUILD REQUIREMENTS: borrowers must NOT delete bank accounts and NOT free-form add accounts/payment
      methods. "Add payment method" button should instead launch a Flinks bank-verification flow (best
      case) or a manual path (void cheque / PAD form → human review → back-end add).

### New loan (origination re-entry)
- [ ] Logged-in "New loan" wizard: Select loan terms → Select vendor → Add co-applicant.
- [ ] Public wizard adds: Create an account → Fill application form → Verify bank account (5 steps).
- [ ] Terms calculator: province dropdown, credit product (gated by province + promo code), amount slider
      ($500–$20,000), interest slider (9.99–23.99%), term slider (3–84 months, unit selector), repayment
      period display, approximate payment, promo-code entry/confirm, live Principal/Interest/Total/APR panel.
- [ ] Cancel button on a pending application (borrower can cancel while in Origination).
- [ ] NEW-BUILD NOTES: this flow should be replaced by PaySpyre's own widget/custom origination front end;
      a new loan = new separate account; Dave also wants an optional "renew current account into a new
      account" (refinance) capability with the required date/payout calculations.

### Active loans / loan servicing
- [ ] Per-loan detail page keyed by Loan ID.
- [ ] Loan summary bar: amount, term, status (Origination / Active), vendor/provider (clinic + practitioner).
- [ ] Next payment widget: payment date, payment amount, "Repayment in N" days countdown.
- [ ] Schedule tab: amortization schedule from the loan agreement — Date, Principal, Interest, Fee (with
      per-row fee breakdown dropdown), Total; toggles for "Initial schedule" and "Show closed payments";
      per-row status (Scheduled/closed) on active loans.
- [ ] Transactions tab (active loans only): transactions actually processed.
- [ ] Customer documents tab: currently mirrors uploaded personal ID docs.
- [ ] NEW-BUILD REQUIREMENTS for documents tab: should instead contain the loan agreement (generated at
      time of loan, then static), terms & conditions agreement, privacy policy (template/standard docs set
      in back end); plus ability to request/generate an account statement (tax-time statements from a
      back-end template, downloadable or emailed on request); plus a future-dated payout calculator
      (limited to e.g. 30 days ahead, includes accruing fees, excludes not-yet-applied payments, with
      messaging that a payoff inquiry does NOT suspend scheduled payments — only actual payoff does).

### Make a payment
- [ ] Make payment button on active loans.
- [ ] Payment modes: Regular payment (defaults to installment amount, editable), Add-on payment (for
      out-of-schedule fees: NSF, Origination, Administration, Repayment, Late; min $0.01 validation),
      Payoff (system-calculated, non-editable, current-date full payout).
- [ ] Payment method fixed to the default connected bank account (displayed, not modifiable in-modal).
      Dave: keep it that way for now; possibly multiple payment methods later.
- [ ] Submit → processes the payment and it shows (in transactions).

### Notifications
- [ ] Notification bell w/ badge; stage-driven banner messages. (Contents of bell dropdown not opened
      in the video.)

---

## 3. Data fields visible

**Login/footer:** email, password; version "PaySpyre Financial - 2026 7.9.0.30"; certification badges.

**Personal details:** profile photo; Full name; Email; Main phone; Alternative phone; Address (street,
city, province, postal); Employment information (status + employer name).

**Documents:** file thumbnail, filename, file size, type ("Image"); buckets = Borrower ID (front/back,
max 2), Co-Borrower ID (max 2), generic Documents.

**Bank account card:** institution/account name ("FlinksCapital Chequing"), currency tag ([USD]/[CAD]),
account purpose ("Operations/Chequing"), ABA (Routing Number), Account number, default-for-payments flag.

**Loan summary:** Loan ID; created date; Loan amount; Term (months); Status (Origination, Active);
Vendor/provider ("TEST VENDOR 2/TV2 - Provider 2", "Rock Dental/Dr. Yusef Khadembashi" — vendor org +
named provider).

**Schedule row:** Status (Scheduled), Date, Principal, Interest, Fee (expandable breakdown), Total.
Toggles: Initial schedule, Show closed payments.

**Next-payment widget:** Payment date, Payment amount, Repayment in (days).

**New-loan calculator:** Province; Credit product; Loan amount ($500–$20,000); Interest (9.99–23.99%);
Term (3–84 months, unit=Month); Repayment period (Monthly); Approximate payment; Promo code; Principal;
Interest total; Total of payments; APR.

**Make payment modal:** payment option (Regular/Add-on/Payoff), option helper text, Transaction amount,
Payment method (institution, routing, account), payoff amount ($14,590.62 example).

**Demo data:** David Test6261 / testing+6261@payspyre.com / (555) 456-4755 / (555) 466-7541 / 132 ABC Rd,
Kelowna, BC V1W 5A5 / XYZ Corp; Loan 5738 $2,400 12mo Origination. Raleigh Bailey; Loan 5713 $16,000 36mo
Active, installment $262.84 (P $167.34 / I $94.50 / fee $1.00), payoff $14,590.62.

---

## 4. Workflow (borrower journey as shown)

1. **Apply (public)**: Welcome page wizard — Select loan terms (province, product via promo code, amount/
   term sliders, live APR/total) → Select vendor → Create an account → Fill application form → Verify bank
   account (Flinks) → Apply now.
2. **Under review (Origination)**: borrower logs in; banner says application submitted for approval and a
   representative may contact them; loan page shows status Origination, projected amortization schedule,
   and a Cancel button; documents tab holds their uploaded ID; borrower can update profile/photo, upload
   more docs, and see (but per Dave not manage) bank accounts.
3. **Approval → Active**: banner switches to the thank-you/make-timely-payments message; loan gains a
   Transactions tab, next-payment widget with countdown, and the Make payment button. (E-sign of the loan
   agreement happens in the origination flow covered elsewhere; here the agreement is described as
   "generated at time of loan and then becomes static.")
4. **Self-service servicing**: view current vs initial schedule, closed payments, processed transactions;
   make Regular / Add-on / Payoff payments from the default bank account; switch default account among
   connected accounts; update contact details; change password; multi-language UI; notifications via bell +
   stage banner.
5. **Repeat business**: "New loan" applies for a new separate loan/account; Dave wants a future renew/
   refinance-into-new-account option with proper payout math.

---

## 5. Dave's editorial comments (verbatim-ish)

Address/employment versioning:
- "If they're adding a new address, **we need to** determine when the... new address was affected... and
  essentially in the background class that as a new address, not an adjustment of the old address... the
  new address would become current, and the old address would become former. **Same with employer
  information.**"

ID document security:
- "Under documents, it shows their ID. **This is something that we should make it so that it is blurred out
  or not accessible.** The concern would be that if somebody happens to gain access to the individual's
  account, that this information would be available for download. So **I would probably restrict this to
  upload new ID** and not whatever the existing one is, and then we would just keep... an ongoing file of
  new and former IDs for the account."

Security / 2FA:
- "**We should also look at the security measures, two-factor authentication. We need to make sure that our
  security measures are finance, industry, bank-level security measures**, and the protection of the
  information for the individual and their financial information **is of utmost importance**... whether
  that be through an authentication app or a text notification with a code or an email code... right now
  **there definitely needs to be an increased level of security**."

Bank accounts:
- "**They cannot delete the bank accounts. We do not want that to be a thing. We do not want them to add
  bank accounts or add payment methods.**"
- "If they need to add a payment method, **there should be a button here that says add payment method**...
  best-case scenario... **it initiates a new bank verification through Flinks**... If we need to do a manual
  process, then we would need to get either a **void check or pre-authorized [debit] form, have a human
  review** of those, and manually add it in the back end."
- "The concern if we have too much flexibility for the borrowers to add and remove bank accounts is without
  oversight, **they could add somebody else's bank account**... and we could get into issue because we're
  pulling funds from an account that the owner did not authorize."

New loan / renewals:
- "The new loan basically allows them to apply for a new separate loan, and **we would need to have this
  particular function like our new widget or a new custom front end** or essentially our new origination
  process... This would be designed as a new account, a new separate account."
- "**It would also be potentially helpful if there was an option to renew a current account into a new
  account.** That would require some calculations and specific dates... but **that would definitely be
  something that would be welcomed.**"

Documents tab content:
- "This [customer documents] actually **shouldn't be** that [the ID uploads], **it should be the loan
  agreement, it should be terms and conditions agreements, it should be privacy policy**, things like that."
- "The loan agreement is **generated at time of loan and then becomes static**. The other two agreements
  are part of a **template or standard agreement that is set in the backend**."

Account statements:
- "**They need to be able to request and generate an account statement. This happens a lot in tax time**...
  we would have a **tax statement template in the back end settings** that is populated upon request and
  made available for them to download **or emailed to them upon request**."

Payout calculator:
- "Additional potential options would be **an option to calculate a payout as of a future date**... within
  the policy limitations... For example, **they could only calculate it 30 days in advance. It considers
  any fees that are going to be generated within that timeframe, but it does not consider any payments**
  that are going to be potentially applied... **That needs to be communicated that payments will not be
  suspended by a payoff inquiry. The only time payments will be suspended is when the account is actually
  payable** [paid out]."

Status messaging (positive):
- "**Good thing here is that there is this message dialog box at the top that updates as their applications
  or accounts go through different stages.**"

Payments:
- "The system currently **defaults to the installment amount but they can also change that amount**. They
  can make an **add-on payment if they have any outstanding fees** and they can select **payoff which is
  not editable** — it's a calculation by the system of what it takes to pay that account in full at that
  moment in time considering principal and accrued fees and accrued interest."
- "The payment method is listed as whatever their selected default bank account is and **that should not be
  modifiable at the moment. In the future we may allow for multiple payment methods but at the moment we're
  just going to work with the one that has been selected and listed in the loan agreement.**"

---

## 6. Integrations / external services referenced

- **Flinks** — bank account connection/verification; connected accounts shown as "FlinksCapital" test
  institution; Dave wants any new payment method added via a Flinks verification flow. "Verify bank
  account" is step 5 of the public application wizard.
- **Bank-account debit (EFT/PAD)** — payments pulled from the default connected chequing account
  (routing + account number); manual fallback = void cheque / pre-authorized debit form with human review.
- **Turnkey Lender** — the underlying platform (UAT domain `*.turnkey-lender.com`), white-labeled PaySpyre;
  version 7.9.0.30.
- **Email** — forgot-password flow; proposed email delivery of tax/account statements; email as a possible
  2FA channel.
- **SMS / authenticator app** — proposed 2FA channels (not yet in product).
- **Multi-language i18n** — ~8-language selector.
- **Document generation** — loan agreement generated at loan time (static thereafter); T&Cs + privacy
  policy from back-end templates; proposed tax-statement template engine.
- (No card processor, e-sign vendor, or support/chat tool visible in this video; payment methods are
  bank-account only, and no support/contact module exists in the portal beyond the banner's "get in touch"
  text.)
