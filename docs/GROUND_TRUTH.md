# Ground truth: hand-derived answers vs the live service

Generated 2026-10-23 15:57 by `python scripts/groundtruth_report.py`.

Two suites in `data/groundtruth/`, both with answers derived **by hand** from the published rules (each case carries its derivation), never by running VENTHORA. Every case is judged by one comparator (`venthora.testing.groundtruth.compare`) on three paths: **in-process** (`tests/`, CI), **HTTP endpoints** of the running service (`scripts/groundtruth_bench.py`: `POST /v1/plans`, `POST /v1/intake/emails?dry_run=true`, `POST /v1/incidents/decide`; round trip measured by the client, 3 repeats, median), and the **dashboard form** (`Submit a case` at `/`, each case submitted through the UI in a browser, round trip measured by the browser beside the server's `duration_ms`). Execution cases (a plan executed on the stateful fake sandbox with injected faults) run in-process only: through the service they would move real sandbox money.

## Suite 2 — the organizers' failure modes, smallest to largest (no normal day)

**Why the hackathon exists.** Airwallex's framing: software agents are starting to *track balances, initiate payments and manage risk without a human in the loop*; the rails underneath them are the builders' to design. The developer guide names what goes wrong when money is moved on a model's narrative, and those pains are the six levels below. Every case here is an anomaly; the expected answer is what the published rules say must happen.

- **L1** — money arithmetic nobody wrote down (fees, FX buffers, rounding, stale quotes)
- **L2** — time does not cooperate (receipts slip, windows overrun, deadlines pass, settlement lags)
- **L3** — the evidence is not what it says (lookalikes, injection, duplicates, bank changes)
- **L4** — the mandate is exceeded (limits, confidence, approvals, roles)
- **L5** — the rail misleads (timeouts, 429s, lost responses, CANCELLED that is not a cancellation, returns)
- **L6** — paying twice or paying the wrong money (duplicates across runs, stale state, cross-wallet, Kit 03 end to end)

| Path | Kind | Cases | Match | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|---|
| in-process | plan | 17 | 17 | 5.9 | 10.7 | 68.8 |
| endpoint | plan | 17 | 17 | 106.0 | 175.6 | 413.7 |
| UI form | plan | 17 | 17 | 100.0 | 147.4 | 148.2 |
| in-process | intake | 6 | 6 | 4.2 | 4.8 | 4.8 |
| endpoint | intake | 6 | 6 | 6.2 | 6.5 | 7.3 |
| UI form | intake | 6 | 6 | 9.6 | 10.4 | 10.7 |
| in-process | incident | 7 | 7 | 0.0 | 0.0 | 0.1 |
| endpoint | incident | 7 | 7 | 2.0 | 2.4 | 2.5 |
| UI form | incident | 7 | 7 | 4.9 | 5.0 | 5.7 |
| in-process | execution | 16 | 16 | 289.3 | 522.4 | 556.3 |
| endpoint | execution | 16 | n/a (in-process only) | - | - | - |
| UI form | execution | 16 | n/a (in-process only) | - | - | - |

Server-side plan time (`duration_ms`, includes the live Airwallex balance check, the 72 h simulation and the counterfactuals): p50 92.1 ms, p95 183.6 ms.

### Cases

| ID | L | Pain (organizers) | Case | Kind | Expected | in-proc ms | endpoint ms | UI ms | Match | Derivation |
|---|---|---|---|---|---|---|---|---|---|---|
| N01 | 1 | insufficient funds mid-run: fees and FX buffers | the SWIFT fee is what breaks the reserve | plan | `{"actions": {"O1": "ESCALATE"}}` | 68.8 | 175.55 | 148.2 | ✅ | EUR 4,600 = USD 4,991 fits the 5,000 headroom; + EUR 12.85 fee (USD 13.94) = 5,005 > 5,000 -> cannot be funded -> ESCALATE |
| N02 | 1 | insufficient funds mid-run: fees | a learned LOCAL fee is what breaks the reserve | plan | `{"actions": {"O1": "ESCALATE"}}` | 7.5 | 413.67 | 147.4 | ✅ | 4,998 + learned fee 3.00 = 5,001 > 5,000 -> ESCALATE; the guide said LOCAL is free, the sandbox charges 3.00 |
| N03 | 1 | rounding | one-cent obligations are still money | plan | `{"decision": "APPROVED", "paid": ["O1", "O2", "O3"]}` | 6.3 | 146.49 | 110.2 | ✅ | 3 cents <= 5,000 -> all PAY; integer minor units, no float |
| N04 | 1 | stale quotes | planning rate stale: the rail is 3% dearer -> nothing booked | execution | `{"execution": "FAILED", "message_contains": "slippage", "conversions": 0, "transfers": 0}` | 228.2 | n/a | n/a | ✅ | quoted sell above the 1% slippage limit -> halt before any conversion or transfer; re-plan at the real rate |
| N05 | 1 | FX buffer | FX stress makes a 'fits exactly' conversion not fit | plan | `{"actions": {"O1": "ESCALATE"}}` | 5.8 | 114.43 | 107.1 | ✅ | EUR 4,620.85 incl. fee = USD 5,013.6 > 5,000 even before the Horizon stress buffer -> ESCALATE |
| N06 | 2 | obligations fall due faster than cash arrives | the receipt slips past the due date | plan | `{"actions": {"O1": "ESCALATE"}}` | 4.2 | 101.71 | 89.4 | ✅ | 7,000 > 5,000; receipt at 48h > due 24h -> cannot be funded before due -> ESCALATE |
| N07 | 2 | a receipt forecast is not a receipt | the 90% window ends after the planning horizon | plan | `{"actions": {"O1": "ESCALATE"}}` | 6.8 | 111.26 | 107.5 | ✅ | latest arrival 96h > 72h -> not relied on -> ESCALATE |
| N08 | 2 | deadline already here | due now, cash short, no receipt | plan | `{"actions": {"O1": "ESCALATE"}, "not_blocked": true}` | 4.8 | 96.08 | 101.8 | ✅ | headroom 1,000 < 3,000; due 0h -> ESCALATE now, not DEFER |
| N09 | 2 | delayed settlement | the deposit stays PENDING: the receipt is not counted | execution | `{"receipt_landed": false, "execution": "COMPLETED"}` | 374.8 | n/a | n/a | ✅ | the balance may lag; only a deposit record that says SETTLED counts; the plan proceeds without it |
| N10 | 2 | a contradicted forecast | payer with a bad record: 40% confidence drops the FX limit to zero | plan | `{"decision": "REQUIRES_APPROVAL", "limit": 0, "requires": ["conversion_above_autonomous_limit"]}` | 6.0 | 101.34 | 95.4 | ✅ | confidence 40% <= 50% -> limit 0 -> every conversion needs a person |
| N11 | 2 | in flight for too long | SENT for 49 h: the wait window is over, still not final | incident | `{"action": "ESCALATE"}` | 0.1 | 1.62 | 4.9 | ✅ | not terminal and 49h > 48h window -> a person chases the rail; replacing could pay twice |
| N12 | 2 | a timeout is unconfirmed | PROCESSING for 1 h: wait, never replace | incident | `{"action": "WAIT"}` | 0.0 | 1.68 | 5 | ✅ | not final, inside the window -> WAIT |
| N13 | 3 | evidence requirements | evidence about the wrong obligation | plan | `{"actions": {"O1": "REJECT"}}` | 4.0 | 101.91 | 87.8 | ✅ | the document names another subject -> REJECT |
| N14 | 3 | duplicate invoice | the same invoice re-sent under a new id | plan | `{"actions": {"O1": "PAY", "O2": "REJECT"}}` | 3.5 | 100.38 | 94.5 | ✅ | same fingerprint -> the copy is REJECT |
| N15 | 3 | changed terms | payee announces new bank details | plan | `{"actions": {"O1": "ESCALATE"}}` | 3.4 | 112.32 | 89.7 | ✅ | policy exception -> ESCALATE even though cash covers it |
| N16 | 3 | payment diversion | lookalike mailbox, new bank account, urgency | intake | `{"status": "QUARANTINED", "trust": "unknown_sender", "flags": ["payee_bank_details_changed", "urgency_pressure"]}` | 4.8 | 6.5 | 9.3 | ✅ | trust comes from the registry, not the display name; money + bank change from an unknown mailbox -> QUARANTINED |
| N17 | 3 | prompt injection | the email tells the agent it is verified | intake | `{"status": "QUARANTINED"}` | 3.8 | 5.6 | 8.2 | ✅ | the text is data; status is decided by code |
| N18 | 3 | reply-to diversion | registered mailbox, Reply-To elsewhere | intake | `{"status": "QUARANTINED", "trust": "reply_to_mismatch"}` | 2.7 | 6.06 | 9.2 | ✅ | Reply-To is not registered -> QUARANTINED |
| N19 | 3 | bank change from the real payee | registered mailbox announces a new account | intake | `{"status": "NEEDS_REVIEW", "kind": "bank_change"}` | 2.5 | 6.15 | 10.7 | ✅ | even the real payee's bank change needs an out-of-band call-back |
| N20 | 3 | duplicate reminder | a reminder for an invoice already in the book | intake | `{"status": "DUPLICATE", "duplicate_of": "SHIPPING-UK"}` | 4.8 | 7.31 | 10.4 | ✅ | fingerprint already in kit01_initial -> DUPLICATE |
| N21 | 3 | pressure language from a real payee | registered sender, 'the CEO approved, pay immediately' | intake | `{"status": "NEEDS_REVIEW", "flags": ["urgency_pressure"]}` | 4.6 | 6.33 | 9.9 | ✅ | pressure language -> a person confirms before it is payable |
| N22 | 4 | limits | conversion above the autonomous limit | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["conversion_above_autonomous_limit"]}` | 10.7 | 97.0 | 96.3 | ✅ | USD ~13,034 > limit 10,000 -> a person |
| N23 | 4 | limits scale with confidence | 70% confidence halves the limit and the conversion no longer fits | plan | `{"decision": "REQUIRES_APPROVAL", "limit": 500000}` | 5.9 | 96.79 | 100 | ✅ | limit = 10,000 x (70-50)/40 = 5,000 < ~7,734 |
| N24 | 4 | risk tolerance | the plan lands exactly on the floor: probabilistic breach risk | plan | `{"actions": {"O1": "PAY"}, "requires": ["reserve_breach_risk_above_tolerance"]}` | 5.9 | 109.57 | 98 | ✅ | deterministic headroom 3,000 = 3,000 -> PAY, but a lognormal stress breaches the floor in ~half the paths > 5% -> a person |
| N25 | 4 | roles | a viewer asks for a plan | plan | `{"decision": "BLOCKED"}` | 3.8 | 97.54 | 100.9 | ✅ | viewer -> BLOCKED |
| N26 | 4 | approvals bound to exact terms | an approval given on different terms is refused | execution | `{"execution": "REFUSED", "transfers": 0}` | 151.8 | n/a | n/a | ✅ | limit 0 -> REQUIRES_APPROVAL; the approval's terms_digest does not match the plan -> REFUSED, nothing moves |
| N27 | 4 | approver role | a viewer 'approves' | execution | `{"execution": "REFUSED", "transfers": 0}` | 170.8 | n/a | n/a | ✅ | role viewer is not in approver_roles -> REFUSED |
| N28 | 4 | no approval at all | REQUIRES_APPROVAL executed without a person | execution | `{"execution": "REFUSED", "transfers": 0}` | 210.6 | n/a | n/a | ✅ | no approval -> REFUSED |
| N29 | 5 | a timeout is unconfirmed | the transfer response is lost: recovered by request_id, paid once | execution | `{"execution": "COMPLETED", "transfers": 3, "invariants_ok": true}` | 522.4 | n/a | n/a | ✅ | the create succeeded on the rail; the retry finds it by request_id instead of creating a second transfer |
| N30 | 5 | rate limits | 429 storm on transfer create | execution | `{"execution": "COMPLETED", "transfers": 3}` | 442.5 | n/a | n/a | ✅ | backoff and retry with the same request_id -> every payout once |
| N31 | 5 | duplicate request id | the rail answers duplicate_request_id | execution | `{"execution": "FAILED", "message_contains": "duplicate_request_id"}` | 309.5 | n/a | n/a | ✅ | the operation is recorded FAILED and the claim released; nothing is sent again blindly |
| N32 | 5 | insufficient funds at the rail | wallet read says yes, rail says balance_insufficient | execution | `{"execution": "FAILED", "message_contains": "balance_insufficient"}` | 298.8 | n/a | n/a | ✅ | halt, reconcile, report; later payouts are not attempted |
| N33 | 5 | PENDING states | the conversion stays SCHEDULED: transfers are held | execution | `{"execution": "IN_FLIGHT", "transfers": 0}` | 230.7 | n/a | n/a | ✅ | money not yet in the payout currency -> no transfer; the run is resumable |
| N34 | 5 | CANCELLED does not mean a person cancelled | CANCELLED with BENEFICIARY_BANK_RETURNED | incident | `{"action": "REPLACE"}` | 0.0 | 2.27 | 4.9 | ✅ | funds returned, payee record not in question -> REPLACE under the duplicate lock with a new request_id |
| N35 | 5 | wrong payee details | CANCELLED with INVALID_ACCOUNT_NAME_OR_NUMBER | incident | `{"action": "ESCALATE"}` | 0.0 | 2.05 | 5.7 | ✅ | paying again pays the wrong account again -> ESCALATE |
| N36 | 5 | a person or control stopped it | CANCELLED with RECALL_REQUESTED | incident | `{"action": "ESCALATE"}` | 0.0 | 2.4 | 4.4 | ✅ | only a person may restart it |
| N37 | 5 | unknown failure shape | FAILED with no typed failure | incident | `{"action": "ESCALATE", "reason_contains": "unknown"}` | 0.0 | 1.81 | 3.9 | ✅ | fail closed: UNKNOWN -> ESCALATE (this is what the real sandbox did in cycle 5, run 1) |
| N38 | 5 | one replacement only | a second return after one replacement | incident | `{"action": "ESCALATE"}` | 0.0 | 2.46 | 4.1 | ✅ | max_replacements 1 reached -> a person decides |
| N39 | 5 | bank return after SENT | one payout fails after SENT: refund expected, fee lost | execution | `{"execution": "FAILED", "message_contains": "SHIPPING-UK", "invariants_ok": true}` | 464.6 | n/a | n/a | ✅ | reconciliation expects the amount back and the fee gone; the other payouts stand |
| N40 | 5 | every payout fails | all three payouts fail after SENT | execution | `{"execution": "FAILED", "invariants_ok": true}` | 460.1 | n/a | n/a | ✅ | three refunds expected, three fees lost, reconciled to the cent |
| N41 | 6 | without paying twice | the same invoice executed twice: second run refused before any money moves | execution | `{"execution": "FAILED", "message_contains": "duplicate payment blocked", "invariants_ok": true}` | 556.3 | n/a | n/a | ✅ | the payout claim per invoice fingerprint survives across executions |
| N42 | 6 | stale state | the wallet holds less than the plan assumes | execution | `{"plan": "BLOCKED", "execution": null}` | 115.6 | n/a | n/a | ✅ | live balances do not cover the plan -> BLOCKED at proof time; nothing executes |
| N43 | 6 | only foreign cash | base wallet already below the floor, foreign wallets cover the payouts | plan | `{"paid": ["O1", "O2"], "n_conversions": 0, "requires": ["reserve_breach_risk_above_tolerance"]}` | 8.1 | 106.79 | 95.2 | ✅ | nothing is spent from USD, so the floor (a rule on USD spend) is not breached; the 100% breach risk goes to a person |
| N44 | 6 | Kit 03 end to end | bank return -> replace under the lock -> resolved only when the original is recorded | execution | `{"incident": "RESOLVED", "incident_decision": "REPLACE", "invariants_ok": true}` | 279.8 | n/a | n/a | ✅ | returned funds -> REPLACE with a new request_id and the second fee budgeted; RESOLVED after the original's CANCELLED/type/refund are recorded |
| N45 | 6 | no money for the second fee | replacement would need a fee the wallet cannot cover | execution | `{"incident": "ESCALATED", "incident_decision": "REPLACE"}` | 229.0 | n/a | n/a | ✅ | REPLACE decided, but EUR 1,513.38 needed and EUR 1,512.85 refunded -> refused, ESCALATED |
| N46 | 6 | the biggest anomaly: everything at once | cash short, receipt contradicted, bank change, duplicate, SWIFT fee, viewer-free | plan | `{"actions": {"O1": "ESCALATE", "O2": "CONVERT_AND_PAY", "O3": "REJECT", "O4": "ESCALATE", "O5": "ESCALATE"}, "paid": ["O2"]}` | 9.9 | 105.98 | 132.2 | ✅ | headroom 8,000 - 5,000 = 3,000. O1 5,000 > 3,000 and the 60% receipt is not relied on -> ESCALATE. O2 EUR 2,012.85 incl. fee = USD 2,184 <= 3,000 -> CONVERT_AND_PAY. O3 same fingerprint as O2 -> REJECT. O4 bank-change flag -> ESCALATE. O5 1,000 > 3,000 - 2,184 = 816 (and O2 at priority 9 outranks O5 at 2) -> ESCALATE. Nothing else is paid. |

## Suite 1 — the rules, one by one

37 treasury plans and 17 intake emails that exercise one rule each (fit / do not fit / exactly at the limit, priorities, evidence, duplicates, receipts, limits scaling with confidence, roles).

| Path | Kind | Cases | Match | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|---|
| in-process | plan | 37 | 37 | 4.6 | 8.9 | 12.3 |
| endpoint | plan | 37 | 37 | 90.8 | 94.7 | 97.6 |
| UI form | plan | 37 | 37 | 88.8 | 94.0 | 186.7 |
| in-process | intake | 17 | 17 | 3.4 | 4.5 | 5.3 |
| endpoint | intake | 17 | 17 | 4.4 | 5.2 | 5.3 |
| UI form | intake | 17 | 17 | 6.8 | 9.0 | 11.0 |

Server-side plan time (`duration_ms`, includes the live Airwallex balance check, the 72 h simulation and the counterfactuals): p50 82.3 ms, p95 89.9 ms.

### Cases

| ID | Case | Kind | Expected | in-proc ms | endpoint ms | UI ms | Match | Derivation |
|---|---|---|---|---|---|---|---|---|
| P01 | single obligation fits | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 5.3 | 94.11 | 186.7 | ✅ | headroom 5,000 >= 3,000 -> PAY; nothing needs a person |
| P02 | single obligation does not fit, no receipt | plan | `{"actions": {"O1": "ESCALATE"}, "not_blocked": true}` | 4.6 | 90.77 | 94.5 | ✅ | 6,000 > headroom 5,000 and no receipt -> ESCALATE |
| P03 | obligation equals the headroom exactly | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 4.1 | 94.65 | 88 | ✅ | 5,000 <= 5,000 -> PAY (cash after = floor, allowed) |
| P04 | one cent over the headroom | plan | `{"actions": {"O1": "ESCALATE"}}` | 4.8 | 88.48 | 89.3 | ✅ | 5,000.01 > 5,000 -> cannot be funded -> ESCALATE |
| P05 | two obligations both fit | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY", "O2": "PAY"}}` | 4.5 | 91.25 | 88.5 | ✅ | 2,000 + 2,500 = 4,500 <= 5,000 -> both PAY |
| P06 | two obligations, only the higher priority fits | plan | `{"actions": {"O1": "PAY", "O2": "ESCALATE"}}` | 4.9 | 86.85 | 88.8 | ✅ | priority 10 first: 3,000 fits; 3,000 more would breach -> ESCALATE |
| P07 | same priority, earlier due first | plan | `{"actions": {"O2": "PAY", "O1": "ESCALATE"}}` | 4.6 | 96.05 | 92.2 | ✅ | tie on priority -> due 12h paid, due 48h cannot be funded |
| P08 | duplicate invoice fingerprint | plan | `{"actions": {"O1": "PAY", "O2": "REJECT"}}` | 4.1 | 86.42 | 88.8 | ✅ | same fingerprint -> second copy REJECT, never paid twice |
| P09 | evidence rejected | plan | `{"actions": {"O1": "REJECT"}}` | 3.4 | 88.59 | 88.9 | ✅ | rejected evidence -> REJECT |
| P10 | evidence pending | plan | `{"actions": {"O1": "REJECT"}}` | 3.9 | 88.5 | 86.7 | ✅ | pending evidence -> REJECT |
| P11 | evidence missing | plan | `{"actions": {"O1": "REJECT"}}` | 3.8 | 91.68 | 83.1 | ✅ | no document -> REJECT |
| P12 | evidence names another obligation | plan | `{"actions": {"O1": "REJECT"}}` | 3.5 | 85.45 | 80.7 | ✅ | document is about another subject -> REJECT |
| P13 | already settled record | plan | `{"actions": {"O1": "REJECT"}}` | 3.3 | 92.43 | 86.1 | ✅ | settled -> not a payable -> REJECT |
| P14 | payee bank details changed | plan | `{"actions": {"O1": "ESCALATE"}}` | 3.6 | 90.85 | 82.5 | ✅ | escalate_flags -> ESCALATE even though it fits |
| P15 | viewer cannot plan | plan | `{"decision": "BLOCKED"}` | 4.0 | 85.69 | 92.3 | ✅ | actor_role viewer -> BLOCKED |
| P16 | finance manager plans like the agent | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 3.7 | 87.99 | 91.1 | ✅ | role only changes who may approve |
| P17 | does not fit now, reliable receipt lands before due | plan | `{"actions": {"O1": "DEFER"}}` | 5.8 | 93.3 | 90.2 | ✅ | 7,000 > 5,000; receipt 8,000 at 12h (90% >= 80%, 12h <= 72h) before due 48h -> DEFER |
| P18 | receipt not reliable (60%) | plan | `{"actions": {"O1": "ESCALATE"}}` | 6.3 | 93.57 | 89.8 | ✅ | 60% < 80% -> not relied on -> ESCALATE |
| P19 | receipt lands after due | plan | `{"actions": {"O1": "ESCALATE"}}` | 6.2 | 93.7 | 91.5 | ✅ | receipt at 48h > due 24h -> ESCALATE |
| P20 | receipt window ends after the horizon | plan | `{"actions": {"O1": "ESCALATE"}}` | 11.6 | 90.47 | 94 | ✅ | latest arrival 100h > 72h horizon -> not reliable -> ESCALATE |
| P21 | receipt covers one of two deferrals | plan | `{"actions": {"O1": "PAY", "O2": "ESCALATE"}}` | 5.8 | 89.06 | 90.2 | ✅ | O1 fits (4,000 <= 5,000); O2 needs 4,000, receipt only 3,000 -> ESCALATE |
| P22 | EUR payout converted inside the limit | plan | `{"decision": "APPROVED", "actions": {"O1": "CONVERT_AND_PAY"}, "n_conversions": 1}` | 4.7 | 89.62 | 88.6 | ✅ | EUR 1,000 + 12.85 fee = EUR 1,012.85 ~ USD 1,099; headroom 15,000; limit 10,000 (no receipt -> full) -> autonomous |
| P23 | conversion above the autonomous limit | plan | `{"decision": "REQUIRES_APPROVAL", "actions": {"O1": "CONVERT_AND_PAY"}, "requires": ["conversion_above_autonomous_limit"]}` | 4.6 | 87.86 | 85.2 | ✅ | EUR 12,012.85 ~ USD 13,034 > limit 10,000 (headroom 25,000 is fine) -> a person approves |
| P24 | limit zero: every conversion needs a person | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["conversion_above_autonomous_limit"]}` | 4.1 | 89.74 | 88 | ✅ | any conversion > 0 -> REQUIRES_APPROVAL |
| P25 | EUR wallet already covers the EUR payout | plan | `{"decision": "APPROVED", "n_conversions": 0, "paid": ["O1"]}` | 3.7 | 90.67 | 86.7 | ✅ | EUR 2,000 >= 1,012.85 -> no conversion, cost 0 |
| P26 | GBP wallet covers part: convert the shortfall only | plan | `{"decision": "APPROVED", "conversions": {"GBP": 60000}}` | 5.1 | 91.18 | 86.6 | ✅ | need GBP 1,000 (LOCAL, no fee), have 400 -> buy exactly 600 |
| P27 | stress eats the headroom | plan | `{"actions": {"O1": "ESCALATE"}}` | 12.3 | 97.61 | 90.1 | ✅ | headroom = 10,000 - 5,000 - 3,000 = 2,000 < 3,000 -> ESCALATE |
| P28 | stress leaves exactly enough (deterministic) but no margin (probabilistic) | plan | `{"decision": "REQUIRES_APPROVAL", "actions": {"O1": "PAY"}, "requires": ["reserve_breach_risk_above_tolerance"]}` | 8.9 | 91.11 | 86.2 | ✅ | deterministic: headroom = 10,000 - 5,000 - 2,000 = 3,000 = 3,000 -> PAY. Probabilistic: the 72h simulation draws the unexpected outflow with a lognormal size around 2,000, so landing exactly on the floor breaches it in about half the paths, far above the 5% tolerance -> a person approves. (The first hand answer missed the second rule; recorded as such.) |
| P29 | zero reserve floor | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 4.0 | 91.09 | 85.9 | ✅ | floor 0: cash 1,000 pays 1,000 |
| P30 | zero cash | plan | `{"paid": [], "not_blocked": true}` | 4.9 | 94.71 | 85.2 | ✅ | nothing can be paid; the plan is still a plan (escalates), not BLOCKED |
| P31 | twelve small obligations all fit | plan | `{"decision": "APPROVED", "paid": ["O1", "O2", "O3", "O4", "O5", "O6", "O7", "O8", "O9", "O10", "O11", "O12"]}` | 5.4 | 93.34 | 92.8 | ✅ | 12 x 100 = 1,200 <= 5,000 -> all PAY |
| P32 | large amounts, still inside the live sandbox wallet | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 4.0 | 88.35 | 88.3 | ✅ | USD 9,000,000 cash, USD 1,000,000 obligation, floor 5,000 -> PAY. (Amounts above the live wallet, e.g. USD 10bn, are BLOCKED by the live-balance check when the service is connected to the sandbox: the plan is grounded in real money.) |
| P33 | confidence 90%: limit is full | plan | `{"decision": "APPROVED", "limit": 1000000}` | 5.3 | 91.28 | 85.2 | ✅ | conf 90% >= 90% -> limit 10,000; conversion ~USD 7,734 < 10,000 -> autonomous |
| P34 | confidence 70%: limit halves | plan | `{"decision": "REQUIRES_APPROVAL", "limit": 500000, "requires": ["conversion_above_autonomous_limit"]}` | 4.8 | 93.37 | 88.6 | ✅ | limit = 10,000 x (70-50)/(90-50) = 5,000; conversion ~7,734 > 5,000 -> a person |
| P35 | confidence 40%: limit zero | plan | `{"decision": "REQUIRES_APPROVAL", "limit": 0}` | 4.7 | 90.02 | 91.9 | ✅ | conf 40% <= 50% -> limit 0 -> any conversion needs a person |
| P36 | priority 10 due now and priority 1 due at the horizon both fit | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY", "O2": "PAY"}}` | 4.3 | 91.06 | 92.3 | ✅ | 4,000 <= 5,000 -> both PAY regardless of priority |
| P37 | cheapest obligation dropped, not the most urgent | plan | `{"actions": {"O1": "PAY", "O2": "ESCALATE"}}` | 5.0 | 88.52 | 88.9 | ✅ | 4,000 fits; 1,500 more = 5,500 > 5,000 -> O2 cannot be funded |
| I01 | registered mailbox, clean invoice | intake | `{"status": "VERIFIED", "trust": "registered", "kind": "supplier_invoice", "amount_minor": 125000, "currency": "USD", "invoice_no": "LD-90", "due_date": "2026-11-05"}` | 3.8 | 4.84 | 9 | ✅ | exact registered gmail address; amount + invoice quoted; no bank change |
| I02 | unknown mailbox with the same display name | intake | `{"status": "QUARANTINED", "trust": "unknown_sender", "claimed_name": "Lumen Design Studio"}` | 3.5 | 4.37 | 6.6 | ✅ | display name is not identity; unknown mailbox asking for money -> QUARANTINED |
| I03 | registered mailbox announcing new bank details | intake | `{"status": "NEEDS_REVIEW", "trust": "registered", "kind": "bank_change", "flags": ["payee_bank_details_changed"]}` | 3.8 | 3.99 | 7.1 | ✅ | registered sender but a bank change needs an out-of-band call-back -> NEEDS_REVIEW, never auto-payable |
| I04 | unknown mailbox, bank change, urgency | intake | `{"status": "QUARANTINED", "kind": "bank_change", "flags": ["payee_bank_details_changed", "urgency_pressure"]}` | 3.4 | 4.05 | 6.6 | ✅ | classic payment-diversion pattern -> QUARANTINED |
| I05 | registered mailbox but Reply-To elsewhere | intake | `{"status": "QUARANTINED", "trust": "reply_to_mismatch"}` | 3.4 | 5.26 | 6.2 | ✅ | Reply-To is not a registered mailbox -> reply_to_mismatch -> QUARANTINED |
| I06 | registered customer remittance (outlook) | intake | `{"status": "VERIFIED", "kind": "remittance_notice", "amount_minor": 200000, "due_date": "2026-10-30"}` | 3.1 | 5.18 | 5.6 | ✅ | registered payer mailbox + remittance language -> receivable |
| I07 | registered contractor timesheet (yahoo) | intake | `{"status": "VERIFIED", "kind": "contractor_claim", "amount_minor": 100000, "invoice_no": "OK-2026-11", "due_date": "2026-11-07"}` | 3.4 | 4.72 | 6.1 | ✅ | registered yahoo mailbox; amount is the total (largest figure); within 14 days of 24 Oct = 7 Nov |
| I08 | registered mailbox, no amount readable | intake | `{"status": "NEEDS_REVIEW", "trust": "registered"}` | 3.3 | 3.96 | 6.5 | ✅ | nothing to pay can be read -> a person looks |
| I09 | injection text cannot verify itself | intake | `{"status": "QUARANTINED", "trust": "unknown_sender"}` | 3.0 | 4.07 | 7.1 | ✅ | the text is data; trust comes from the registry only |
| I10 | registered sender with pressure language | intake | `{"status": "NEEDS_REVIEW", "flags": ["urgency_pressure"]}` | 3.0 | 3.27 | 6.9 | ✅ | registered, but pressure language -> a person confirms first |
| I11 | html body | intake | `{"status": "VERIFIED", "amount_minor": 32000, "invoice_no": "LD-97", "due_date": "2026-11-02"}` | 3.3 | 4.03 | 5.8 | ✅ | tags stripped; same reading |
| I12 | euro symbol and thousands separator | intake | `{"status": "VERIFIED", "amount_minor": 150000, "currency": "EUR", "due_date": "2026-11-10"}` | 4.5 | 4.7 | 7.1 | ✅ | € -> EUR; 1,500.00 -> 150000 minor |
| I13 | duplicate of an invoice already in the book | intake | `{"status": "DUPLICATE", "duplicate_of": "SHIPPING-UK"}` | 5.3 | 4.65 | 7.2 | ✅ | fingerprint thames-freight:TF-311 already in kit01_initial -> DUPLICATE |
| I14 | embargoed future date does not matter for triage, amount in subject-less body | intake | `{"status": "VERIFIED", "amount_minor": 1000, "invoice_no": "LD-99", "due_date": "2026-12-01"}` | 3.1 | 4.53 | 11 | ✅ | no subject; fields read from the body |
| I15 | corporate domain match (non-freemail) | intake | `{"status": "VERIFIED", "trust": "registered", "currency": "EUR", "amount_minor": 400000, "invoice_no": "INV-2026-0700"}` | 3.1 | 3.83 | 6.8 | ✅ | exact registered corporate mailbox -> registered |
| I16 | same corporate domain, different mailbox | intake | `{"status": "VERIFIED", "trust": "registered"}` | 3.8 | 4.61 | 7.4 | ✅ | registered domain (not freemail) -> registered |
| I17 | freemail lookalike of a corporate payee | intake | `{"status": "QUARANTINED", "trust": "unknown_sender", "claimed_name": "Rheinwerk Komponenten GmbH"}` | 3.1 | 3.89 | 6.8 | ✅ | freemail is never matched by domain |

## What the exercise changed

- **P28 / N24** — the hand answer first said APPROVED when the headroom exactly covers the payment. The service answered REQUIRES_APPROVAL: the 72-hour simulation draws the unexpected outflow with a lognormal size, so a plan that lands exactly on the floor breaches it in about half the paths, above the 5% tolerance. The probabilistic rule is the documented one; the expectation was corrected and the case kept.
- **P32** — USD 10 bn cash was BLOCKED by the live service (the sandbox wallet holds about USD 10 M) while passing in-process: a connected service grounds every plan in real balances. The case now uses amounts inside the wallet.
- **I04** — the lookalike's invoice LD-77 was already in the live intake queue, so the live service answered DUPLICATE instead of QUARANTINED: the queue is shared state and the duplicate lock applies to it. The case uses another invoice number.
- **N41** — paying the same invoices twice: a second *plan* of the same state after the first execution is BLOCKED by the live-balance check before the duplicate lock is even reached (the fake wallet is lower); the case tops the wallet up so that the lock itself is what refuses the second run (`duplicate payment blocked before any money moved`).
- **Harness** — execution cases set fake credentials in the environment; without restoring them, later plan cases saw a configured-but-unreachable Airwallex and were BLOCKED. Restored after each execution case.
