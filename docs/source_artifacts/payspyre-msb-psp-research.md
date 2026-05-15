# PaySpyre Financial — MSB/PSP Registration Legal Research
**Prepared for:** David Wilson / Michael (MMS Law Brief)
**Date:** April 2026
**Research Scope:** FINTRAC MSB registration (PCMLTFA), Bank of Canada PSP registration (RPAA), provincial requirements (BC, Alberta), competitor landscape, timelines, and regulatory path to June 2026 launch

---

## Executive Summary

PaySpyre's regulatory exposure depends heavily on which business model it operates. The analysis below shows a meaningful distinction across all three models:

| Model | MSB Required? | PSP Required? | PCMLTFA Reporting Entity? | Provincial License? |
|---|---|---|---|---|
| **A: Clinic-Funded Servicer** | **Likely not** (critical carve-out applies) | **Likely not** (incidental exclusion — strong case) | **Yes** — as financing entity (April 2025) | **No** (APR < 32%) |
| **B: Direct Lender** | **Likely not** (lender ≠ MSB) | **Likely not** (incidental exclusion — solid case) | **Yes** — as financing entity | **No** (APR < 32%) |
| **C: Portfolio Lender (B2B)** | **No** (B2B, no consumer payments) | **No** | Possibly (depends on business purpose) | **No** |

**Key finding:** PaySpyre does NOT appear to need MSB registration under any of its three models, and likely does NOT need PSP registration under Models A and B — provided payment collection is structured as incidental to lending. However, as of April 1, 2025, PaySpyre IS a reporting entity under the PCMLTFA as a "financing or leasing entity," which requires a full AML compliance program. This is lower-burden than MSB registration but still requires immediate action.

**The June 2026 launch is achievable** without waiting for MSB or PSP registration, but PaySpyre must implement an AML compliance program and file with FINTRAC as a financing entity before operating.

---

## 1. FINTRAC — Money Services Business (MSB) Analysis

### 1.1 What Makes an MSB

Under the *Proceeds of Crime (Money Laundering) and Terrorist Financing Act* (PCMLTFA), S.C. 2000, c 17, s. 5(h), a **Money Services Business** is a person or entity with a place of business in Canada engaged in the business of providing at least one of:

1. Foreign exchange dealing
2. **Remitting or transmitting funds** by any means or through any person, entity, or electronic funds transfer network
3. Issuing or redeeming money orders, traveller's cheques, or similar negotiable instruments
4. Dealing in virtual currencies
5. Crowdfunding platform services
6. Cheque cashing
7. Armoured car services

**The pivotal question for PaySpyre is whether collecting loan payments via PAD and remitting net proceeds to clinics constitutes "remitting or transmitting funds."**

### 1.2 Remitting/Transmitting Funds — What It Covers

FINTRAC's guidance on [remitting or transmitting funds](https://fintrac-canafe.canada.ca/msb-esm/msb-eng) specifies that this includes:
- Sending funds from one person/entity to another using EFT networks, or traditional methods
- Invoice payment services (acting as intermediary between payer and payee)
- Payment services for goods and services (acting as intermediary between buyer and seller)

**Critical exceptions:**
- A person or entity that **solely receives payments on behalf of the payee to settle a debt** and does not further transfer the payment instructions to the original payee (not an MSB)
- A person or entity that **solely accepts a payment for goods or services that they supplied to their own customer** (not an MSB)

### 1.3 Application to PaySpyre's Models

#### Model A: Clinic-Funded Servicer

**Facts:** PaySpyre collects PAD payments from patients (borrowers) and remits funds to clinics (lenders), retaining 50% of interest and origination fees.

**MSB Analysis:**

The critical question is whether PaySpyre is acting as an **intermediary remitting funds** or whether it is **collecting payments to settle debts** (the exception).

