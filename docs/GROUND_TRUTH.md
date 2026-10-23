# Ground truth: hand-derived answers vs the live service

Generated 2026-10-23 21:46 by `python scripts/groundtruth_report.py`.

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
| in-process | plan | 17 | 17 | 3.4 | 7.3 | 36.4 |
| endpoint | plan | 17 | 17 | 106.0 | 175.6 | 413.7 |
| UI form | plan | 17 | 17 | 100.0 | 147.4 | 148.2 |
| in-process | intake | 6 | 6 | 2.1 | 2.3 | 3.9 |
| endpoint | intake | 6 | 6 | 6.2 | 6.5 | 7.3 |
| UI form | intake | 6 | 6 | 9.6 | 10.4 | 10.7 |
| in-process | incident | 7 | 7 | 0.0 | 0.0 | 0.0 |
| endpoint | incident | 7 | 7 | 2.0 | 2.4 | 2.5 |
| UI form | incident | 7 | 7 | 4.9 | 5.0 | 5.7 |
| in-process | execution | 16 | 16 | 250.2 | 397.2 | 447.3 |
| endpoint | execution | 16 | n/a (in-process only) | - | - | - |
| UI form | execution | 16 | n/a (in-process only) | - | - | - |

Server-side plan time (`duration_ms`, includes the live Airwallex balance check, the 72 h simulation and the counterfactuals): p50 92.1 ms, p95 183.6 ms.

### Cases

