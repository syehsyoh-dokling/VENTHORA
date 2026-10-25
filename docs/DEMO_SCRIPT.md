# Demo voice-over: VENTHORA alpha, about four and three-quarter minutes

A product walkthrough for people who would use VENTHORA, built around what the organizers ask a demo to show:
**the user's goal, the agent's initial plan, new information, the revised decision, and the resulting financial state**,
with amounts, limits, reserves, validation, duplicate protection and state changes controlled by code. The video must be
**under five minutes**; this cut runs about 4:40 including the real waits on Airwallex.

**Before recording:** `python scripts/demo_server.py --reset`, browser at 100 % zoom and at least 1,500 px wide.
Each operations run moves real sandbox money, so reset between takes. Figures that follow live sandbox rates are said in
round numbers. Stage directions are in brackets and are not read aloud.

The cut has three parts, and each one shows strengths the others do not:

| Part | Page | Strengths it shows |
|---|---|---|
| Opening | `/flow` | mandate lanes, evidence, two policy engines, AI suggestions judged like plans, approvals, Airwallex, incident handling, reconciliation |
| Operations | `/live` | the five demo elements on real money, counterfactual proof, only affected decisions change, finality, learned fees |
| Control center | `/` | audit trail, budget against plan, a ledger per book and team, ground truth |

---

## Part 1 · Opening: how it works (0:00 – 1:20)

**[Open `http://localhost:8001/flow?speed=1.5` and press F for full screen.]** At 1.5x each step lasts about four
seconds and the page moves on by itself; read one line per step as its box appears. To set the pace yourself, press
Space to pause and click anywhere for the next step.

| Step | On screen | Voice-over |
|---|---|---|
| 1 | Mandate assigned | It starts with your mandate: the goal, the money, the reserve. |
| 2 | Green lane | What you check, VENTHORA does on its own, within your limit. |
| 3 | Amber lane | What you uncheck comes back to you. Big payments need two people. |
| 4 | Blue lane | Some rules never switch off: no double payments, no crossing the reserve. |
| 5 | Requests arrive | Requests arrive from everywhere: invoices, payroll, emails, forms. |
| 6 | Read and verify | It reads each one, and keeps only what it can quote. |
| 7 | Forecast | It forecasts the money coming in, and how sure it can be. |
| 8 | Plan | Then it plans the next seventy-two hours, in milliseconds. |
| 9 | Prove | Two independent engines check that plan against your mandate. Both must agree. |
| 10 | Verdict | Every plan, even an AI's suggestion, gets a verdict and a reason. |
| 11 | Blocked | Blocked means never paid, like an invoice sent twice. |
| 12 | Approval | Anything outside your mandate waits for you, like changed bank details. |
| 13 | Airwallex | Approved payments go to Airwallex: quote, convert, pay, confirm. |
| 14 | Approval to Airwallex | Once you approve, it takes the same path, sent exactly once. |
| 15 | Payout failed | If a payout fails, it waits, replaces it once, or calls you. |
| 16 | Re-plan loop | Money coming in counts only when Airwallex confirms it. Then it re-plans. |
| 17 | Reconcile | Every movement is matched to your ledger, to the cent. |
| 18 | Control center | And your CFO sees all of it in the Control center. |
| 19 | End card | This is VENTHORA alpha, live on Airwallex. Let's see it run. |

## Part 2 · Operations, live on Airwallex (1:20 – 4:00)

**[Switch to `http://localhost:8001/live`. Click Next right after the last sentence of each block; the time codes include
the waits for Airwallex, about 4 s for the incoming payment and about 10 s for the execution.]**

**[1:20 – 1:45 · Step 1, the mandate form, scroll slowly]**
Here it is, live. You write the goal and set every limit yourself. Anything you uncheck comes back to you. It also
watches currency exposure, overdue invoices and late interest, and payments from twenty-five thousand dollars need two
different approvers.

**[1:45 – 1:52 · Requests received, then click Save the mandate and start]**
Here's what's waiting. Save the mandate, and VENTHORA opens the book on your Airwallex wallet.

**[1:52 – 2:28 · Step 2, the plan; open Checks and alternatives at the end]**
In under a second it has a plan. Payroll, the UK freight bill and the German supplier get paid. An invoice sent a
second time is rejected as a duplicate, and a contractor's new bank details come to you, quoted word for word. The
subscription waits for a customer payment due tomorrow. And it proves why every other option is worse: paying
everything now would cross your reserve by about two thousand dollars.

**[2:28 – 2:40 · Step 3, new information]**
Then the customer writes: five days late. Confidence in that money drops from ninety to seventy percent.

**[2:40 – 2:57 · Step 4, the updated plan; point at the highlighted row]**
Only what the news affects changes. It halves how much it may convert on its own, and the subscription now comes to
you. Everything else stays as planned.

**[2:57 – 3:15 · Incoming payment confirmed]**
When the money lands, it counts only once Airwallex confirms it, then re-plans on its own. The subscription is paid,
with an alert that its budget line is almost used.

**[3:15 – 4:00 · Step 5, the result; point at the table, then the wallet panel]**
Then it pays, on Airwallex. Both conversions settle, and all four payments come back paid. Every fee is what Airwallex
really charged, and VENTHORA learns it for the next plan. The wallet moved about twenty-one thousand dollars, matching
the ledger to the cent, and your reserve was never touched. The duplicate and the changed bank account were never paid.

## Part 3 · Control center (4:00 – 4:30)

**[4:00 – 4:15 · Click Control center; scroll through Plan & proof, Budget and Books · ledger]**
Everything stays on the record: an audit trail that can't be altered, spending against each budget line, and a separate
ledger for every book and team, so one team's money is never spent by another.

**[4:15 – 4:30 · Ground-truth checks, then Resilience test matrix]**
And you can test it yourself: three hundred twenty-three checked cases, and a hundred and thirty-six stress scenarios,
all passed.

## Close (4:30 – 4:40)

**[Scroll back to the top]**
VENTHORA alpha. Set your rules, and let it run your payments on Airwallex.

---

**Timing.** The opening is fixed by the animation at about 1:20. The rest is about 360 spoken words, about 2:25 at 150
words a minute, plus clicks, pointing and about 15 seconds of waits on Airwallex. Total about 4:40, with some slack. If a take runs long, shorten
the Control center part to the ground-truth sentence first.

**What each claim rests on.** The counterfactual figure comes from the plan's "What if we pay every verified obligation
now?" check on the demo book (USD 1,951.63 over the floor on 25 Oct 2026; it moves with live rates). The AI suggestion
line refers to the challenger gate (`POST /v1/plans/challenge`), which blocked a local model's attempt to pay payroll
twice on the real sandbox. The incident line refers to the Incident Commander's WAIT, REPLACE and ESCALATE decisions.
Learned fees are the per-rail fees the executor records after each payout. Details are in [INNOVATION.md](INNOVATION.md).
