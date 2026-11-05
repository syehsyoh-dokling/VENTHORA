# Dashboard voice-over, timed to the recorded take (2:24)

The dashboard part of the video is recorded by `scripts/record_demo.py`: Microsoft Edge at 1920x1080 drives the
operations page on the real Airwallex sandbox, then the Control center, with a visible cursor and the part being described
outlined. The take used here was recorded on 25 Oct 2026 on a clean store and passed every check the script makes:
the incoming payment SETTLED, both conversions SETTLED, all four transfers PAID, and the take is under three minutes.

The recording has no sound. Read each line when its time code appears; the times come from the take's own cue log
(`demo_video/cues.json`), so they match the picture. The pauses during the two waits on Airwallex are intentional.
About 310 words in 2:24, about 130 words a minute, so there is room to breathe.

Every figure below is on screen in this take. Figures that follow live sandbox rates are said in round numbers.

| Time | On screen | Voice-over |
|---|---|---|
| 0:00 | Goal, then the permissions with their limits, then the safeguards | Your goal: pay every bill due in seventy-two hours, keeping at least ten thousand dollars. Every permission has a limit, and safeguards watch currency exposure, overdue invoices and late interest. |
| 0:13 | Budget period, the approval matrix, requests received | Budgets run monthly, quarterly or yearly. Payments from twenty-five thousand dollars need two different approvers, never the requester. |
| 0:22 | Save the mandate and start, then the initial plan | Save the mandate, and the plan arrives in a tenth of a second. Payroll, freight and the German supplier are paid. A duplicate invoice is rejected, and changed bank details go to a person. |
| 0:36 | Checks and alternatives | The subscription waits for a customer payment. Every plan shows its checks and alternatives: paying everything now would breach the reserve. |
| 0:46 | New information: the customer's email, quoted | Then the customer emails: five days late. VENTHORA quotes it, and confidence drops from ninety to seventy percent. |
| 0:55 | Revised decision, then the changed row | Only affected decisions change. The conversion limit halves to five thousand dollars, so approval is required, and the subscription is escalated. The rest stays locked. |
| 1:06 | Waiting for Airwallex, then SETTLED and the re-plan | Now the customer's payment arrives. VENTHORA counts it only when Airwallex confirms it settled, then re-reads the balance and recalculates only what it affects. The subscription is paid, with a budget alert. |
| 1:22 | Execution on Airwallex (waiting for the rail) | Then VENTHORA pays through Airwallex: two currency conversions and four payments, each sent exactly once. |
| 1:33 | Result, then the wallet against the book's ledger | All four payments come back paid, with Airwallex references and the real fees. The wallet moved about twenty-one thousand dollars, matching the ledger to the cent, and the reserve held. |
| 1:46 | Control center: VENTHORA services (Platform Setup → Airwallex webhooks) | In the Control center, two policy engines must agree, every event is hash-chained, and production is refused. |
| 1:53 | Reporting → Total Spending (Against budget), then Books & ledger and its Segregation panel | Budgets are tracked against plan, here with an alert at eighty-three percent. Each book keeps its own ledger, owner, payees and approvers. |
| 2:04 | System Setting → Checks & resilience, Ground-truth checks: case N34, a payout the bank returned | If the bank returns a payout, VENTHORA replaces it under the same invoice lock, without paying twice. Check it yourself. |
| 2:14 | Resilience test matrix, 136 of 136 | Every one of the hundred thirty-six stress scenarios passes. |
| 2:19 | Back to the top | VENTHORA: your rules, proven plans, payments reconciled to the cent. |

## Cue log of the take

| Time | Scene |
|---|---|
| 0:00.9 | Goal and mandate: the goal |
| 0:03.0 | Permissions, each with its own limit |
| 0:07.0 | Safeguards: FX exposure, overdue invoices, late interest |
| 0:10.0 | Always enforced: no duplicates, verified bank changes |
| 0:13.0 | Budget period: monthly, quarterly or annual |
| 0:16.0 | Approvers and the approval matrix |
| 0:19.5 | Requests received: payables, payroll, email |
| 0:22.0 | Save the mandate and start |
| 0:25.0 | Initial plan: requested |
| 0:27.4 | Initial plan: decisions with reasons |
| 0:36.0 | Initial plan: checks and counterfactual alternatives |
| 0:46.0 | New information: the customer's delay email, quoted |
| 0:55.0 | Revised decision: FX limit halved, approval required |
| 1:00.5 | Revised decision: only the affected row changes, the rest locked |
| 1:06.0 | Incoming payment: waiting for Airwallex to settle it |
| 1:11.3 | Incoming payment: SETTLED, balance re-read, re-planned |
| 1:17.2 | Incoming payment: the subscription is paid, budget alert |
| 1:22.2 | Execution on Airwallex: waiting for the rail |
| 1:33.0 | Result: conversions settled, four payments PAID |
| 1:40.0 | Result: wallet movement equals the book's ledger |
| 1:46.0 | Control center: open |
| 1:49.6 | Platform: two policy engines, hash-chained audit, production refused |
| 1:53.0 | Budget against plan for the latest run |
| 1:58.0 | Books, ledgers and tenants |
| 2:04.0 | Ground-truth checks: a payout the bank returned (Kit 03) |
| 2:14.0 | Resilience test matrix: 136 of 136 |
| 2:19.0 | Close: back to the top |
| 2:23.0 | End |

## What is on screen in this take

| Claim | On screen |
|---|---|
| Plan in a tenth of a second | plan card: planned and proven in 106 ms |
| Confidence 90 % to 70 %, limit USD 10,000 to 5,000 | new-information and revised-decision cards |
| Only the affected row changes | "Changed: SAAS-USD DEFER → ESCALATE · 3 decisions locked" |
| Counted only when settled | incoming payment SETTLED, USD 10,000, "counted only once SETTLED" |
| Four payments paid, real fees | result table: four transfers PAID, fees 3.00, 2.25, 13.43, 3.00 |
| About twenty-one thousand dollars, to the cent | wallet change USD -20,957.90 against the book ledger; reconciliation PASS |
| Reserve held | reserve floor PASS; book USD cash 19,042.10 against a floor of 10,000 |
| Budget alert at 83 % | Reporting → Total Spending (Against budget), line "saas" |
| Returned payout replaced, ground truth matches | case N34: REPLACE, ground truth PASS |
| 136 of 136 | System Setting → Checks & resilience, Resilience test matrix |

## Recording again

The sandbox does not always behave the same way: in one rehearsal the incoming deposit stayed PENDING, so the
subscription was not paid and only three payments went out. The script checks for this and exits with code 2 when a
take does not show the story above; reset the store and record again.

```bash
python scripts/demo_server.py --reset
python scripts/record_demo.py
```

When the timing of a new take differs, update the time codes from its `demo_video/cues.md`. The cue log above is the
25 Oct take's own record and keeps the page names of that day; the "On screen" column uses the current menu.