| ID | L | Pain (organizers) | Case | Kind | Expected | in-proc ms | endpoint ms | UI ms | Match | Derivation |
|---|---|---|---|---|---|---|---|---|---|---|
| N01 | 1 | insufficient funds mid-run: fees and FX buffers | the SWIFT fee is what breaks the reserve | plan | `{"actions": {"O1": "ESCALATE"}}` | 36.4 | 175.55 | 148.2 | ✅ | EUR 4,600 = USD 4,991 fits the 5,000 headroom; + EUR 12.85 fee (USD 13.94) = 5,005 > 5,000 -> cannot be funded -> ESCALATE |
| N02 | 1 | insufficient funds mid-run: fees | a learned LOCAL fee is what breaks the reserve | plan | `{"actions": {"O1": "ESCALATE"}}` | 3.4 | 413.67 | 147.4 | ✅ | 4,998 + learned fee 3.00 = 5,001 > 5,000 -> ESCALATE; the guide said LOCAL is free, the sandbox charges 3.00 |
| N03 | 1 | rounding | one-cent obligations are still money | plan | `{"decision": "APPROVED", "paid": ["O1", "O2", "O3"]}` | 2.8 | 146.49 | 110.2 | ✅ | 3 cents <= 5,000 -> all PAY; integer minor units, no float |
| N04 | 1 | stale quotes | planning rate stale: the rail is 3% dearer -> nothing booked | execution | `{"execution": "FAILED", "message_contains": "slippage", "conversions": 0, "transfers": 0}` | 203.2 | n/a | n/a | ✅ | quoted sell above the 1% slippage limit -> halt before any conversion or transfer; re-plan at the real rate |
| N05 | 1 | FX buffer | FX stress makes a 'fits exactly' conversion not fit | plan | `{"actions": {"O1": "ESCALATE"}}` | 3.4 | 114.43 | 107.1 | ✅ | EUR 4,620.85 incl. fee = USD 5,013.6 > 5,000 even before the Horizon stress buffer -> ESCALATE |
| N06 | 2 | obligations fall due faster than cash arrives | the receipt slips past the due date | plan | `{"actions": {"O1": "ESCALATE"}}` | 4.0 | 101.71 | 89.4 | ✅ | 7,000 > 5,000; receipt at 48h > due 24h -> cannot be funded before due -> ESCALATE |
| N07 | 2 | a receipt forecast is not a receipt | the 90% window ends after the planning horizon | plan | `{"actions": {"O1": "ESCALATE"}}` | 7.3 | 111.26 | 107.5 | ✅ | latest arrival 96h > 72h -> not relied on -> ESCALATE |
| N08 | 2 | deadline already here | due now, cash short, no receipt | plan | `{"actions": {"O1": "ESCALATE"}, "not_blocked": true}` | 2.6 | 96.08 | 101.8 | ✅ | headroom 1,000 < 3,000; due 0h -> ESCALATE now, not DEFER |
| N09 | 2 | delayed settlement | the deposit stays PENDING: the receipt is not counted | execution | `{"receipt_landed": false, "execution": "COMPLETED"}` | 391.1 | n/a | n/a | ✅ | the balance may lag; only a deposit record that says SETTLED counts; the plan proceeds without it |
| N10 | 2 | a contradicted forecast | payer with a bad record: 40% confidence drops the FX limit to zero | plan | `{"decision": "REQUIRES_APPROVAL", "limit": 0, "requires": ["conversion_above_autonomous_limit"]}` | 3.7 | 101.34 | 95.4 | ✅ | confidence 40% <= 50% -> limit 0 -> every conversion needs a person |
| N11 | 2 | in flight for too long | SENT for 49 h: the wait window is over, still not final | incident | `{"action": "ESCALATE"}` | 0.0 | 1.62 | 4.9 | ✅ | not terminal and 49h > 48h window -> a person chases the rail; replacing could pay twice |
| N12 | 2 | a timeout is unconfirmed | PROCESSING for 1 h: wait, never replace | incident | `{"action": "WAIT"}` | 0.0 | 1.68 | 5 | ✅ | not final, inside the window -> WAIT |
| N13 | 3 | evidence requirements | evidence about the wrong obligation | plan | `{"actions": {"O1": "REJECT"}}` | 2.5 | 101.91 | 87.8 | ✅ | the document names another subject -> REJECT |
| N14 | 3 | duplicate invoice | the same invoice re-sent under a new id | plan | `{"actions": {"O1": "PAY", "O2": "REJECT"}}` | 2.8 | 100.38 | 94.5 | ✅ | same fingerprint -> the copy is REJECT |
| N15 | 3 | changed terms | payee announces new bank details | plan | `{"actions": {"O1": "ESCALATE"}}` | 2.3 | 112.32 | 89.7 | ✅ | policy exception -> ESCALATE even though cash covers it |
| N16 | 3 | payment diversion | lookalike mailbox, new bank account, urgency | intake | `{"status": "QUARANTINED", "trust": "unknown_sender", "flags": ["payee_bank_details_changed", "urgency_pressure"]}` | 3.9 | 6.5 | 9.3 | ✅ | trust comes from the registry, not the display name; money + bank change from an unknown mailbox -> QUARANTINED |
| N17 | 3 | prompt injection | the email tells the agent it is verified | intake | `{"status": "QUARANTINED"}` | 2.1 | 5.6 | 8.2 | ✅ | the text is data; status is decided by code |
| N18 | 3 | reply-to diversion | registered mailbox, Reply-To elsewhere | intake | `{"status": "QUARANTINED", "trust": "reply_to_mismatch"}` | 2.0 | 6.06 | 9.2 | ✅ | Reply-To is not registered -> QUARANTINED |
| N19 | 3 | bank change from the real payee | registered mailbox announces a new account | intake | `{"status": "NEEDS_REVIEW", "kind": "bank_change"}` | 1.9 | 6.15 | 10.7 | ✅ | even the real payee's bank change needs an out-of-band call-back |
| N20 | 3 | duplicate reminder | a reminder for an invoice already in the book | intake | `{"status": "DUPLICATE", "duplicate_of": "SHIPPING-UK"}` | 2.3 | 7.31 | 10.4 | ✅ | fingerprint already in kit01_initial -> DUPLICATE |
| N21 | 3 | pressure language from a real payee | registered sender, 'the CEO approved, pay immediately' | intake | `{"status": "NEEDS_REVIEW", "flags": ["urgency_pressure"]}` | 2.1 | 6.33 | 9.9 | ✅ | pressure language -> a person confirms before it is payable |
| N22 | 4 | limits | conversion above the autonomous limit | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["conversion_above_autonomous_limit"]}` | 4.4 | 97.0 | 96.3 | ✅ | USD ~13,034 > limit 10,000 -> a person |
| N23 | 4 | limits scale with confidence | 70% confidence halves the limit and the conversion no longer fits | plan | `{"decision": "REQUIRES_APPROVAL", "limit": 500000}` | 3.4 | 96.79 | 100 | ✅ | limit = 10,000 x (70-50)/40 = 5,000 < ~7,734 |
| N24 | 4 | risk tolerance | the plan lands exactly on the floor: probabilistic breach risk | plan | `{"actions": {"O1": "PAY"}, "requires": ["reserve_breach_risk_above_tolerance"]}` | 4.8 | 109.57 | 98 | ✅ | deterministic headroom 3,000 = 3,000 -> PAY, but a lognormal stress breaches the floor in ~half the paths > 5% -> a person |
| N25 | 4 | roles | a viewer asks for a plan | plan | `{"decision": "BLOCKED"}` | 2.6 | 97.54 | 100.9 | ✅ | viewer -> BLOCKED |
| N26 | 4 | approvals bound to exact terms | an approval given on different terms is refused | execution | `{"execution": "REFUSED", "transfers": 0}` | 105.1 | n/a | n/a | ✅ | limit 0 -> REQUIRES_APPROVAL; the approval's terms_digest does not match the plan -> REFUSED, nothing moves |
| N27 | 4 | approver role | a viewer 'approves' | execution | `{"execution": "REFUSED", "transfers": 0}` | 101.6 | n/a | n/a | ✅ | role viewer is not in approver_roles -> REFUSED |
| N28 | 4 | no approval at all | REQUIRES_APPROVAL executed without a person | execution | `{"execution": "REFUSED", "transfers": 0}` | 107.8 | n/a | n/a | ✅ | no approval -> REFUSED |
| N29 | 5 | a timeout is unconfirmed | the transfer response is lost: recovered by request_id, paid once | execution | `{"execution": "COMPLETED", "transfers": 3, "invariants_ok": true}` | 397.2 | n/a | n/a | ✅ | the create succeeded on the rail; the retry finds it by request_id instead of creating a second transfer |
| N30 | 5 | rate limits | 429 storm on transfer create | execution | `{"execution": "COMPLETED", "transfers": 3}` | 381.2 | n/a | n/a | ✅ | backoff and retry with the same request_id -> every payout once |
| N31 | 5 | duplicate request id | the rail answers duplicate_request_id | execution | `{"execution": "FAILED", "message_contains": "duplicate_request_id"}` | 248.9 | n/a | n/a | ✅ | the operation is recorded FAILED and the claim released; nothing is sent again blindly |
| N32 | 5 | insufficient funds at the rail | wallet read says yes, rail says balance_insufficient | execution | `{"execution": "FAILED", "message_contains": "balance_insufficient"}` | 251.5 | n/a | n/a | ✅ | halt, reconcile, report; later payouts are not attempted |
| N33 | 5 | PENDING states | the conversion stays SCHEDULED: transfers are held | execution | `{"execution": "IN_FLIGHT", "transfers": 0}` | 196.2 | n/a | n/a | ✅ | money not yet in the payout currency -> no transfer; the run is resumable |
| N34 | 5 | CANCELLED does not mean a person cancelled | CANCELLED with BENEFICIARY_BANK_RETURNED | incident | `{"action": "REPLACE"}` | 0.0 | 2.27 | 4.9 | ✅ | funds returned, payee record not in question -> REPLACE under the duplicate lock with a new request_id |
| N35 | 5 | wrong payee details | CANCELLED with INVALID_ACCOUNT_NAME_OR_NUMBER | incident | `{"action": "ESCALATE"}` | 0.0 | 2.05 | 5.7 | ✅ | paying again pays the wrong account again -> ESCALATE |
| N36 | 5 | a person or control stopped it | CANCELLED with RECALL_REQUESTED | incident | `{"action": "ESCALATE"}` | 0.0 | 2.4 | 4.4 | ✅ | only a person may restart it |
| N37 | 5 | unknown failure shape | FAILED with no typed failure | incident | `{"action": "ESCALATE", "reason_contains": "unknown"}` | 0.0 | 1.81 | 3.9 | ✅ | fail closed: UNKNOWN -> ESCALATE (this is what the real sandbox did in cycle 5, run 1) |
| N38 | 5 | one replacement only | a second return after one replacement | incident | `{"action": "ESCALATE"}` | 0.0 | 2.46 | 4.1 | ✅ | max_replacements 1 reached -> a person decides |
| N39 | 5 | bank return after SENT | one payout fails after SENT: refund expected, fee lost | execution | `{"execution": "FAILED", "message_contains": "SHIPPING-UK", "invariants_ok": true}` | 378.9 | n/a | n/a | ✅ | reconciliation expects the amount back and the fee gone; the other payouts stand |
| N40 | 5 | every payout fails | all three payouts fail after SENT | execution | `{"execution": "FAILED", "invariants_ok": true}` | 346.7 | n/a | n/a | ✅ | three refunds expected, three fees lost, reconciled to the cent |
| N41 | 6 | without paying twice | the same invoice executed twice: second run refused before any money moves | execution | `{"execution": "FAILED", "message_contains": "duplicate payment blocked", "invariants_ok": true}` | 447.3 | n/a | n/a | ✅ | the payout claim per invoice fingerprint survives across executions |
| N42 | 6 | stale state | the wallet holds less than the plan assumes | execution | `{"plan": "BLOCKED", "execution": null}` | 95.9 | n/a | n/a | ✅ | live balances do not cover the plan -> BLOCKED at proof time; nothing executes |
| N43 | 6 | only foreign cash | base wallet already below the floor, foreign wallets cover the payouts | plan | `{"paid": ["O1", "O2"], "n_conversions": 0, "requires": ["reserve_breach_risk_above_tolerance"]}` | 3.0 | 106.79 | 95.2 | ✅ | nothing is spent from USD, so the floor (a rule on USD spend) is not breached; the 100% breach risk goes to a person |
| N44 | 6 | Kit 03 end to end | bank return -> replace under the lock -> resolved only when the original is recorded | execution | `{"incident": "RESOLVED", "incident_decision": "REPLACE", "invariants_ok": true}` | 252.0 | n/a | n/a | ✅ | returned funds -> REPLACE with a new request_id and the second fee budgeted; RESOLVED after the original's CANCELLED/type/refund are recorded |
| N45 | 6 | no money for the second fee | replacement would need a fee the wallet cannot cover | execution | `{"incident": "ESCALATED", "incident_decision": "REPLACE"}` | 196.8 | n/a | n/a | ✅ | REPLACE decided, but EUR 1,513.38 needed and EUR 1,512.85 refunded -> refused, ESCALATED |
| N46 | 6 | the biggest anomaly: everything at once | cash short, receipt contradicted, bank change, duplicate, SWIFT fee, viewer-free | plan | `{"actions": {"O1": "ESCALATE", "O2": "CONVERT_AND_PAY", "O3": "REJECT", "O4": "ESCALATE", "O5": "ESCALATE"}, "paid": ["O2"]}` | 5.4 | 105.98 | 132.2 | ✅ | headroom 8,000 - 5,000 = 3,000. O1 5,000 > 3,000 and the 60% receipt is not relied on -> ESCALATE. O2 EUR 2,012.85 incl. fee = USD 2,184 <= 3,000 -> CONVERT_AND_PAY. O3 same fingerprint as O2 -> REJECT. O4 bank-change flag -> ESCALATE. O5 1,000 > 3,000 - 2,184 = 816 (and O2 at priority 9 outranks O5 at 2) -> ESCALATE. Nothing else is paid. |

## Suite 3 — Budget · mandate: the period's plan is the agent's mandate

20 cases around every threshold of a budget line: inside the auto share, exactly the mandate, one cent over (the agent stops and raises a BUDGET REQUEST with the exact shortfall), above the auto share (a person), the alert threshold with and without require_approval_on_alert, realized spend eating the line, a grant restoring it, two lines, priority inside a line, unbudgeted obligations, bypass signed or not, the whole November book against the November plan, minus from what is still owed, a viewer, and no budget at all. Real-sandbox cycle 8 (SANDBOX_RESULTS.md) runs the same mandate end to end: pay inside the line, stop at it, grant, continue.

| Path | Kind | Cases | Match | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|---|
| in-process | plan | 20 | 20 | 2.9 | 3.8 | 3.8 |
| endpoint | plan | 20 | 20 | 122.0 | 128.0 | 129.3 |
| UI form | plan | 20 | 20 | 102.5 | 163.0 | 378.1 |

Server-side plan time (`duration_ms`, includes the live Airwallex balance check, the 72 h simulation and the counterfactuals): p50 115.3 ms, p95 420.0 ms.

### Cases

| ID | L | Pain (organizers) | Case | Kind | Expected | in-proc ms | endpoint ms | UI ms | Match | Derivation |
|---|---|---|---|---|---|---|---|---|---|---|
| B01 | 4 | the agent acts within its mandate | inside the line, inside the auto share | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "budget_status": {"suppliers": "auto"}, "requires": []}` | 3.6 | 129.28 | 378.1 | ✅ | 3,000 of 10,000 = 30% < alert 80%, < auto 100% -> PAY autonomously |
| B02 | 4 | the agent acts within its mandate | exactly the mandate | plan | `{"actions": {"O1": "PAY"}, "budget_available": {"suppliers": 0}, "requires": []}` | 3.0 | 122.71 | 114.4 | ✅ | 10,000 = mandate -> cap allows it (<=); utilisation 100% >= alert 80% -> ALERT, but the line does not require approval on alert -> APPROVED with an alert |
| B03 | 4 | the agent acts within its mandate | one cent over the mandate: the agent stops at the line | plan | `{"actions": {"O1": "ESCALATE"}, "paid": [], "budget_requests": {"suppliers": 1}}` | 3.4 | 121.82 | 97.9 | ✅ | cap = 10,000; 10,000.01 does not fit -> ESCALATE with a budget request for the shortfall 0.01 |
| B04 | 4 | the agent acts within its mandate | above the auto share needs a person | plan | `{"decision": "REQUIRES_APPROVAL", "actions": {"O1": "PAY"}, "requires": ["budget_line_above_auto_share"]}` | 3.8 | 121.37 | 107.6 | ✅ | 8,500 > 10,000 x 80% = 8,000 -> payable, but a person approves |
| B05 | 4 | the agent acts within its mandate | exactly the auto share is still autonomous | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "requires": []}` | 2.8 | 126.53 | 99.4 | ✅ | 8,000 = 80% of 10,000: not above -> autonomous; 80% < alert 90% -> no alert |
| B06 | 4 | the agent acts within its mandate | alert threshold reached (notification, still autonomous) | plan | `{"decision": "APPROVED", "budget_alerts": 1, "budget_status": {"suppliers": "alert"}}` | 2.0 | 121.48 | 102.1 | ✅ | 82% >= alert 80% -> ALERT raised; auto share 100% -> no approval needed |
| B07 | 4 | the agent acts within its mandate | alert threshold with require_approval_on_alert | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["budget_line_alert_requires_approval"], "budget_alerts": 1}` | 2.4 | 122.18 | 163 | ✅ | 82% >= 80% and the line demands a person at the alert -> REQUIRES_APPROVAL |
| B08 | 4 | the agent acts within its mandate | realized spend eats the line | plan | `{"actions": {"O1": "ESCALATE"}, "budget_requests": {"suppliers": 1}}` | 2.9 | 123.51 | 101.6 | ✅ | cap = 10,000 - 7,500 = 2,500 < 3,000 -> ESCALATE + request for 500 |
| B09 | 4 | the agent acts within its mandate | a grant restores the mandate | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "budget_available": {"suppliers": 50000}}` | 2.7 | 127.99 | 102.5 | ✅ | mandate 11,000 - realized 7,500 = 3,500 >= 3,000 -> PAY; available after = 500 |
| B10 | 4 | the agent acts within its mandate | two lines, one exhausted, the other fine | plan | `{"actions": {"O1": "ESCALATE", "O2": "PAY"}, "budget_requests": {"suppliers": 1}}` | 3.6 | 122.29 | 105.7 | ✅ | suppliers cap 2,500 < 3,000 -> ESCALATE + request 500; saas 2,000 of 5,000 -> PAY |
| B11 | 4 | the agent acts within its mandate | priority within a line: the line pays the urgent one | plan | `{"actions": {"O1": "PAY", "O2": "ESCALATE"}, "budget_requests": {"suppliers": 1}}` | 3.7 | 119.59 | 105.2 | ✅ | both fit the cash, only one fits the line (6,000 + 6,000 > 10,000): priority 10 paid, the other raises a request for 2,000 |
| B12 | 4 | the agent acts within its mandate | unbudgeted obligation needs a person | plan | `{"decision": "REQUIRES_APPROVAL", "actions": {"O1": "PAY"}, "requires": ["unbudgeted_obligation"]}` | 2.4 | 120.25 | 100.2 | ✅ | 'marketing' is no line of 2026-11 -> payable by cash, but a person must place it |
| B13 | 4 | the agent acts within its mandate | unbudgeted allowed by the budget's setting | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "requires": []}` | 3.3 | 122.61 | 104.1 | ✅ | the budget says unbudgeted obligations do not need a person |
| B14 | 4 | the agent acts within its mandate | bypass nobody signed keeps the cap | plan | `{"actions": {"O1": "ESCALATE"}, "budget_requests": {"suppliers": 1}}` | 3.2 | 116.66 | 100.5 | ✅ | unsigned bypass -> cap 2,000 applies -> ESCALATE + request |
| B15 | 4 | the agent acts within its mandate | signed bypass lifts the cap | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "budget_status": {"suppliers": "bypass"}, "requires": []}` | 2.6 | 119.16 | 98.1 | ✅ | signed bypass -> no cap, no approval reason; the line shows as bypass |
| B16 | 4 | the agent acts within its mandate | bypass signed but spending inside it is still recorded | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 1.9 | 115.67 | 102.9 | ✅ | realized 9,000 on a 2,000 line with a signed bypass: still payable; the pipeline shows it, nobody stops it |
| B17 | 4 | the agent acts within its mandate | the whole November book against the November plan | plan | `{"decision": "APPROVED", "paid": ["O1", "O2", "O3", "O4", "O5"], "budget_alerts": 1, "requires": []}` | 3.8 | 125.7 | 104.3 | ✅ | every line fits: payroll 89% (< alert 90%), logistics 75%, suppliers 57%, contractors 50% (< 60% alert), saas 80% = alert -> one alert, no approval |
| B18 | 4 | the agent acts within its mandate | minus from what is still owed | plan | `{"actions": {"O1": "PAY", "O2": "ESCALATE"}, "budget_minus": {"suppliers": 200000}, "budget_requests": {"suppliers": 1}}` | 2.6 | 121.02 | 97.1 | ✅ | 7,000 paid; 5,000 still owed; 12,000 - 10,000 = minus 2,000 -> request for 2,000 |
| B19 | 4 | the agent acts within its mandate | viewer with a budget is still blocked | plan | `{"decision": "BLOCKED"}` | 3.2 | 123.34 | 101.2 | ✅ | actor_role viewer -> BLOCKED regardless of budget |
| B20 | 4 | the agent acts within its mandate | no budget attached: nothing changes | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "budget_attached": false}` | 2.1 | 121.46 | 102.4 | ✅ | without a budget the plan is the old plan |

