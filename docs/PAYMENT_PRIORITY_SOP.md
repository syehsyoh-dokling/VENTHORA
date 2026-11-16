# SOP: what VENTHORA pays first

**Status:** draft for the finance team (FCO).
**Scope:** every outgoing payment VENTHORA makes on Airwallex: payroll, fringe benefits, suppliers and purchase orders,
consultants, office and operations, travel, cash advances and petty cash, other direct costs.
**Owner:** the holder of the Delegation of Authority (FCO Authorize → Delegation of Authority).

## 1. Purpose

The organisation's money is not always enough to pay everything that falls due at once. This SOP fixes, in advance and in
writing, three things:

- which payments may be made at all;
- which come first when cash is short;
- what the agent does with the rest.

VENTHORA applies it on its own. A person is asked only when a payment falls outside it (management by exception).

## 2. Principles

1. **Nothing unsafe is paid, whatever its priority.** Priority decides the order among payments that are allowed. It never
   makes a payment allowed.
2. **Wages first.** International labour standards protect the regular payment of wages. They rank workers' wage claims
   ahead of other creditors when an employer is insolvent (ILO C95, C173).
3. **Statutory before commercial.** Withheld payroll taxes and social security belong to the state and the employee;
   failing to remit them carries penalties and, in many jurisdictions, personal liability.
4. **Keep operations running.** Next comes a supplier whose non-payment stops operations, then the others.
5. **A promise of money is not money.** An expected receipt is relied on only when it is reliable and lands inside the
   planning horizon.
6. **Real time.** A payment that passes every check is paid when it arrives. The horizon is a guardrail that keeps cash
   for what falls due next, not a schedule.

## 3. Gate 1: may it be paid at all? (fail-closed)

Every payment must pass all of these, in any order. One failure holds the payment; it is never paid "because it is
important".

| # | Check | Standard behind it | Where in VENTHORA |
|---|---|---|---|
| G1 | The request is genuine: from a registered mailbox or a known person, evidence quoted from the document | BEC controls: email alone is not trusted | Intake checks; Pending payment |
| G2 | The payee is registered, and its bank account is verified by a **call-back on the number of record** (never the number in the message) | FBI/IC3 BEC guidance as relayed by banks (J.P. Morgan BEC toolkit) | Payees; Executions → Payee call-backs |
| G3 | A changed bank account waits a **cooling period** before the first payment | Common practice: a hold of 24–48 h. VENTHORA uses 72 h, which is stricter | Treasury limits: `payee_cooling_hours` |
| G4 | A supplier invoice draws on an **open PO with room**, not expired; the PO is locked on its number and value | Three-way match, procurement control | Intake → Purchase order; Commitment |
| G5 | Not a duplicate: never paid twice | AP control | Duplicate check (invoice fingerprint) |
| G6 | Documents required by the document rules are present | Internal control | Document validation |
| G7 | Inside the **Delegation of Authority** for its category; otherwise one person decides. The person who asked or changed it never approves it | Segregation of duties, dual control (AFP) | DoA; Executions & incidents |
| G8 | Inside the **budget line** (or a signed bypass) | Budget control | Budget allocation → Budget lines |

## 4. Gate 2: protective limits on the cash position

| # | Limit | Where in VENTHORA |
|---|---|---|
| L1 | The **reserve floor** is never crossed, plus an FX stress buffer | Treasury limits: reserve floor |
| L2 | The chance of breaching the floor over the horizon (P(breach)) stays under the threshold; above it a person approves | Treasury limits; the simulation in every plan |
| L3 | Per-payment, per-payee, daily outflow, open FX position and daily conversion caps | Treasury limits and controls |
| L4 | An expected receipt counts only if it is reliable (confidence ≥ threshold) **and** lands inside the horizon | Planning horizon; full / zero FX confidence |

## 5. Priority tiers (when cash cannot cover everything)

The order is strict: a higher tier is paid before a lower one. Inside a tier, the earliest due date goes first.

| Tier | Priority (1–10) | What | Why | VENTHORA today |
|---|---|---|---|---|
| **1 Wages** | 10 | Net pay of employees (payroll) | ILO C95/C173; hardship; legal exposure | DoA 01 Personnel = 10 ✅ |
| **2 Statutory** | 10 | Withheld payroll tax, social security, other taxes due | Penalties, personal liability of officers | Payment Priority SOP "Taxes and social security" = 10; account 2200 ✅ |
| **3 Operations-critical** | 9–10 | A sole-source supplier whose non-payment halts operations (shipping, utilities, critical services); fringe benefits (insurance cover lapses) | Continuity | Payee priority 9–10; DoA 02 Fringe = 9 ✅ |
| **4 Contracted and financing** | 7–8 | Contracted services and consultants; debt service under covenant (a missed payment can trigger default) | Contract and covenant risk | DoA 03 Consultants ✅; "Loan and debt service", account 2300 ✅ |
| **5 Normal** | 5–6 | Suppliers, office and operations, travel, cash advances, petty cash, other direct costs | Due date and cost of lateness | DoA 04–07 = 5–6 ✅ |
| **6 Deferrable** | 1–2 | Subscriptions and anything cheapest to defer | Least harm when late | Payee priority 1–2 ✅ |

