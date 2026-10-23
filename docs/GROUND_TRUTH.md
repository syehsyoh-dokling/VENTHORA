# Ground truth: hand-derived answers vs the live service

Generated 2026-10-23 16:55 by `python scripts/groundtruth_report.py`.

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
| in-process | plan | 17 | 17 | 7.5 | 17.7 | 217.1 |
| endpoint | plan | 17 | 17 | 106.0 | 175.6 | 413.7 |
| UI form | plan | 17 | 17 | 100.0 | 147.4 | 148.2 |
| in-process | intake | 6 | 6 | 5.8 | 6.8 | 9.5 |
| endpoint | intake | 6 | 6 | 6.2 | 6.5 | 7.3 |
| UI form | intake | 6 | 6 | 9.6 | 10.4 | 10.7 |
| in-process | incident | 7 | 7 | 0.0 | 0.1 | 0.1 |
| endpoint | incident | 7 | 7 | 2.0 | 2.4 | 2.5 |
| UI form | incident | 7 | 7 | 4.9 | 5.0 | 5.7 |
| in-process | execution | 16 | 16 | 377.1 | 693.8 | 785.7 |
| endpoint | execution | 16 | n/a (in-process only) | - | - | - |
| UI form | execution | 16 | n/a (in-process only) | - | - | - |

Server-side plan time (`duration_ms`, includes the live Airwallex balance check, the 72 h simulation and the counterfactuals): p50 92.1 ms, p95 183.6 ms.

### Cases

