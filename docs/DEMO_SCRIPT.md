# Demo narration script: word for word, about four and a half minutes

The organizers ask for a video **under five minutes**. The spoken text is about 530 words: about three and a half
minutes of speech at 150–160 words a minute. The time codes below include the pauses for clicks, page loads and the
real execution in scene 6, which bring the video to about 4:30 and leave a margin under the limit. The figures are the ones the dashboard shows on a
clean demo server (`python scripts/demo_server.py --reset`), as recorded in `docs/demo_run_log.json`. Stage directions
are in brackets and are not read aloud. What to click in each scene is in `docs/DEMO_STORYBOARD.md`.

---

## Opening (0:00 – 0:15)

[Dashboard open at the top. Do not click yet.]

This is VENTHORA, a treasury agent on Airwallex for Kit 01, with Kit 03's incident handling on top. The AI proposes,
deterministic engines verify, and Airwallex executes. Every decision you will see comes from a rule, and every payment
ran on the real sandbox.

## Scene 1 · The plan, proven (0:15 – 0:50)

[Press **Plan** on the Kit 01 initial book. Point at the Decision tile, the decisions, the stages, the counterfactuals.]

Day one: five obligations in three currencies, two receivables, a five-thousand-dollar reserve floor. Payroll and
shipping go out, converting exactly what they need. The duplicate invoice is rejected. The contractor whose email
changed bank details is escalated, not paid. The cheapest bill waits for a reliable receipt.

Twelve stages check the plan, from evidence and a seventy-two-hour simulation to the policy in two engines and live
balances. On the right, the counterfactuals: pay everything now, and the reserve breaches with a twenty-eight percent
probability. The plan is approved because it is proven.

## Scene 2 · An email that wants money (0:50 – 1:10)

[Submit a case: suite **anomalies**, case **N16**, press **Submit**.]

A lookalike mailbox: we moved banks, it is urgent, pay today. The pipeline checks the registry, not the tone. The
sender is not registered, so the request is quarantined, with the three phrases that decided it quoted verbatim. No
model was asked for an opinion.

## Scene 3 · The call-back nobody can skip (1:10 – 1:45)

[Payees · lifecycle: on Lumen Design press **Bank change**, then **Call-back** with a wrong number, then with the
registered number.]

If the bank change is genuine, the payee moves to CHANGED, and no plan and no approval can pay it. A call-back to the
number in the email is refused by code. A call-back to the number registered at onboarding verifies the payee and
starts a seventy-two-hour cooling period, in which every payment still needs a person.

## Scene 4 · The plan is the mandate (1:45 – 2:05)

[Scroll to Budget · mandate. Point along one line, then at the utilisation bars.]

The monthly plan is the agent's mandate, line by line. Inside the autonomous share it acts alone; at the alert
threshold it warns; above the mandate it stops and raises a budget request with the exact shortfall.

## Scene 5 · How much, and how many signatures (2:05 – 2:30)

[Submit a case: suite **limits**, case **V03**, Submit. Then suite **approvals**, case **Q03**, Submit.]

One cent above the single-payment limit, and the plan needs approval. At the two-person threshold, the plan states what
it will take: two approvals from two different people, in named roles. The same person twice counts once, and the
requester can never approve.

## Scene 6 · Money moves, and the books agree to the cent (2:30 – 3:10)

[In the second terminal run `python scripts/demo_walkthrough.py`. Open the new COMPLETED run in Executions, then the
new book in Books · ledger.]

Now a real payment: one thousand dollars to a verified payee, inside every control, so the agent executes alone on the
sandbox. It claims the invoice before money moves, so it can never be paid twice, polls the transfer to PAID, and
reconciles: the wallet moved by exactly this book's cash lines. Discrepancies: none. The wallet is shared; the book's
ledger is not.

## Scene 7 · A supplier says the money never arrived (3:10 – 3:35)

[Submit a case: kind **incident facts**. The BENEFICIARY_BANK_RETURNED facts, Submit; then INVALID_BENEFICIARY_DETAILS,
Submit.]

Kit 03. The agent reads the rail's typed failure, not the supplier's email. Returned by the bank: replace once, with a
new request id, under the same duplicate lock. Wrong account details: escalate, because paying again pays the wrong
account again.

## Scene 8 · What lateness costs (3:35 – 3:55)

[Submit a case: suite **aging**, case **G02**, Submit. Then **D03**, Submit.]

Deferring has a price. An invoice forty-five days overdue that the book cannot fund accrues ninety-eight dollars of
interest, and the plan stops for a person. A supplier's final notice, read from the inbox, raises the dunning level.

## Scene 9 · Whose book, whose suppliers, whose signature (3:55 – 4:15)

[Scroll to Tenants · books. Point at the Acme pattern book, then at the demo book.]

One wallet, several tenants. Each book pays only its own payees, is signed only by its own people, and keeps its own
policy and ledger. An unregistered book cannot spend at all.

## Closing (4:15 – 4:30)

[Scroll back to the top. Hold on the Decision tile.]

Three hundred twenty-one hand-derived test cases on three paths, the policy proven identical in two engines on over ten
thousand inputs, eighteen cycles on the real sandbox. The AI proposes, the engines verify, Airwallex executes. Thank you.

---

Spoken text: about 530 words. If a take runs long, drop scene 4 (budget) first; it is the one scene whose point is
also visible in scene 1's stage list.