**Within a tier**, the tie-breakers are:
1. the earliest due date;
2. the higher cost of being late. Under EU Directive 2011/7/EU, late B2B payment carries statutory interest of the
   reference rate plus at least 8 points. Terms are 60 days at most between businesses, and 30 days when none are agreed.

VENTHORA ranks by priority, then due date (Theorem engine). The cost of lateness moves a payment one step up inside its level (section 9).

## 6. What the agent does with each payment

| Decision | When |
|---|---|
| **PAY** | Passes Gate 1 and Gate 2 and fits after the higher tiers |
| **CONVERT** (then pay) | The payment is in another currency: buy only the shortfall plus its fee, at a locked quote |
| **DEFER** | It does not fit now, **and** a reliable receipt lands before it falls due |
| **ESCALATE** | It does not fit and no reliable receipt covers it in time, or it fails Gate 1 / a policy limit |

Every decision carries its reason, and the plan is proved before money moves (ProofCore). An escalated payment appears in
Executions & incidents or Pending payment with what to fix.

## 7. Timing

- **Real time.** A request that becomes verified and passes every check is paid then. A sweep every minute retries what
  waits on cash, a quote or a due date.
- **The horizon is a guardrail.** The default planning horizon is **72 h**. That is the case of the hackathon's Starter
  Kit 01: five obligations, three currencies, due within 72 h. The horizon is set per organisation, from 1 to 720 h.
  For governments under fiscal stress the IMF suggests forecasting 4–6 weeks ahead; organisations with monthly payroll
  may choose a horizon that reaches the next pay day.
- **Payroll** is paid on the pay day of DoA 01. A salary that is the same as the last one paid goes on its own; only a
  change waits for one person.

## 8. Who decides what

| Situation | Who | Where |
|---|---|---|
| Everything inside this SOP | VENTHORA, on its own | – |
| A changed salary, a first salary, a one-period adjustment | One person, not the one who changed it | Executions & incidents → Payroll changes |
| A bank account to verify | A person who calls the number of record | Executions → Payee call-backs; Payees |
| A request a check stopped | A person corrects it; Pay re-runs the checks | Pending payment |
| A change to this SOP's limits | The holder of the mandate authority; weakening a critical control needs an acknowledged warning | DoA → Treasury limits and controls |

## 9. Where each rule lives in VENTHORA

| Rule | Where |
|---|---|
| The levels and order of the categories (section 5) | FCO Authorize → Standard Payment Priority Policy |
| Taxes and social security at priority 10, with wages | Payment Priority SOP: the first two High categories share 10. Charge them to account **2200** (Taxes and social security payable) |
| Loan and debt service | Payment Priority SOP, category "Loan and debt service". Charge them to account **2300** (Loan and debt service) |
| The cost of being late as a tie-breaker | Payment Priority SOP: an overdue payment, a second reminder or final notice, or late interest of 8% a year or more moves one step up inside its level |
| Real-time payment on arrival (section 7) | Delegation of Authority → Treasury limits and controls → "Pay a verified request the moment it passes every check". Off until the mandate authority switches it on; a sweep every minute retries what waited |
| Gate 1 and Gate 2 | Intake, Payees, Commitment, Document validation, Delegation of Authority, Budget allocation, Treasury limits |

## References

- ILO, Protection of Wages Convention, 1949 (No. 95), and Protection of Workers' Claims (Employer's Insolvency)
  Convention, 1992 (No. 173):
  - <https://www.ilo.org/international-labour-standards/international-labour-standards-wages>
  - <https://jus.uio.no/english/services/library/treaties/02/2-09/ilo_insolvency.html>
- Directive 2011/7/EU on combating late payment in commercial transactions: <https://www.legislation.gov.uk/eudr/2011/7/article/3>
- IMF, Government Cash Management under Fiscal Stress (2020): <https://blog-pfm.imf.org/en/pfmblog/2020/05/government-cash-management-under-fiscal-stress>
- IMF, Preparing Public Financial Management Systems for Emergency Response (2020), on payment prioritisation through a
  centralised treasury: <https://imf.org/-/media/Files/Publications/covid19-special-notes/special-series-on-covid19-preparing-public-financial-management-systems-for-emergency-response.ashx>
- J.P. Morgan, Business Email Compromise toolkit (call-back on the number of record): <https://www.jpmorgan.com/content/dam/jpm/commercial-banking/insights/cybersecurity/774340_Fraud_BEC_Toolkit_FINAL_New2_ADA_02192021.pdf>
- AFP, payments fraud and controls (dual control, segregation of duties): <https://afponline.org/training-resources/resources/articles/Details/4-ways-to-reduce-payments-fraud>
- FM Magazine, A playbook to manage cash in a crisis (2020; payroll first, then key suppliers): <https://www.fm-magazine.com/news/2020/apr/how-to-manage-cash-during-coronavirus-crisis/>
- Airwallex hackathon guide, Starter Kit 01 (requirements #42–49), as recorded in [HACKATHON_COMPLIANCE.md](HACKATHON_COMPLIANCE.md).

Some figures above come from secondary sources: the 24–48 h hold and the call-back rule (bank guidance), and the
statutory-tax tier (practitioner guidance). Check them against the primary texts and the organisation's own
jurisdiction before adopting this SOP formally.