| ID | L | Pain (organizers) | Case | Kind | Expected | in-proc ms | endpoint ms | UI ms | Match | Derivation |
|---|---|---|---|---|---|---|---|---|---|---|
| N01 | 1 | insufficient funds mid-run: fees and FX buffers | the SWIFT fee is what breaks the reserve | plan | `{"actions": {"O1": "ESCALATE"}}` | 217.1 | 175.55 | 148.2 | ✅ | EUR 4,600 = USD 4,991 fits the 5,000 headroom; + EUR 12.85 fee (USD 13.94) = 5,005 > 5,000 -> cannot be funded -> ESCALATE |
| N02 | 1 | insufficient funds mid-run: fees | a learned LOCAL fee is what breaks the reserve | plan | `{"actions": {"O1": "ESCALATE"}}` | 4.9 | 413.67 | 147.4 | ✅ | 4,998 + learned fee 3.00 = 5,001 > 5,000 -> ESCALATE; the guide said LOCAL is free, the sandbox charges 3.00 |
| N03 | 1 | rounding | one-cent obligations are still money | plan | `{"decision": "APPROVED", "paid": ["O1", "O2", "O3"]}` | 4.5 | 146.49 | 110.2 | ✅ | 3 cents <= 5,000 -> all PAY; integer minor units, no float |
| N04 | 1 | stale quotes | planning rate stale: the rail is 3% dearer -> nothing booked | execution | `{"execution": "FAILED", "message_contains": "slippage", "conversions": 0, "transfers": 0}` | 357.4 | n/a | n/a | ✅ | quoted sell above the 1% slippage limit -> halt before any conversion or transfer; re-plan at the real rate |
| N05 | 1 | FX buffer | FX stress makes a 'fits exactly' conversion not fit | plan | `{"actions": {"O1": "ESCALATE"}}` | 7.3 | 114.43 | 107.1 | ✅ | EUR 4,620.85 incl. fee = USD 5,013.6 > 5,000 even before the Horizon stress buffer -> ESCALATE |
| N06 | 2 | obligations fall due faster than cash arrives | the receipt slips past the due date | plan | `{"actions": {"O1": "ESCALATE"}}` | 8.3 | 101.71 | 89.4 | ✅ | 7,000 > 5,000; receipt at 48h > due 24h -> cannot be funded before due -> ESCALATE |
| N07 | 2 | a receipt forecast is not a receipt | the 90% window ends after the planning horizon | plan | `{"actions": {"O1": "ESCALATE"}}` | 17.7 | 111.26 | 107.5 | ✅ | latest arrival 96h > 72h -> not relied on -> ESCALATE |
| N08 | 2 | deadline already here | due now, cash short, no receipt | plan | `{"actions": {"O1": "ESCALATE"}, "not_blocked": true}` | 7.2 | 96.08 | 101.8 | ✅ | headroom 1,000 < 3,000; due 0h -> ESCALATE now, not DEFER |
| N09 | 2 | delayed settlement | the deposit stays PENDING: the receipt is not counted | execution | `{"receipt_landed": false, "execution": "COMPLETED"}` | 693.8 | n/a | n/a | ✅ | the balance may lag; only a deposit record that says SETTLED counts; the plan proceeds without it |
| N10 | 2 | a contradicted forecast | payer with a bad record: 40% confidence drops the FX limit to zero | plan | `{"decision": "REQUIRES_APPROVAL", "limit": 0, "requires": ["conversion_above_autonomous_limit"]}` | 9.9 | 101.34 | 95.4 | ✅ | confidence 40% <= 50% -> limit 0 -> every conversion needs a person |
| N11 | 2 | in flight for too long | SENT for 49 h: the wait window is over, still not final | incident | `{"action": "ESCALATE"}` | 0.1 | 1.62 | 4.9 | ✅ | not terminal and 49h > 48h window -> a person chases the rail; replacing could pay twice |
| N12 | 2 | a timeout is unconfirmed | PROCESSING for 1 h: wait, never replace | incident | `{"action": "WAIT"}` | 0.0 | 1.68 | 5 | ✅ | not final, inside the window -> WAIT |
| N13 | 3 | evidence requirements | evidence about the wrong obligation | plan | `{"actions": {"O1": "REJECT"}}` | 6.9 | 101.91 | 87.8 | ✅ | the document names another subject -> REJECT |
| N14 | 3 | duplicate invoice | the same invoice re-sent under a new id | plan | `{"actions": {"O1": "PAY", "O2": "REJECT"}}` | 6.6 | 100.38 | 94.5 | ✅ | same fingerprint -> the copy is REJECT |
| N15 | 3 | changed terms | payee announces new bank details | plan | `{"actions": {"O1": "ESCALATE"}}` | 7.2 | 112.32 | 89.7 | ✅ | policy exception -> ESCALATE even though cash covers it |
| N16 | 3 | payment diversion | lookalike mailbox, new bank account, urgency | intake | `{"status": "QUARANTINED", "trust": "unknown_sender", "flags": ["payee_bank_details_changed", "urgency_pressure"]}` | 9.5 | 6.5 | 9.3 | ✅ | trust comes from the registry, not the display name; money + bank change from an unknown mailbox -> QUARANTINED |
| N17 | 3 | prompt injection | the email tells the agent it is verified | intake | `{"status": "QUARANTINED"}` | 5.7 | 5.6 | 8.2 | ✅ | the text is data; status is decided by code |
| N18 | 3 | reply-to diversion | registered mailbox, Reply-To elsewhere | intake | `{"status": "QUARANTINED", "trust": "reply_to_mismatch"}` | 4.6 | 6.06 | 9.2 | ✅ | Reply-To is not registered -> QUARANTINED |
| N19 | 3 | bank change from the real payee | registered mailbox announces a new account | intake | `{"status": "NEEDS_REVIEW", "kind": "bank_change"}` | 5.5 | 6.15 | 10.7 | ✅ | even the real payee's bank change needs an out-of-band call-back |
| N20 | 3 | duplicate reminder | a reminder for an invoice already in the book | intake | `{"status": "DUPLICATE", "duplicate_of": "SHIPPING-UK"}` | 6.8 | 7.31 | 10.4 | ✅ | fingerprint already in kit01_initial -> DUPLICATE |
| N21 | 3 | pressure language from a real payee | registered sender, 'the CEO approved, pay immediately' | intake | `{"status": "NEEDS_REVIEW", "flags": ["urgency_pressure"]}` | 6.0 | 6.33 | 9.9 | ✅ | pressure language -> a person confirms before it is payable |
| N22 | 4 | limits | conversion above the autonomous limit | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["conversion_above_autonomous_limit"]}` | 13.6 | 97.0 | 96.3 | ✅ | USD ~13,034 > limit 10,000 -> a person |
| N23 | 4 | limits scale with confidence | 70% confidence halves the limit and the conversion no longer fits | plan | `{"decision": "REQUIRES_APPROVAL", "limit": 500000}` | 8.1 | 96.79 | 100 | ✅ | limit = 10,000 x (70-50)/40 = 5,000 < ~7,734 |
| N24 | 4 | risk tolerance | the plan lands exactly on the floor: probabilistic breach risk | plan | `{"actions": {"O1": "PAY"}, "requires": ["reserve_breach_risk_above_tolerance"]}` | 13.9 | 109.57 | 98 | ✅ | deterministic headroom 3,000 = 3,000 -> PAY, but a lognormal stress breaches the floor in ~half the paths > 5% -> a person |
| N25 | 4 | roles | a viewer asks for a plan | plan | `{"decision": "BLOCKED"}` | 6.6 | 97.54 | 100.9 | ✅ | viewer -> BLOCKED |
| N26 | 4 | approvals bound to exact terms | an approval given on different terms is refused | execution | `{"execution": "REFUSED", "transfers": 0}` | 188.0 | n/a | n/a | ✅ | limit 0 -> REQUIRES_APPROVAL; the approval's terms_digest does not match the plan -> REFUSED, nothing moves |
| N27 | 4 | approver role | a viewer 'approves' | execution | `{"execution": "REFUSED", "transfers": 0}` | 184.9 | n/a | n/a | ✅ | role viewer is not in approver_roles -> REFUSED |
| N28 | 4 | no approval at all | REQUIRES_APPROVAL executed without a person | execution | `{"execution": "REFUSED", "transfers": 0}` | 174.7 | n/a | n/a | ✅ | no approval -> REFUSED |
| N29 | 5 | a timeout is unconfirmed | the transfer response is lost: recovered by request_id, paid once | execution | `{"execution": "COMPLETED", "transfers": 3, "invariants_ok": true}` | 520.8 | n/a | n/a | ✅ | the create succeeded on the rail; the retry finds it by request_id instead of creating a second transfer |
| N30 | 5 | rate limits | 429 storm on transfer create | execution | `{"execution": "COMPLETED", "transfers": 3}` | 566.9 | n/a | n/a | ✅ | backoff and retry with the same request_id -> every payout once |
| N31 | 5 | duplicate request id | the rail answers duplicate_request_id | execution | `{"execution": "FAILED", "message_contains": "duplicate_request_id"}` | 396.9 | n/a | n/a | ✅ | the operation is recorded FAILED and the claim released; nothing is sent again blindly |
| N32 | 5 | insufficient funds at the rail | wallet read says yes, rail says balance_insufficient | execution | `{"execution": "FAILED", "message_contains": "balance_insufficient"}` | 415.8 | n/a | n/a | ✅ | halt, reconcile, report; later payouts are not attempted |
| N33 | 5 | PENDING states | the conversion stays SCHEDULED: transfers are held | execution | `{"execution": "IN_FLIGHT", "transfers": 0}` | 313.2 | n/a | n/a | ✅ | money not yet in the payout currency -> no transfer; the run is resumable |
| N34 | 5 | CANCELLED does not mean a person cancelled | CANCELLED with BENEFICIARY_BANK_RETURNED | incident | `{"action": "REPLACE"}` | 0.1 | 2.27 | 4.9 | ✅ | funds returned, payee record not in question -> REPLACE under the duplicate lock with a new request_id |
| N35 | 5 | wrong payee details | CANCELLED with INVALID_ACCOUNT_NAME_OR_NUMBER | incident | `{"action": "ESCALATE"}` | 0.0 | 2.05 | 5.7 | ✅ | paying again pays the wrong account again -> ESCALATE |
| N36 | 5 | a person or control stopped it | CANCELLED with RECALL_REQUESTED | incident | `{"action": "ESCALATE"}` | 0.0 | 2.4 | 4.4 | ✅ | only a person may restart it |
| N37 | 5 | unknown failure shape | FAILED with no typed failure | incident | `{"action": "ESCALATE", "reason_contains": "unknown"}` | 0.0 | 1.81 | 3.9 | ✅ | fail closed: UNKNOWN -> ESCALATE (this is what the real sandbox did in cycle 5, run 1) |
| N38 | 5 | one replacement only | a second return after one replacement | incident | `{"action": "ESCALATE"}` | 0.0 | 2.46 | 4.1 | ✅ | max_replacements 1 reached -> a person decides |
| N39 | 5 | bank return after SENT | one payout fails after SENT: refund expected, fee lost | execution | `{"execution": "FAILED", "message_contains": "SHIPPING-UK", "invariants_ok": true}` | 785.7 | n/a | n/a | ✅ | reconciliation expects the amount back and the fee gone; the other payouts stand |
| N40 | 5 | every payout fails | all three payouts fail after SENT | execution | `{"execution": "FAILED", "invariants_ok": true}` | 628.6 | n/a | n/a | ✅ | three refunds expected, three fees lost, reconciled to the cent |
| N41 | 6 | without paying twice | the same invoice executed twice: second run refused before any money moves | execution | `{"execution": "FAILED", "message_contains": "duplicate payment blocked", "invariants_ok": true}` | 608.5 | n/a | n/a | ✅ | the payout claim per invoice fingerprint survives across executions |
| N42 | 6 | stale state | the wallet holds less than the plan assumes | execution | `{"plan": "BLOCKED", "execution": null}` | 126.6 | n/a | n/a | ✅ | live balances do not cover the plan -> BLOCKED at proof time; nothing executes |
| N43 | 6 | only foreign cash | base wallet already below the floor, foreign wallets cover the payouts | plan | `{"paid": ["O1", "O2"], "n_conversions": 0, "requires": ["reserve_breach_risk_above_tolerance"]}` | 7.5 | 106.79 | 95.2 | ✅ | nothing is spent from USD, so the floor (a rule on USD spend) is not breached; the 100% breach risk goes to a person |
| N44 | 6 | Kit 03 end to end | bank return -> replace under the lock -> resolved only when the original is recorded | execution | `{"incident": "RESOLVED", "incident_decision": "REPLACE", "invariants_ok": true}` | 341.4 | n/a | n/a | ✅ | returned funds -> REPLACE with a new request_id and the second fee budgeted; RESOLVED after the original's CANCELLED/type/refund are recorded |
| N45 | 6 | no money for the second fee | replacement would need a fee the wallet cannot cover | execution | `{"incident": "ESCALATED", "incident_decision": "REPLACE"}` | 269.9 | n/a | n/a | ✅ | REPLACE decided, but EUR 1,513.38 needed and EUR 1,512.85 refunded -> refused, ESCALATED |
| N46 | 6 | the biggest anomaly: everything at once | cash short, receipt contradicted, bank change, duplicate, SWIFT fee, viewer-free | plan | `{"actions": {"O1": "ESCALATE", "O2": "CONVERT_AND_PAY", "O3": "REJECT", "O4": "ESCALATE", "O5": "ESCALATE"}, "paid": ["O2"]}` | 10.9 | 105.98 | 132.2 | ✅ | headroom 8,000 - 5,000 = 3,000. O1 5,000 > 3,000 and the 60% receipt is not relied on -> ESCALATE. O2 EUR 2,012.85 incl. fee = USD 2,184 <= 3,000 -> CONVERT_AND_PAY. O3 same fingerprint as O2 -> REJECT. O4 bank-change flag -> ESCALATE. O5 1,000 > 3,000 - 2,184 = 816 (and O2 at priority 9 outranks O5 at 2) -> ESCALATE. Nothing else is paid. |

## Suite 3 — Budget · mandate: the period's plan is the agent's mandate

20 cases around every threshold of a budget line: inside the auto share, exactly the mandate, one cent over (the agent stops and raises a BUDGET REQUEST with the exact shortfall), above the auto share (a person), the alert threshold with and without require_approval_on_alert, realized spend eating the line, a grant restoring it, two lines, priority inside a line, unbudgeted obligations, bypass signed or not, the whole November book against the November plan, minus from what is still owed, a viewer, and no budget at all. Real-sandbox cycle 8 (SANDBOX_RESULTS.md) runs the same mandate end to end: pay inside the line, stop at it, grant, continue.

| Path | Kind | Cases | Match | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|---|
| in-process | plan | 20 | 20 | 5.9 | 7.5 | 8.4 |
| endpoint | plan | 20 | 20 | 94.9 | 116.5 | 122.0 |
| UI form | plan | 20 | 20 | 102.5 | 163.0 | 378.1 |

Server-side plan time (`duration_ms`, includes the live Airwallex balance check, the 72 h simulation and the counterfactuals): p50 82.1 ms, p95 341.2 ms.

### Cases

| ID | L | Pain (organizers) | Case | Kind | Expected | in-proc ms | endpoint ms | UI ms | Match | Derivation |
|---|---|---|---|---|---|---|---|---|---|---|
| B01 | 4 | the agent acts within its mandate | inside the line, inside the auto share | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "budget_status": {"suppliers": "auto"}, "requires": []}` | 8.4 | 95.68 | 378.1 | ✅ | 3,000 of 10,000 = 30% < alert 80%, < auto 100% -> PAY autonomously |
| B02 | 4 | the agent acts within its mandate | exactly the mandate | plan | `{"actions": {"O1": "PAY"}, "budget_available": {"suppliers": 0}, "requires": []}` | 6.5 | 116.48 | 114.4 | ✅ | 10,000 = mandate -> cap allows it (<=); utilisation 100% >= alert 80% -> ALERT, but the line does not require approval on alert -> APPROVED with an alert |
| B03 | 4 | the agent acts within its mandate | one cent over the mandate: the agent stops at the line | plan | `{"actions": {"O1": "ESCALATE"}, "paid": [], "budget_requests": {"suppliers": 1}}` | 7.3 | 101.61 | 97.9 | ✅ | cap = 10,000; 10,000.01 does not fit -> ESCALATE with a budget request for the shortfall 0.01 |
| B04 | 4 | the agent acts within its mandate | above the auto share needs a person | plan | `{"decision": "REQUIRES_APPROVAL", "actions": {"O1": "PAY"}, "requires": ["budget_line_above_auto_share"]}` | 6.3 | 91.63 | 107.6 | ✅ | 8,500 > 10,000 x 80% = 8,000 -> payable, but a person approves |
| B05 | 4 | the agent acts within its mandate | exactly the auto share is still autonomous | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "requires": []}` | 5.6 | 85.93 | 99.4 | ✅ | 8,000 = 80% of 10,000: not above -> autonomous; 80% < alert 90% -> no alert |
| B06 | 4 | the agent acts within its mandate | alert threshold reached (notification, still autonomous) | plan | `{"decision": "APPROVED", "budget_alerts": 1, "budget_status": {"suppliers": "alert"}}` | 5.1 | 95.71 | 102.1 | ✅ | 82% >= alert 80% -> ALERT raised; auto share 100% -> no approval needed |
| B07 | 4 | the agent acts within its mandate | alert threshold with require_approval_on_alert | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["budget_line_alert_requires_approval"], "budget_alerts": 1}` | 4.9 | 96.9 | 163 | ✅ | 82% >= 80% and the line demands a person at the alert -> REQUIRES_APPROVAL |
| B08 | 4 | the agent acts within its mandate | realized spend eats the line | plan | `{"actions": {"O1": "ESCALATE"}, "budget_requests": {"suppliers": 1}}` | 5.3 | 95.37 | 101.6 | ✅ | cap = 10,000 - 7,500 = 2,500 < 3,000 -> ESCALATE + request for 500 |
| B09 | 4 | the agent acts within its mandate | a grant restores the mandate | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "budget_available": {"suppliers": 50000}}` | 5.6 | 94.61 | 102.5 | ✅ | mandate 11,000 - realized 7,500 = 3,500 >= 3,000 -> PAY; available after = 500 |
| B10 | 4 | the agent acts within its mandate | two lines, one exhausted, the other fine | plan | `{"actions": {"O1": "ESCALATE", "O2": "PAY"}, "budget_requests": {"suppliers": 1}}` | 7.5 | 122.03 | 105.7 | ✅ | suppliers cap 2,500 < 3,000 -> ESCALATE + request 500; saas 2,000 of 5,000 -> PAY |
| B11 | 4 | the agent acts within its mandate | priority within a line: the line pays the urgent one | plan | `{"actions": {"O1": "PAY", "O2": "ESCALATE"}, "budget_requests": {"suppliers": 1}}` | 7.4 | 101.13 | 105.2 | ✅ | both fit the cash, only one fits the line (6,000 + 6,000 > 10,000): priority 10 paid, the other raises a request for 2,000 |
| B12 | 4 | the agent acts within its mandate | unbudgeted obligation needs a person | plan | `{"decision": "REQUIRES_APPROVAL", "actions": {"O1": "PAY"}, "requires": ["unbudgeted_obligation"]}` | 5.5 | 90.69 | 100.2 | ✅ | 'marketing' is no line of 2026-11 -> payable by cash, but a person must place it |
| B13 | 4 | the agent acts within its mandate | unbudgeted allowed by the budget's setting | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "requires": []}` | 5.6 | 94.64 | 104.1 | ✅ | the budget says unbudgeted obligations do not need a person |
| B14 | 4 | the agent acts within its mandate | bypass nobody signed keeps the cap | plan | `{"actions": {"O1": "ESCALATE"}, "budget_requests": {"suppliers": 1}}` | 6.5 | 95.1 | 100.5 | ✅ | unsigned bypass -> cap 2,000 applies -> ESCALATE + request |
| B15 | 4 | the agent acts within its mandate | signed bypass lifts the cap | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "budget_status": {"suppliers": "bypass"}, "requires": []}` | 5.0 | 92.92 | 98.1 | ✅ | signed bypass -> no cap, no approval reason; the line shows as bypass |
| B16 | 4 | the agent acts within its mandate | bypass signed but spending inside it is still recorded | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 5.0 | 89.23 | 102.9 | ✅ | realized 9,000 on a 2,000 line with a signed bypass: still payable; the pipeline shows it, nobody stops it |
| B17 | 4 | the agent acts within its mandate | the whole November book against the November plan | plan | `{"decision": "APPROVED", "paid": ["O1", "O2", "O3", "O4", "O5"], "budget_alerts": 1, "requires": []}` | 6.7 | 91.67 | 104.3 | ✅ | every line fits: payroll 89% (< alert 90%), logistics 75%, suppliers 57%, contractors 50% (< 60% alert), saas 80% = alert -> one alert, no approval |
| B18 | 4 | the agent acts within its mandate | minus from what is still owed | plan | `{"actions": {"O1": "PAY", "O2": "ESCALATE"}, "budget_minus": {"suppliers": 200000}, "budget_requests": {"suppliers": 1}}` | 7.1 | 94.55 | 97.1 | ✅ | 7,000 paid; 5,000 still owed; 12,000 - 10,000 = minus 2,000 -> request for 2,000 |
| B19 | 4 | the agent acts within its mandate | viewer with a budget is still blocked | plan | `{"decision": "BLOCKED"}` | 6.2 | 97.83 | 101.2 | ✅ | actor_role viewer -> BLOCKED regardless of budget |
| B20 | 4 | the agent acts within its mandate | no budget attached: nothing changes | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "budget_attached": false}` | 5.6 | 92.26 | 102.4 | ✅ | without a budget the plan is the old plan |

