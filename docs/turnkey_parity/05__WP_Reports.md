# Reports Workplace — Feature Inventory

Video: Turnkey Lender "Reports" workplace (kelowna-dental-uat.turnkey-lender.com, PaySpyre Financial branding, version footer "PaySpyre Financial - 2026 7.9.0.30", ISO logo in footer). ~17 min, 52 frames @ 20s.

Top nav (constant across all frames): **Origination | Risk Evaluation | Underwriting | Servicing | Collection | Reports | Archive | Settings | Tools** — plus notification bell, globe/language selector, and user menu icons top-right.

Left sidebar (the Reports workplace's own categories — different layout from other workplaces, as Dave notes):
1. **Business performance** (URL `/App#/portfolio/dashboard/`)
2. **Portfolio** (`/App#/portfolio/portfolio`)
3. **Operational** (`/App#/portfolio/operational`)
4. **Risks** (`/App#/portfolio/risks`)
5. **Scoring** (`/App#/portfolio/scoring`)
6. **Underwriting** (`/App#/portfolio/officers`)

---

## 1. Screen-by-screen walkthrough (chronological, with frame refs)

### f0001–f0003 (0:00–1:00) — Business performance dashboard, Comparison period = Monthly
Dave opens the Reports/analytics section. Dashboard of 7 widget cards (detailed in §2/§3):
Geographical report (map), Portfolio report (5 KPI sparkline tiles), Risk report (donut + 6 legend metrics), Collection report (4 tiles + collectability gauge), Performance report, Top write-off reason grid, Time series report (bottom).
Narration: lots of changes needed here, "primarily around collections and quote unquote early payments or bad debt at risk and losses"; platform "was not designed to work with daily simple interest accounts, which is the norm in Canada."

### f0004–f0009 (1:00–3:00) — Comparison period switched Monthly → Annually
All KPI figures update (Portfolio report now: Disbursed $16.00k, Repaid $30.81k, Profit $7.20k; Risk report Paid on time $26.84k, Paid late $3.00, Loses $1.79k; Collection report Paid late $3.00, Write-offs $1.79k; Performance report count 3, ↓-16; Top write-off reason now shows "Test Environment Only - Test Account Write-off: 1", Bankruptcy: 1). Demonstrates the daily/weekly/monthly/quarterly/annual comparison filter.

### f0006–f0007 (~1:40–2:20) — Geographical report filter dropdown
Dave changes the Geographical report filter from **"Processed Applications"** to **"Portfolio"** (dropdown options narrated: Processed Applications / Delinquency by heat map / Portfolio outstandings by heat map). Map is blank because test-environment addresses are fake — normally a heat-map distribution of active accounts. Dave: default should be Portfolio.

### f0008–f0016 (2:20–5:20) — Portfolio report tiles + vendor-filter requirements
Mouse hovers over Portfolio report tiles (Disbursed amount, Profit) while Dave narrates the vendor-filter requirement (all/multi-select/single vendor checkboxes; Dr. Webster multi-location example) and the gross/vendor/PaySpyre profit-split dropdown requirement.

### f0016–f0026 (5:00–8:40) — Risk report widget
Hover over Risk report donut and legend items (Early payments, Paid on time, At risk). Long narration on why "early payments" is ill-defined, how scheduled amortization payments are immune to extra payments (interest-short trap explanation), and what the buckets should actually be (current month late / PD30+ / written-off losses).

### f0027–f0030 (8:40–10:00) — Collection report widget
Hover over Bad debt tile, Debt roll rate tile, Bad debt Collectability gauge. Narration: replace "bad debt" with explicit delinquency queues (current month late, PD30, PD60, PD90, default, insolvency, written off); debt roll = % of current-month-late rolling into PD30; collectability score idea (fictitious initially, not a priority).

### f0031–f0033 (10:00–11:00) — Performance report + Number/Amount toggle
Hover over the Performance report's **Number / Amount** toggle. Narration: performance report = number of processed applications, toggleable between count and dollar amount; top write-off reason grid noted.

### f0033–f0034 (10:40–11:20) — Time series report (scrolled down)
Full-width time-series chart, dropdown "Number of Processed Applications", x-axis Jul 2021 → Jul 2026, y-axis 0–1.0. Narration lists the time-series metric options: number of processed applications, amount, percent of applications, portfolio, profitability, yield, repaid amount, written-off amount. Dave: "I would also put current month lates, POT 30s, POT 60s, POT 90s, and defaults in there."

### f0035–f0039 (11:20–13:00) — Portfolio category page
Page "Portfolio" with controls: **"- All Reports -" dropdown | Monthly dropdown | Chart / Table / Both toggle | Export button**, and a **date-range slider** (From 2021-08-31 To 2026-07-06) with mini area-chart brush over the full history (Oct 2021 → Jul 2026).
Charts: **"Active loans vs Portfolio"** (dual-axis line: Active loans count left axis 0–60, Portfolio $ right axis to ~500k) and **"Overall repayment amount per interval"** (lines: Repaid, Principal, Fees, Interest amount; peaks ~50k).
f0037: Dave clicks **Both** and drags the slider (From 2025-09-21 To 2026-07-05) — chart zooms and a **table** appears below (columns: Date | Active loans | Portfolio; monthly rows e.g. 2026-07-31 / 23 / $707,150.02). Narration: the All-Reports dropdown just brings the selected report to the top and hides the rest; sliding scale hones a time frame; chart/table/both; export; "we also need to have the vendor selection here."

### f0040 (13:00–13:20) — Operational category page
Page "Operational" with dropdowns: **Monthly | Origination efficiency** (narrated alternatives: business status, application statistics, origination efficiency, all reports).
Section "Origination efficiency": **Chart/Table/Both toggle | "Applications originated" dropdown | "Users" dropdown | Export**. Multi-line chart of applications originated per user per month; legend of officers: David Wilson, Olga Admin, Administrator Initial, Ok Sh, Andy Wilson. Dave: "again, we want to have vendor filters."

### f0041–f0042 (13:20–14:00) — Risks category page
Page "Risks": **All Reports dropdown | Monthly | Chart/Table/Both | date-range slider** (2021-08-31 → 2026-07-06). Report sections visible: **"Bad rate trend"** and **"Delinquency performance"** (charts still rendering/blank in frames). Narration: risks = delinquency — current month late, PD30/60/90, defaults, insolvency accounts, default accounts.

### f0043–f0047 (14:00–15:40) — Scoring category page
Controls: report dropdown (**System stability** → **Scorecard accuracy** → **- All Reports -**), period dropdown (**Last month**), **Export** and **Update** buttons.
- f0043–f0044: **System stability** table — columns: # | Score (560-1000, 460-559, 219-459, 142-218, 0-141) | Risk level (Target client, Acceptable client, Restricted client, Strongly Restricted client, Rejected clients) | Actual % | Expected % | (A-E) | (A/E) | Ln(A/E) | Index; pink summary row with overall stability index **0.4894**.
- f0045: **Scorecard accuracy** table — # | Score | Risk level | Accounts | Bad | Bad rate | Expected bad rate (e.g. Target client: 12 accounts, 5 bad, 41.67% vs 0.58% expected).
- f0046–f0047: **All Reports** stacked view — System stability + Scorecard accuracy + **Delinquency performance** (per score band; **# / %** toggle; columns: # | Score | Accounts | Current | 1-30 | 31-60 | 61-90 | 91+ | Written off) + **Final score** section heading at page bottom (distribution report, cut off).
Narration: risk-level mapping (rejected=poor/fail, strongly restricted=weak, restricted=average, acceptable=good, target=excellent); actual vs expected performance; per-vendor scorecard idea (outlier vendor in Toronto → assign/adjust scorecards per vendor; default scorecard + overrides).

### f0048–f0052 (15:40–17:20) — Underwriting category page (end of video)
URL `/portfolio/officers`. Controls: **- All Reports - | Last month | Export | Update**.
- **"Underwriter monitoring"** table — # | Officer | Accounts | Loan amount | Bad, each as value + share bar: David Wilson 13 (92.86%) / $110,113.50 (98.66%) / 4 (80.00%); Olga Admin 1 (7.14%) / $1,500.00 (1.34%) / 1 (20.00%).
- **"Overrides by underwriters"** table — # | Officer | High-side | Low-side | Approved | Rejected (David Wilson: 14 (93.33%) approved; Olga Admin 1 (6.67%)).
Narration: track AI decisioning; number of applications, loan amounts, bad accounts; time-frame + vendor filters; human/underwriter override tracking AND a **vendor overrides** category (list vendors, how many accounts overridden, how many go bad — track their decision making). "Okay, and that concludes the reports and metrics section."

---

## 2. Complete report catalog

### Category 1: Business performance (dashboard) — `/portfolio/dashboard/`
Global filter: **Comparison period** dropdown (Daily / Weekly / Monthly / Quarterly / Annually) — all widget figures recompute against the chosen period, with delta-vs-previous-period arrows.

| Widget/report | Purpose | Filters/controls | Data shown |
|---|---|---|---|
| **Geographical report** | Heat-map distribution of activity on a map (Leaflet/OpenStreetMap) | Dropdown: **Processed Applications** / **Delinquency by heat map** / **Portfolio outstandings by heat map**; map zoom +/- | Map heat blobs (blank in UAT — fake addresses) |
| **Portfolio report** | Portfolio-level money KPIs | comparison period (global) | 5 sparkline tiles: Portfolio size, Disbursed amount, Repaid Amount, Profit, Profit/Portfolio — each with mini trend chart, current value, and red/green delta vs prior period |
| **Risk report** | Payment-behaviour breakdown | comparison period | Donut chart + 6 metrics: Early payments, Paid on time, Paid late, Bad debt, At risk, Loses (each $ value + delta) |
| **Collection report** | Collections/delinquency snapshot | comparison period | Tiles: Bad debt (with step chart), Paid late, Write-offs, Debt roll rate; plus **Bad debt Collectability** stacked bar (Low % / Medium % / High %) |
| **Performance report** | Application throughput | **Number / Amount** toggle | Number of processed applications (count + delta + trend line) |
| **Top write-off reason** | Write-off cause ranking | **Number / Amount** toggle | Grid of reason tiles with counts: Bankruptcy, Insolvency, Liquidation, Unenforceable, The debt is too old, Abscond, Uneconomical to Collect, Uncollectible, (+ "Test Environment Only - Test Account Write-off" in UAT data) |
| **Time series report** | Any core metric over time | Metric dropdown: Number of Processed Applications / amount / percent of applications / portfolio / profitability / yield / repaid amount / written-off amount (per narration) | Full-width line chart, monthly x-axis over 5 years |

### Category 2: Portfolio — `/portfolio/portfolio`
Controls: "- All Reports -" dropdown (brings a selected report to top, hides the rest) | period granularity dropdown (Monthly, etc.) | **Chart / Table / Both** view toggle | **Export** button | **date-range slider** (brush over full history mini-chart, draggable From/To handles).
Reports shown:
- **Active loans vs Portfolio** — dual-axis line: active loan count vs portfolio $ balance over time. Table mode: Date | Active loans | Portfolio.
- **Overall repayment amount per interval** — lines: Repaid, Principal, Fees, Interest amount per interval.
- (further reports below the fold under "All Reports", not scrolled to on camera)

### Category 3: Operational — `/portfolio/operational`
Controls: period dropdown (Monthly) | report dropdown: **Business status / Application statistics / Origination efficiency / All reports** (per narration; "Origination efficiency" shown).
- **Origination efficiency** — per-user throughput chart. Own controls: Chart/Table/Both | metric dropdown ("Applications originated") | "Users" multi-select dropdown | Export. Line per officer (David Wilson, Olga Admin, Administrator Initial, Ok Sh, Andy Wilson).

### Category 4: Risks — `/portfolio/risks`
Controls: All Reports dropdown | Monthly | Chart/Table/Both | date-range slider.
Reports visible: **Bad rate trend**, **Delinquency performance**. (Dave doesn't drill in; frames show them loading.)

### Category 5: Scoring — `/portfolio/scoring`
Controls: report dropdown (**System stability / Scorecard accuracy / - All Reports -**) | period dropdown (**Last month**) | **Export** | **Update** (recompute) buttons.
- **System stability** — population-stability table per score band: # | Score | Risk level | Actual % | Expected % | (A-E) | (A/E) | Ln(A/E) | Index; overall index (0.4894) in highlighted summary row (PSI-style report).
- **Scorecard accuracy** — per score band: # | Score | Risk level | Accounts | Bad | Bad rate | Expected bad rate.
- **Delinquency performance** (in All Reports view) — per score band aging: # | Score | Accounts | Current | 1-30 | 31-60 | 61-90 | 91+ | Written off; **# / %** toggle.
- **Final score** — score-distribution report (section heading visible at page bottom, content below the fold).
Score bands / risk levels (the scorecard's grade scale): 560-1000 = Target client; 460-559 = Acceptable client; 219-459 = Restricted client; 142-218 = Strongly Restricted client; 0-141 = Rejected clients.

### Category 6: Underwriting — `/portfolio/officers`
Controls: - All Reports - dropdown | Last month period dropdown | **Export** | **Update**.
- **Underwriter monitoring** — per officer: # | Officer | Accounts (count + % share bar) | Loan amount ($ + % share bar) | Bad (count + % share bar).
- **Overrides by underwriters** — per officer: # | Officer | High-side | Low-side | Approved | Rejected (count + % each).

---

## 3. Data fields / metrics visible (every column, KPI, chart)

**Business performance KPIs (with sample UAT values, Monthly→Annually):**
- Portfolio size: $706.95k (delta -$2.01k monthly / -$7.56k annually)
- Disbursed amount: 0.00 / $16.00k (delta -$16.00k / -$45.34k)
- Repaid Amount: $2.42k / $30.81k (delta -$1.05k / +$11.48k)
- Profit: $415.39 / $7.20k (delta +$67.56 / -$1.33k)
- Profit / Portfolio: 0.00 / 0.01
- Risk report: Early payments 0.00; Paid on time $2.42k/$26.84k; Paid late 0.00/$3.00; Bad debt $3.23k; At risk $3.23k; Loses 0.00/$1.79k — donut chart of the same
- Collection report: Bad debt $3.23k (step-line mini chart); Paid late 0.00/$3.00; Write-offs 0.00/$1.79k; Debt roll rate $49.00; Bad debt Collectability: Low 0.00% / Medium 100.00% / High 0.00% (color-banded bar)
- Performance report: Number of processed applications 0 (↓-1) / 3 (↓-16), trend line
- Top write-off reason counts: Bankruptcy 1, all others 0 (+Test-account write-off 1)
- Time series: y 0–1.0 normalized, x monthly Jul 2021–Jul 2026, metric-selectable

**Portfolio category:**
- Active loans vs Portfolio: left axis count (0–60), right axis $ (0–500k+); legend Active loans / Portfolio
- Table: Date (month-end), Active loans (23–25), Portfolio ($690k–$708k range)
- Overall repayment amount per interval: legend Repaid / Principal / Fees / Interest amount; y up to ~50k
- Date-range brush: From 2021-08-31 / To 2026-07-06 (editable via drag)

**Operational:** Applications originated per user per month (0–50 range), officer legend.

**Risks:** Bad rate trend; Delinquency performance (chart placeholders).

**Scoring:**
- System stability columns: #, Score, Risk level, Actual %, Expected %, (A-E), (A/E), Ln(A/E), Index; e.g. Target 32.43% actual vs 13.37% expected, index 0.1689; overall 0.4894
- Scorecard accuracy columns: #, Score, Risk level, Accounts, Bad, Bad rate, Expected bad rate; e.g. Strongly Restricted: 6 accounts, 5 bad, 83.33% vs 6.40% expected
- Delinquency performance columns: #, Score, Accounts, Current, 1-30, 31-60, 61-90, 91+, Written off; # / % toggle; e.g. 560-1000: 83 accounts, 25 current, 46 written off

**Underwriting:**
- Underwriter monitoring: Officer, Accounts n (%), Loan amount $ (%), Bad n (%) — David Wilson 13 (92.86%), $110,113.50 (98.66%), 4 (80.00%); Olga Admin 1 (7.14%), $1,500.00 (1.34%), 1 (20.00%)
- Overrides by underwriters: Officer, High-side n (%), Low-side n (%), Approved n (%), Rejected n (%) — David Wilson approved 14 (93.33%); Olga Admin 1 (6.67%)

**Chrome/branding:** URL kelowna-dental-uat.turnkey-lender.com; footer "PaySpyre Financial - 2026 7.9.0.30" with ISO-certification logo; map credits "Leaflet | © OpenStreetMap contributors".

---

## 4. Dave's editorial comments (verbatim-ish, must-haves vs ignorable)

**Overall framing / gaps:**
- "There's a lot of changes that need to be made here, primarily around collections and quote unquote early payments or bad debt at risk and losses."
- "The current platform iteration was not designed to work with daily simple interest accounts, which is the norm in Canada." (root cause of the report changes)

**Geographical report:**
- "Normally there is a map here with a heat map distribution of our active accounts" — blank only because UAT addresses are fake.
- "The default here should actually be portfolio, and then we can look at applications and delinquency separately."

**Vendor filter — the single most repeated requirement (stated for Business performance, Portfolio, Operational, time series, Scoring, Underwriting):**
- "The other thing that we need up top here as well as the period for a filter is vendor. We need all... and then we should be able to have selections where we add a vendor or have multiple vendors or only have one vendor, so basically check boxes by vendors like SelectAll."
- Use case: "if we wanted to take a look at the total portfolio for Dr. Webster in all of his locations [Kelowna Dental Centre, Yaletown, Bright Side] and all his vendor accounts, then we would be able to select those three vendor accounts and have a total report."
- "We also need to have the vendor selection here [Portfolio category]... so that we can dial in and effectively communicate vendor performance."
- "Again, we want to have vendor filters [Operational]."

**Profit split (Portfolio report):**
- "We would want this [profit] to be broken down to the portion that is due to PaySpyre and the portion that is due to vendors... a drop down list here of gross or vendor or PaySpyre so that we can differentiate and understand our profitability per vendor and know how much that vendor has made themselves using our services."

**Early payments / payment mechanics (design constraint, not just a report):**
- "It's hard to recognize what classifies as early payments. I guess this would be kind of extra payments that are outside of installments."
- "The payments that are set up on the amortization schedule at the time that the loan is created are not impacted by extra payments." Scheduled payments still come out; only changeable "manually... through the scheduled transaction functions in the servicing [side]."
- Rationale: prepaying via extra payments while interest accrues → borrower "paying interest short... a very negative situation for the borrower... very bad public perception that we're taking advantage."
- Report definition: "Early payments, I guess, would be extra payments... essentially anything that's not part of scheduled payments. To identify this, we would have to have an ongoing understanding of what was scheduled to come out on any given moment."
- "Paid on time, so those would be those scheduled payments... what was actually paid on time versus what was expected, and again, paid late... how many accounts were past due and brought up to date and paid late."

**Bad debt / delinquency buckets (Risk + Collection reports) — must-have replacement taxonomy:**
- "I wouldn't say bad debt is a good thing [as a label]. I would say what we want here is kind of like current month late, and then at risk would be anything that is POT 30 or above, and then losses is anything that has been actually written off."
- "So collection report, again, bad debt doesn't make a lot of sense. We want the different queues going current month late. How much do we have in current month late? ...in POT 30s? ...POT 60s? ...POT 90s? ...in default? ...insolvency? How much have we written off?"
- "This debt roll is actually the percentage of current month late that roll into a new month and become POT 30s."

**Collectability score (nice-to-have, low priority):**
- "If we can come up with a collectability score, that would be fantastic. I think it's going to be highly inaccurate initially."
- "I'm not so much worried about the fictitious collectability score as having a solid understanding of our different delinquency buckets and where they are." ← the actual must-have.

**Time series report additions:**
- "I would also put current month lates, POT 30s, POT 60s, POT 90s, and defaults in there."

**UI behaviors he wants kept (Portfolio category):**
- "All this dropdown does is bring whatever report you're looking [for] to the top and hide the rest of them."
- "You can change the time frame with this sliding scale to hone in on a particular time frame. We can chart, table, both. We can export. We can have a selection for time."
- "The time reports are daily, weekly, monthly, quarterly and annually... static... in all business performance, portfolio, operational and risks."

**Scoring:**
- Risk-level → grade mapping: "rejected would be poor/fail clients, strongly restricted would be weak, restricted would be average, acceptable would be good, target would be excellent."
- "We probably, again, should have something in here for vendors so that we can see if we've got an outlier vendor, say, for example, somebody in Toronto that has a riskier portfolio."
- Per-vendor scorecards: "Maybe we need to assign a specific scorecard to that vendor and we want to be able to assign and adjust scorecards per vendor. So we're going to have a default scorecard and then... we want to have the ability to do that." (must-have capability)

**Underwriting:**
- "This is basically tracking the underwriter's performance, so we need to be tracking the AI decisioning here, the number of applications, the loan amounts, the bad accounts and we're looking at time frames, we're looking at vendors as far as filter options."
- "Overrides by underwriters is going to be something that we need to do because humans should have the ability to override — or certain underwriters should have the ability to... override particular things."
- "We should also have a category here for vendor overrides so that we can track all the list of vendors and how many accounts are getting overridden, how many of those are going bad, to track their decision making." (new report, doesn't exist in TL)

**Explicitly ignorable:** the "Test Environment Only - Test Account Write-off" reason (UAT artifact); the blank map (fake test addresses); the fictitious collectability score (deprioritized by Dave himself). Nothing else was marked "skip"/"we don't use this" — Dave treats the whole workplace as in-scope but re-specified.

---

## 5. Integrations/external services referenced

- **Leaflet + © OpenStreetMap** — map rendering for the Geographical heat-map report (visible credit on the map widget).
- **Export buttons** — present on Portfolio, Operational (Origination efficiency), Scoring, and Underwriting pages (file/spreadsheet export of the chart/table data; format not shown on camera). No accounting-system (QuickBooks/GL) export appears in this video.
- **ISO certification badge** + Turnkey Lender platform footer ("PaySpyre Financial - 2026 7.9.0.30") — white-labeled TL instance on `*.turnkey-lender.com` UAT.
- Cross-references to other workplaces: scheduled-transaction editing lives in **Servicing** ("the scheduled transaction functions in the servicing [side]"); AI/automated decisioning referenced as the thing Underwriting reports must monitor.
- No email, payment-processor, or credit-bureau integrations shown in this workplace.