## Suite 4 — finance controls: instalments, three-way match, period cut-off, reconciliation cases

Instalments (a deposit then the balance: two payouts of one invoice under the duplicate lock, never past the contract total), the three-way match (contract − paid so far vs what the invoice claims; over-claims beyond tolerance need a person), the period cut-off (due after period_end carries forward to the next period; due before period_start is late-posted; a closed period takes no autonomous spend) and reconciliation cases (a mismatch opens a case that locks the book until a person closes it with a note).

| Path | Kind | Cases | Match | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|---|
| in-process | plan | 16 | 16 | 2.7 | 3.1 | 3.6 |
| endpoint | plan | 16 | 16 | 98.7 | 115.3 | 119.8 |
| UI form | plan | 16 | 16 | 89.8 | 101.1 | 146.5 |
| in-process | execution | 2 | 2 | 411.0 | 387.8 | 434.2 |
| endpoint | execution | 2 | n/a (in-process only) | - | - | - |
| UI form | execution | 2 | n/a (in-process only) | - | - | - |

Server-side plan time (`duration_ms`, includes the live Airwallex balance check, the 72 h simulation and the counterfactuals): p50 83.7 ms, p95 94.3 ms.

### Cases

| ID | L | Pain (organizers) | Case | Kind | Expected | in-proc ms | endpoint ms | UI ms | Match | Derivation |
|---|---|---|---|---|---|---|---|---|---|---|
| K01 | 4 | a deposit then the balance | deposit and balance of one invoice are two payouts | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY", "O2": "PAY"}}` | 3.6 | 119.77 | 146.5 | ✅ | same invoice, different instalments -> both payable; 3,000 + 7,000 = contract 10,000 |
| K02 | 4 | paying twice | the balance sent twice is a duplicate | plan | `{"actions": {"O1": "PAY", "O2": "REJECT"}}` | 3.1 | 101.44 | 84.5 | ✅ | same (invoice, instalment) -> the second copy is REJECT |
| K03 | 4 | over-claim on the balance | a balance that overshoots the contract is rejected | plan | `{"actions": {"O1": "REJECT"}}` | 2.9 | 95.58 | 89.4 | ✅ | 3,000 paid + 8,000 claimed = 11,000 > contract 10,000 -> REJECT before anything else |
| K04 | 4 | double-billing after a deposit | whole-invoice payout after a deposit is a duplicate of the invoice | plan | `{"actions": {"O1": "PAY", "O2": "PAY"}, "decision": "REQUIRES_APPROVAL", "requires": ["amount_mismatch"]}` | 2.7 | 100.1 | 101.1 | ✅ | O2 claims the whole 10,000 although 3,000 is paid: expected 7,000, claimed 10,000 -> over-claim -> a person (amount_mismatch); O1 unaffected |
| K05 | 4 | three-way match | invoice equals the contract | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "requires": []}` | 2.6 | 115.35 | 93.7 | ✅ | expected 10,000 = claimed -> matched |
| K06 | 4 | rounding and small variances | invoice inside tolerance (+0.4%) | plan | `{"decision": "APPROVED", "requires": []}` | 1.9 | 93.96 | 94.6 | ✅ | diff 40 <= tolerance max(5, 0.5% of 10,000 = 50) -> matched |
| K07 | 4 | over-claim | invoice above tolerance (+1%) | plan | `{"decision": "REQUIRES_APPROVAL", "actions": {"O1": "PAY"}, "requires": ["amount_mismatch"]}` | 2.2 | 97.26 | 91.9 | ✅ | diff 100 > 50 -> over-claim -> a person |
| K08 | 4 | under-claim | invoice below the contract (under-claim) is fine | plan | `{"decision": "APPROVED", "requires": []}` | 2.9 | 105.59 | 87.4 | ✅ | the payee bills less; paying less is never unsafe; noted as under-claim |
| K09 | 4 | controls compose | over-claim that is escalated anyway needs no second reason | plan | `{"actions": {"O1": "ESCALATE"}, "requires": []}` | 2.8 | 102.85 | 87.9 | ✅ | the bank-change flag already sends it to a person; the plan pays nothing on it, so no amount_mismatch |
| K10 | 4 | strict books | tolerance set to zero: one cent over needs a person | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["amount_mismatch"]}` | 2.2 | 101.58 | 94.3 | ✅ | tolerance 0 -> any excess is an over-claim |
| K11 | 4 | cut-off | due inside the period draws on the line | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "budget_carry_forward": 0, "budget_status": {"suppliers": "auto"}}` | 2.6 | 88.19 | 83.9 | ✅ | as_of 10 Nov + 24h = 11 Nov, inside Nov -> counted on 'suppliers' |
| K12 | 4 | cut-off | due after the period end carries forward | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "budget_carry_forward": 1, "budget_paying_in_period": 0}` | 2.2 | 83.12 | 86 | ✅ | 29 Nov + 72h = 2 Dec > 30 Nov -> next period's commitment; it is still payable from cash but draws on no November line |
| K13 | 4 | cut-off | explicit due_date after the period end | plan | `{"budget_carry_forward": 1}` | 3.0 | 84.53 | 86.8 | ✅ | due_date wins over due_in_hours; 5 Dec is after 30 Nov -> carry-forward |
| K14 | 4 | cut-off | late-posted: due before the period started | plan | `{"budget_late_posted": 1, "budget_carry_forward": 0, "actions": {"O1": "PAY"}}` | 2.5 | 87.73 | 90.2 | ✅ | 28 Oct < 1 Nov -> late-posted on the November statement, still payable |
| K15 | 4 | cut-off | closed period: spending against it needs a person | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["period_closed"]}` | 2.9 | 100.97 | 86.5 | ✅ | the statement was issued; a payment that draws on the closed period goes to a person |
| K16 | 4 | cut-off | closed period but the payment carries forward: no reason | plan | `{"decision": "APPROVED", "requires": []}` | 2.1 | 85.44 | 92.1 | ✅ | nothing is paid against November (carry-forward) -> period_closed does not apply |
| K17 | 4 | reconciliation mismatch | a mismatch opens a case and the book is locked for the next run | execution | `{"execution": "REFUSED", "message_contains": "locked by open reconciliation case"}` | 387.8 | n/a | n/a | ✅ | the rail debits EUR 0.63 it never reports on the transfer -> live EUR differs from expected by 63 minor -> RECONCILIATION_MISMATCH opens a case -> the second run is REFUSED before anything moves |
| K18 | 4 | reconciliation mismatch | closing the case unlocks the book | execution | `{"execution": "FAILED", "message_contains": "duplicate payment blocked"}` | 434.2 | n/a | n/a | ✅ | with the case closed by a person (note required), the next run reaches the duplicate lock as usual (the invoices were paid in run 1) |

## Suite 5 — reporting periods: the admin's cut-off, payments by payment instant, one reporting currency

Cut-off day + time + timezone set by an admin (inclusive to the last second of the cut-off minute, day clamped to the month's end, year roll-over, timezone conversion); payments booked to the period of their payment instant; an invoice paid in instalments is reported as reconciled only when its parts add up to the contract, in the month of the last part; consolidation of every currency to the reporting currency at the period's recorded closing rates, beside the base cost actually paid (FX difference); a missing rate is flagged, never guessed.

| Path | Kind | Cases | Match | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|---|
| in-process | reporting | 18 | 18 | 0.0 | 0.1 | 0.1 |
| endpoint | reporting | 18 | 18 | 1.1 | 1.4 | 1.4 |
| UI form | reporting | 18 | 18 | 2.8 | 5.0 | 5.9 |

### Cases

| ID | L | Pain (organizers) | Case | Kind | Expected | in-proc ms | endpoint ms | UI ms | Match | Derivation |
|---|---|---|---|---|---|---|---|---|---|---|
| R01 | 4 | period cut-off and reporting | posted at the cut-off minute belongs to the month | reporting | `{"reporting_period": "2026-11"}` | 0.1 | 1.3 | 5.9 | ✅ | 25 Nov 23:59:30 <= cut-off 25 Nov 23:59:59.999 -> November |
| R02 | 4 | period cut-off and reporting | posted one minute after the cut-off belongs to next month | reporting | `{"reporting_period": "2026-12"}` | 0.0 | 1.01 | 2.4 | ✅ | after the cut-off -> December |
| R03 | 4 | period cut-off and reporting | posted on the 1st belongs to the running month | reporting | `{"reporting_period": "2026-11"}` | 0.0 | 1.27 | 2.8 | ✅ | 1 Nov is after October's cut-off (25 Oct) and before November's -> November |
| R04 | 4 | period cut-off and reporting | timezone matters: 25 Nov 17:30 UTC is 26 Nov 00:30 Jakarta | reporting | `{"reporting_period": "2026-12"}` | 0.0 | 1.22 | 2.5 | ✅ | converted to Asia/Jakarta it is past the cut-off -> December |
| R05 | 4 | period cut-off and reporting | a naive timestamp is read as local time | reporting | `{"reporting_period": "2026-11"}` | 0.0 | 1.36 | 2.8 | ✅ | no offset -> treated as Jakarta time, before the cut-off -> November |
| R06 | 4 | period cut-off and reporting | December posting after the cut-off rolls into January | reporting | `{"reporting_period": "2027-01"}` | 0.0 | 1.21 | 3.3 | ✅ | year roll-over |
| R07 | 4 | period cut-off and reporting | cut-off day 31 is clamped in a 30-day month | reporting | `{"reporting_period": "2026-11"}` | 0.0 | 1.04 | 2.5 | ✅ | day 31 -> 30 Nov; 23:00 <= 23:59 -> November |
| R08 | 4 | period cut-off and reporting | cut-off day 31 clamped: 1 Dec is December | reporting | `{"reporting_period": "2026-12"}` | 0.0 | 1.27 | 2.9 | ✅ | after 30 Nov 23:59 -> December |
| R09 | 4 | period cut-off and reporting | cut-off 28 at noon: 28th 12:01 is next month | reporting | `{"reporting_period": "2026-12"}` | 0.0 | 0.97 | 3.2 | ✅ | 12:01 > 12:00:59 -> December |
| R10 | 4 | two-layer reporting | November report: three payments, the deposit of A among them | reporting | `{"payments": 3, "by_currency": {"USD": 30000, "EUR": 40000, "GBP": 20000}, "invoices_completed": ["inv-B"], "invoices_open": ["inv-A", "inv-C"]}` | 0.1 | 1.36 | 2.9 | ✅ | t1, t3, t4 paid before 25 Nov 23:59 -> November; inv-B fully paid in November -> completed; A and C still open |
| R11 | 4 | two-layer reporting | December report: the balance of A completes the invoice | reporting | `{"payments": 1, "by_currency": {"USD": 70000}, "invoices_completed": ["inv-A"], "invoices_open": ["inv-C"]}` | 0.1 | 1.1 | 2.6 | ✅ | t2 paid 3 Dec -> December; deposit 30,000 + balance 70,000 = contract 100,000 -> RECONCILED in December; C still open |
| R12 | 4 | two-layer reporting | invoice statement across months | reporting | `{"invoice_status": "RECONCILED", "invoice_paid_minor": 100000, "invoice_outstanding_minor": 0, "invoice_completed_in": "2026-12", "instalment_periods": ["2026-11", "2026-12"]}` | 0.0 | 1.18 | 2.8 | ✅ | each instalment stays in the month it was paid; the invoice is reconciled when they add up |
| R13 | 4 | two-layer reporting | an open invoice shows what is outstanding | reporting | `{"invoice_status": "OPEN", "invoice_paid_minor": 20000, "invoice_outstanding_minor": 30000, "invoice_completed_in": null}` | 0.0 | 0.97 | 5.0 | ✅ | deposit 20,000 of 50,000 -> 30,000 outstanding, not reconciled |
| R14 | 4 | one reporting currency | November consolidated to IDR at closing rates | reporting | `{"reporting_total_minor": 16180000, "missing_rates": []}` | 0.1 | 1.23 | 2.6 | ✅ | USD 300 x 15,800 + EUR 400 x 17,900 + GBP 200 x 21,400 = 4,740,000 + 7,160,000 + 4,280,000 = IDR 16,180,000 (IDR has no minor unit) |
| R15 | 4 | one reporting currency | FX difference: base cost at the base closing rate vs each currency at its own | reporting | `{"fx_difference_minor": 569600}` | 0.1 | 0.92 | 2.9 | ✅ | base cost USD 300 + 434 + 254 = 988 x 15,800 = 15,610,400; consolidated 16,180,000; difference +569,600 IDR (the agent's realised USD cost was cheaper than closing rates imply) |
| R16 | 4 | one reporting currency | a missing closing rate is reported, not guessed | reporting | `{"missing_rates": ["GBP"], "fx_difference_minor": null}` | 0.1 | 1.1 | 3.5 | ✅ | no GBP rate -> consolidation flags GBP and gives no FX difference |
| R17 | 4 | one reporting currency | December uses December's closing rates | reporting | `{"reporting_total_minor": 11200000}` | 0.1 | 1.12 | 3.4 | ✅ | USD 700 x 16,000 = IDR 11,200,000 |
| R18 | 4 | period cut-off and reporting | a 14-day cut-off puts a 15th posting into next month | reporting | `{"reporting_period": "2026-12"}` | 0.0 | 0.85 | 2.6 | ✅ | admin chooses the day; 15 Nov is after 14 Nov 23:59 -> December |

## Suite 6 — the book ledger: double-entry per book, a book never spends another book's money

Balanced entries per currency (open, deposit, grant, convert, commit, settle, refund, adjust); a commit or conversion the book cannot fund is refused before money moves; the wallet must move by exactly the sum of every book's cash lines, which is what reconciliation now checks on a shared wallet.

| Path | Kind | Cases | Match | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|---|
| in-process | ledger | 15 | 15 | 0.1 | 0.1 | 0.1 |
| endpoint | ledger | 15 | 15 | 1.2 | 1.4 | 1.5 |
| UI form | ledger | 15 | 15 | 3.0 | 3.7 | 3.8 |

### Cases

| ID | L | Pain (organizers) | Case | Kind | Expected | in-proc ms | endpoint ms | UI ms | Match | Derivation |
|---|---|---|---|---|---|---|---|---|---|---|
| L01 | 4 | a book never spends another book's money | opening the book | ledger | `{"cash": {"USD": 2250000, "EUR": 0, "GBP": 0}, "posted": 1, "all_balanced": true}` | 0.1 | 1.31 | 3.8 | ✅ | cash +22,500 / funding −22,500 per currency; balanced |
| L02 | 4 | a book never spends another book's money | a conversion moves cash across currencies | ledger | `{"cash": {"USD": 1814606, "EUR": 401285, "GBP": 0}, "all_balanced": true}` | 0.1 | 1.45 | 3.0 | ✅ | USD −4,353.94 via fx; EUR +4,012.85 via fx; each currency balanced on its own |
| L03 | 4 | a book never spends another book's money | a commit takes amount + fee from cash | ledger | `{"cash": {"USD": 1449700, "EUR": 0, "GBP": 0}, "committed": {"USD": 800000, "EUR": 0, "GBP": 0}, "fees": {"USD": 300, "EUR": 0, "GBP": 0}}` | 0.1 | 1.2 | 2.7 | ✅ | cash −8,003; committed +8,000; fees +3 |
| L04 | 4 | a book never spends another book's money | settle turns in-flight money into expense | ledger | `{"committed": {"USD": 0, "EUR": 0, "GBP": 0}, "expense": {"USD": 800000, "EUR": 0, "GBP": 0}, "cash": {"USD": 1449700, "EUR": 0, "GBP": 0}}` | 0.1 | 1.04 | 3.5 | ✅ | committed −8,000 / expense +8,000; cash unchanged |
| L05 | 4 | a book never spends another book's money | a refund brings the amount back, not the fee | ledger | `{"cash": {"USD": 2249700, "EUR": 0, "GBP": 0}, "committed": {"USD": 0, "EUR": 0, "GBP": 0}, "fees": {"USD": 300, "EUR": 0, "GBP": 0}}` | 0.1 | 0.94 | 3.0 | ✅ | cash back to 22,497: the fee is gone for good |
| L06 | 4 | a book never spends another book's money | a commit beyond the book's cash is refused | ledger | `{"posted": 1, "cash": {"USD": 2250000, "EUR": 0, "GBP": 0}}` | 0.0 | 0.94 | 2.8 | ✅ | 23,000 > 22,500 -> refused; nothing posted; whatever the shared wallet holds |
| L07 | 4 | a book never spends another book's money | a commit in a currency the book does not hold is refused | ledger | `{"posted": 1}` | 0.0 | 1.34 | 3.7 | ✅ | EUR cash 0 -> refused until a conversion funds it |
| L08 | 4 | a book never spends another book's money | convert then commit in the bought currency | ledger | `{"posted": 3, "cash": {"USD": 1814606, "EUR": 0, "GBP": 0}, "committed": {"USD": 0, "EUR": 400000, "GBP": 0}, "fees": {"USD": 0, "EUR": 1285, "GBP": 0}}` | 0.1 | 1.52 | 2.8 | ✅ | EUR 4,012.85 bought covers 4,000 + 12.85 fee exactly -> EUR cash 0 |
| L09 | 4 | a book never spends another book's money | the real fee is larger than the conversion bought: refused before money moves | ledger | `{"posted": 2}` | 0.1 | 1.26 | 2.8 | ✅ | 4,013.37 > 4,012.85 -> the pre-flight refuses by 0.52 (on the rail the executor would record it as an overdraft to report) |
| L10 | 4 | a book never spends another book's money | a deposit funds the book | ledger | `{"cash": {"USD": 3250000, "EUR": 0, "GBP": 0}}` | 0.1 | 0.84 | 2.5 | ✅ | cash +10,000 / funding −10,000 |
| L11 | 4 | a book never spends another book's money | a grant funds the book | ledger | `{"cash": {"USD": 2360000, "EUR": 0, "GBP": 0}}` | 0.0 | 1.26 | 2.9 | ✅ | cash +1,100 |
| L12 | 4 | a book never spends another book's money | an adjustment can go either way | ledger | `{"cash": {"USD": 2249937, "EUR": 0, "GBP": 0}}` | 0.0 | 1.01 | 3.1 | ✅ | an unreported bank charge of 0.63 recorded by a person |
| L13 | 4 | a book never spends another book's money | an unbalanced entry is refused | ledger | `{"posted": 1}` | 0.1 | 1.29 | 2.6 | ✅ | unknown kind -> refused; the journal stays balanced |
| L14 | 4 | a book never spends another book's money | the wallet moves by exactly the cash lines | ledger | `{"cash_delta": {"USD": 1132200, "GBP": 0, "EUR": 0}, "committed": {"USD": 0, "EUR": 0, "GBP": 0}}` | 0.1 | 1.13 | 3.0 | ✅ | cash lines: USD +22,500 −3,175 −8,003 = +11,322; GBP +2,500 −2,500 = 0: reconciliation compares the wallet's movement to exactly this |
| L15 | 4 | a book never spends another book's money | two books on one wallet: each is its own ledger | ledger | `{"posted": 2, "cash": {"USD": 100000}, "committed": {"USD": 400000}}` | 0.0 | 0.76 | 3.0 | ✅ | book A has 5,000: 4,000 fits, another 2,000 does not — even if book B's money is in the same wallet |

## Suite 7 — limits and velocity: how much, to whom, how often

A payout above the single-payment limit; a payee whose paid-this-period plus paying-now exceeds its period limit (history from the spend ledger, two payouts in one plan count together); a payee never PAID before getting more than the first-payment limit (no history = everyone is new); rolling 24 h / 7 d outflow plus paying now above the daily / weekly limit; several reasons at once, each named; no limits = nothing changes; an escalated payout does not count as paying now.

| Path | Kind | Cases | Match | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|---|
| in-process | plan | 18 | 18 | 2.6 | 3.9 | 8.1 |
| endpoint | plan | 18 | 18 | 127.2 | 131.8 | 132.8 |
| UI form | plan | 18 | 18 | 135.5 | 214.5 | 571.9 |

Server-side plan time (`duration_ms`, includes the live Airwallex balance check, the 72 h simulation and the counterfactuals): p50 115.2 ms, p95 119.5 ms.

### Cases

| ID | L | Pain (organizers) | Case | Kind | Expected | in-proc ms | endpoint ms | UI ms | Match | Derivation |
|---|---|---|---|---|---|---|---|---|---|---|
| V01 | 4 | how much, to whom, how often | single payout inside the limit | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "requires": []}` | 3.9 | 123.81 | 571.9 | ✅ | 4,000 <= 5,000 -> autonomous |
| V02 | 4 | how much, to whom, how often | single payout exactly at the limit | plan | `{"decision": "APPROVED", "requires": []}` | 2.6 | 123.24 | 201.5 | ✅ | 5,000 is not above 5,000 -> autonomous |
| V03 | 4 | how much, to whom, how often | single payout one cent above the limit | plan | `{"decision": "REQUIRES_APPROVAL", "actions": {"O1": "PAY"}, "requires": ["single_payment_above_limit"]}` | 2.5 | 126.3 | 214.5 | ✅ | 5,000.01 > 5,000 -> a person |
| V04 | 4 | how much, to whom, how often | two payouts each inside the single limit | plan | `{"decision": "APPROVED", "requires": []}` | 2.6 | 126.02 | 124.8 | ✅ | the single limit is per payout, not per plan |
| V05 | 4 | how much, to whom, how often | payee period limit: history + now exceeds it | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["payee_period_limit_exceeded"]}` | 8.1 | 129.96 | 126.4 | ✅ | paid 4,000 + now 3,000 = 7,000 > 6,000 -> a person |
| V06 | 4 | how much, to whom, how often | payee period limit: history + now exactly at it | plan | `{"decision": "APPROVED", "requires": []}` | 3.0 | 125.03 | 135.4 | ✅ | 4,000 + 2,000 = 6,000, not above -> autonomous |
| V07 | 4 | how much, to whom, how often | payee period limit: two payouts to the same payee in one plan | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["payee_period_limit_exceeded"]}` | 2.6 | 129.52 | 128.9 | ✅ | 3,500 + 3,500 = 7,000 to one payee > 6,000 even with no history |
| V08 | 4 | how much, to whom, how often | a different payee is not affected by another's history | plan | `{"decision": "APPROVED", "requires": []}` | 2.4 | 130.93 | 129.4 | ✅ | limits are per payee |
| V09 | 4 | how much, to whom, how often | first payment to a new payee above the first-payment limit | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["first_payment_to_new_payee_above_limit"]}` | 2.0 | 131.84 | 189.3 | ✅ | ben-uk-logistics never PAID before; 3,000 > 1,000 -> a person |
| V10 | 4 | how much, to whom, how often | first payment to a new payee inside the first-payment limit | plan | `{"decision": "APPROVED", "requires": []}` | 2.0 | 126.96 | 128.8 | ✅ | 1,000 is not above 1,000 -> autonomous |
| V11 | 4 | how much, to whom, how often | a known payee is not subject to the first-payment limit | plan | `{"decision": "APPROVED", "requires": []}` | 2.7 | 128.62 | 152.1 | ✅ | ben-saas-vendor was PAID before -> no first-payment cap |
| V12 | 4 | how much, to whom, how often | no history at all: every payee is new | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["first_payment_to_new_payee_above_limit"]}` | 2.7 | 121.43 | 125.6 | ✅ | without history the books remember nobody -> first payment rule applies |
| V13 | 4 | how much, to whom, how often | daily outflow: rolling 24 h + now above the limit | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["daily_outflow_limit_exceeded"]}` | 2.6 | 124.67 | 137.6 | ✅ | 8,000 + 3,000 = 11,000 > 10,000 |
| V14 | 4 | how much, to whom, how often | daily outflow exactly at the limit | plan | `{"decision": "APPROVED", "requires": []}` | 2.3 | 132.78 | 205.6 | ✅ | 8,000 + 2,000 = 10,000, not above |
| V15 | 4 | how much, to whom, how often | weekly outflow above the limit while daily is fine | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["weekly_outflow_limit_exceeded"]}` | 1.9 | 123.17 | 135.6 | ✅ | 24h 1,000 + 3,000 = 4,000 <= 10,000; 7d 19,000 + 3,000 = 22,000 > 20,000 |
| V16 | 4 | how much, to whom, how often | several limits at once, every reason named | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["daily_outflow_limit_exceeded", "first_payment_to_new_payee_above_limit", "single_payment_above_limit"]}` | 2.1 | 127.73 | 125.6 | ✅ | 6,000 > 5,000 single; new payee 6,000 > 1,000; 9,000 + 6,000 > 10,000 daily -> three reasons |
| V17 | 4 | how much, to whom, how often | no limits set: nothing changes | plan | `{"decision": "APPROVED", "requires": []}` | 2.6 | 127.87 | 130.5 | ✅ | every limit null -> no reason |
| V18 | 4 | how much, to whom, how often | an escalated payout does not count as paying now | plan | `{"actions": {"O1": "ESCALATE"}, "requires": []}` | 3.0 | 127.5 | 187.4 | ✅ | the bank-change flag escalates it; nothing is paid now, so no limit reason |

## Suite 8 — payee lifecycle: NEW → VERIFIED → ACTIVE, cooling after a bank change, the call-back is mandatory

Plans against a payee registry: NEW, CHANGED, SUSPENDED and unregistered payees are ESCALATED (no payment, not even with an approval); a VERIFIED payee needs a person inside the cooling period (payee_in_cooling_period) and until its first PAID payout (payee_not_yet_active); ACTIVE adds nothing; an invoice announcing new details escalates even an ACTIVE payee; no registry = the control is off. The state machine itself: register, the call-back only by an approver role and only on the registered number (a number from the request is refused), a bank change that starts (or restarts) the cooling period, payments that promote only after it, suspend / reinstate (another call-back).

| Path | Kind | Cases | Match | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|---|
| in-process | plan | 14 | 14 | 2.5 | 3.7 | 3.9 |
| endpoint | plan | 14 | 14 | 90.6 | 109.7 | 119.8 |
| UI form | plan | 14 | 14 | 93.4 | 97.4 | 142.4 |
| in-process | payees | 18 | 18 | 0.0 | 0.0 | 0.1 |
| endpoint | payees | 18 | 18 | 1.1 | 1.8 | 1.9 |
| UI form | payees | 18 | 18 | 3.3 | 4.0 | 5.3 |

Server-side plan time (`duration_ms`, includes the live Airwallex balance check, the 72 h simulation and the counterfactuals): p50 82.3 ms, p95 102.1 ms.

### Cases

| ID | L | Pain (organizers) | Case | Kind | Expected | in-proc ms | endpoint ms | UI ms | Match | Derivation |
|---|---|---|---|---|---|---|---|---|---|---|
| Y01 | 4 | who may be paid at all, and when alone | an ACTIVE payee is paid autonomously | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "requires": []}` | 3.9 | 93.37 | 142.4 | ✅ | ACTIVE: verified and paid before -> this control adds nothing |
| Y02 | 4 | who may be paid at all, and when alone | a NEW payee is never paid: escalated to a person | plan | `{"decision": "APPROVED", "actions": {"O1": "ESCALATE"}, "paid": []}` | 2.1 | 109.71 | 95.6 | ✅ | NEW: nobody confirmed the bank details by call-back -> ESCALATE; no approval can pay it |
| Y03 | 4 | who may be paid at all, and when alone | a payee whose bank details changed is never paid until the call-back | plan | `{"decision": "APPROVED", "actions": {"O1": "ESCALATE"}, "paid": []}` | 2.7 | 89.77 | 97.2 | ✅ | CHANGED -> ESCALATE: paying the new account on the strength of an email is the classic fraud |
| Y04 | 4 | who may be paid at all, and when alone | a suspended payee is never paid | plan | `{"decision": "APPROVED", "actions": {"O1": "ESCALATE"}, "paid": []}` | 3.2 | 89.61 | 90.0 | ✅ | SUSPENDED -> ESCALATE |
| Y05 | 4 | who may be paid at all, and when alone | an unregistered payee (no record at all) is never paid | plan | `{"decision": "APPROVED", "actions": {"O1": "ESCALATE"}, "paid": []}` | 3.6 | 91.2 | 83.4 | ✅ | registry attached, no record for ben-nobody -> payee_unregistered -> ESCALATE |
| Y06 | 4 | who may be paid at all, and when alone | VERIFIED, never paid: the first payment needs a person | plan | `{"decision": "REQUIRES_APPROVAL", "actions": {"O1": "PAY"}, "requires": ["payee_not_yet_active"]}` | 2.5 | 100.11 | 94.3 | ✅ | call-back done, no PAID payout yet -> a person approves the first one |
| Y07 | 4 | who may be paid at all, and when alone | VERIFIED inside the cooling period after a bank change | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["payee_in_cooling_period"]}` | 2.2 | 93.1 | 93.6 | ✅ | now 23 Oct 12:00 < cooling_until 26 Oct 12:00 -> payee_in_cooling_period |
| Y08 | 4 | who may be paid at all, and when alone | the cooling period is over but the new details were never paid | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["payee_not_yet_active"]}` | 2.6 | 90.75 | 93.3 | ✅ | cooling ended one second ago -> not in cooling; still VERIFIED (never paid on these details) -> first payment by a person |
| Y09 | 4 | who may be paid at all, and when alone | cooling ends exactly now | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["payee_not_yet_active"]}` | 2.4 | 90.38 | 93.0 | ✅ | now == cooling_until is not before it -> the cooling period is over at that instant |
| Y10 | 4 | who may be paid at all, and when alone | one ACTIVE payee paid, one NEW payee escalated, in one plan | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY", "O2": "ESCALATE"}, "paid": ["O1"]}` | 2.5 | 89.37 | 88.4 | ✅ | the escalation does not stop the safe part of the plan |
| Y11 | 4 | who may be paid at all, and when alone | ACTIVE payee, but the invoice itself announces new bank details | plan | `{"decision": "APPROVED", "actions": {"O1": "ESCALATE"}, "paid": []}` | 3.7 | 119.75 | 94.3 | ✅ | the evidence flag escalates even when the registry still says ACTIVE (the registry is updated by the intake) |
| Y12 | 4 | who may be paid at all, and when alone | no registry attached: the lifecycle is not enforced | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "requires": []}` | 2.3 | 84.64 | 87.9 | ✅ | payees = null -> the control is off (plain plans and older suites unchanged) |
| Y13 | 4 | who may be paid at all, and when alone | a viewer cannot plan, whatever the payee | plan | `{"decision": "BLOCKED"}` | 2.2 | 89.58 | 97.4 | ✅ | role before everything |
| Y14 | 4 | who may be paid at all, and when alone | ACTIVE payee with a cooling_until in the past left on the record | plan | `{"decision": "APPROVED", "requires": []}` | 2.3 | 84.58 | 92.0 | ✅ | ACTIVE is ACTIVE: cooling only matters while VERIFIED |
| Z01 | 4 | who may be paid at all, and when alone | register a payee | payees | `{"status": "NEW", "bank_fingerprint": "bank:a1", "refused_n": 0, "payable": false}` | 0.0 | 1.49 | 5.3 | ✅ | registered -> NEW; not payable |
| Z02 | 4 | who may be paid at all, and when alone | the call-back on the registered number verifies | payees | `{"status": "VERIFIED", "cooling_until": null, "refused_n": 0, "payable": true, "autonomous": false}` | 0.0 | 1.2 | 3.1 | ✅ | finance_manager called the registered number -> VERIFIED; payable, but not alone yet |
| Z03 | 4 | who may be paid at all, and when alone | a call-back to a number from the request is refused | payees | `{"status": "NEW", "refused_n": 1, "last_refusal_contains": "registered number"}` | 0.0 | 0.87 | 3.1 | ✅ | the number must be the one on file; the record stays NEW |
| Z04 | 4 | who may be paid at all, and when alone | a treasury agent may not verify | payees | `{"status": "NEW", "refused_n": 1, "last_refusal_contains": "may not verify"}` | 0.0 | 1.03 | 3.3 | ✅ | only approver roles can confirm a payee |
| Z05 | 4 | who may be paid at all, and when alone | no registered number: no call-back possible | payees | `{"status": "NEW", "refused_n": 1, "last_refusal_contains": "no registered phone"}` | 0.0 | 1.87 | 4.0 | ✅ | register the number first |
| Z06 | 4 | who may be paid at all, and when alone | a bank change on an ACTIVE payee starts the cooling period | payees | `{"status": "CHANGED", "bank_fingerprint": "bank:b2", "cooling_until": "2026-10-26T12:00:00+00:00", "payable": false}` | 0.0 | 1.78 | 3.3 | ✅ | ACTIVE -> CHANGED; cooling_until = 23 Oct 12:00 + 72 h |
| Z07 | 4 | who may be paid at all, and when alone | the call-back after a change verifies but keeps the cooling period | payees | `{"status": "VERIFIED", "cooling_until": "2026-10-26T12:00:00+00:00", "payable": true, "autonomous": false}` | 0.0 | 1.12 | 3.1 | ✅ | VERIFIED with cooling_until unchanged |
| Z08 | 4 | who may be paid at all, and when alone | a payment inside the cooling period does not activate | payees | `{"status": "VERIFIED", "paid_count": 2, "autonomous": false}` | 0.1 | 1.41 | 3.6 | ✅ | paid 24 Oct < cooling_until 26 Oct -> stays VERIFIED (every payment still by a person) |
| Z09 | 4 | who may be paid at all, and when alone | a payment after the cooling period activates | payees | `{"status": "ACTIVE", "paid_count": 2, "autonomous": true}` | 0.0 | 1.16 | 3.6 | ✅ | paid at exactly cooling_until -> not before it -> ACTIVE |
| Z10 | 4 | who may be paid at all, and when alone | a new payee: verified, then the first payment activates | payees | `{"status": "ACTIVE", "paid_count": 1, "autonomous": true}` | 0.0 | 1.11 | 3.0 | ✅ | no cooling on a first verification -> ACTIVE at the first PAID payout |
| Z11 | 4 | who may be paid at all, and when alone | the same bank details are not a change | payees | `{"status": "ACTIVE", "refused_n": 1, "last_refusal_contains": "unchanged"}` | 0.0 | 1.15 | 3.4 | ✅ | fingerprint on file == fingerprint announced -> refused, nothing changes |
| Z12 | 4 | who may be paid at all, and when alone | paying a NEW payee is refused by the machine itself | payees | `{"status": "NEW", "paid_count": 0, "refused_n": 1, "last_refusal_contains": "cannot be paid"}` | 0.0 | 1.07 | 3.6 | ✅ | the gate never asks; if anything did, the record refuses |
| Z13 | 4 | who may be paid at all, and when alone | suspend, then a payment is refused, then reinstate goes back to NEW | payees | `{"status": "NEW", "refused_n": 1, "verified_by": null, "payable": false}` | 0.0 | 0.9 | 3.1 | ✅ | SUSPENDED refuses the payment; reinstating requires another call-back (NEW) |
| Z14 | 4 | who may be paid at all, and when alone | a change with cooling_hours 0 still needs the call-back, then one approved payment | payees | `{"status": "ACTIVE", "refused_n": 1, "cooling_until": "2026-10-23T12:00:00+00:00", "paid_count": 2}` | 0.0 | 1.11 | 3.3 | ✅ | paid on CHANGED refused; verify -> VERIFIED (cooling ends at once); paid at cooling_until -> ACTIVE |
| Z15 | 4 | who may be paid at all, and when alone | a second bank change while CHANGED restarts the cooling period | payees | `{"status": "CHANGED", "bank_fingerprint": "bank:c3", "cooling_until": "2026-10-27T12:00:00+00:00"}` | 0.0 | 1.13 | 3.1 | ✅ | the clock restarts from the latest change |
| Z16 | 4 | who may be paid at all, and when alone | a viewer may not suspend | payees | `{"status": "ACTIVE", "refused_n": 1}` | 0.0 | 1.46 | 3.5 | ✅ | role check on every person action |
| Z17 | 4 | who may be paid at all, and when alone | events on an unregistered payee are refused | payees | `{"status": null, "refused_n": 1, "last_refusal_contains": "unregistered"}` | 0.0 | 1.0 | 2.8 | ✅ | nothing to verify without a record |
| Z18 | 4 | who may be paid at all, and when alone | verifying an ACTIVE payee is refused (nothing pending) | payees | `{"status": "ACTIVE", "refused_n": 1, "last_refusal_contains": "nothing to verify"}` | 0.0 | 1.19 | 3.3 | ✅ | a call-back is for NEW or CHANGED |

## Suite 1 — the rules, one by one

37 treasury plans and 17 intake emails that exercise one rule each (fit / do not fit / exactly at the limit, priorities, evidence, duplicates, receipts, limits scaling with confidence, roles).

| Path | Kind | Cases | Match | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|---|
| in-process | plan | 37 | 37 | 3.1 | 5.4 | 6.9 |
| endpoint | plan | 37 | 37 | 90.8 | 94.7 | 97.6 |
| UI form | plan | 37 | 37 | 88.8 | 94.0 | 186.7 |
| in-process | intake | 17 | 17 | 1.8 | 2.2 | 3.6 |
| endpoint | intake | 17 | 17 | 4.4 | 5.2 | 5.3 |
| UI form | intake | 17 | 17 | 6.8 | 9.0 | 11.0 |

Server-side plan time (`duration_ms`, includes the live Airwallex balance check, the 72 h simulation and the counterfactuals): p50 82.3 ms, p95 89.9 ms.

### Cases

| ID | Case | Kind | Expected | in-proc ms | endpoint ms | UI ms | Match | Derivation |
|---|---|---|---|---|---|---|---|---|
| P01 | single obligation fits | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 3.5 | 94.11 | 186.7 | ✅ | headroom 5,000 >= 3,000 -> PAY; nothing needs a person |
| P02 | single obligation does not fit, no receipt | plan | `{"actions": {"O1": "ESCALATE"}, "not_blocked": true}` | 3.3 | 90.77 | 94.5 | ✅ | 6,000 > headroom 5,000 and no receipt -> ESCALATE |
| P03 | obligation equals the headroom exactly | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 2.7 | 94.65 | 88 | ✅ | 5,000 <= 5,000 -> PAY (cash after = floor, allowed) |
| P04 | one cent over the headroom | plan | `{"actions": {"O1": "ESCALATE"}}` | 2.6 | 88.48 | 89.3 | ✅ | 5,000.01 > 5,000 -> cannot be funded -> ESCALATE |
| P05 | two obligations both fit | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY", "O2": "PAY"}}` | 3.1 | 91.25 | 88.5 | ✅ | 2,000 + 2,500 = 4,500 <= 5,000 -> both PAY |
| P06 | two obligations, only the higher priority fits | plan | `{"actions": {"O1": "PAY", "O2": "ESCALATE"}}` | 2.9 | 86.85 | 88.8 | ✅ | priority 10 first: 3,000 fits; 3,000 more would breach -> ESCALATE |
| P07 | same priority, earlier due first | plan | `{"actions": {"O2": "PAY", "O1": "ESCALATE"}}` | 3.4 | 96.05 | 92.2 | ✅ | tie on priority -> due 12h paid, due 48h cannot be funded |
| P08 | duplicate invoice fingerprint | plan | `{"actions": {"O1": "PAY", "O2": "REJECT"}}` | 3.3 | 86.42 | 88.8 | ✅ | same fingerprint -> second copy REJECT, never paid twice |
| P09 | evidence rejected | plan | `{"actions": {"O1": "REJECT"}}` | 2.2 | 88.59 | 88.9 | ✅ | rejected evidence -> REJECT |
| P10 | evidence pending | plan | `{"actions": {"O1": "REJECT"}}` | 2.8 | 88.5 | 86.7 | ✅ | pending evidence -> REJECT |
| P11 | evidence missing | plan | `{"actions": {"O1": "REJECT"}}` | 2.5 | 91.68 | 83.1 | ✅ | no document -> REJECT |
| P12 | evidence names another obligation | plan | `{"actions": {"O1": "REJECT"}}` | 2.8 | 85.45 | 80.7 | ✅ | document is about another subject -> REJECT |
| P13 | already settled record | plan | `{"actions": {"O1": "REJECT"}}` | 2.4 | 92.43 | 86.1 | ✅ | settled -> not a payable -> REJECT |
| P14 | payee bank details changed | plan | `{"actions": {"O1": "ESCALATE"}}` | 3.3 | 90.85 | 82.5 | ✅ | escalate_flags -> ESCALATE even though it fits |
| P15 | viewer cannot plan | plan | `{"decision": "BLOCKED"}` | 2.3 | 85.69 | 92.3 | ✅ | actor_role viewer -> BLOCKED |
| P16 | finance manager plans like the agent | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 2.4 | 87.99 | 91.1 | ✅ | role only changes who may approve |
| P17 | does not fit now, reliable receipt lands before due | plan | `{"actions": {"O1": "DEFER"}}` | 3.1 | 93.3 | 90.2 | ✅ | 7,000 > 5,000; receipt 8,000 at 12h (90% >= 80%, 12h <= 72h) before due 48h -> DEFER |
| P18 | receipt not reliable (60%) | plan | `{"actions": {"O1": "ESCALATE"}}` | 3.4 | 93.57 | 89.8 | ✅ | 60% < 80% -> not relied on -> ESCALATE |
| P19 | receipt lands after due | plan | `{"actions": {"O1": "ESCALATE"}}` | 4.5 | 93.7 | 91.5 | ✅ | receipt at 48h > due 24h -> ESCALATE |
| P20 | receipt window ends after the horizon | plan | `{"actions": {"O1": "ESCALATE"}}` | 6.9 | 90.47 | 94 | ✅ | latest arrival 100h > 72h horizon -> not reliable -> ESCALATE |
| P21 | receipt covers one of two deferrals | plan | `{"actions": {"O1": "PAY", "O2": "ESCALATE"}}` | 3.4 | 89.06 | 90.2 | ✅ | O1 fits (4,000 <= 5,000); O2 needs 4,000, receipt only 3,000 -> ESCALATE |
| P22 | EUR payout converted inside the limit | plan | `{"decision": "APPROVED", "actions": {"O1": "CONVERT_AND_PAY"}, "n_conversions": 1}` | 2.6 | 89.62 | 88.6 | ✅ | EUR 1,000 + 12.85 fee = EUR 1,012.85 ~ USD 1,099; headroom 15,000; limit 10,000 (no receipt -> full) -> autonomous |
| P23 | conversion above the autonomous limit | plan | `{"decision": "REQUIRES_APPROVAL", "actions": {"O1": "CONVERT_AND_PAY"}, "requires": ["conversion_above_autonomous_limit"]}` | 3.5 | 87.86 | 85.2 | ✅ | EUR 12,012.85 ~ USD 13,034 > limit 10,000 (headroom 25,000 is fine) -> a person approves |
| P24 | limit zero: every conversion needs a person | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["conversion_above_autonomous_limit"]}` | 3.5 | 89.74 | 88 | ✅ | any conversion > 0 -> REQUIRES_APPROVAL |
| P25 | EUR wallet already covers the EUR payout | plan | `{"decision": "APPROVED", "n_conversions": 0, "paid": ["O1"]}` | 2.5 | 90.67 | 86.7 | ✅ | EUR 2,000 >= 1,012.85 -> no conversion, cost 0 |
| P26 | GBP wallet covers part: convert the shortfall only | plan | `{"decision": "APPROVED", "conversions": {"GBP": 60000}}` | 2.5 | 91.18 | 86.6 | ✅ | need GBP 1,000 (LOCAL, no fee), have 400 -> buy exactly 600 |
| P27 | stress eats the headroom | plan | `{"actions": {"O1": "ESCALATE"}}` | 6.2 | 97.61 | 90.1 | ✅ | headroom = 10,000 - 5,000 - 3,000 = 2,000 < 3,000 -> ESCALATE |
| P28 | stress leaves exactly enough (deterministic) but no margin (probabilistic) | plan | `{"decision": "REQUIRES_APPROVAL", "actions": {"O1": "PAY"}, "requires": ["reserve_breach_risk_above_tolerance"]}` | 5.4 | 91.11 | 86.2 | ✅ | deterministic: headroom = 10,000 - 5,000 - 2,000 = 3,000 = 3,000 -> PAY. Probabilistic: the 72h simulation draws the unexpected outflow with a lognormal size around 2,000, so landing exactly on the floor breaches it in about half the paths, far above the 5% tolerance -> a person approves. (The first hand answer missed the second rule; recorded as such.) |
| P29 | zero reserve floor | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 2.7 | 91.09 | 85.9 | ✅ | floor 0: cash 1,000 pays 1,000 |
| P30 | zero cash | plan | `{"paid": [], "not_blocked": true}` | 2.6 | 94.71 | 85.2 | ✅ | nothing can be paid; the plan is still a plan (escalates), not BLOCKED |
| P31 | twelve small obligations all fit | plan | `{"decision": "APPROVED", "paid": ["O1", "O2", "O3", "O4", "O5", "O6", "O7", "O8", "O9", "O10", "O11", "O12"]}` | 3.4 | 93.34 | 92.8 | ✅ | 12 x 100 = 1,200 <= 5,000 -> all PAY |
| P32 | large amounts, still inside the live sandbox wallet | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 2.5 | 88.35 | 88.3 | ✅ | USD 9,000,000 cash, USD 1,000,000 obligation, floor 5,000 -> PAY. (Amounts above the live wallet, e.g. USD 10bn, are BLOCKED by the live-balance check when the service is connected to the sandbox: the plan is grounded in real money.) |
| P33 | confidence 90%: limit is full | plan | `{"decision": "APPROVED", "limit": 1000000}` | 3.7 | 91.28 | 85.2 | ✅ | conf 90% >= 90% -> limit 10,000; conversion ~USD 7,734 < 10,000 -> autonomous |
| P34 | confidence 70%: limit halves | plan | `{"decision": "REQUIRES_APPROVAL", "limit": 500000, "requires": ["conversion_above_autonomous_limit"]}` | 2.9 | 93.37 | 88.6 | ✅ | limit = 10,000 x (70-50)/(90-50) = 5,000; conversion ~7,734 > 5,000 -> a person |
| P35 | confidence 40%: limit zero | plan | `{"decision": "REQUIRES_APPROVAL", "limit": 0}` | 3.1 | 90.02 | 91.9 | ✅ | conf 40% <= 50% -> limit 0 -> any conversion needs a person |
| P36 | priority 10 due now and priority 1 due at the horizon both fit | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY", "O2": "PAY"}}` | 2.1 | 91.06 | 92.3 | ✅ | 4,000 <= 5,000 -> both PAY regardless of priority |
| P37 | cheapest obligation dropped, not the most urgent | plan | `{"actions": {"O1": "PAY", "O2": "ESCALATE"}}` | 3.5 | 88.52 | 88.9 | ✅ | 4,000 fits; 1,500 more = 5,500 > 5,000 -> O2 cannot be funded |
| I01 | registered mailbox, clean invoice | intake | `{"status": "VERIFIED", "trust": "registered", "kind": "supplier_invoice", "amount_minor": 125000, "currency": "USD", "invoice_no": "LD-90", "due_date": "2026-11-05"}` | 3.6 | 4.84 | 9 | ✅ | exact registered gmail address; amount + invoice quoted; no bank change |
| I02 | unknown mailbox with the same display name | intake | `{"status": "QUARANTINED", "trust": "unknown_sender", "claimed_name": "Lumen Design Studio"}` | 1.9 | 4.37 | 6.6 | ✅ | display name is not identity; unknown mailbox asking for money -> QUARANTINED |
| I03 | registered mailbox announcing new bank details | intake | `{"status": "NEEDS_REVIEW", "trust": "registered", "kind": "bank_change", "flags": ["payee_bank_details_changed"]}` | 1.8 | 3.99 | 7.1 | ✅ | registered sender but a bank change needs an out-of-band call-back -> NEEDS_REVIEW, never auto-payable |
| I04 | unknown mailbox, bank change, urgency | intake | `{"status": "QUARANTINED", "kind": "bank_change", "flags": ["payee_bank_details_changed", "urgency_pressure"]}` | 1.7 | 4.05 | 6.6 | ✅ | classic payment-diversion pattern -> QUARANTINED |
| I05 | registered mailbox but Reply-To elsewhere | intake | `{"status": "QUARANTINED", "trust": "reply_to_mismatch"}` | 1.8 | 5.26 | 6.2 | ✅ | Reply-To is not a registered mailbox -> reply_to_mismatch -> QUARANTINED |
| I06 | registered customer remittance (outlook) | intake | `{"status": "VERIFIED", "kind": "remittance_notice", "amount_minor": 200000, "due_date": "2026-10-30"}` | 1.7 | 5.18 | 5.6 | ✅ | registered payer mailbox + remittance language -> receivable |
| I07 | registered contractor timesheet (yahoo) | intake | `{"status": "VERIFIED", "kind": "contractor_claim", "amount_minor": 100000, "invoice_no": "OK-2026-11", "due_date": "2026-11-07"}` | 1.7 | 4.72 | 6.1 | ✅ | registered yahoo mailbox; amount is the total (largest figure); within 14 days of 24 Oct = 7 Nov |
| I08 | registered mailbox, no amount readable | intake | `{"status": "NEEDS_REVIEW", "trust": "registered"}` | 1.7 | 3.96 | 6.5 | ✅ | nothing to pay can be read -> a person looks |
| I09 | injection text cannot verify itself | intake | `{"status": "QUARANTINED", "trust": "unknown_sender"}` | 2.0 | 4.07 | 7.1 | ✅ | the text is data; trust comes from the registry only |
| I10 | registered sender with pressure language | intake | `{"status": "NEEDS_REVIEW", "flags": ["urgency_pressure"]}` | 2.2 | 3.27 | 6.9 | ✅ | registered, but pressure language -> a person confirms first |
| I11 | html body | intake | `{"status": "VERIFIED", "amount_minor": 32000, "invoice_no": "LD-97", "due_date": "2026-11-02"}` | 1.8 | 4.03 | 5.8 | ✅ | tags stripped; same reading |
| I12 | euro symbol and thousands separator | intake | `{"status": "VERIFIED", "amount_minor": 150000, "currency": "EUR", "due_date": "2026-11-10"}` | 1.8 | 4.7 | 7.1 | ✅ | € -> EUR; 1,500.00 -> 150000 minor |
| I13 | duplicate of an invoice already in the book | intake | `{"status": "DUPLICATE", "duplicate_of": "SHIPPING-UK"}` | 2.0 | 4.65 | 7.2 | ✅ | fingerprint thames-freight:TF-311 already in kit01_initial -> DUPLICATE |
| I14 | embargoed future date does not matter for triage, amount in subject-less body | intake | `{"status": "VERIFIED", "amount_minor": 1000, "invoice_no": "LD-99", "due_date": "2026-12-01"}` | 1.7 | 4.53 | 11 | ✅ | no subject; fields read from the body |
| I15 | corporate domain match (non-freemail) | intake | `{"status": "VERIFIED", "trust": "registered", "currency": "EUR", "amount_minor": 400000, "invoice_no": "INV-2026-0700"}` | 1.6 | 3.83 | 6.8 | ✅ | exact registered corporate mailbox -> registered |
| I16 | same corporate domain, different mailbox | intake | `{"status": "VERIFIED", "trust": "registered"}` | 1.6 | 4.61 | 7.4 | ✅ | registered domain (not freemail) -> registered |
| I17 | freemail lookalike of a corporate payee | intake | `{"status": "QUARANTINED", "trust": "unknown_sender", "claimed_name": "Rheinwerk Komponenten GmbH"}` | 1.6 | 3.89 | 6.8 | ✅ | freemail is never matched by domain |