## Suite 1 — the rules, one by one

37 treasury plans and 17 intake emails that exercise one rule each (fit / do not fit / exactly at the limit, priorities, evidence, duplicates, receipts, limits scaling with confidence, roles).

| Path | Kind | Cases | Match | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|---|
| in-process | plan | 37 | 37 | 7.0 | 11.3 | 17.3 |
| endpoint | plan | 37 | 37 | 90.8 | 94.7 | 97.6 |
| UI form | plan | 37 | 37 | 88.8 | 94.0 | 186.7 |
| in-process | intake | 17 | 17 | 4.2 | 5.3 | 9.1 |
| endpoint | intake | 17 | 17 | 4.4 | 5.2 | 5.3 |
| UI form | intake | 17 | 17 | 6.8 | 9.0 | 11.0 |

Server-side plan time (`duration_ms`, includes the live Airwallex balance check, the 72 h simulation and the counterfactuals): p50 82.3 ms, p95 89.9 ms.

### Cases

| ID | Case | Kind | Expected | in-proc ms | endpoint ms | UI ms | Match | Derivation |
|---|---|---|---|---|---|---|---|---|
| P01 | single obligation fits | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 7.0 | 94.11 | 186.7 | ✅ | headroom 5,000 >= 3,000 -> PAY; nothing needs a person |
| P02 | single obligation does not fit, no receipt | plan | `{"actions": {"O1": "ESCALATE"}, "not_blocked": true}` | 8.9 | 90.77 | 94.5 | ✅ | 6,000 > headroom 5,000 and no receipt -> ESCALATE |
| P03 | obligation equals the headroom exactly | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 9.0 | 94.65 | 88 | ✅ | 5,000 <= 5,000 -> PAY (cash after = floor, allowed) |
| P04 | one cent over the headroom | plan | `{"actions": {"O1": "ESCALATE"}}` | 10.6 | 88.48 | 89.3 | ✅ | 5,000.01 > 5,000 -> cannot be funded -> ESCALATE |
| P05 | two obligations both fit | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY", "O2": "PAY"}}` | 11.3 | 91.25 | 88.5 | ✅ | 2,000 + 2,500 = 4,500 <= 5,000 -> both PAY |
| P06 | two obligations, only the higher priority fits | plan | `{"actions": {"O1": "PAY", "O2": "ESCALATE"}}` | 10.0 | 86.85 | 88.8 | ✅ | priority 10 first: 3,000 fits; 3,000 more would breach -> ESCALATE |
| P07 | same priority, earlier due first | plan | `{"actions": {"O2": "PAY", "O1": "ESCALATE"}}` | 10.0 | 96.05 | 92.2 | ✅ | tie on priority -> due 12h paid, due 48h cannot be funded |
| P08 | duplicate invoice fingerprint | plan | `{"actions": {"O1": "PAY", "O2": "REJECT"}}` | 7.2 | 86.42 | 88.8 | ✅ | same fingerprint -> second copy REJECT, never paid twice |
| P09 | evidence rejected | plan | `{"actions": {"O1": "REJECT"}}` | 5.9 | 88.59 | 88.9 | ✅ | rejected evidence -> REJECT |
| P10 | evidence pending | plan | `{"actions": {"O1": "REJECT"}}` | 7.0 | 88.5 | 86.7 | ✅ | pending evidence -> REJECT |
| P11 | evidence missing | plan | `{"actions": {"O1": "REJECT"}}` | 6.7 | 91.68 | 83.1 | ✅ | no document -> REJECT |
| P12 | evidence names another obligation | plan | `{"actions": {"O1": "REJECT"}}` | 6.5 | 85.45 | 80.7 | ✅ | document is about another subject -> REJECT |
| P13 | already settled record | plan | `{"actions": {"O1": "REJECT"}}` | 5.4 | 92.43 | 86.1 | ✅ | settled -> not a payable -> REJECT |
| P14 | payee bank details changed | plan | `{"actions": {"O1": "ESCALATE"}}` | 6.7 | 90.85 | 82.5 | ✅ | escalate_flags -> ESCALATE even though it fits |
| P15 | viewer cannot plan | plan | `{"decision": "BLOCKED"}` | 6.4 | 85.69 | 92.3 | ✅ | actor_role viewer -> BLOCKED |
| P16 | finance manager plans like the agent | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 6.3 | 87.99 | 91.1 | ✅ | role only changes who may approve |
| P17 | does not fit now, reliable receipt lands before due | plan | `{"actions": {"O1": "DEFER"}}` | 10.1 | 93.3 | 90.2 | ✅ | 7,000 > 5,000; receipt 8,000 at 12h (90% >= 80%, 12h <= 72h) before due 48h -> DEFER |
| P18 | receipt not reliable (60%) | plan | `{"actions": {"O1": "ESCALATE"}}` | 7.3 | 93.57 | 89.8 | ✅ | 60% < 80% -> not relied on -> ESCALATE |
| P19 | receipt lands after due | plan | `{"actions": {"O1": "ESCALATE"}}` | 7.9 | 93.7 | 91.5 | ✅ | receipt at 48h > due 24h -> ESCALATE |
| P20 | receipt window ends after the horizon | plan | `{"actions": {"O1": "ESCALATE"}}` | 17.3 | 90.47 | 94 | ✅ | latest arrival 100h > 72h horizon -> not reliable -> ESCALATE |
| P21 | receipt covers one of two deferrals | plan | `{"actions": {"O1": "PAY", "O2": "ESCALATE"}}` | 8.1 | 89.06 | 90.2 | ✅ | O1 fits (4,000 <= 5,000); O2 needs 4,000, receipt only 3,000 -> ESCALATE |
| P22 | EUR payout converted inside the limit | plan | `{"decision": "APPROVED", "actions": {"O1": "CONVERT_AND_PAY"}, "n_conversions": 1}` | 6.6 | 89.62 | 88.6 | ✅ | EUR 1,000 + 12.85 fee = EUR 1,012.85 ~ USD 1,099; headroom 15,000; limit 10,000 (no receipt -> full) -> autonomous |
| P23 | conversion above the autonomous limit | plan | `{"decision": "REQUIRES_APPROVAL", "actions": {"O1": "CONVERT_AND_PAY"}, "requires": ["conversion_above_autonomous_limit"]}` | 6.5 | 87.86 | 85.2 | ✅ | EUR 12,012.85 ~ USD 13,034 > limit 10,000 (headroom 25,000 is fine) -> a person approves |
| P24 | limit zero: every conversion needs a person | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["conversion_above_autonomous_limit"]}` | 5.5 | 89.74 | 88 | ✅ | any conversion > 0 -> REQUIRES_APPROVAL |
| P25 | EUR wallet already covers the EUR payout | plan | `{"decision": "APPROVED", "n_conversions": 0, "paid": ["O1"]}` | 6.4 | 90.67 | 86.7 | ✅ | EUR 2,000 >= 1,012.85 -> no conversion, cost 0 |
| P26 | GBP wallet covers part: convert the shortfall only | plan | `{"decision": "APPROVED", "conversions": {"GBP": 60000}}` | 5.6 | 91.18 | 86.6 | ✅ | need GBP 1,000 (LOCAL, no fee), have 400 -> buy exactly 600 |
| P27 | stress eats the headroom | plan | `{"actions": {"O1": "ESCALATE"}}` | 15.8 | 97.61 | 90.1 | ✅ | headroom = 10,000 - 5,000 - 3,000 = 2,000 < 3,000 -> ESCALATE |
| P28 | stress leaves exactly enough (deterministic) but no margin (probabilistic) | plan | `{"decision": "REQUIRES_APPROVAL", "actions": {"O1": "PAY"}, "requires": ["reserve_breach_risk_above_tolerance"]}` | 10.5 | 91.11 | 86.2 | ✅ | deterministic: headroom = 10,000 - 5,000 - 2,000 = 3,000 = 3,000 -> PAY. Probabilistic: the 72h simulation draws the unexpected outflow with a lognormal size around 2,000, so landing exactly on the floor breaches it in about half the paths, far above the 5% tolerance -> a person approves. (The first hand answer missed the second rule; recorded as such.) |
| P29 | zero reserve floor | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 5.9 | 91.09 | 85.9 | ✅ | floor 0: cash 1,000 pays 1,000 |
| P30 | zero cash | plan | `{"paid": [], "not_blocked": true}` | 7.1 | 94.71 | 85.2 | ✅ | nothing can be paid; the plan is still a plan (escalates), not BLOCKED |
| P31 | twelve small obligations all fit | plan | `{"decision": "APPROVED", "paid": ["O1", "O2", "O3", "O4", "O5", "O6", "O7", "O8", "O9", "O10", "O11", "O12"]}` | 7.1 | 93.34 | 92.8 | ✅ | 12 x 100 = 1,200 <= 5,000 -> all PAY |
| P32 | large amounts, still inside the live sandbox wallet | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 5.3 | 88.35 | 88.3 | ✅ | USD 9,000,000 cash, USD 1,000,000 obligation, floor 5,000 -> PAY. (Amounts above the live wallet, e.g. USD 10bn, are BLOCKED by the live-balance check when the service is connected to the sandbox: the plan is grounded in real money.) |
| P33 | confidence 90%: limit is full | plan | `{"decision": "APPROVED", "limit": 1000000}` | 6.8 | 91.28 | 85.2 | ✅ | conf 90% >= 90% -> limit 10,000; conversion ~USD 7,734 < 10,000 -> autonomous |
| P34 | confidence 70%: limit halves | plan | `{"decision": "REQUIRES_APPROVAL", "limit": 500000, "requires": ["conversion_above_autonomous_limit"]}` | 6.2 | 93.37 | 88.6 | ✅ | limit = 10,000 x (70-50)/(90-50) = 5,000; conversion ~7,734 > 5,000 -> a person |
| P35 | confidence 40%: limit zero | plan | `{"decision": "REQUIRES_APPROVAL", "limit": 0}` | 7.3 | 90.02 | 91.9 | ✅ | conf 40% <= 50% -> limit 0 -> any conversion needs a person |
| P36 | priority 10 due now and priority 1 due at the horizon both fit | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY", "O2": "PAY"}}` | 5.6 | 91.06 | 92.3 | ✅ | 4,000 <= 5,000 -> both PAY regardless of priority |
| P37 | cheapest obligation dropped, not the most urgent | plan | `{"actions": {"O1": "PAY", "O2": "ESCALATE"}}` | 6.3 | 88.52 | 88.9 | ✅ | 4,000 fits; 1,500 more = 5,500 > 5,000 -> O2 cannot be funded |
| I01 | registered mailbox, clean invoice | intake | `{"status": "VERIFIED", "trust": "registered", "kind": "supplier_invoice", "amount_minor": 125000, "currency": "USD", "invoice_no": "LD-90", "due_date": "2026-11-05"}` | 4.3 | 4.84 | 9 | ✅ | exact registered gmail address; amount + invoice quoted; no bank change |
| I02 | unknown mailbox with the same display name | intake | `{"status": "QUARANTINED", "trust": "unknown_sender", "claimed_name": "Lumen Design Studio"}` | 3.5 | 4.37 | 6.6 | ✅ | display name is not identity; unknown mailbox asking for money -> QUARANTINED |
| I03 | registered mailbox announcing new bank details | intake | `{"status": "NEEDS_REVIEW", "trust": "registered", "kind": "bank_change", "flags": ["payee_bank_details_changed"]}` | 3.7 | 3.99 | 7.1 | ✅ | registered sender but a bank change needs an out-of-band call-back -> NEEDS_REVIEW, never auto-payable |
| I04 | unknown mailbox, bank change, urgency | intake | `{"status": "QUARANTINED", "kind": "bank_change", "flags": ["payee_bank_details_changed", "urgency_pressure"]}` | 3.2 | 4.05 | 6.6 | ✅ | classic payment-diversion pattern -> QUARANTINED |
| I05 | registered mailbox but Reply-To elsewhere | intake | `{"status": "QUARANTINED", "trust": "reply_to_mismatch"}` | 3.8 | 5.26 | 6.2 | ✅ | Reply-To is not a registered mailbox -> reply_to_mismatch -> QUARANTINED |
| I06 | registered customer remittance (outlook) | intake | `{"status": "VERIFIED", "kind": "remittance_notice", "amount_minor": 200000, "due_date": "2026-10-30"}` | 3.9 | 5.18 | 5.6 | ✅ | registered payer mailbox + remittance language -> receivable |
| I07 | registered contractor timesheet (yahoo) | intake | `{"status": "VERIFIED", "kind": "contractor_claim", "amount_minor": 100000, "invoice_no": "OK-2026-11", "due_date": "2026-11-07"}` | 4.2 | 4.72 | 6.1 | ✅ | registered yahoo mailbox; amount is the total (largest figure); within 14 days of 24 Oct = 7 Nov |
| I08 | registered mailbox, no amount readable | intake | `{"status": "NEEDS_REVIEW", "trust": "registered"}` | 4.7 | 3.96 | 6.5 | ✅ | nothing to pay can be read -> a person looks |
| I09 | injection text cannot verify itself | intake | `{"status": "QUARANTINED", "trust": "unknown_sender"}` | 4.6 | 4.07 | 7.1 | ✅ | the text is data; trust comes from the registry only |
| I10 | registered sender with pressure language | intake | `{"status": "NEEDS_REVIEW", "flags": ["urgency_pressure"]}` | 4.5 | 3.27 | 6.9 | ✅ | registered, but pressure language -> a person confirms first |
| I11 | html body | intake | `{"status": "VERIFIED", "amount_minor": 32000, "invoice_no": "LD-97", "due_date": "2026-11-02"}` | 3.7 | 4.03 | 5.8 | ✅ | tags stripped; same reading |
| I12 | euro symbol and thousands separator | intake | `{"status": "VERIFIED", "amount_minor": 150000, "currency": "EUR", "due_date": "2026-11-10"}` | 5.3 | 4.7 | 7.1 | ✅ | € -> EUR; 1,500.00 -> 150000 minor |
| I13 | duplicate of an invoice already in the book | intake | `{"status": "DUPLICATE", "duplicate_of": "SHIPPING-UK"}` | 9.1 | 4.65 | 7.2 | ✅ | fingerprint thames-freight:TF-311 already in kit01_initial -> DUPLICATE |
| I14 | embargoed future date does not matter for triage, amount in subject-less body | intake | `{"status": "VERIFIED", "amount_minor": 1000, "invoice_no": "LD-99", "due_date": "2026-12-01"}` | 4.0 | 4.53 | 11 | ✅ | no subject; fields read from the body |
| I15 | corporate domain match (non-freemail) | intake | `{"status": "VERIFIED", "trust": "registered", "currency": "EUR", "amount_minor": 400000, "invoice_no": "INV-2026-0700"}` | 4.0 | 3.83 | 6.8 | ✅ | exact registered corporate mailbox -> registered |
| I16 | same corporate domain, different mailbox | intake | `{"status": "VERIFIED", "trust": "registered"}` | 4.5 | 4.61 | 7.4 | ✅ | registered domain (not freemail) -> registered |
| I17 | freemail lookalike of a corporate payee | intake | `{"status": "QUARANTINED", "trust": "unknown_sender", "claimed_name": "Rheinwerk Komponenten GmbH"}` | 4.6 | 3.89 | 6.8 | ✅ | freemail is never matched by domain |

