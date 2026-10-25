# Demo voice-over: the final script, about four and a half minutes

Built around what the organizers ask a demo to show: **the user's goal, the agent's initial plan, new information, the
revised decision, and the resulting financial state**, with amounts, limits, reserves, validation, duplicate protection
and state changes controlled by code. The video must be **under five minutes**; this one runs about 4:30 including the
real waits on Airwallex.

**Before recording:** `python scripts/demo_server.py --reset`, open `http://localhost:8001/live`, browser at 100 % zoom
and at least 1,500 px wide. Each run moves real sandbox money, so reset between takes. Click **Next** right after the
last sentence of each block; the time codes include the pauses for Airwallex (about 4 s for the incoming payment, about
10 s for the execution). Figures that follow live sandbox rates are said in round numbers.

Stage directions are in brackets and are not read aloud.

---

## Part 1 · The operations page (0:00 – 3:25)

**[0:00 – 0:15 · Operations page, nothing clicked]**
Software agents are starting to move real money. The hard part isn't making them act. It's making sure they can't act
wrongly. This is VENTHORA: an AI treasury agent that must prove a payment is safe before it's allowed to make it, live on
Airwallex.

**[0:15 – 0:45 · Step 1, the mandate form, scroll slowly]**
It starts with the user's goal: pay every bill due in the next seventy-two hours, in dollars, euros and pounds, without
the wallet ever dropping below a ten-thousand-dollar reserve.
The finance manager gives VENTHORA thirty thousand dollars and a mandate. Every permission has a limit, and unticking one
sends that action back to a person. Some rules can never be switched off: no invoice is paid twice, and a changed bank
account is never paid until someone calls the supplier back.

**[0:45 – 0:55 · Requests received, then click Grant]**
Invoices arrive from our systems, emails from suppliers and a customer. She grants the mandate, and the book opens on the
live Airwallex wallet.

**[0:55 – 1:30 · Step 2, the initial plan]**
VENTHORA builds its plan. Payroll, UK shipping and the German supplier are paid, converting exactly the currency they
need. Two payments it refuses on its own: a duplicate of the German invoice, sent again by email, and Lumen Design's
payment, because Lumen emailed new bank details. VENTHORA quotes that email word for word and hands it to a person.
The software bill can wait: a customer has promised ten thousand dollars tomorrow.

**[1:30 – 1:50 · Step 3, new information]**
Then the customer writes again: the payment is five days late. VENTHORA weighs their track record, and its confidence in
that money falls from ninety to seventy percent.

**[1:50 – 2:15 · Step 4, the revised decision; point at the flashing row]**
So it revises the plan, and only what's affected changes. Its authority to convert currency alone is halved, so the
conversion now needs a person, and the software bill is escalated. Everything else stays exactly as it was.

**[2:15 – 2:40 · Payment received]**
Now the customer's money lands on Airwallex. VENTHORA counts it only once it's settled, re-reads the balance, and revises
again on its own. The software bill is paid, and its budget line passes the alert threshold.

**[2:40 – 3:25 · Step 5, the resulting state; point at the table, then the wallet panel]**
Then it executes, live. Both conversions settle, and all four transfers come back paid, each with its Airwallex
reference. The wallet moved by about twenty-one thousand dollars, and every cent matches this book's ledger. Euro and
pound balances land at exactly zero, and the reserve was never touched.
What wasn't paid? The duplicate, and the changed bank account. Stopped by rules, not by luck.

## Part 2 · The Control center (3:25 – 4:20)

**[3:25 – 3:40 · Click Control center; header, Plan & proof and Platform]**
Everything stays accountable in the Control center. Any plan can be proven again, the payment policy runs in two
independent engines that must agree, and every event is chained into a tamper-evident audit trail.

**[3:40 – 3:55 · Budget, then Books · ledger]**
The budget shows what this run spent, line by line, against the month's plan. Each book keeps its own double-entry
ledger: the wallet is shared, but no book can spend another book's money.

**[3:55 – 4:05 · Reporting periods]**
At the month's cut-off, all four payments land in the right period, consolidated into rupiah, and every invoice is
settled.

**[4:05 – 4:20 · Ground-truth checks, then Resilience test matrix]**
And you don't have to take our word for it. Anyone can run one of three hundred twenty-three hand-checked cases, and
across a hundred and thirty-six stress scenarios, every safety rule held.

## Close (4:20 – 4:30)

**[Scroll back to the top]**
The AI proposes. The engines verify. Airwallex executes. That's VENTHORA.

---

Spoken text: about 520 words, about 3.5 minutes at 150 words a minute; with the clicks and the waits on Airwallex, about 4:30. If a take
runs long, drop the Reporting periods block (3:55 – 4:05) first.
