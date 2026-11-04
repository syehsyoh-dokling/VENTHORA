# Requirements review: what the organisers ask, what VENTHORA has

Reviewed on 4 Nov 2026 against the organisers' guide (<https://airwallexdev.com/guide>, items numbered as they appear)
and the hackathon page (<https://airwallex.hackerearth.com/>). Submission is due **13 Nov 2026**.

Legend: ✅ met · ⚠ met with a caveat or only partly · ❌ missing · n/a not our track.

## 1. What to build (guide 1–12)

| # | The guide asks | VENTHORA | Where |
|---|---|---|---|
| 1 | A finance task someone repeats, and the decision inside it | ✅ paying the organisation's obligations every day with limited cash | the whole app |
| 2 | Choose differently when a balance, deadline, evidence or risk changes | ✅ new email or deposit → revised decision, only the affected ones | Autopilot Console steps 3–4 |
| 3 | Move money; say which actions need a person | ✅ | Delegation of Authority, Executions & incidents, README approval table |
| 4 | Amounts, limits, reserves, validation, duplicates, state transitions in code | ✅ | ProofCore, Treasury limits, duplicate lock |
| 5 | The model reads text, compares evidence, proposes, explains | ✅ (rules only when no model is set) | Intake reader, Agent (AI & Model) |
| 6 | Typed tools, not unrestricted credentials | ✅ scoped API keys; mandate, DoA, SOP never by key | App Integration · API access |
| 7 | Bind each approval to amount, currency, counterparty, terms, evidence | ✅ plan approvals carry a terms digest; payroll approvals bound to amount, currency, account and charge code | executor; Payroll changes (fixed 4 Nov) |
| 8 | Sandbox-only simulation behind one interface | ✅ | Airwallex gateway |
| 9 | End with a financial action or an escalation | ✅ | every run |
| 10 | A rule with one answer belongs in code | ✅ | engines, policy |
| 11 | Pick a starter kit | ✅ Kit 01 and Kit 03 | – |
| 12 | Own amounts and rates | ✅ | scenario data |

## 2. The five demo elements (guide 13–17)

| # | Show | VENTHORA | Caveat |
|---|---|---|---|
| 13 | The user's goal | ✅ Autopilot Console step 1 | ⚠ the Console sits at the bottom of System Setting |
| 14 | The agent's initial plan | ✅ step 2 | ⚠ the plan is not on the main menu |
| 15 | The new information | ✅ step 3 (quoted, verified email) | – |
| 16 | The revised decision | ✅ step 4 (changed decisions, the rest locked) | – |
| 17 | The resulting balance, payment or status | ✅ step 5 (paid, reconciled to the cent) | ❌ **the video is not recorded yet** |

## 3. Sandbox rules (guide 18–24)

| # | Rule | VENTHORA |
|---|---|---|
| 18–20 | Sandbox only, synthetic data, sandbox payment details | ✅ refuses production; all data fictional |
| 21–22 | Kit 9, kits 11–12 | n/a |
| 23 | Do not commit credentials | ✅ secret scan before every commit |
| 24 | Simulation behind one interface | ✅ |

## 4. Starter Kit 01: Adaptive Treasury Controller (guide 25–40)

| # | Requirement | VENTHORA |
|---|---|---|
| 25–27 | Cannot fund everything; 5 obligations, 3 currencies, 72 h; part paid after the reserve floor | ✅ |
| 28 | Fund the shipping invoice that stops operations | ✅ priority 10; Payment Priority SOP |
| 29 | Convert the minimum for the time-sensitive supplier | ✅ shortfall plus fee |
| 30 | Defer the cheapest | ✅ only on a reliable receipt |
| 31 | Escalate a policy exception | ✅ bank change → escalated |
| 32 | After a deposit, recalculate only what it affects | ✅ locked decisions, diff |
| 33–35 | Conversion limit tied to forecast confidence; a contradicting email lowers it and needs a person; thresholds in code | ✅ Treasury limits: full and zero confidence |
| 36 | The end-to-end flow on the sandbox | ✅ Autopilot Console; `scripts/real_suite.py` |
| 37 | EUR 12.85 per SWIFT payout in the conversion | ✅ ↔ the observed sandbox fee overrides it (EUR 13.37) |
| 38–40 | No `x-api-version` on FX; single-use quote; booking over REST | ✅ |

## 5. Starter Kit 03: Payment Ops Incident Commander (guide 41–50)

| # | Requirement | VENTHORA |
|---|---|---|
| 41–43 | Delayed transfer, never paid twice; wait, replace or escalate; finality and locks in code | ✅ |
| 44–46 | CANCELLED is a status; SENT before PAID; FAILED with a failure type | ✅ |
| 47–49 | New `request_id` for the replacement; two SWIFT fees; the original is recorded too | ✅ |
| 50 | The flow on the sandbox | ✅ `venthora-sandbox --scenario kit03_incident` |

## 6. Endpoints (guide 51–68)

✅ all of Kit 01 and Kit 03 (base URL, login, major units, UUID request ids, balances, rates, quotes, conversions,
beneficiary schema and create, transfers, simulated deposit and transitions, transfer list). See HACKATHON_COMPLIANCE.md.

## 7. Safety and human in the loop (guide 69–78)

| # | Expectation | VENTHORA |
|---|---|---|
| 69 | Say which actions need a person | ✅ ⚠ the README approval table predates Pending payment, Payroll changes and pay-on-arrival |
| 70 | Approvals bound to what was shown | ✅ |
| 71 | No owner keys to the agent | ✅ |
| 72 | Escalate exceptions and contradicted forecasts | ✅ |
| 73 | One customer's funds never for another's (kits 5–8) | ✅ bonus: books and segregation |
| 74 | A timeout is unconfirmed; check before retrying | ✅ in-flight and duplicate locks |
| 75–78 | Card data, merchants, kits 9–10 | n/a |

## 8. Submission (hackathon page)

| Requirement | Status | Action |
|---|---|---|
| A working demo | ✅ locally and on the sandbox | Keep the demo data complete (section 9) |
| A video walkthrough under five minutes | ❌ | Record it after the menu work; storyboard and voice-over exist (DEMO_STORYBOARD.md, DEMO_VOICEOVER_3MIN.md) but name old menus |
| A link to the repository with setup instructions | ⚠ the repository is **private** | Make it public, or give the judges access, before 13 Nov; README quick start exists |

## 9. Gaps inside VENTHORA that weaken the demo

| # | Gap | Effect | Fix |
|---|---|---|---|
| V1 | Pay on arrival is built but **off** | A verified invoice waits instead of being paid in real time | Switch it on in DoA → Treasury limits before recording (sandbox) |
| V2 | The demo employees have **no own bank account** | Every payroll line shows NOT PAID; staff claims are never reimbursed | Register sandbox accounts for the demo staff |
| V3 | The plan is not on the main menu | Judges do not see the decision engine first | "Agent plan" menu (section 10) |
| V4 | Reporting overlaps (Plan & proof, Run reports, Payees, Payment reports and Reporting periods) | Duplicate pages, confusing tour | Merge as proposed |
| V5 | Docs name old menus (Operations run, Payment → Purchase orders, Payroll register) | The video script and the README disagree with the app | Update README, DEMO_SCRIPT, STORYBOARD, VOICEOVER |
| V6 | Kits 5–8 (connected accounts, `x-on-behalf-of`) | n/a; Kit 6 payroll would be a second track | Not before 13 Nov |

## 10. Proposed menus

| Menu | What | Why it fits |
|---|---|---|
| **Agent plan** (main menu, under Dashboard) | Tabs: Today's plan (real queue, live balance, each decision and its reason); Proof & what-if; Autopilot Console | Guide 2, 5 and 13–17 are the heart of the judging; the decision engine should be the first thing a judge opens, on real data, not only in a scenario |
| **Hackathon evidence** (Platform Setup) | Every guide item with its status and a link to the screen or test that proves it; Checks & resilience; the 136-scenario matrix; Run reports | One place where nothing can be missed; the compliance matrix becomes live instead of a document |
| **Demo readiness** (extend Setup Checklist) | Bank connected, webhook tunnel up, sandbox balance, employee accounts, pay on arrival, repository visibility, video | Demo Day runs live; a missing account or a closed tunnel shows before the judges see it |
| **Reporting, merged** | 7 category reports, Period close (Payment reports + Reporting periods), Spending, Books & ledger | No page twice |
