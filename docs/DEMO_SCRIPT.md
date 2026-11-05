# Demo voice-over: the two dashboards, about four and three-quarter minutes

Every sentence describes something visible on the operations page (`/live`) or the Control center (`/`), so the video
shows each strength as it is named. It covers what the organizers ask a demo to show: **the user's goal, the agent's
initial plan, new information, the revised decision, and the resulting financial state**. The video must be **under five
minutes**; this one runs about 4:45 including the real waits on Airwallex.

**Before recording:** `python scripts/demo_server.py --reset`, open `http://localhost:8001/live`, browser at 100 % zoom
and at least 1,500 px wide. Each run moves real sandbox money, so reset between takes. Click **Next** right after the
last sentence of each block. Figures that follow live sandbox rates are said in round numbers. Stage directions are in
brackets and are not read aloud.

---

## Operations page (0:00 – 3:20)

**[0:00 – 0:40 · Step 1, the mandate form; scroll slowly through the permissions, guards and locked rules]**
You tell VENTHORA what you want: every bill due in the next seventy-two hours paid on time, in dollars, euros and
pounds, with at least ten thousand dollars always left in the wallet. Then you set its mandate. Each permission has a
limit you choose. Uncheck one, and that action comes back to you. It also guards your currency exposure, overdue
invoices and late interest. And some rules can't be turned off: no invoice is paid twice, and a changed bank account is
never paid until you've confirmed it.

**[0:40 – 1:05 · Approvers and the approval matrix, then Requests received; click Save the mandate and start]**
Your approvers are here: one for routine payments, two different people from twenty-five thousand dollars, and the
requester can never approve. Here's what's waiting: invoices from your payables and payroll systems, and emails from
suppliers and customers. Save the mandate, and VENTHORA opens the book on your Airwallex wallet.

**[1:05 – 1:50 · Step 2, the plan; at the end open Checks and alternatives]**
In under a second it has a plan. Payroll, the UK freight bill and the German supplier get paid, buying exactly the
euros and pounds they need. A resent invoice is rejected as a duplicate. A contractor who emailed new bank details
comes to you, with the email quoted word for word. The subscription waits for a customer payment due tomorrow.
Every plan shows its checks: two thousand simulated runs of the next seventy-two hours for your reserve, and why each
alternative is worse. Paying everything now would cross your reserve by about two thousand dollars.

**[1:50 – 2:05 · Step 3, new information]**
Then the customer writes: their payment will be five days late. VENTHORA weighs that customer's track record, and its
confidence drops from ninety to seventy percent.

**[2:05 – 2:22 · Step 4, the revised decision; point at the highlighted row]**
Only what the news affects changes. It halves how much currency it may convert on its own, so that conversion now waits
for you, and the subscription comes to you too. Everything else stays as planned.

**[2:22 – 2:40 · Incoming payment confirmed]**
The customer's money lands. VENTHORA counts it only once Airwallex confirms it, re-reads the balance, and re-plans on
its own. The subscription is paid, with an alert that its budget line is almost used.

**[2:40 – 3:20 · Step 5, the result; point at the table, then the wallet and ledger panels]**
Then it pays, on Airwallex. Both conversions settle, and all four payments come back paid, each with its Airwallex
reference and the fee Airwallex actually charged. The wallet moved about twenty-one thousand dollars, and every cent
matches the book's ledger. Your reserve was never touched, and the duplicate and the changed bank account were never
paid.

## Control center (3:20 – 4:45)

**[3:20 – 3:40 · Click Control center; Payment → Agent plan (2 · Transaction log), then Platform Setup → Airwallex webhooks (VENTHORA services)]**
The Control center keeps all of it on the record. Any plan can be re-proven here, with every alternative and its chance
of breaching the reserve. Your policy runs in two engines that must agree, the Airwallex connection refuses production,
and every event goes into a hash-chained audit trail.

**[3:40 – 3:55 · Reporting → Total Spending (Against budget), then Reporting → Books & ledger and its Segregation panel]**
You see spending against each budget line, a double-entry ledger for every book, and which team owns each book, with
its own payees, approvers and limits. One team's money is never spent by another.

**[Optional, before the payees block · Procurement Process → Vendor Sourcing, then Quotation; click Approve on the recommended row]**
A purchase request gets three quotations: the agent finds the vendors, a person sends the request, and approving the
cheapest compliant quote raises the PO.

**[3:55 – 4:05 · Users & Beneficiaries → Payee lifecycle]**
Every payee moves from new, to verified, to active. A bank change starts a cooling period, and the call-back goes only
to the number already on file.

**[4:05 – 4:13 · Reporting → Period close]**
At your cut-off, each payment lands in the right period, consolidated into your reporting currency, with the exchange
difference shown.

**[4:13 – 4:38 · System Setting → Checks & resilience, Ground-truth checks: choose "anomalies", then case N34, then Submit; then the Resilience test matrix]**
And you can test it yourself. Take a payout the bank returned: VENTHORA replaces it under the same invoice lock. Had the
account details been wrong, it would call a person instead. Three hundred twenty-three checks like this, from lookalike
emails to injected instructions, all pass, along with a hundred and thirty-six stress scenarios.

**[4:38 – 4:45 · Scroll back to the top]**
VENTHORA alpha: your rules, proven plans, and payments on Airwallex reconciled to the cent.

---

**Timing.** About 585 spoken words, about 3:55 at 150 words a minute, plus about 45 seconds of clicks and waits on
Airwallex. Total about 4:40. The optional procurement block adds about 10 seconds: record it only by dropping the Period close
block. If a take runs long, drop the Period close block first, then the Payee lifecycle block.

**What each claim rests on, all on screen.** The checks and the counterfactual figure are in the plan card's "Checks and
alternatives" (paying every verified obligation now: USD 1,951.63 over the floor on 25 Oct 2026; it moves with live
rates). The approval matrix is in the mandate form. Fees are in the result table's Detail column. The two engines, the
production refusal and the audit chain are in the VENTHORA services tiles (Platform Setup → Airwallex webhooks). Case N34 returns REPLACE and matches its ground truth;
N35 (wrong account details) returns ESCALATE. Lookalike emails and injected instructions are intake cases I02, I09 and
I17.
