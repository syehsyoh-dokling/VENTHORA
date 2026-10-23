# Demo narration script: word for word, about five minutes

Read at a brisk, clear pace, roughly 160 words a minute (about 820 spoken words); at a slower 140 the video runs
close to six minutes, which is still inside most submission limits. The figures are the ones the dashboard shows on a clean demo server
(`python scripts/demo_server.py --reset`), as recorded in `docs/demo_run_log.json`. Stage directions are in brackets and
are not read aloud; the scene timings add up to about five minutes and ten seconds. The storyboard with what to click
is `docs/DEMO_STORYBOARD.md`.

---

## Opening (0:00 – 0:20)

[Dashboard open at the top. Do not click yet.]

This is VENTHORA, an evidence-gated treasury agent on Airwallex, built for Kit 01, the adaptive treasury controller,
with Kit 03's incident handling on top. The idea in one sentence: the AI proposes, deterministic engines verify, and
Airwallex executes. Every decision you are about to see comes from a rule you can read, and every payment ran on the
real sandbox.

## Scene 1 · The plan, proven (0:20 – 1:00)

[Press **Plan** on the Kit 01 initial book. Point at the Decision tile, then the decisions table, then the stage list,
then the counterfactual column.]

Here is day one. Five obligations in three currencies, two receivables, a reserve floor of five thousand dollars.
Payroll and shipping must go out; the agent pays them and converts exactly what it needs. The European supplier is
paid; its duplicate invoice is rejected on the fingerprint. The contractor whose email changed bank details is
escalated to a person, not paid. The cheapest bill is deferred until a reliable receipt lands.

Twelve stages check that plan: evidence, risk, a seventy-two-hour simulation, the optimizer, the invariants, the
budget, the policy in two engines, and live balances. And on the right, the counterfactuals: pay everything now and
the reserve breaches with a twenty-eight percent probability. The plan is approved because it is proven, not because
it sounds right.

## Scene 2 · An email that wants money (1:00 – 1:30)

[Submit a case: suite **anomalies**, case **N16**, press **Submit**.]

Now the classic attack. An email from a lookalike mailbox: we have moved banks, it is urgent, please pay today. The
intake pipeline does not weigh the tone; it checks the registry. The sender is not the registered mailbox, so the
request is quarantined, and the three phrases that decided it are quoted verbatim: new bank, urgent, immediately. No
language model was asked for an opinion, and the result matches the hand-derived ground truth in twelve milliseconds.

## Scene 3 · The call-back nobody can skip (1:30 – 2:10)

[Payees · lifecycle: on Lumen Design press **Bank change**, enter any identifier. Then **Call-back** with a wrong
number, read the refusal. Then **Call-back** with the registered number shown in the row.]

Suppose the bank change were genuine. The payee moves to CHANGED, and from this moment no plan and no approval can pay
it. The only way out is a call-back. If I enter the number printed in the email, the system refuses: the call-back
must go to the number registered at onboarding. With the registered number, the payee is verified, and a cooling
period of seventy-two hours begins, during which every payment still needs a person. The agent pays the new account
alone only after a real payment has gone through.

## Scene 4 · The plan is the mandate (2:10 – 2:35)

[Scroll to Budget · mandate. Point along one line, then at the utilisation bars.]

The organisation's monthly plan is the agent's mandate, line by line: payroll, logistics, suppliers, contractors,
software. Inside the autonomous share, the agent acts alone; here payroll is at eighty-nine percent, logistics at
seventy-nine, suppliers at sixty-two. At the alert threshold it warns. Above the mandate it stops and raises a budget
request with the exact shortfall, and a person grants or refuses it.

## Scene 5 · How much, and how many signatures (2:35 – 3:05)

[Submit a case: suite **limits**, case **V03**, Submit. Then suite **approvals**, case **Q03**, Submit.]

Limits are numbers, not judgement. One cent above the single-payment limit, and the plan requires approval. Then the
approval matrix: at the two-person threshold the plan itself announces what it will take: two approvals, from two
different people, in named roles. The same person twice counts once, and the person who submitted the plan can never
be one of its approvers. Four eyes, enforced before money moves.

## Scene 6 · Money moves, and the books agree to the cent (3:05 – 3:50)

[In the second terminal run `python scripts/demo_walkthrough.py`; wait for scene 6. Then Executions: open the new
COMPLETED run. Then Books · ledger: the new book.]

Now a real payment. A fresh book, one thousand dollars to a verified payee, inside every control, so the agent
executes alone on the Airwallex sandbox. It claims the invoice before any money moves, so it can never be paid twice.
It creates the transfer with a persisted request id, polls it to PAID, and then reconciles: the wallet moved by
exactly the sum of this book's cash lines. Discrepancies: none. The book's own ledger shows cash of eighteen thousand
nine hundred ninety-seven, expense one thousand, fees three dollars. The wallet is shared; the book is not.

## Scene 7 · A supplier says the money never arrived (3:50 – 4:15)

[Submit a case: kind **incident facts**. Paste the BENEFICIARY_BANK_RETURNED facts, Submit. Then the
INVALID_BENEFICIARY_DETAILS facts, Submit.]

Kit 03. The supplier says the money never arrived. The agent does not trust the email; it reads the rail's typed
failure. Returned by the beneficiary bank: replace once, with a new request id and a second fee budgeted, under the
same duplicate lock. Wrong account details: escalate, because paying again would pay the wrong account again. Wait,
replace, or escalate: a decision table, not a conversation.

## Scene 8 · What lateness costs (4:15 – 4:40)

[Submit a case: suite **aging**, case **G02**, Submit. Then **D03**, Submit.]

An agent that only optimises cash would defer the cheapest bill forever. So lateness has a price. An invoice
forty-five days overdue that the book cannot fund is bucketed, its late interest is accrued, ninety-eight dollars and
sixty-three cents, and the plan stops for a person instead of deferring quietly. A supplier's final notice, read from
the inbox with its quote, raises the book's dunning level to three.

## Scene 9 · Whose book, whose suppliers, whose signature (4:40 – 5:00)

[Scroll to Tenants · books. Point at the Acme pattern book, then at the demo book under the sandbox lab.]

One wallet, several tenants. Each book may pay only its own payees, is signed only by its own people, runs under its
own policy values, and keeps its own ledger. A book the registry does not know cannot spend at all. And here is the
book we just paid from, with its one execution and its cash.

## Closing (5:00 – 5:15)

[Scroll back to the top. Hold on the Decision tile.]

Twelve ground-truth suites, three hundred twenty-one hand-derived cases, reproduced in-process, through the API and
through this form. The policy proven identical in Python and in OPA on ten thousand six hundred eighteen inputs.
Eighteen cycles on the real sandbox. The AI proposes. The engines verify. Airwallex executes. Thank you.

---

Word count of the spoken text: about eight hundred and twenty.