## Both suites on the real Airwallex sandbox

`python scripts/groundtruth_real.py`, 2026-10-23 13:23 UTC, approved by the owner as finance manager.

**Part A** — the 120 plan / intake / incident cases through the live service: **120 / 120 match**; 90 / 90 plans had their `Airwallex · live balances` stage read from the sandbox (none SKIPPED); plan round trip p50 89.8 ms.

**Part B** — the execution anomalies on the real rail, a fresh book per run (new obligation ids and invoice fingerprints, so nothing already paid is re-paid): **7 / 7 match**; 9 cannot be forced on a real rail.

| ID | Case | On the real rail | Result | ms | Match |
|---|---|---|---|---|---|
| N04 | planning rate stale: the rail is 3% dearer -> nothing booked | the planning rate is held stale (EUR 1.05) while the sandbox quotes the live rate (~1.13): the quote is above the slippage limit | plan APPROVED · execution FAILED | 755 | ✅ |
| N09 | the deposit stays PENDING: the receipt is not counted | not reproducible: the sandbox settles a simulated deposit by itself; a deposit that never settles cannot be requested | fake sandbox only | - | – |
| N26 | an approval given on different terms is refused | a finance manager's approval carrying another plan's terms | plan REQUIRES_APPROVAL · execution REFUSED | 179 | ✅ |
| N27 | a viewer 'approves' | an approval by a viewer | plan REQUIRES_APPROVAL · execution REFUSED | 230 | ✅ |
| N28 | REQUIRES_APPROVAL executed without a person | REQUIRES_APPROVAL executed with no approval | plan REQUIRES_APPROVAL · execution REFUSED | 194 | ✅ |
| N29 | the transfer response is lost: recovered by request_id, paid once | not reproducible: a lost HTTP response cannot be injected into the real rail (covered on the fake; the recovery path is request_id lookup, which the real rail supports) | fake sandbox only | - | – |
| N30 | 429 storm on transfer create | not reproducible: 429 storms cannot be requested from the sandbox | fake sandbox only | - | – |
| N31 | the rail answers duplicate_request_id | not reproducible: duplicate_request_id would require re-sending a used request_id, which the executor never does by design | fake sandbox only | - | – |
| N32 | wallet read says yes, rail says balance_insufficient | not reproducible: balance_insufficient at the rail needs the wallet to drop between the balance read and the transfer; the live-balance check prevents the setup | fake sandbox only | - | – |
| N33 | the conversion stays SCHEDULED: transfers are held | not reproducible: sandbox conversions settle immediately; a conversion that stays SCHEDULED cannot be requested | fake sandbox only | - | – |
| N39 | one payout fails after SENT: refund expected, fee lost | not reproducible: a bank return after SENT is exactly N44's mechanism on the real rail; not run twice | fake sandbox only | - | – |
| N40 | all three payouts fail after SENT | not reproducible: same mechanism as N44, three times; not run to keep the sandbox ledger readable | fake sandbox only | - | – |
| N41 | the same invoice executed twice: second run refused before any money moves | the same invoices executed twice on the real store: the payout claim refuses the second run | plan REQUIRES_APPROVAL · execution FAILED | 7676 | ✅ |
| N42 | the wallet holds less than the plan assumes | a treasury allocation larger than the live wallet (USD 20,000,000 against ~9.98 M): live balances do not cover the plan | plan BLOCKED · execution None | 89 | ✅ |
| N44 | bank return -> replace under the lock -> resolved only when the original is recorded | real transfer left in flight, failed on the sandbox with BENEFICIARY_BANK_RETURNED, replaced under the duplicate lock | plan APPROVED · execution IN_FLIGHT · incident RESOLVED (REPLACE, 1 follow-ups) | 14438 | ✅ |
| N45 | replacement would need a fee the wallet cannot cover | not reproducible: the replacement's budget check reads the live EUR wallet (~10 M), which cannot be set to EUR 1,512.85 | fake sandbox only | - | – |

## What the exercise changed

- **P28 / N24** — the hand answer first said APPROVED when the headroom exactly covers the payment. The service answered REQUIRES_APPROVAL: the 72-hour simulation draws the unexpected outflow with a lognormal size, so a plan that lands exactly on the floor breaches it in about half the paths, above the 5% tolerance. The probabilistic rule is the documented one; the expectation was corrected and the case kept.
- **P32** — USD 10 bn cash was BLOCKED by the live service (the sandbox wallet holds about USD 10 M) while passing in-process: a connected service grounds every plan in real balances. The case now uses amounts inside the wallet.
- **I04** — the lookalike's invoice LD-77 was already in the live intake queue, so the live service answered DUPLICATE instead of QUARANTINED: the queue is shared state and the duplicate lock applies to it. The case uses another invoice number.
- **N41** — paying the same invoices twice: a second *plan* of the same state after the first execution is BLOCKED by the live-balance check before the duplicate lock is even reached (the fake wallet is lower); the case tops the wallet up so that the lock itself is what refuses the second run (`duplicate payment blocked before any money moved`).
- **Harness** — execution cases set fake credentials in the environment; without restoring them, later plan cases saw a configured-but-unreachable Airwallex and were BLOCKED. Restored after each execution case.