Arguments PaySpyre is NOT an MSB:
- PaySpyre is not in the business of money transmission for its own sake; it is in the business of **loan administration/servicing**
- The fund flows (PAD collection → remittance to clinic) are incidental to the core service of underwriting and managing loans
- The 2022 FINTRAC retraction of PI-7670 (payment processing exemption) primarily targeted PSPs acting as intermediaries between buyers and sellers of goods — not loan servicers
- There is a strong analogy to the FinCEN (U.S.) ruling on [debt management companies](https://www.fincen.gov/resources/statutes-regulations/administrative-rulings/definition-money-services-business-debt): where fund collection/remission is ancillary to a debt management service, it does not constitute money transmission
- PaySpyre's revenue comes from **interest income and origination fees** (lending revenue), not from the act of fund transfer itself

Arguments PaySpyre COULD be an MSB:
- FINTRAC has taken the position (post-2022) that invoice payment services and payment services for goods/services are "remitting or transmitting funds"
- [Blakes](https://www.blakes.com/insights/changes-to-regulations-under-the-proceeds-of-crime/) and [Osler](https://www.osler.com/en/insights/updates/fintrac-retracts-merchant-servicing-and-payment-processing-exemptions/) noted that the deletion of the payment processing exclusion significantly expanded MSB coverage
- PaySpyre does collect from patients and remit to clinics — a three-party fund flow

**Weight of analysis for Model A:** **Likely not an MSB**, but the argument is not airtight without legal opinion. The strongest protective factor is that PaySpyre is not advertising or marketing a payment service — it markets a **lending platform**. Revenue is from lending, not from fund transmission. If this position is taken, PaySpyre should maintain documentation of: (a) that its fund flows are incidental to loan servicing, (b) that it does not market payment services, and (c) that it does not charge transaction fees for money movement.

#### Model B: Direct Lender

**Facts:** PaySpyre funds and owns the loan. It collects PAD payments directly from borrowers. No third-party remittance needed to fund the loan — PaySpyre is the lender and the recipient.

**MSB Analysis:** This is the **strongest case for non-MSB status**. PaySpyre is collecting repayments on its own loans. This falls squarely within the FINTRAC exception: "solely accepts a payment for goods or services [here: credit] that they supplied to their own customer." A direct lender collecting its own loan payments has never been treated as an MSB under PCMLTFA. The fund flow is entirely within PaySpyre's own lending operation.

**Conclusion for Model B: Not an MSB.**

#### Model C: Portfolio Lender (B2B)

**Facts:** PaySpyre lends money to dental clinics, secured by clinic patient loan portfolios. Purely B2B secured lending.

**MSB Analysis:** This is clearly not an MSB activity. PaySpyre is making a secured business loan to a clinic, not remitting funds between parties in a payment chain. There are no consumer payment flows that could be characterized as money transmission.

**Conclusion for Model C: Not an MSB.**

### 1.4 Critical Distinction: MSB vs. Reporting Entity (Financing Entity)

This is the **October 2024/April 2025 change David is likely thinking about.** The October 2024 and April 2025 amendments to the PCMLTFA did NOT create new MSB categories for lenders. Instead, they created a **separate category of reporting entity — "financing or leasing entities."**

Under the amended PCMLTFA regulations (effective **April 1, 2025**), financing or leasing entities are now **reporting entities** with AML obligations if they engage in:
- Financing or leasing of **property for business purposes** (not real property)
- Financing or leasing of **property valued at $100,000 or more**
- Financing or leasing of **passenger vehicles in Canada**

**Does PaySpyre qualify?** This requires legal analysis. If "property for business purposes" encompasses dental equipment financing or business lending (Model C), PaySpyre may be caught. However, PaySpyre's core product — **patient personal loans for dental procedures** — is arguably consumer lending for personal use, not property financing for business purposes. The $100,000 threshold also appears oriented toward equipment/asset financing, not personal consumer loans.

**If PaySpyre's loans are consumer personal loans (not business property financing), it may not be a "financing or leasing entity" under the PCMLTFA.** However, if Model C (B2B lending to clinics) involves lending against business property, it could trigger the financing entity rules.

**MMS Law must advise on whether PaySpyre's consumer dental loans constitute "financing of property for business purposes."**

If PaySpyre IS a financing entity:
- It must implement a PCMLTFA compliance program (compliance officer, policies, risk assessment, training)
- It must verify client identities (KYC)
- It must report suspicious transactions, large cash transactions, and listed entity property
- **Compliance deadline: April 1, 2026** (one year transition period)
- It is **NOT required to register with FINTRAC** (financing entities are reporting entities, not MSBs — different obligation)

### 1.5 If MSB Registration Were Required

Should MMS Law conclude PaySpyre does need MSB registration (which this analysis considers unlikely for all three models), the practical details are:

**Registration Process:**
- Apply via FINTRAC's online portal (no fee)
- Must submit: corporate structure, ownership, services offered, locations, AML/CFT compliance program, compliance officer appointment
- As of 2025, FINTRAC conducts criminal record checks on CEOs, directors, and 20%+ shareholders

**Timeline:** In 2025, typical approval takes **5–6 months** (increased from 1–3 months in prior years). Some applications take longer.

**Compliance Obligations Once Registered:**
- Compliance program (officer, policies, risk assessment, effectiveness review every 2 years)
- KYC: Verify identity for transactions ≥ $1,000
- Record keeping: 5 years minimum
- Report suspicious transactions, large cash ($10,000+), and EFTs
- Registration renewal: Every 2 years

**Penalties for Operating Without Registration:**
- Administrative penalty: Up to **$100,000** for operating as an MSB without registration
- Criminal sanctions: Up to $1,000,000 fine (entity) or $250,000 (individual) on summary conviction, plus up to 2 years imprisonment; on indictment, $2,000,000 or up to 5 years
- Proposed (Bill C-2, 2025): Very serious violations up to **$20 million** or 3% of global revenue

**Can PaySpyre operate while registration is pending?** Technically, MSBs must register **before** beginning operations. However, the practical enforcement risk during a bona fide application period is lower than for an entity that has never engaged with FINTRAC. Seek legal advice before operating.

---

## 2. Retail Payment Activities Act (RPAA) — PSP Analysis

### 2.1 Governing Framework

The *Retail Payment Activities Act*, S.C. 2021, c. 23, in force **November 1, 2024**, requires **payment service providers (PSPs)** to register with the Bank of Canada.

A PSP is defined as "an individual or entity that performs payment functions as a **service or business activity that is not incidental** to another service or business activity." (RPAA, s. 2)

### 2.2 Five Payment Functions

Under RPAA s. 2, a PSP performs at least one of:

1. **Account provision/maintenance** — Storing end-user personal/financial information for future EFTs
2. **Holding funds** — Keeping payer/payee funds at rest pending future withdrawal/transfer
3. **EFT initiation** — Launching the first payment instruction enabling an EFT at an end-user's request
4. **EFT authorization or instruction transmission** — Establishing whether EFT can proceed; receiving/sending payment instructions
5. **Clearing or settlement services** — Netting, position calculation, final settlement

### 2.3 The Four-Part Test

A business must register as a PSP if **all four** are true:
1. It performs one or more payment functions as a **non-incidental** service
2. The function relates to electronic fund transfers in fiat currency (retail payment activities)
3. Geographic scope: place of business in Canada, or both directing services at AND serving Canadians
4. No applicable exclusion applies

### 2.4 The Critical Incidental Exclusion

The Bank of Canada has published definitive case scenarios. The most relevant is the [Bank of Canada's Lending Case Scenario](https://www.bankofcanada.ca/2024/10/case-scenarios-about-lending/) (October 2024):

**Company A (Financing Company — Not a PSP):**
- Partners with merchants to offer consumer financing
- Customers complete loan application on tablets, consent to PAD repayments
- Company A transfers funds equal to purchase amount to merchant's account
- Borrowers repay via **pre-authorized debits** or credit card payments to Company A
- Revenue: only interest from borrowers (lending); no fees from merchant or payments
- Does not advertise as payment solution; seen as loan provider

**Conclusion: Company A is NOT a PSP. No registration required.**

**Why not?** Even though Company A stores personal/financial information (payment function #1) and collects PAD repayments (which technically constitutes payment function #3 or #4), these functions are **incidental to lending**. Key indicators:
- All revenue from interest on loans (not payment fees)
- Not marketing a payment service
- Customers/merchants perceive it as a loan provider
- Payments only to issue loan and collect repayment — not to facilitate commerce between third parties

**Company B (BNPL — IS a PSP):**
- Integrates as a checkout payment method
- Charges merchants transaction fees (distinct payment revenue)
- Markets itself as a payment option to merchants
- Both merchants and consumers perceive it as a payment service

**Conclusion: Company B IS a PSP. Registration required.**

### 2.5 Application to PaySpyre

#### PaySpyre vs. the Bank of Canada's Lending Case Scenarios

| Factor | Company A (Not PSP) | PaySpyre Model A | PaySpyre Model B |
|---|---|---|---|
| Revenue from payments? | No — interest only | Primarily interest + fees (not payment transmission fees) | Interest only |
| Markets as payment service? | No | No — markets as lending platform | No |
| Merchant/clinic charges fees for fund transmission? | No | No (origination fees = loan fees, not payment fees) | N/A |
| Customer/clinic perceives as payment service? | No — loan provider | No — dental financing platform | No — lender |
| Initiates PAD on own account? | Yes — collect own loan repayments | Yes — collect loan repayments | Yes — collect own repayments |
| Remits to third party? | Yes — to merchant | **Yes — to clinic** (this is the key difference) | No |

**Key distinction for Model A:** PaySpyre collects from patients and **remits net proceeds to clinics**. Company A also transfers funds to the merchant. The Bank's case scenario treats the fund transfer to the merchant as "purchasing the future revenue stream from the borrower" — part of the loan mechanics, not a payment service. PaySpyre's remittance to clinics is structurally analogous: clinics provided the loan capital, and PaySpyre remits collected repayments back as loan administrator. This is more analogous to Company A than Company B.

**Critical factors supporting the incidental exclusion for PaySpyre Model A:**
- All revenue is from interest income and origination fees (lending revenue), not payment transmission fees
- PaySpyre does not market itself as a payment service to clinics or patients
- Clinics and patients understand PaySpyre as a **loan origination and servicing platform**
- The PAD collection and remittance are necessary to execute the lending service — they have no purpose independent of the loan
- PaySpyre does not provide payment infrastructure or APIs to enable third-party commerce

**Factors creating some risk:**
- The three-party flow (patient → PaySpyre → clinic) with PaySpyre holding funds in transit could be characterized as "holding funds on behalf of an end user"
- The remittance function is more prominent than in Company A (regular distributions to clinics)
- Rotessa (PAD processor) as a third party in the payment chain adds complexity

**Mitigation:** Structuring PaySpyre's documentation, contracts, and marketing to emphasize the lending nature of the service — never the payment facilitation aspect — is essential. The revenue model (no separate payment fees) is the strongest protective factor.

**For Model B (Direct Lender):** The case is even cleaner than Company A. PaySpyre collects repayments on its own loans with no third-party remittance. This is textbook incidental payment activity.

**Conclusion:**
- **Model A:** Likely not a PSP; incidental exclusion applies with appropriate structuring. Borderline — legal opinion recommended.
- **Model B:** Not a PSP; strong case under Bank of Canada guidance.
- **Model C:** Not a PSP; B2B lending with no consumer payment functions.

### 2.6 What About Rotessa (PaySpyre's PAD Processor)?

Rotessa is the entity actually initiating and processing the PAD debits. Under the RPAA, if Rotessa is a registered PSP (or an agent of a registered PSP), and PaySpyre is merely the **instructing party** telling Rotessa to initiate debits, PaySpyre may not be performing payment functions at all — it is directing a PSP to act on its behalf. Under RPAA, **agents and mandataries of registered PSPs are excluded from RPAA registration**.

**PaySpyre should confirm with Rotessa whether Rotessa is registered as a PSP under the RPAA.** If Rotessa is registered and PaySpyre is acting as Rotessa's client (not performing independent payment functions), the RPAA argument becomes stronger. However, PaySpyre should be careful about being characterized as an agent of Rotessa versus a customer of Rotessa's services.

### 2.7 PSP Registration Details

If registration is ultimately required:

**Process:**
- Apply via Bank of Canada's **PSP Connect** portal
- One-time non-refundable application fee: **$2,500**
- Processing time: Up to **10 months** per current Bank of Canada guidance
- Must demonstrate: operational risk management framework, incident response procedures, end-user fund safeguarding plan

**Deadline:** Entities planning to operate after **September 8, 2025** should have already applied. For a new entrant like PaySpyre in 2026, apply immediately upon determination that registration is required.

**Annual obligations once registered:**
- Annual assessment fee (based on volume/size)
- Annual report covering compliance, metrics, and incident history (first due March 31, 2026 for existing registrants)
- Notify Bank of Canada of significant business changes

**Penalties for Non-Registration:**
- Violation of RPAA s. 104 for operating without submitting application
- Notice of violation (NOV) with administrative monetary penalties:
  - Serious violation: up to **$1 million**
  - Very serious violation: up to **$10 million**
- Low potential harm baseline: ~$30,000; medium: ~$300,000; high: ~$1.2 million
- Public disclosure of non-compliance

---

## 3. Provincial Requirements

### 3.1 British Columbia

**High-Cost Credit Licensing (Consumer Protection BC):**

Under BPCPA Part 6.3 and the *High-Cost Credit Products Regulation* (BC Reg 290/2021), **any person who offers, arranges, provides, or facilitates high-cost credit products** (including loan brokers) to BC consumers must be licensed by Consumer Protection BC.

**High-cost credit = APR > 32%.**

PaySpyre's maximum rate is **29.9% APR**, which is **below the 32% threshold**. Therefore:

**PaySpyre does NOT need a high-cost credit license in BC** under current rates.

However:
- As of January 1, 2025, the Criminal Code caps high-cost credit APR at 35%. PaySpyre is below both the provincial license threshold (32%) and the criminal threshold (35%).
- If PaySpyre ever raises rates above 32% APR, a Consumer Protection BC license becomes mandatory
- PaySpyre should maintain documentation of its APR calculations to demonstrate compliance with the sub-32% threshold

**Payday Lending:**
PaySpyre's products (multi-month installment loans) do not meet the definition of a payday loan ($1,500 or less, 62-day term or less). No payday lending license required.

**Other BC requirements:**
- Standard business registration in BC
- Cost of credit disclosure requirements under BPCPA apply to all consumer lenders (not just high-cost)
- PaySpyre must provide clear disclosure of APR, total cost of borrowing, and loan terms in its loan agreements

**BC FINTRAC Note:** BC does not have a separate provincial MSB licensing requirement. FINTRAC federal registration (if applicable) covers BC.

### 3.2 Alberta

**Consumer Lending:**
PaySpyre already has a vendor NTT in Alberta. Alberta's consumer protection framework for installment loans above the payday loan threshold (loans > $1,500 or > 62 days) does not require a separate lending license for non-payday products, as long as:
- Rates are below criminal usury limits (now 35% APR under Criminal Code)
- Cost of credit disclosures are provided per Alberta's Consumer Protection Act

PaySpyre's 29.9% max APR is below both the 35% criminal threshold and below the 32% threshold that triggers Alberta's high-cost credit licensing (Alberta has a similar 32% APR threshold for "high-cost credit" products).

**Note on future plans:** If PaySpyre expands high-cost credit products to any province at > 32% APR, licensing will be required in that province.

### 3.3 Multi-Province Lending

For each additional province PaySpyre enters, it should:
1. Confirm APR stays below the province's high-cost credit threshold (typically 32%)
2. Review provincial cost-of-credit disclosure requirements
3. Ensure loan agreements comply with provincial consumer protection acts
4. Obtain provincial business licenses where required

---

## 4. How Business Model Structure Affects Registration

### 4.1 Detailed Analysis by Model

**Model A: Clinic-Funded Servicer**

PaySpyre's role: underwriter, servicer, payment collector/remitter
- **MSB:** Not required. PaySpyre is in the business of loan servicing, not money transmission. The collection and remittance of loan payments is incidental to its lending service.
- **PSP:** Likely not required. Applying the Bank of Canada's lending case scenario (Company A), PaySpyre's payment activities are incidental to its lending business. No separate payment fees; no payment service marketing; borrowers and clinics perceive PaySpyre as a lending platform.
- **PCMLTFA Reporting Entity:** Uncertain — depends on whether patient dental loans constitute "financing of property for business purposes." If yes, PaySpyre must implement AML compliance program by April 1, 2026. If no, still best practice to consult FINTRAC.
- **Provincial License:** Not required at current rates (< 32% APR).
- **Regulatory Burden:** Lowest

**Model B: Direct Lender**

PaySpyre's role: lender and servicer, collecting its own loan repayments
- **MSB:** Not required. Collecting one's own loan repayments is squarely outside the MSB definition.
- **PSP:** Very likely not required. Even stronger case than Model A — no third-party remittance. The Bank of Canada's Company A scenario directly applies.
- **PCMLTFA Reporting Entity:** Same analysis as Model A — consumer dental loans may not trigger the "financing entity" rules.
- **Provincial License:** Not required at current rates.
- **Regulatory Burden:** Very low

**Model C: Portfolio Lending to Clinics (B2B)**

PaySpyre's role: B2B secured lender
- **MSB:** Not required. B2B secured lending has no consumer payment flows that constitute money transmission.
- **PSP:** Not required. No consumer payment functions performed.
- **PCMLTFA Reporting Entity:** **More likely to apply.** This is exactly the type of business lending (financing business property/receivables) that the April 2025 rules target. PaySpyre lending to clinics secured by patient loan portfolios may qualify as "financing property for business purposes." Legal opinion required.
- **Provincial License:** Not required (B2B lending).
- **Regulatory Burden:** AML compliance program needed if financing entity rules apply.

### 4.2 The Registration-Minimizing Structure

Based on this analysis, the structure with the **lowest regulatory burden** is:

1. **Operate as a lender/servicer** (Models A or B), not as a payment intermediary
2. **Ensure all revenue comes from lending** (interest, origination fees) — zero fees labeled as "payment processing" or "transmission fees"
3. **Market as a dental financing platform**, not a payment service
4. **Use Rotessa as the PSP** for the actual PAD processing — confirm Rotessa's RPAA registration, and ensure PaySpyre acts as Rotessa's client rather than as an independent payment processor
5. **Structure contracts** to show PaySpyre is collecting its own loan receivables (or acting as agent/administrator for the clinic as lender), not facilitating third-party payments
6. **Implement PCMLTFA AML compliance program** as a financing entity (required by April 1, 2026 regardless)
7. **Maintain rates below 32% APR** to avoid provincial high-cost credit licensing
8. **Document the legal analysis** for both the MSB and PSP non-registration positions — if regulators ever inquire, PaySpyre needs a defensible paper trail

---

## 5. Competitor Registration Status

### 5.1 LendCare Capital Inc.

LendCare is Canada's largest point-of-sale lending platform, operating in healthcare (dental, vision, cosmetic), automotive, and recreational sectors. It is a **direct lender** — it provides its own capital to fund patient loans.

**Registration status:** LendCare would be treated as a **lender** and **financing entity** under PCMLTFA (not an MSB). It is not known to be registered as an MSB on the FINTRAC public registry in the capacity of a dental lender. LendCare is not a PSP — it does not market payment services. Its model is structurally identical to Company A in the Bank of Canada's case scenario (direct lender collecting own repayments).

LendCare was acquired by Patient Capital Inc. (private equity) and operates under OAC Financial / Patient Capital branding. As a large financing entity, it would be subject to the April 2025 PCMLTFA rules and would need an AML compliance program.

### 5.2 iFinance Canada / Dentalcard

iFinance/Dentalcard provides patient financing for dental/medical procedures. It operates as a **direct lender** or platform lender (depending on product).

**Registration status:** Like LendCare, iFinance operates as a lender, not an MSB. Under the current regulatory framework, dental/medical patient lenders are classified as financing entities (PCMLTFA, April 2025), not MSBs. There is no public record of iFinance being registered as an MSB on the FINTRAC registry for its lending operations.

### 5.3 Fairstone Financial

Fairstone is a large consumer lender (personal loans, home equity). Its parent company is now **Fairstone Bank of Canada** (a federally regulated bank under the Bank Act), which exempts it from RPAA (banks are exempt from RPAA) and from MSB registration requirements. Fairstone lobbied the Bank of Canada and Finance Canada on the Criminal Code interest rate cap (35% APR) — it operates above 32% APR and would need high-cost credit licenses in applicable provinces.

### 5.4 Medicard Finance

Medicard provides patient financing for medical/dental procedures. It operates as a consumer lender. Based on publicly available information, Medicard is structured as a **consumer finance company/lender** with no indication of MSB registration. It would be subject to the April 2025 PCMLTFA financing entity rules.

### 5.5 Competitive Takeaway

None of PaySpyre's direct competitors in dental patient financing appear to be registered as MSBs. They operate as lenders/financing entities. This strongly supports the position that **dental patient lending platforms are not MSBs**. The April 2025 PCMLTFA expansion put all of them into the "reporting entity — financing entity" category, which requires AML compliance programs but not MSB registration.

---

## 6. Timeline, Urgency, and Operational Windows

### 6.1 MSB Registration Timeline

If MSB registration is ultimately determined to be required:
- **Application preparation:** 2–4 weeks (with professional assistance)
- **FINTRAC review:** 5–6 months in 2025–2026 (increased significantly from prior years)
- **Total:** ~6–7 months from start to registration
- PaySpyre cannot operate as an MSB prior to registration (pre-registration operation is a serious violation)
- **For a June 2026 launch:** Application would need to be submitted by November–December 2025 at the latest

**However, based on this analysis, PaySpyre is very unlikely to need MSB registration.**

### 6.2 PSP Registration Timeline

If PSP registration is ultimately determined to be required:
- **Application fee:** $2,500 (non-refundable, one-time)
- **Processing time:** Up to **10 months** per Bank of Canada guidance
- **For a June 2026 launch:** Application would need to be submitted immediately (April 2026)
- Operating without a submitted application is a violation of RPAA s. 104

**Based on this analysis, PaySpyre likely does not need PSP registration under Models A and B.**

### 6.3 PCMLTFA Financing Entity Compliance Timeline

**This is the most likely mandatory obligation:**
- **Effective date:** April 1, 2025 (obligations came into force)
- **Compliance grace period:** FINTRAC emphasized education and outreach in the first year
- **Full compliance expected by:** April 1, 2026
- **What's required before launch:**
  1. Appoint a compliance officer
  2. Write AML policies and procedures
  3. Conduct a risk assessment
  4. Establish KYC/identity verification procedures for borrowers
  5. Set up suspicious transaction reporting protocols
  6. Implement ongoing monitoring

**Estimated time to implement:** 4–8 weeks with professional AML consultant assistance.

### 6.4 Provincial Compliance Timeline

- **BC & Alberta:** No license required at < 32% APR
- Ensure cost-of-credit disclosure forms comply with provincial requirements in each province before lending there
- **Estimated time:** 2–4 weeks to update loan agreement templates with provincial disclosure requirements

### 6.5 June 2026 Launch Assessment

**Realistic path to June 2026 launch:**

| Task | Who | Timeline | Must-Have? |
|---|---|---|---|
| PCMLTFA AML compliance program | AML consultant | 4–8 weeks | **Yes** |
| Loan agreement disclosure updates (BC, AB) | MMS Law | 3–4 weeks | **Yes** |
| Legal opinion: MSB registration required? | MMS Law | 2–3 weeks | **Yes** |
| Legal opinion: PSP registration required? | MMS Law | 2–3 weeks | **Yes** |
| MSB registration (if required) | FINTRAC | 5–6 months | **Only if required** |
| PSP registration (if required) | Bank of Canada | Up to 10 months | **Only if required** |
| Rotessa confirmation of RPAA registration | PaySpyre/Rotessa | 1 week | **Yes** |

**If MSB and PSP registration are NOT required (the likely outcome), the June 2026 launch is achievable** if PaySpyre acts now (April–May 2026) on the AML compliance program, legal opinions, and loan agreement updates.

**If either registration IS required, the June 2026 launch is in serious jeopardy.** PSP registration alone can take 10 months.

---

## 7. Recommended Regulatory Path for PaySpyre

### 7.1 Immediate Actions (Next 30 Days)

1. **Retain MMS Law for definitive legal opinions** on:
   - MSB registration requirement for each model (focus on Model A)
   - PSP registration requirement for each model
   - Whether PaySpyre's consumer dental loans constitute "financing for business purposes" under PCMLTFA s. 5(j)
   - Cost-of-credit disclosure requirements for BC and Alberta

2. **Contact Rotessa** to confirm their RPAA PSP registration status and whether PaySpyre's use of Rotessa for PAD processing is structured as Rotessa's client (not as an independent PSP)

3. **Begin AML compliance program development.** Even if PaySpyre turns out not to be a financing entity, having a voluntary AML program is a sign of good governance and will be necessary if the legal opinion goes the other way. Engage an AML compliance consultant.

### 7.2 Model Structuring Recommendations

Regardless of the final legal opinions, PaySpyre should take the following steps to maximize the strength of its non-MSB and non-PSP positions:

**Structuring for Non-MSB Status:**
- Ensure all revenue is characterized as lending revenue (interest, origination fees) — not payment processing fees
- Do not create a fee schedule for "fund transmission" or "payment processing"
- Contracts with clinics should describe PaySpyre as a "loan servicer" or "loan administrator," not a "payment processor"
- Marketing materials should describe PaySpyre as a "patient financing platform" — never a "payment solution" or "payment processing service"

**Structuring for Non-PSP Status:**
- Ensure Rotessa (as the PAD initiator) is the registered PSP, and PaySpyre is Rotessa's client/end-user
- Do not build or market independent payment infrastructure to clinics or patients
- Ensure the account/data stored for patients is for underwriting and loan management purposes, not for independent payment facilitation
- Do not charge separately for payment-related services

### 7.3 Document the Exemption Position

If PaySpyre determines (with legal advice) that it is not an MSB and not a PSP, it should maintain a compliance file documenting:
- The analysis performed
- The legal opinions obtained
- Evidence of revenue model (no payment fees)
- Evidence of marketing approach (lending platform, not payment service)
- Rotessa's PSP registration status

If FINTRAC or the Bank of Canada ever asks, PaySpyre needs to be able to demonstrate a good-faith analysis.

### 7.4 Priority Sequence

1. **Get legal opinions from MMS Law** (weeks 1–3)
2. **Implement AML compliance program** (weeks 1–8) — parallel track, assume it will be needed
3. **Confirm Rotessa's RPAA status** (week 1)
4. **Update loan agreement templates** for BC/AB disclosures (weeks 2–6)
5. **Apply for MSB/PSP registration only if legal opinions say it's required** (immediately upon receiving opinions, given timelines)
6. **Do not launch** without resolved MSB/PSP determinations and AML program in place

---

## 8. MMS Law Brief — Draft for Michael to Send to Vanessa Carle / Leanne Holman

---

**PRIVILEGED AND CONFIDENTIAL — SOLICITOR-CLIENT COMMUNICATION**

**To:** Vanessa Carle / Leanne Holman, MMS Law
**From:** Michael [Last Name], on behalf of PaySpyre Financial Inc.
**Re:** Urgent — Legal Opinions Required: MSB/PSP Registration Requirements
**Date:** April 2026

---

**Background**

PaySpyre Financial Inc. is a Vancouver-based financial technology company building a dental patient financing platform. We are targeting a functional launch for testing by end of June 2026 and require definitive legal opinions on our registration obligations before we can operate.

**Business Models**

PaySpyre operates (or plans to operate) under three models:

**Model A — Clinic-Funded Servicing (Current Model)**
The dental clinic provides the loan capital from its own funds. PaySpyre:
- Underwrites and approves the patient loan application
- Manages the loan through a software platform
- Collects payments from patients via Pre-Authorized Debit (PAD) using Rotessa as the PAD processor
- Remits collected funds to the clinic (net of PaySpyre's 50% interest share and origination fees)
- Earns: 50% of interest income, origination fees (charged to clinic), and marketplace lead fees

**Model B — Direct Lending (Planned, Loans Under $5K)**
- PaySpyre funds loans using its own capital or a credit facility
- Collects PAD repayments directly from borrowers
- Earns: 100% of interest income

**Model C — Portfolio Lending Facility (Planned)**
- PaySpyre lends money to dental clinics secured by the clinic's patient loan portfolio
- B2B secured lending; no direct consumer payment flows

Maximum interest rate across all consumer products: 29.9% APR.

**Specific Legal Questions Requiring Opinion**

**Question 1 — FINTRAC MSB Registration**

Under the *Proceeds of Crime (Money Laundering) and Terrorist Financing Act* (PCMLTFA), S.C. 2000, c 17, s. 5(h), is PaySpyre required to register as a Money Services Business with FINTRAC for any of Models A, B, or C?

Specifically:
- Does collecting PAD loan repayments from patients and remitting net proceeds to clinics (Model A) constitute "remitting or transmitting funds" under the PCMLTFA, or does it fall within the exception for entities that "solely receive payments on behalf of the payee to settle a debt"?
- Does operating as a direct lender collecting its own loan repayments (Model B) constitute MSB activity?
- What is the impact of FINTRAC's 2022 retraction of Policy Interpretation PI-7670 on PaySpyre's analysis?

**Question 2 — PCMLTFA Financing Entity**

Under the 2025 amendments to the PCMLTFA (s. 5(j)), are PaySpyre's patient consumer dental loans considered "financing of property for business purposes"? Does this trigger reporting entity obligations for Model A/B? What about Model C (B2B lending)?

If PaySpyre is a reporting entity as a financing entity:
- What compliance obligations must be in place before it begins operations?
- Is the April 1, 2026 deadline correct?

**Question 3 — RPAA PSP Registration**

Under the *Retail Payment Activities Act*, S.C. 2021, c. 23, is PaySpyre required to register as a Payment Service Provider with the Bank of Canada for any of Models A, B, or C?

Specifically:
- Do PaySpyre's payment activities (PAD collection, holding collected funds, remitting to clinics) constitute "payment functions" under RPAA s. 2, or are they "incidental to a non-payment business activity" (lending)?
- Does the Bank of Canada's published case scenario on lending companies (Company A, October 2024) support the conclusion that PaySpyre's payment collection is incidental to its lending service?
- Does using Rotessa as the PAD processor affect the analysis — is Rotessa the PSP, with PaySpyre merely instructing it?
- Does Model A (where funds flow patient → PaySpyre → clinic) create a holding/remittance function that would be characterized as a PSP function?

**Question 4 — Provincial Requirements**

- Do any of Models A, B, or C require licensing under BC's BPCPA (high-cost credit) at 29.9% APR?
- What cost-of-credit disclosure requirements apply in BC and Alberta for installment loans below the 32% threshold?
- Are there any other provincial licensing requirements applicable to PaySpyre's operations in BC and Alberta?

**Questions on Timelines and Compliance**

- If MSB registration is required, can PaySpyre operate in a limited testing capacity (no real-money transactions) while the application is pending?
- If PSP registration is required, can PaySpyre operate at all before registration is granted, given the 10-month processing time?
- What documentation should PaySpyre maintain to demonstrate its exemption from MSB/PSP registration if it takes the position that neither is required?

**Relevant Legislation and Guidance**

- *Proceeds of Crime (Money Laundering) and Terrorist Financing Act*, S.C. 2000, c. 17
- *Proceeds of Crime (Money Laundering) and Terrorist Financing Regulations*, SOR/2002-184
- FINTRAC Guidance: [Money Services Businesses](https://fintrac-canafe.canada.ca/msb-esm/msb-eng)
- FINTRAC Guidance: [Financing or Leasing Entities](https://fintrac-canafe.canada.ca/re-ed/lease-bail-eng)
- *Retail Payment Activities Act*, S.C. 2021, c. 23
- Bank of Canada: [Criteria for Registering PSPs](https://www.bankofcanada.ca/2024/10/criteria-for-registering-payment-service-providers/)
- Bank of Canada: [Case Scenarios About Lending](https://www.bankofcanada.ca/2024/10/case-scenarios-about-lending/)
- *Business Practices and Consumer Protection Act*, SBC 2004 c 2, Part 6.3
- Alberta Consumer Protection Act

**Output Required**

1. Written legal opinion on each of Questions 1–4 for each business model (A, B, C)
2. For any registration determined to be required: estimated timeline for approval, ongoing compliance obligations, and cost estimate for initial registration + first-year compliance
3. Recommended business structuring to minimize regulatory burden while maintaining operational viability
4. Form of compliance file documentation to support the non-registration position for obligations not triggered

**Timeline**

We require these opinions no later than **May 15, 2026** to allow time for any registration applications before our June 2026 launch target.

Please confirm your ability to take on this engagement and estimated fee.

---

## 9. Summary Reference Table

| Issue | Statute | PaySpyre's Likely Status | Action Required |
|---|---|---|---|
| MSB Registration | PCMLTFA s. 5(h) | **Not required** (all 3 models) | Obtain legal opinion; document exemption position |
| PCMLTFA Reporting Entity (Financing Entity) | PCMLTFA s. 5(j) + PCMLTFR | **Likely required** for Models A/B if consumer loans = "financing for business purposes" | Implement AML compliance program by April 1, 2026 |
| PSP Registration | RPAA s. 2 | **Likely not required** (Models A, B); Not required (Model C) | Confirm with Rotessa; obtain legal opinion |
| BC High-Cost Credit License | BPCPA Part 6.3 | **Not required** (APR < 32%) | Ensure rates stay below 32%; maintain disclosure compliance |
| Alberta Consumer Lending | AB Consumer Protection Act | **Not required** for standard installment loans below 32% | Ensure cost-of-credit disclosure in AB |
| Cost of Credit Disclosure | BC BPCPA + AB CPA | **Required** in all provinces | Update loan agreement templates |

---

## 10. Key Authorities Cited

- [FINTRAC — Money Services Businesses](https://fintrac-canafe.canada.ca/msb-esm/msb-eng) (PCMLTFA, S.C. 2000, c 17, s. 5(h))
- [FINTRAC — Financing or Leasing Entities](https://fintrac-canafe.canada.ca/re-ed/lease-bail-eng) (PCMLTFA, PCMLTFR SOR/2002-184, s. 1(2))
- [FINTRAC — Modernization and Changes for Reporting Entities](https://fintrac-canafe.canada.ca/businesses-entreprises/changes-changements-eng)
- [Bank of Canada — Criteria for Registering PSPs](https://www.bankofcanada.ca/2024/10/criteria-for-registering-payment-service-providers/)
- [Bank of Canada — Case Scenarios About Lending (critical)](https://www.bankofcanada.ca/2024/10/case-scenarios-about-lending/)
- [Bank of Canada — PSP Registry and Registration](https://www.bankofcanada.ca/core-functions/retail-payments-supervision/supervisory-framework-registration/)
- [Bank of Canada — Administrative Monetary Penalties](https://www.bankofcanada.ca/2025/03/administrative-monetary-penalties/)
- [*Retail Payment Activities Act*, S.C. 2021, c. 23, s. 2](https://laws-lois.justice.gc.ca/eng/acts/R-7.36/page-1.html)
- [Blakes — Changes to Regulations Under PCMLTFA](https://www.blakes.com/insights/changes-to-regulations-under-the-proceeds-of-crime/) (2022)
- [Osler — FINTRAC Retracts PI-7670](https://www.osler.com/en/insights/updates/fintrac-retracts-merchant-servicing-and-payment-processing-exemptions/) (2024)
- [BLG — Canada's RPAA: Do You Need to Register?](https://www.blg.com/en/insights/2024/10/canadas-retail-payment-activities-act-do-you-need-to-register) (2024)
- [Cassels — To Register or Not to Register Under RPAA](https://cassels.com/insights/to-register-or-not-to-register-the-bank-of-canadas-guidance-to-registration-under-the-rpaa/) (2024)
- [DLA Piper — RPAA Exceptions and Exclusions](https://www.dlapiper.com/insights/publications/2025/01/breaking-down-exceptions-and-exclusions-in-the-retail-payment-activities-act) (2025)
- [MLT Aikins — BC High-Cost Credit](https://www.mltaikins.com/insights/high-cost-credit-lending-in-british-columbia-what-lenders-need-to-know/) (2023)
- [Consumer Protection BC — High-Cost Credit Products](https://www.consumerprotectionbc.ca/consumer-help/consumer-help-high-cost_credit_products/)
- [Platino Consulting — AML for Financing Companies](https://www.platinoconsulting.com/blog/aml-compliance-requirements-for-canadian-financing-leasing-companies) (2026)
- [Canadian Lenders Association — Regulatory Calendar (Sept 2025)](https://www.canadianlenders.org/wp-content/uploads/2025/09/Canadian-Lenders-Association-Regulatory-Calendar-Blakes-April-September-2025-Update.pdf)
- FinCEN (U.S.) — [Definition of MSB: Debt Management Company](https://www.fincen.gov/resources/statutes-regulations/administrative-rulings/definition-money-services-business-debt) (instructive by analogy)

---

*Research conducted April 2026. This memorandum constitutes research findings only and is not a legal opinion. PaySpyre should retain qualified legal counsel (MMS Law) to provide definitive opinions before commencing operations.*