## Both suites on the real Airwallex sandbox

`python scripts/groundtruth_real.py`, 2026-10-23 09:20 UTC, approved by the owner as finance manager.

**Part A** — the 84 plan / intake / incident cases through the live service: **84 / 84 match**; 54 / 54 plans had their `Airwallex · live balances` stage read from the sandbox (none SKIPPED); plan round trip p50 105.0 ms.

**Part B** — the execution anomalies on the real rail, a fresh book per run (new obligation ids and invoice fingerprints, so nothing already paid is re-paid): **7 / 7 match**; 9 cannot be forced on a real rail.

| ID | Case | On the real rail | Result | ms | Match |
|---|---|---|---|---|---|
| N04 | planning rate stale: the rail is 3% dearer -> nothing booked | the planning rate is held stale (EUR 1.05) while the sandbox quotes the live rate (~1.13): the quote is above the slippage limit | plan APPROVED · execution FAILED | 846 | ✅ |
| N09 | the deposit stays PENDING: the receipt is not counted | not reproducible: the sandbox settles a simulated deposit by itself; a deposit that never settles cannot be requested | fake sandbox only | - | – |
| N26 | an approval given on different terms is refused | a finance manager's approval carrying another plan's terms | plan REQUIRES_APPROVAL · execution REFUSED | 207 | ✅ |
| N27 | a viewer 'approves' | an approval by a viewer | plan REQUIRES_APPROVAL · execution REFUSED | 192 | ✅ |
| N28 | REQUIRES_APPROVAL executed without a person | REQUIRES_APPROVAL executed with no approval | plan REQUIRES_APPROVAL · execution REFUSED | 203 | ✅ |
| N29 | the transfer response is lost: recovered by request_id, paid once | not reproducible: a lost HTTP response cannot be injected into the real rail (covered on the fake; the recovery path is request_id lookup, which the real rail supports) | fake sandbox only | - | – |
| N30 | 429 storm on transfer create | not reproducible: 429 storms cannot be requested from the sandbox | fake sandbox only | - | – |
| N31 | the rail answers duplicate_request_id | not reproducible: duplicate_request_id would require re-sending a used request_id, which the executor never does by design | fake sandbox only | - | – |
| N32 | wallet read says yes, rail says balance_insufficient | not reproducible: balance_insufficient at the rail needs the wallet to drop between the balance read and the transfer; the live-balance check prevents the setup | fake sandbox only | - | – |
| N33 | the conversion stays SCHEDULED: transfers are held | not reproducible: sandbox conversions settle immediately; a conversion that stays SCHEDULED cannot be requested | fake sandbox only | - | – |
| N39 | one payout fails after SENT: refund expected, fee lost | not reproducible: a bank return after SENT is exactly N44's mechanism on the real rail; not run twice | fake sandbox only | - | – |
| N40 | all three payouts fail after SENT | not reproducible: same mechanism as N44, three times; not run to keep the sandbox ledger readable | fake sandbox only | - | – |
| N41 | the same invoice executed twice: second run refused before any money moves | the same invoices executed twice on the real store: the payout claim refuses the second run | plan REQUIRES_APPROVAL · execution FAILED | 9351 | ✅ |
| N42 | the wallet holds less than the plan assumes | a treasury allocation larger than the live wallet (USD 20,000,000 against ~9.98 M): live balances do not cover the plan | plan BLOCKED · execution None | 99 | ✅ |
| N44 | bank return -> replace under the lock -> resolved only when the original is recorded | real transfer left in flight, failed on the sandbox with BENEFICIARY_BANK_RETURNED, replaced under the duplicate lock | plan APPROVED · execution IN_FLIGHT · incident RESOLVED (REPLACE, 1 follow-ups) | 14823 | ✅ |
| N45 | replacement would need a fee the wallet cannot cover | not reproducible: the replacement's budget check reads the live EUR wallet (~10 M), which cannot be set to EUR 1,512.85 | fake sandbox only | - | – |

## What the exercise changed

- **P28 / N24** — the hand answer first said APPROVED when the headroom exactly covers the payment. The service answered REQUIRES_APPROVAL: the 72-hour simulation draws the unexpected outflow with a lognormal size, so a plan that lands exactly on the floor breaches it in about half the paths, above the 5% tolerance. The probabilistic rule is the documented one; the expectation was corrected and the case kept.
- **P32** — USD 10 bn cash was BLOCKED by the live service (the sandbox wallet holds about USD 10 M) while passing in-process: a connected service grounds every plan in real balances. The case now uses amounts inside the wallet.
- **I04** — the lookalike's invoice LD-77 was already in the live intake queue, so the live service answered DUPLICATE instead of QUARANTINED: the queue is shared state and the duplicate lock applies to it. The case uses another invoice number.
- **N41** — paying the same invoices twice: a second *plan* of the same state after the first execution is BLOCKED by the live-balance check before the duplicate lock is even reached (the fake wallet is lower); the case tops the wallet up so that the lock itself is what refuses the second run (`duplicate payment blocked before any money moved`).
- **Harness** — execution cases set fake credentials in the environment; without restoring them, later plan cases saw a configured-but-unreachable Airwallex and were BLOCKED. Restored after each execution case.
