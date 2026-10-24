# Demo storyboard: VENTHORA in nine scenes (about five minutes)

Everything on screen is the dashboard at `http://localhost:8001`, served on clean stores by `python scripts/demo_server.py`
(the shared stores carry every test cycle's history, which is not where a demo should start). Every amount that moves is
on the Airwallex sandbox; nothing in the narration is a model opinion, every outcome on screen comes from a rule.

## Before recording

```bash
python scripts/demo_server.py --reset
```

Open `http://localhost:8001`, press **Plan** once so the page is warm, then start the screen recorder (Windows Game Bar:
Win+Alt+R, or OBS). Keep `python scripts/demo_walkthrough.py` ready in a second terminal: it performs scenes 3 and 6
(the call-backs and the real execution) through the same endpoints the dashboard uses, and prints each scene's cue.
Its last run is in `docs/demo_run_log.json`; `docs/demo.gif` is a silent capture of the dashboard flow for the README.

## The live page: the builder guide's five elements on the real sandbox

`http://localhost:8001/live` is built for the video's working-demo part. The guide asks a demo to show the user's goal,
the agent's initial plan, new information, the revised decision and the resulting financial state; the page walks
through exactly those, one **Next** button at a time, on a fresh book of USD 30,000 ring-fenced from the live Airwallex
wallet with a USD 10,000 reserve floor (`data/scenarios/demo_live.json`, made by `scripts/make_demo_live.py`).

**The mandate form (step 1).** Before anything runs, the finance manager grants the mandate on one form, drawn from a
single specification (`MANDATES` in `src/venthora/live.py`) that also turns the submitted form into the book's policy,
budget, approvers and payees, so no row is decorative:

- the goal, the book and organisation, the allocation ring-fenced from the wallet, the reserve floor, the planning horizon,
  the budget period (monthly, quarterly or annual) and the receipt-confidence band for FX;
- **what VENTHORA may do on its own**, each with its amount: a single payout, one payee per reporting period, a first
  payment to a payee, outflow per 24 hours and per 7 days, currency conversion, daily conversion volume, deferring against
  an expected customer payment, spending within each budget line's autonomous share, accepting a simulated reserve-breach
  risk. **Unchecking a permission revokes it**: that action then always waits for an approver;
- **safeguards VENTHORA keeps** (unchecked = not enforced): open FX position per currency, hedge coverage, overdue days,
  late-interest exposure, the supplier-reminder level;
- **always enforced** (cannot be revoked): default late interest, the three-way-match tolerance, the call-back and
  cooling period after a bank change, no invoice or instalment paid twice, evidence only when quoted verbatim, the book
  stopped when a reconciliation does not add up;
- the budget lines (amount, autonomous share, alert, approver at alert, unlimited only when signed), whether a payment
  outside every line waits for an approver;
- the approvers (name and role: only they may approve this book's plans, never the requester) and the approval matrix;
- the payees VENTHORA may pay (unchecked = never paid, escalated), with status, registered mailbox and call-back number;
- the reporting cut-off (day, time, timezone) and reporting currency;
- every request received, by channel: payroll system, accounts payable system, the AP inbox, customer email.

Afterwards step 1 shows the mandate as granted: each permission *granted* or *revoked*, each safeguard *enforced* or
*off*, with its amount. **Change the mandate** starts a new run. Tests (`tests/test_live.py`) prove the effect: revoking
currency conversion sends the conversion to a person; an unchecked payee is never paid; an approver who is not on the
book's list is refused.

| Step on the page | What happens, for real | What the viewer sees |
|---|---|---|
| 1 · Goal and mandate | the mandate is applied and the book opens on the live wallet | the form above, then the granted mandate |
| 2 · Initial plan | the requests received so far are read and verified; Theorem plans, ProofCore proves | `APPROVED`: payroll, shipping and the EU supplier paid, the duplicate rejected, Lumen escalated (quoted), SaaS deferred on Acme's receipt |
| 3 · New information | Acme's later email is read and calibrated | the email quoted; confidence 90 % → 70 %, expected 24 h → 144 h |
| 4 · Revised decision | only the touched decisions are reconsidered | `REQUIRES_APPROVAL`: FX limit USD 10,000 → 5,000; SaaS DEFER → ESCALATE (row highlighted), five decisions locked |
| 3 + 4 · Payment received | a new USD 10,000 deposit on Airwallex, counted only once SETTLED | the deposit id and status; SaaS ESCALATE → PAY; the SaaS budget line crosses its 80 % alert |
| 5 · Resulting state | execution on the sandbox, reconciliation to the cent | two conversions and four transfers PAID with Airwallex ids; wallet before / after; the book's ledger (EUR and GBP cash exactly 0); reconciliation PASS |

The right-hand panel polls the Airwallex wallet every three seconds (balance and change since the run started), the
book's managed budget against the floor, then its ledger; the event log lists every step with its time in milliseconds,
and every step is also written to the audit chain. A refresh keeps the run (the session id is in the URL). Each run
creates one new deposit and moves real sandbox money, so restart the demo server with `--reset` between takes.

Measured on the real sandbox, 24 Oct 2026: start 0.8 s, plan 1.0 s, news 2 ms, revise 109 ms, deposit to SETTLED 2.4 s,
execution with polling to PAID and reconciliation 9.1 s.

## Scenes

| # | Scene (≈ seconds) | What to do on screen | Narration outline | What the viewer sees |
|---|---|---|---|---|
| 1 | **The plan, proven** (40) | Plan & proof: book *Kit 01 – Initial plan*, press **Plan**. Point at the Decision tile, the decisions table, the stage list, the counterfactual column. | Five obligations, two receivables, a reserve floor. The agent pays what it can prove safe, defers the cheapest bill, escalates the contractor whose email changed bank details. Every stage is a check, not a narrative; the counterfactuals show what breaks if we pay more. | `APPROVED`; PAY / CONVERT_AND_PAY / REJECT / ESCALATE / DEFER; 12 stages PASS or WARN; counterfactuals FAIL in red with the breach probability. |
| 2 | **An email that wants money** (30) | Submit a case: suite **anomalies**, case **N16**, Submit. | A supplier "moved banks", urgent, pay today. The sender is not the registered mailbox. Quarantined, with the verbatim quotes that decided it. The model never saw this; the registry did. | `QUARANTINED · unknown_sender · bank change`, round trip in ms, ground truth PASS. |
| 3 | **The call-back nobody can skip** (40) | Payees · lifecycle: on **ben-lumen-design** press **Bank change** (any fingerprint), then **Call-back** with a wrong number, then with the registered number shown in the row. | New bank details move the payee to CHANGED: no plan and no approval can pay it. A call-back to a number taken from the email is refused by code. The call-back to the registered number verifies, and a cooling period follows during which every payment still needs a person. | Status CHANGED → the refusal message → VERIFIED with *Cooling until*. |
| 4 | **The plan is the mandate** (25) | Budget · mandate: read the line table (plan, realized, paying now, available, status), the utilisation bars. | The organisation's monthly plan is the agent's mandate per line. Inside the auto share it acts alone; at the alert threshold it warns; above the mandate it stops and raises a budget request with the exact shortfall. | Five lines with `auto`; utilisation 89 / 79 / 62 %. |
| 5 | **How much, and how many signatures** (30) | Submit a case: suite **limits**, case **V03**, Submit; then suite **approvals**, case **Q03**, Submit. | One cent above the single-payment limit stops the plan for a person. At the two-person threshold the plan itself says what it will take: two approvals from different people, in named roles, never the requester. | `REQUIRES_APPROVAL · single_payment_above_limit`; then the Decision tile: *2 approvals from different people (cfo, finance_manager); tier two*. |
| 6 | **Money moves, and the books agree to the cent** (45) | Run `python scripts/demo_walkthrough.py` (scene 6 executes); then Executions: open the new COMPLETED run; Books · ledger: the new book. | A fresh book, USD 1,000 to a verified payee, inside every control: the agent executes alone on the sandbox. Claims before money moves, a single-use quote where a conversion is needed, the transfer polled to PAID, and then the wallet's movement against every book's cash lines: zero unexplained. | Execution COMPLETED; reconciliation discrepancies `{}`; ledger row: cash 18,997.00, expense 1,000.00, fees 3.00. |
| 7 | **A supplier says the money never arrived** (30) | Submit a case: kind **incident facts**, paste `{"status":"CANCELLED","failure_type":"BENEFICIARY_BANK_RETURNED","hours_in_flight":6}`, Submit; then the same with `INVALID_BENEFICIARY_DETAILS`. | Kit 03. The rail's typed failure decides, not the supplier's email. Returned by the beneficiary bank: replace once, new request id, second fee budgeted, under the duplicate lock. Wrong account details: escalate, because paying again pays the wrong account again. | `REPLACE` with its reason; `ESCALATE` with its reason. |
| 8 | **What lateness costs** (30) | Submit a case: suite **aging**, case **G02**, Submit; then **D03**. | An invoice 45 days overdue that the book cannot fund: bucketed 31–60, interest accrued at 8 % a year, and the plan stops for a person instead of deferring quietly. A supplier's final notice, read from the inbox with its quote, raises the book's dunning level. | `REQUIRES_APPROVAL · overdue_obligation_unpaid`, accrued USD 98.63; `VERIFIED`, dunning level 3, flag `dunning_notice_L3`. |
| 9 | **Whose book, whose suppliers, whose signature** (30) | Tenants · books: scroll the registry; point at *tenant-real-\** (one payee, one approver, its own single-payment limit) and at the demo book under the sandbox lab with its cash. | One wallet, several tenants. Each book may pay only its payees, is signed only by its people, runs under its own policy and keeps its own ledger. An unregistered book cannot spend at all. Nothing on this page was decided by a model, and all of it ran on the real sandbox. | Registry rows; the demo book's executions and cash. |

## Closing line (10 s)

Twelve ground-truth suites, 322 hand-derived cases reproduced in-process, through the endpoints and through this form;
the policy proven in two engines on 10,618 inputs; eighteen cycles on the real sandbox. The agent proposes, the engines
verify, Airwallex executes.

## Notes for the recording

- The walkthrough script is idempotent for the narration but not for the stores: each run creates one new demo book and
  one real USD 1,000 transfer. Use `--reset` on the demo server to start clean.
- Scene 3 leaves Lumen Design VERIFIED with a cooling period; a reset restores ACTIVE.
- If the sandbox settlement simulation is slow, scene 6 may show IN_FLIGHT for a few seconds; the Executions list refreshes,
  and a later `resume` settles it. The payee stays VERIFIED until a PAID payout, by design.
- Keep the browser at 100 % zoom and at least 1,500 px wide so the stage table and the counterfactual column sit side by side.
