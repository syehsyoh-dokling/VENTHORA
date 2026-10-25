# Demo voice-over: VENTHORA alpha, about four and a half minutes

Written as a product walkthrough for people who would use VENTHORA, built around what the organizers ask a demo to show:
**the user's goal, the agent's initial plan, new information, the revised decision, and the resulting financial state**,
with amounts, limits, reserves, validation, duplicate protection and state changes controlled by code. The video must be
**under five minutes**; this one runs about 4:30 including the real waits on Airwallex.

**Before recording:** `python scripts/demo_server.py --reset`, open `http://localhost:8001/live`, browser at 100 % zoom
and at least 1,500 px wide. Each run moves real sandbox money, so reset between takes. Click **Next** right after the
last sentence of each block; the time codes include the pauses for Airwallex (about 4 s for the incoming payment, about
10 s for the execution). Figures that follow live sandbox rates are said in round numbers.

Stage directions are in brackets and are not read aloud.

---

## Opening · How it works (`/flow`, about 1:15)

Play `http://localhost:8001/flow?speed=1.5` in full screen (F). At 1.5x each step lasts about four seconds and the
page moves on by itself; read one line per step as its box appears. To control the pace yourself, pause with Space and
click anywhere for the next step. About 190 spoken words.

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
| 10 | Verdict | Every decision gets a verdict, and a reason. |
| 11 | Blocked | Blocked means never paid, like an invoice sent twice. |
| 12 | Approval | Anything outside your mandate waits for you, like changed bank details. |
| 13 | Airwallex | Approved payments go to Airwallex: quote, convert, pay, confirm. |
| 14 | Approval to Airwallex | Once you approve, it takes the same path, sent exactly once. |
| 15 | Re-plan loop | Money coming in counts only when Airwallex confirms it. Then it re-plans. |
| 16 | Reconcile | Every movement is matched to your ledger, to the cent. |
| 17 | Control center | And your CFO sees all of it in the Control center. |
| 18 | End card | This is VENTHORA alpha, live on Airwallex. Let's see it run. |

**Staying under five minutes with the opening:** the opening adds about 1:15. Skip the first operations block
(0:00 – 0:15, the opening already introduces VENTHORA) and Part 2 (the Control center, which step 17 now covers).
Together that saves about 1:10, so the whole video stays at about 4:35.

---

## Part 1 · Operations (0:00 – 3:25)

**[0:00 – 0:15 · Operations page, nothing clicked]**
This is VENTHORA, an AI treasury agent for finance teams that pay suppliers in more than one currency. It plans your
payments, moves the money through Airwallex, and closes the books. And it never pays anything it can't prove is safe.
This is the alpha, running live on Airwallex.

**[0:15 – 0:45 · Step 1, the mandate form, scroll slowly]**
You start by telling it what you want: every bill due in the next seventy-two hours paid on time, in dollars, euros and
pounds, with at least ten thousand dollars always left in the wallet.
Then you set its mandate. Each permission has a limit you choose. Uncheck one, and that action always comes back to you
for approval. A few rules can't be turned off: an invoice is never paid twice, and a supplier who changes bank details is
never paid until you've confirmed it on the number you already have.

**[0:45 – 0:55 · Requests received, then click Save the mandate and start]**
Here's what's waiting: invoices from your accounts-payable and payroll systems, and emails from suppliers and customers.
Save the mandate, and VENTHORA opens the book on your Airwallex wallet.

**[0:55 – 1:30 · Step 2, the plan]**
In under a second it has a plan. Payroll, the UK freight bill and the German supplier get paid, buying exactly the euros
and pounds they need. It refuses two payments on its own: a duplicate of the German invoice that came in again by email,
and a contractor who emailed new bank details. That one it sends to you, with the email quoted word for word. The
software subscription can wait, because a customer's ten-thousand-dollar payment is due tomorrow.

**[1:30 – 1:50 · Step 3, new information]**
Then the customer writes: their payment will be five days late. VENTHORA checks how reliable that customer has been, and
its confidence in the money drops from ninety to seventy percent.

**[1:50 – 2:15 · Step 4, the updated plan; point at the highlighted row]**
It updates the plan, and changes only what the news affects. It halves how much currency it may convert on its own, so
that conversion now waits for you. The subscription can't wait for money that may arrive too late, so it comes to you
too. Everything else stays exactly as planned.

**[2:15 – 2:40 · Incoming payment confirmed]**
The customer's money lands. VENTHORA counts it only once Airwallex confirms it, re-reads the balance, and updates the plan
on its own. The subscription is paid, and you're told that budget line has passed its alert level.

**[2:40 – 3:25 · Step 5, the result; point at the table, then the wallet panel]**
Then it pays, on Airwallex. Both conversions settle, and all four payments come back paid, each with its Airwallex
reference. The wallet moved about twenty-one thousand dollars, and every cent matches the book's ledger. Euro and pound
balances end at exactly zero, and your reserve was never touched.
The duplicate and the changed bank account were never paid. Your rules stopped them.

## Part 2 · Control center (3:25 – 4:20)

**[3:25 – 3:40 · Click Control center; header, Plan & proof and Platform]**
Everything VENTHORA does stays on the record. You can re-check any plan, your payment policy is enforced by two
independent engines that must agree, and every event is written to an audit trail that can't be altered.

**[3:40 – 3:55 · Budget, then Books · ledger]**
You see what was spent against each budget line this month, and every book keeps its own double-entry ledger, so one
team's money is never spent by another.

**[3:55 – 4:05 · Reporting periods]**
At your month-end cut-off, every payment lands in the right period, consolidated into your reporting currency, and every
invoice is marked settled.

**[4:05 – 4:20 · Ground-truth checks, then Resilience test matrix]**
And you can test it yourself: three hundred twenty-three checked cases you can run from this page, and a hundred and
thirty-six stress scenarios, every one passed.

## Close (4:20 – 4:30)

**[Scroll back to the top]**
VENTHORA alpha. Connect your Airwallex sandbox, set your rules, and let it run your payments.

---

Spoken text: about 560 words, about 3.7 minutes at 150 words a minute; with the clicks and the waits on Airwallex, about
4:30. If a take runs long, drop the Reporting periods block (3:55 – 4:05) first.
