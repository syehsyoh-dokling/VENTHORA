# Ground truth: hand-derived answers vs the live service

Generated 2026-10-24 17:30 by `python scripts/groundtruth_report.py`.

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
| in-process | plan | 17 | 17 | 4.5 | 8.1 | 41.3 |
| endpoint | plan | 17 | 17 | 106.0 | 175.6 | 413.7 |
| UI form | plan | 17 | 17 | 100.0 | 147.4 | 148.2 |
| in-process | intake | 6 | 6 | 2.1 | 2.9 | 4.2 |
| endpoint | intake | 6 | 6 | 6.2 | 6.5 | 7.3 |
| UI form | intake | 6 | 6 | 9.6 | 10.4 | 10.7 |
| in-process | incident | 7 | 7 | 0.0 | 0.0 | 0.0 |
| endpoint | incident | 7 | 7 | 2.0 | 2.4 | 2.5 |
| UI form | incident | 7 | 7 | 4.9 | 5.0 | 5.7 |
| in-process | execution | 16 | 16 | 266.8 | 415.2 | 449.9 |
| endpoint | execution | 16 | n/a (in-process only) | - | - | - |
| UI form | execution | 16 | n/a (in-process only) | - | - | - |

Server-side plan time (`duration_ms`, includes the live Airwallex balance check, the 72 h simulation and the counterfactuals): p50 92.1 ms, p95 183.6 ms.

### Cases

| ID | L | Pain (organizers) | Case | Kind | Expected | in-proc ms | endpoint ms | UI ms | Match | Derivation |
|---|---|---|---|---|---|---|---|---|---|---|
| N01 | 1 | insufficient funds mid-run: fees and FX buffers | the SWIFT fee is what breaks the reserve | plan | `{"actions": {"O1": "ESCALATE"}}` | 41.3 | 175.55 | 148.2 | ✅ | EUR 4,600 = USD 4,991 fits the 5,000 headroom; + EUR 12.85 fee (USD 13.94) = 5,005 > 5,000 -> cannot be funded -> ESCALATE |
| N02 | 1 | insufficient funds mid-run: fees | a learned LOCAL fee is what breaks the reserve | plan | `{"actions": {"O1": "ESCALATE"}}` | 7.1 | 413.67 | 147.4 | ✅ | 4,998 + learned fee 3.00 = 5,001 > 5,000 -> ESCALATE; the guide said LOCAL is free, the sandbox charges 3.00 |
| N03 | 1 | rounding | one-cent obligations are still money | plan | `{"decision": "APPROVED", "paid": ["O1", "O2", "O3"]}` | 4.7 | 146.49 | 110.2 | ✅ | 3 cents <= 5,000 -> all PAY; integer minor units, no float |
| N04 | 1 | stale quotes | planning rate stale: the rail is 3% dearer -> nothing booked | execution | `{"execution": "FAILED", "message_contains": "slippage", "conversions": 0, "transfers": 0}` | 242.5 | n/a | n/a | ✅ | quoted sell above the 1% slippage limit -> halt before any conversion or transfer; re-plan at the real rate |
| N05 | 1 | FX buffer | FX stress makes a 'fits exactly' conversion not fit | plan | `{"actions": {"O1": "ESCALATE"}}` | 4.7 | 114.43 | 107.1 | ✅ | EUR 4,620.85 incl. fee = USD 5,013.6 > 5,000 even before the Horizon stress buffer -> ESCALATE |
| N06 | 2 | obligations fall due faster than cash arrives | the receipt slips past the due date | plan | `{"actions": {"O1": "ESCALATE"}}` | 4.2 | 101.71 | 89.4 | ✅ | 7,000 > 5,000; receipt at 48h > due 24h -> cannot be funded before due -> ESCALATE |
| N07 | 2 | a receipt forecast is not a receipt | the 90% window ends after the planning horizon | plan | `{"actions": {"O1": "ESCALATE"}}` | 8.1 | 111.26 | 107.5 | ✅ | latest arrival 96h > 72h -> not relied on -> ESCALATE |
| N08 | 2 | deadline already here | due now, cash short, no receipt | plan | `{"actions": {"O1": "ESCALATE"}, "not_blocked": true}` | 3.4 | 96.08 | 101.8 | ✅ | headroom 1,000 < 3,000; due 0h -> ESCALATE now, not DEFER |
| N09 | 2 | delayed settlement | the deposit stays PENDING: the receipt is not counted | execution | `{"receipt_landed": false, "execution": "COMPLETED"}` | 415.2 | n/a | n/a | ✅ | the balance may lag; only a deposit record that says SETTLED counts; the plan proceeds without it |
| N10 | 2 | a contradicted forecast | payer with a bad record: 40% confidence drops the FX limit to zero | plan | `{"decision": "REQUIRES_APPROVAL", "limit": 0, "requires": ["conversion_above_autonomous_limit"]}` | 4.0 | 101.34 | 95.4 | ✅ | confidence 40% <= 50% -> limit 0 -> every conversion needs a person |
| N11 | 2 | in flight for too long | SENT for 49 h: the wait window is over, still not final | incident | `{"action": "ESCALATE"}` | 0.0 | 1.62 | 4.9 | ✅ | not terminal and 49h > 48h window -> a person chases the rail; replacing could pay twice |
| N12 | 2 | a timeout is unconfirmed | PROCESSING for 1 h: wait, never replace | incident | `{"action": "WAIT"}` | 0.0 | 1.68 | 5 | ✅ | not final, inside the window -> WAIT |
| N13 | 3 | evidence requirements | evidence about the wrong obligation | plan | `{"actions": {"O1": "REJECT"}}` | 3.0 | 101.91 | 87.8 | ✅ | the document names another subject -> REJECT |
| N14 | 3 | duplicate invoice | the same invoice re-sent under a new id | plan | `{"actions": {"O1": "PAY", "O2": "REJECT"}}` | 3.3 | 100.38 | 94.5 | ✅ | same fingerprint -> the copy is REJECT |
| N15 | 3 | changed terms | payee announces new bank details | plan | `{"actions": {"O1": "ESCALATE"}}` | 4.5 | 112.32 | 89.7 | ✅ | policy exception -> ESCALATE even though cash covers it |
| N16 | 3 | payment diversion | lookalike mailbox, new bank account, urgency | intake | `{"status": "QUARANTINED", "trust": "unknown_sender", "flags": ["payee_bank_details_changed", "urgency_pressure"]}` | 4.2 | 6.5 | 9.3 | ✅ | trust comes from the registry, not the display name; money + bank change from an unknown mailbox -> QUARANTINED |
| N17 | 3 | prompt injection | the email tells the agent it is verified | intake | `{"status": "QUARANTINED"}` | 2.1 | 5.6 | 8.2 | ✅ | the text is data; status is decided by code |
| N18 | 3 | reply-to diversion | registered mailbox, Reply-To elsewhere | intake | `{"status": "QUARANTINED", "trust": "reply_to_mismatch"}` | 2.0 | 6.06 | 9.2 | ✅ | Reply-To is not registered -> QUARANTINED |
| N19 | 3 | bank change from the real payee | registered mailbox announces a new account | intake | `{"status": "NEEDS_REVIEW", "kind": "bank_change"}` | 1.9 | 6.15 | 10.7 | ✅ | even the real payee's bank change needs an out-of-band call-back |
| N20 | 3 | duplicate reminder | a reminder for an invoice already in the book | intake | `{"status": "DUPLICATE", "duplicate_of": "SHIPPING-UK"}` | 2.1 | 7.31 | 10.4 | ✅ | fingerprint already in kit01_initial -> DUPLICATE |
| N21 | 3 | pressure language from a real payee | registered sender, 'the CEO approved, pay immediately' | intake | `{"status": "NEEDS_REVIEW", "flags": ["urgency_pressure"]}` | 2.9 | 6.33 | 9.9 | ✅ | pressure language -> a person confirms before it is payable |
| N22 | 4 | limits | conversion above the autonomous limit | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["conversion_above_autonomous_limit"]}` | 4.7 | 97.0 | 96.3 | ✅ | USD ~13,034 > limit 10,000 -> a person |
| N23 | 4 | limits scale with confidence | 70% confidence halves the limit and the conversion no longer fits | plan | `{"decision": "REQUIRES_APPROVAL", "limit": 500000}` | 3.5 | 96.79 | 100 | ✅ | limit = 10,000 x (70-50)/40 = 5,000 < ~7,734 |
| N24 | 4 | risk tolerance | the plan lands exactly on the floor: probabilistic breach risk | plan | `{"actions": {"O1": "PAY"}, "requires": ["reserve_breach_risk_above_tolerance"]}` | 5.1 | 109.57 | 98 | ✅ | deterministic headroom 3,000 = 3,000 -> PAY, but a lognormal stress breaches the floor in ~half the paths > 5% -> a person |
| N25 | 4 | roles | a viewer asks for a plan | plan | `{"decision": "BLOCKED"}` | 3.9 | 97.54 | 100.9 | ✅ | viewer -> BLOCKED |
| N26 | 4 | approvals bound to exact terms | an approval given on different terms is refused | execution | `{"execution": "REFUSED", "transfers": 0}` | 109.3 | n/a | n/a | ✅ | limit 0 -> REQUIRES_APPROVAL; the approval's terms_digest does not match the plan -> REFUSED, nothing moves |
| N27 | 4 | approver role | a viewer 'approves' | execution | `{"execution": "REFUSED", "transfers": 0}` | 109.4 | n/a | n/a | ✅ | role viewer is not in approver_roles -> REFUSED |
| N28 | 4 | no approval at all | REQUIRES_APPROVAL executed without a person | execution | `{"execution": "REFUSED", "transfers": 0}` | 110.2 | n/a | n/a | ✅ | no approval -> REFUSED |
| N29 | 5 | a timeout is unconfirmed | the transfer response is lost: recovered by request_id, paid once | execution | `{"execution": "COMPLETED", "transfers": 3, "invariants_ok": true}` | 400.1 | n/a | n/a | ✅ | the create succeeded on the rail; the retry finds it by request_id instead of creating a second transfer |
| N30 | 5 | rate limits | 429 storm on transfer create | execution | `{"execution": "COMPLETED", "transfers": 3}` | 396.6 | n/a | n/a | ✅ | backoff and retry with the same request_id -> every payout once |
| N31 | 5 | duplicate request id | the rail answers duplicate_request_id | execution | `{"execution": "FAILED", "message_contains": "duplicate_request_id"}` | 265.2 | n/a | n/a | ✅ | the operation is recorded FAILED and the claim released; nothing is sent again blindly |
| N32 | 5 | insufficient funds at the rail | wallet read says yes, rail says balance_insufficient | execution | `{"execution": "FAILED", "message_contains": "balance_insufficient"}` | 268.4 | n/a | n/a | ✅ | halt, reconcile, report; later payouts are not attempted |
| N33 | 5 | PENDING states | the conversion stays SCHEDULED: transfers are held | execution | `{"execution": "IN_FLIGHT", "transfers": 0}` | 207.8 | n/a | n/a | ✅ | money not yet in the payout currency -> no transfer; the run is resumable |
| N34 | 5 | CANCELLED does not mean a person cancelled | CANCELLED with BENEFICIARY_BANK_RETURNED | incident | `{"action": "REPLACE"}` | 0.0 | 2.27 | 4.9 | ✅ | funds returned, payee record not in question -> REPLACE under the duplicate lock with a new request_id |
| N35 | 5 | wrong payee details | CANCELLED with INVALID_ACCOUNT_NAME_OR_NUMBER | incident | `{"action": "ESCALATE"}` | 0.0 | 2.05 | 5.7 | ✅ | paying again pays the wrong account again -> ESCALATE |
| N36 | 5 | a person or control stopped it | CANCELLED with RECALL_REQUESTED | incident | `{"action": "ESCALATE"}` | 0.0 | 2.4 | 4.4 | ✅ | only a person may restart it |
| N37 | 5 | unknown failure shape | FAILED with no typed failure | incident | `{"action": "ESCALATE", "reason_contains": "unknown"}` | 0.0 | 1.81 | 3.9 | ✅ | fail closed: UNKNOWN -> ESCALATE (this is what the real sandbox did in cycle 5, run 1) |
| N38 | 5 | one replacement only | a second return after one replacement | incident | `{"action": "ESCALATE"}` | 0.0 | 2.46 | 4.1 | ✅ | max_replacements 1 reached -> a person decides |
| N39 | 5 | bank return after SENT | one payout fails after SENT: refund expected, fee lost | execution | `{"execution": "FAILED", "message_contains": "SHIPPING-UK", "invariants_ok": true}` | 397.9 | n/a | n/a | ✅ | reconciliation expects the amount back and the fee gone; the other payouts stand |
| N40 | 5 | every payout fails | all three payouts fail after SENT | execution | `{"execution": "FAILED", "invariants_ok": true}` | 364.7 | n/a | n/a | ✅ | three refunds expected, three fees lost, reconciled to the cent |
| N41 | 6 | without paying twice | the same invoice executed twice: second run refused before any money moves | execution | `{"execution": "FAILED", "message_contains": "duplicate payment blocked", "invariants_ok": true}` | 449.9 | n/a | n/a | ✅ | the payout claim per invoice fingerprint survives across executions |
| N42 | 6 | stale state | the wallet holds less than the plan assumes | execution | `{"plan": "BLOCKED", "execution": null}` | 108.6 | n/a | n/a | ✅ | live balances do not cover the plan -> BLOCKED at proof time; nothing executes |
| N43 | 6 | only foreign cash | base wallet already below the floor, foreign wallets cover the payouts | plan | `{"paid": ["O1", "O2"], "n_conversions": 0, "requires": ["reserve_breach_risk_above_tolerance"]}` | 3.9 | 106.79 | 95.2 | ✅ | nothing is spent from USD, so the floor (a rule on USD spend) is not breached; the 100% breach risk goes to a person |
| N44 | 6 | Kit 03 end to end | bank return -> replace under the lock -> resolved only when the original is recorded | execution | `{"incident": "RESOLVED", "incident_decision": "REPLACE", "invariants_ok": true}` | 279.5 | n/a | n/a | ✅ | returned funds -> REPLACE with a new request_id and the second fee budgeted; RESOLVED after the original's CANCELLED/type/refund are recorded |
| N45 | 6 | no money for the second fee | replacement would need a fee the wallet cannot cover | execution | `{"incident": "ESCALATED", "incident_decision": "REPLACE"}` | 217.6 | n/a | n/a | ✅ | REPLACE decided, but EUR 1,513.38 needed and EUR 1,512.85 refunded -> refused, ESCALATED |
| N46 | 6 | the biggest anomaly: everything at once | cash short, receipt contradicted, bank change, duplicate, SWIFT fee, viewer-free | plan | `{"actions": {"O1": "ESCALATE", "O2": "CONVERT_AND_PAY", "O3": "REJECT", "O4": "ESCALATE", "O5": "ESCALATE"}, "paid": ["O2"]}` | 5.1 | 105.98 | 132.2 | ✅ | headroom 8,000 - 5,000 = 3,000. O1 5,000 > 3,000 and the 60% receipt is not relied on -> ESCALATE. O2 EUR 2,012.85 incl. fee = USD 2,184 <= 3,000 -> CONVERT_AND_PAY. O3 same fingerprint as O2 -> REJECT. O4 bank-change flag -> ESCALATE. O5 1,000 > 3,000 - 2,184 = 816 (and O2 at priority 9 outranks O5 at 2) -> ESCALATE. Nothing else is paid. |

## Suite 3 — Budget · mandate: the period's plan is the agent's mandate

20 cases around every threshold of a budget line: inside the auto share, exactly the mandate, one cent over (the agent stops and raises a BUDGET REQUEST with the exact shortfall), above the auto share (a person), the alert threshold with and without require_approval_on_alert, realized spend eating the line, a grant restoring it, two lines, priority inside a line, unbudgeted obligations, bypass signed or not, the whole November book against the November plan, minus from what is still owed, a viewer, and no budget at all. Real-sandbox cycle 8 (SANDBOX_RESULTS.md) runs the same mandate end to end: pay inside the line, stop at it, grant, continue.

| Path | Kind | Cases | Match | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|---|
| in-process | plan | 20 | 20 | 3.5 | 5.2 | 5.4 |
| endpoint | plan | 20 | 20 | 122.0 | 128.0 | 129.3 |
| UI form | plan | 20 | 20 | 102.5 | 163.0 | 378.1 |

Server-side plan time (`duration_ms`, includes the live Airwallex balance check, the 72 h simulation and the counterfactuals): p50 115.3 ms, p95 420.0 ms.

### Cases

| ID | L | Pain (organizers) | Case | Kind | Expected | in-proc ms | endpoint ms | UI ms | Match | Derivation |
|---|---|---|---|---|---|---|---|---|---|---|
| B01 | 4 | the agent acts within its mandate | inside the line, inside the auto share | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "budget_status": {"suppliers": "auto"}, "requires": []}` | 5.2 | 129.28 | 378.1 | ✅ | 3,000 of 10,000 = 30% < alert 80%, < auto 100% -> PAY autonomously |
| B02 | 4 | the agent acts within its mandate | exactly the mandate | plan | `{"actions": {"O1": "PAY"}, "budget_available": {"suppliers": 0}, "requires": []}` | 3.4 | 122.71 | 114.4 | ✅ | 10,000 = mandate -> cap allows it (<=); utilisation 100% >= alert 80% -> ALERT, but the line does not require approval on alert -> APPROVED with an alert |
| B03 | 4 | the agent acts within its mandate | one cent over the mandate: the agent stops at the line | plan | `{"actions": {"O1": "ESCALATE"}, "paid": [], "budget_requests": {"suppliers": 1}}` | 2.8 | 121.82 | 97.9 | ✅ | cap = 10,000; 10,000.01 does not fit -> ESCALATE with a budget request for the shortfall 0.01 |
| B04 | 4 | the agent acts within its mandate | above the auto share needs a person | plan | `{"decision": "REQUIRES_APPROVAL", "actions": {"O1": "PAY"}, "requires": ["budget_line_above_auto_share"]}` | 3.6 | 121.37 | 107.6 | ✅ | 8,500 > 10,000 x 80% = 8,000 -> payable, but a person approves |
| B05 | 4 | the agent acts within its mandate | exactly the auto share is still autonomous | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "requires": []}` | 4.3 | 126.53 | 99.4 | ✅ | 8,000 = 80% of 10,000: not above -> autonomous; 80% < alert 90% -> no alert |
| B06 | 4 | the agent acts within its mandate | alert threshold reached (notification, still autonomous) | plan | `{"decision": "APPROVED", "budget_alerts": 1, "budget_status": {"suppliers": "alert"}}` | 3.1 | 121.48 | 102.1 | ✅ | 82% >= alert 80% -> ALERT raised; auto share 100% -> no approval needed |
| B07 | 4 | the agent acts within its mandate | alert threshold with require_approval_on_alert | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["budget_line_alert_requires_approval"], "budget_alerts": 1}` | 3.2 | 122.18 | 163 | ✅ | 82% >= 80% and the line demands a person at the alert -> REQUIRES_APPROVAL |
| B08 | 4 | the agent acts within its mandate | realized spend eats the line | plan | `{"actions": {"O1": "ESCALATE"}, "budget_requests": {"suppliers": 1}}` | 3.8 | 123.51 | 101.6 | ✅ | cap = 10,000 - 7,500 = 2,500 < 3,000 -> ESCALATE + request for 500 |
| B09 | 4 | the agent acts within its mandate | a grant restores the mandate | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "budget_available": {"suppliers": 50000}}` | 3.4 | 127.99 | 102.5 | ✅ | mandate 11,000 - realized 7,500 = 3,500 >= 3,000 -> PAY; available after = 500 |
| B10 | 4 | the agent acts within its mandate | two lines, one exhausted, the other fine | plan | `{"actions": {"O1": "ESCALATE", "O2": "PAY"}, "budget_requests": {"suppliers": 1}}` | 5.0 | 122.29 | 105.7 | ✅ | suppliers cap 2,500 < 3,000 -> ESCALATE + request 500; saas 2,000 of 5,000 -> PAY |
| B11 | 4 | the agent acts within its mandate | priority within a line: the line pays the urgent one | plan | `{"actions": {"O1": "PAY", "O2": "ESCALATE"}, "budget_requests": {"suppliers": 1}}` | 4.8 | 119.59 | 105.2 | ✅ | both fit the cash, only one fits the line (6,000 + 6,000 > 10,000): priority 10 paid, the other raises a request for 2,000 |
| B12 | 4 | the agent acts within its mandate | unbudgeted obligation needs a person | plan | `{"decision": "REQUIRES_APPROVAL", "actions": {"O1": "PAY"}, "requires": ["unbudgeted_obligation"]}` | 3.5 | 120.25 | 100.2 | ✅ | 'marketing' is no line of 2026-11 -> payable by cash, but a person must place it |
| B13 | 4 | the agent acts within its mandate | unbudgeted allowed by the budget's setting | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "requires": []}` | 2.3 | 122.61 | 104.1 | ✅ | the budget says unbudgeted obligations do not need a person |
| B14 | 4 | the agent acts within its mandate | bypass nobody signed keeps the cap | plan | `{"actions": {"O1": "ESCALATE"}, "budget_requests": {"suppliers": 1}}` | 4.2 | 116.66 | 100.5 | ✅ | unsigned bypass -> cap 2,000 applies -> ESCALATE + request |
| B15 | 4 | the agent acts within its mandate | signed bypass lifts the cap | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "budget_status": {"suppliers": "bypass"}, "requires": []}` | 3.4 | 119.16 | 98.1 | ✅ | signed bypass -> no cap, no approval reason; the line shows as bypass |
| B16 | 4 | the agent acts within its mandate | bypass signed but spending inside it is still recorded | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 2.9 | 115.67 | 102.9 | ✅ | realized 9,000 on a 2,000 line with a signed bypass: still payable; the pipeline shows it, nobody stops it |
| B17 | 4 | the agent acts within its mandate | the whole November book against the November plan | plan | `{"decision": "APPROVED", "paid": ["O1", "O2", "O3", "O4", "O5"], "budget_alerts": 1, "requires": []}` | 4.7 | 125.7 | 104.3 | ✅ | every line fits: payroll 89% (< alert 90%), logistics 75%, suppliers 57%, contractors 50% (< 60% alert), saas 80% = alert -> one alert, no approval |
| B18 | 4 | the agent acts within its mandate | minus from what is still owed | plan | `{"actions": {"O1": "PAY", "O2": "ESCALATE"}, "budget_minus": {"suppliers": 200000}, "budget_requests": {"suppliers": 1}}` | 5.4 | 121.02 | 97.1 | ✅ | 7,000 paid; 5,000 still owed; 12,000 - 10,000 = minus 2,000 -> request for 2,000 |
| B19 | 4 | the agent acts within its mandate | viewer with a budget is still blocked | plan | `{"decision": "BLOCKED"}` | 2.9 | 123.34 | 101.2 | ✅ | actor_role viewer -> BLOCKED regardless of budget |
| B20 | 4 | the agent acts within its mandate | no budget attached: nothing changes | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "budget_attached": false}` | 2.6 | 121.46 | 102.4 | ✅ | without a budget the plan is the old plan |

## Suite 4 — finance controls: instalments, three-way match, period cut-off, reconciliation cases

Instalments (a deposit then the balance: two payouts of one invoice under the duplicate lock, never past the contract total), the three-way match (contract − paid so far vs what the invoice claims; over-claims beyond tolerance need a person), the period cut-off (due after period_end carries forward to the next period; due before period_start is late-posted; a closed period takes no autonomous spend) and reconciliation cases (a mismatch opens a case that locks the book until a person closes it with a note).

| Path | Kind | Cases | Match | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|---|
| in-process | plan | 16 | 16 | 3.3 | 4.6 | 4.7 |
| endpoint | plan | 16 | 16 | 98.7 | 115.3 | 119.8 |
| UI form | plan | 16 | 16 | 89.8 | 101.1 | 146.5 |
| in-process | execution | 2 | 2 | 474.5 | 439.8 | 509.2 |
| endpoint | execution | 2 | n/a (in-process only) | - | - | - |
| UI form | execution | 2 | n/a (in-process only) | - | - | - |

Server-side plan time (`duration_ms`, includes the live Airwallex balance check, the 72 h simulation and the counterfactuals): p50 83.7 ms, p95 94.3 ms.

### Cases

| ID | L | Pain (organizers) | Case | Kind | Expected | in-proc ms | endpoint ms | UI ms | Match | Derivation |
|---|---|---|---|---|---|---|---|---|---|---|
| K01 | 4 | a deposit then the balance | deposit and balance of one invoice are two payouts | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY", "O2": "PAY"}}` | 4.6 | 119.77 | 146.5 | ✅ | same invoice, different instalments -> both payable; 3,000 + 7,000 = contract 10,000 |
| K02 | 4 | paying twice | the balance sent twice is a duplicate | plan | `{"actions": {"O1": "PAY", "O2": "REJECT"}}` | 3.4 | 101.44 | 84.5 | ✅ | same (invoice, instalment) -> the second copy is REJECT |
| K03 | 4 | over-claim on the balance | a balance that overshoots the contract is rejected | plan | `{"actions": {"O1": "REJECT"}}` | 2.5 | 95.58 | 89.4 | ✅ | 3,000 paid + 8,000 claimed = 11,000 > contract 10,000 -> REJECT before anything else |
| K04 | 4 | double-billing after a deposit | whole-invoice payout after a deposit is a duplicate of the invoice | plan | `{"actions": {"O1": "PAY", "O2": "PAY"}, "decision": "REQUIRES_APPROVAL", "requires": ["amount_mismatch"]}` | 3.8 | 100.1 | 101.1 | ✅ | O2 claims the whole 10,000 although 3,000 is paid: expected 7,000, claimed 10,000 -> over-claim -> a person (amount_mismatch); O1 unaffected |
| K05 | 4 | three-way match | invoice equals the contract | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "requires": []}` | 4.2 | 115.35 | 93.7 | ✅ | expected 10,000 = claimed -> matched |
| K06 | 4 | rounding and small variances | invoice inside tolerance (+0.4%) | plan | `{"decision": "APPROVED", "requires": []}` | 2.6 | 93.96 | 94.6 | ✅ | diff 40 <= tolerance max(5, 0.5% of 10,000 = 50) -> matched |
| K07 | 4 | over-claim | invoice above tolerance (+1%) | plan | `{"decision": "REQUIRES_APPROVAL", "actions": {"O1": "PAY"}, "requires": ["amount_mismatch"]}` | 2.8 | 97.26 | 91.9 | ✅ | diff 100 > 50 -> over-claim -> a person |
| K08 | 4 | under-claim | invoice below the contract (under-claim) is fine | plan | `{"decision": "APPROVED", "requires": []}` | 2.2 | 105.59 | 87.4 | ✅ | the payee bills less; paying less is never unsafe; noted as under-claim |
| K09 | 4 | controls compose | over-claim that is escalated anyway needs no second reason | plan | `{"actions": {"O1": "ESCALATE"}, "requires": []}` | 2.3 | 102.85 | 87.9 | ✅ | the bank-change flag already sends it to a person; the plan pays nothing on it, so no amount_mismatch |
| K10 | 4 | strict books | tolerance set to zero: one cent over needs a person | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["amount_mismatch"]}` | 4.7 | 101.58 | 94.3 | ✅ | tolerance 0 -> any excess is an over-claim |
| K11 | 4 | cut-off | due inside the period draws on the line | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "budget_carry_forward": 0, "budget_status": {"suppliers": "auto"}}` | 3.7 | 88.19 | 83.9 | ✅ | as_of 10 Nov + 24h = 11 Nov, inside Nov -> counted on 'suppliers' |
| K12 | 4 | cut-off | due after the period end carries forward | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "budget_carry_forward": 1, "budget_paying_in_period": 0}` | 3.0 | 83.12 | 86 | ✅ | 29 Nov + 72h = 2 Dec > 30 Nov -> next period's commitment; it is still payable from cash but draws on no November line |
| K13 | 4 | cut-off | explicit due_date after the period end | plan | `{"budget_carry_forward": 1}` | 2.9 | 84.53 | 86.8 | ✅ | due_date wins over due_in_hours; 5 Dec is after 30 Nov -> carry-forward |
| K14 | 4 | cut-off | late-posted: due before the period started | plan | `{"budget_late_posted": 1, "budget_carry_forward": 0, "actions": {"O1": "PAY"}}` | 3.9 | 87.73 | 90.2 | ✅ | 28 Oct < 1 Nov -> late-posted on the November statement, still payable |
| K15 | 4 | cut-off | closed period: spending against it needs a person | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["period_closed"]}` | 3.4 | 100.97 | 86.5 | ✅ | the statement was issued; a payment that draws on the closed period goes to a person |
| K16 | 4 | cut-off | closed period but the payment carries forward: no reason | plan | `{"decision": "APPROVED", "requires": []}` | 3.3 | 85.44 | 92.1 | ✅ | nothing is paid against November (carry-forward) -> period_closed does not apply |
| K17 | 4 | reconciliation mismatch | a mismatch opens a case and the book is locked for the next run | execution | `{"execution": "REFUSED", "message_contains": "locked by open reconciliation case"}` | 439.8 | n/a | n/a | ✅ | the rail debits EUR 0.63 it never reports on the transfer -> live EUR differs from expected by 63 minor -> RECONCILIATION_MISMATCH opens a case -> the second run is REFUSED before anything moves |
| K18 | 4 | reconciliation mismatch | closing the case unlocks the book | execution | `{"execution": "FAILED", "message_contains": "duplicate payment blocked"}` | 509.2 | n/a | n/a | ✅ | with the case closed by a person (note required), the next run reaches the duplicate lock as usual (the invoices were paid in run 1) |

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
| in-process | ledger | 15 | 15 | 0.0 | 0.1 | 0.1 |
| endpoint | ledger | 15 | 15 | 1.2 | 1.4 | 1.5 |
| UI form | ledger | 15 | 15 | 3.0 | 3.7 | 3.8 |

### Cases

| ID | L | Pain (organizers) | Case | Kind | Expected | in-proc ms | endpoint ms | UI ms | Match | Derivation |
|---|---|---|---|---|---|---|---|---|---|---|
| L01 | 4 | a book never spends another book's money | opening the book | ledger | `{"cash": {"USD": 2250000, "EUR": 0, "GBP": 0}, "posted": 1, "all_balanced": true}` | 0.1 | 1.31 | 3.8 | ✅ | cash +22,500 / funding −22,500 per currency; balanced |
| L02 | 4 | a book never spends another book's money | a conversion moves cash across currencies | ledger | `{"cash": {"USD": 1814606, "EUR": 401285, "GBP": 0}, "all_balanced": true}` | 0.1 | 1.45 | 3.0 | ✅ | USD −4,353.94 via fx; EUR +4,012.85 via fx; each currency balanced on its own |
| L03 | 4 | a book never spends another book's money | a commit takes amount + fee from cash | ledger | `{"cash": {"USD": 1449700, "EUR": 0, "GBP": 0}, "committed": {"USD": 800000, "EUR": 0, "GBP": 0}, "fees": {"USD": 300, "EUR": 0, "GBP": 0}}` | 0.0 | 1.2 | 2.7 | ✅ | cash −8,003; committed +8,000; fees +3 |
| L04 | 4 | a book never spends another book's money | settle turns in-flight money into expense | ledger | `{"committed": {"USD": 0, "EUR": 0, "GBP": 0}, "expense": {"USD": 800000, "EUR": 0, "GBP": 0}, "cash": {"USD": 1449700, "EUR": 0, "GBP": 0}}` | 0.0 | 1.04 | 3.5 | ✅ | committed −8,000 / expense +8,000; cash unchanged |
| L05 | 4 | a book never spends another book's money | a refund brings the amount back, not the fee | ledger | `{"cash": {"USD": 2249700, "EUR": 0, "GBP": 0}, "committed": {"USD": 0, "EUR": 0, "GBP": 0}, "fees": {"USD": 300, "EUR": 0, "GBP": 0}}` | 0.0 | 0.94 | 3.0 | ✅ | cash back to 22,497: the fee is gone for good |
| L06 | 4 | a book never spends another book's money | a commit beyond the book's cash is refused | ledger | `{"posted": 1, "cash": {"USD": 2250000, "EUR": 0, "GBP": 0}}` | 0.0 | 0.94 | 2.8 | ✅ | 23,000 > 22,500 -> refused; nothing posted; whatever the shared wallet holds |
| L07 | 4 | a book never spends another book's money | a commit in a currency the book does not hold is refused | ledger | `{"posted": 1}` | 0.0 | 1.34 | 3.7 | ✅ | EUR cash 0 -> refused until a conversion funds it |
| L08 | 4 | a book never spends another book's money | convert then commit in the bought currency | ledger | `{"posted": 3, "cash": {"USD": 1814606, "EUR": 0, "GBP": 0}, "committed": {"USD": 0, "EUR": 400000, "GBP": 0}, "fees": {"USD": 0, "EUR": 1285, "GBP": 0}}` | 0.1 | 1.52 | 2.8 | ✅ | EUR 4,012.85 bought covers 4,000 + 12.85 fee exactly -> EUR cash 0 |
| L09 | 4 | a book never spends another book's money | the real fee is larger than the conversion bought: refused before money moves | ledger | `{"posted": 2}` | 0.0 | 1.26 | 2.8 | ✅ | 4,013.37 > 4,012.85 -> the pre-flight refuses by 0.52 (on the rail the executor would record it as an overdraft to report) |
| L10 | 4 | a book never spends another book's money | a deposit funds the book | ledger | `{"cash": {"USD": 3250000, "EUR": 0, "GBP": 0}}` | 0.0 | 0.84 | 2.5 | ✅ | cash +10,000 / funding −10,000 |
| L11 | 4 | a book never spends another book's money | a grant funds the book | ledger | `{"cash": {"USD": 2360000, "EUR": 0, "GBP": 0}}` | 0.0 | 1.26 | 2.9 | ✅ | cash +1,100 |
| L12 | 4 | a book never spends another book's money | an adjustment can go either way | ledger | `{"cash": {"USD": 2249937, "EUR": 0, "GBP": 0}}` | 0.0 | 1.01 | 3.1 | ✅ | an unreported bank charge of 0.63 recorded by a person |
| L13 | 4 | a book never spends another book's money | an unbalanced entry is refused | ledger | `{"posted": 1}` | 0.0 | 1.29 | 2.6 | ✅ | unknown kind -> refused; the journal stays balanced |
| L14 | 4 | a book never spends another book's money | the wallet moves by exactly the cash lines | ledger | `{"cash_delta": {"USD": 1132200, "GBP": 0, "EUR": 0}, "committed": {"USD": 0, "EUR": 0, "GBP": 0}}` | 0.1 | 1.13 | 3.0 | ✅ | cash lines: USD +22,500 −3,175 −8,003 = +11,322; GBP +2,500 −2,500 = 0: reconciliation compares the wallet's movement to exactly this |
| L15 | 4 | a book never spends another book's money | two books on one wallet: each is its own ledger | ledger | `{"posted": 2, "cash": {"USD": 100000}, "committed": {"USD": 400000}}` | 0.0 | 0.76 | 3.0 | ✅ | book A has 5,000: 4,000 fits, another 2,000 does not — even if book B's money is in the same wallet |

## Suite 7 — limits and velocity: how much, to whom, how often

A payout above the single-payment limit; a payee whose paid-this-period plus paying-now exceeds its period limit (history from the spend ledger, two payouts in one plan count together); a payee never PAID before getting more than the first-payment limit (no history = everyone is new); rolling 24 h / 7 d outflow plus paying now above the daily / weekly limit; several reasons at once, each named; no limits = nothing changes; an escalated payout does not count as paying now.

| Path | Kind | Cases | Match | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|---|
| in-process | plan | 18 | 18 | 3.5 | 5.3 | 12.2 |
| endpoint | plan | 18 | 18 | 127.2 | 131.8 | 132.8 |
| UI form | plan | 18 | 18 | 135.5 | 214.5 | 571.9 |

Server-side plan time (`duration_ms`, includes the live Airwallex balance check, the 72 h simulation and the counterfactuals): p50 115.2 ms, p95 119.5 ms.

### Cases

| ID | L | Pain (organizers) | Case | Kind | Expected | in-proc ms | endpoint ms | UI ms | Match | Derivation |
|---|---|---|---|---|---|---|---|---|---|---|
| V01 | 4 | how much, to whom, how often | single payout inside the limit | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "requires": []}` | 4.4 | 123.81 | 571.9 | ✅ | 4,000 <= 5,000 -> autonomous |
| V02 | 4 | how much, to whom, how often | single payout exactly at the limit | plan | `{"decision": "APPROVED", "requires": []}` | 3.2 | 123.24 | 201.5 | ✅ | 5,000 is not above 5,000 -> autonomous |
| V03 | 4 | how much, to whom, how often | single payout one cent above the limit | plan | `{"decision": "REQUIRES_APPROVAL", "actions": {"O1": "PAY"}, "requires": ["single_payment_above_limit"]}` | 5.3 | 126.3 | 214.5 | ✅ | 5,000.01 > 5,000 -> a person |
| V04 | 4 | how much, to whom, how often | two payouts each inside the single limit | plan | `{"decision": "APPROVED", "requires": []}` | 3.4 | 126.02 | 124.8 | ✅ | the single limit is per payout, not per plan |
| V05 | 4 | how much, to whom, how often | payee period limit: history + now exceeds it | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["payee_period_limit_exceeded"]}` | 2.9 | 129.96 | 126.4 | ✅ | paid 4,000 + now 3,000 = 7,000 > 6,000 -> a person |
| V06 | 4 | how much, to whom, how often | payee period limit: history + now exactly at it | plan | `{"decision": "APPROVED", "requires": []}` | 12.2 | 125.03 | 135.4 | ✅ | 4,000 + 2,000 = 6,000, not above -> autonomous |
| V07 | 4 | how much, to whom, how often | payee period limit: two payouts to the same payee in one plan | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["payee_period_limit_exceeded"]}` | 2.7 | 129.52 | 128.9 | ✅ | 3,500 + 3,500 = 7,000 to one payee > 6,000 even with no history |
| V08 | 4 | how much, to whom, how often | a different payee is not affected by another's history | plan | `{"decision": "APPROVED", "requires": []}` | 3.2 | 130.93 | 129.4 | ✅ | limits are per payee |
| V09 | 4 | how much, to whom, how often | first payment to a new payee above the first-payment limit | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["first_payment_to_new_payee_above_limit"]}` | 3.4 | 131.84 | 189.3 | ✅ | ben-uk-logistics never PAID before; 3,000 > 1,000 -> a person |
| V10 | 4 | how much, to whom, how often | first payment to a new payee inside the first-payment limit | plan | `{"decision": "APPROVED", "requires": []}` | 4.2 | 126.96 | 128.8 | ✅ | 1,000 is not above 1,000 -> autonomous |
| V11 | 4 | how much, to whom, how often | a known payee is not subject to the first-payment limit | plan | `{"decision": "APPROVED", "requires": []}` | 3.5 | 128.62 | 152.1 | ✅ | ben-saas-vendor was PAID before -> no first-payment cap |
| V12 | 4 | how much, to whom, how often | no history at all: every payee is new | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["first_payment_to_new_payee_above_limit"]}` | 3.2 | 121.43 | 125.6 | ✅ | without history the books remember nobody -> first payment rule applies |
| V13 | 4 | how much, to whom, how often | daily outflow: rolling 24 h + now above the limit | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["daily_outflow_limit_exceeded"]}` | 4.1 | 124.67 | 137.6 | ✅ | 8,000 + 3,000 = 11,000 > 10,000 |
| V14 | 4 | how much, to whom, how often | daily outflow exactly at the limit | plan | `{"decision": "APPROVED", "requires": []}` | 3.6 | 132.78 | 205.6 | ✅ | 8,000 + 2,000 = 10,000, not above |
| V15 | 4 | how much, to whom, how often | weekly outflow above the limit while daily is fine | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["weekly_outflow_limit_exceeded"]}` | 4.1 | 123.17 | 135.6 | ✅ | 24h 1,000 + 3,000 = 4,000 <= 10,000; 7d 19,000 + 3,000 = 22,000 > 20,000 |
| V16 | 4 | how much, to whom, how often | several limits at once, every reason named | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["daily_outflow_limit_exceeded", "first_payment_to_new_payee_above_limit", "single_payment_above_limit"]}` | 3.2 | 127.73 | 125.6 | ✅ | 6,000 > 5,000 single; new payee 6,000 > 1,000; 9,000 + 6,000 > 10,000 daily -> three reasons |
| V17 | 4 | how much, to whom, how often | no limits set: nothing changes | plan | `{"decision": "APPROVED", "requires": []}` | 3.6 | 127.87 | 130.5 | ✅ | every limit null -> no reason |
| V18 | 4 | how much, to whom, how often | an escalated payout does not count as paying now | plan | `{"actions": {"O1": "ESCALATE"}, "requires": []}` | 3.4 | 127.5 | 187.4 | ✅ | the bank-change flag escalates it; nothing is paid now, so no limit reason |

## Suite 8 — payee lifecycle: NEW → VERIFIED → ACTIVE, cooling after a bank change, the call-back is mandatory

Plans against a payee registry: NEW, CHANGED, SUSPENDED and unregistered payees are ESCALATED (no payment, not even with an approval); a VERIFIED payee needs a person inside the cooling period (payee_in_cooling_period) and until its first PAID payout (payee_not_yet_active); ACTIVE adds nothing; an invoice announcing new details escalates even an ACTIVE payee; no registry = the control is off. The state machine itself: register, the call-back only by an approver role and only on the registered number (a number from the request is refused), a bank change that starts (or restarts) the cooling period, payments that promote only after it, suspend / reinstate (another call-back).

| Path | Kind | Cases | Match | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|---|
| in-process | plan | 14 | 14 | 3.8 | 4.5 | 4.8 |
| endpoint | plan | 14 | 14 | 90.6 | 109.7 | 119.8 |
| UI form | plan | 14 | 14 | 93.4 | 97.4 | 142.4 |
| in-process | payees | 18 | 18 | 0.0 | 0.0 | 0.1 |
| endpoint | payees | 18 | 18 | 1.1 | 1.8 | 1.9 |
| UI form | payees | 18 | 18 | 3.3 | 4.0 | 5.3 |

Server-side plan time (`duration_ms`, includes the live Airwallex balance check, the 72 h simulation and the counterfactuals): p50 82.3 ms, p95 102.1 ms.

### Cases

| ID | L | Pain (organizers) | Case | Kind | Expected | in-proc ms | endpoint ms | UI ms | Match | Derivation |
|---|---|---|---|---|---|---|---|---|---|---|
| Y01 | 4 | who may be paid at all, and when alone | an ACTIVE payee is paid autonomously | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "requires": []}` | 4.2 | 93.37 | 142.4 | ✅ | ACTIVE: verified and paid before -> this control adds nothing |
| Y02 | 4 | who may be paid at all, and when alone | a NEW payee is never paid: escalated to a person | plan | `{"decision": "APPROVED", "actions": {"O1": "ESCALATE"}, "paid": []}` | 4.8 | 109.71 | 95.6 | ✅ | NEW: nobody confirmed the bank details by call-back -> ESCALATE; no approval can pay it |
| Y03 | 4 | who may be paid at all, and when alone | a payee whose bank details changed is never paid until the call-back | plan | `{"decision": "APPROVED", "actions": {"O1": "ESCALATE"}, "paid": []}` | 3.2 | 89.77 | 97.2 | ✅ | CHANGED -> ESCALATE: paying the new account on the strength of an email is the classic fraud |
| Y04 | 4 | who may be paid at all, and when alone | a suspended payee is never paid | plan | `{"decision": "APPROVED", "actions": {"O1": "ESCALATE"}, "paid": []}` | 4.4 | 89.61 | 90.0 | ✅ | SUSPENDED -> ESCALATE |
| Y05 | 4 | who may be paid at all, and when alone | an unregistered payee (no record at all) is never paid | plan | `{"decision": "APPROVED", "actions": {"O1": "ESCALATE"}, "paid": []}` | 4.0 | 91.2 | 83.4 | ✅ | registry attached, no record for ben-nobody -> payee_unregistered -> ESCALATE |
| Y06 | 4 | who may be paid at all, and when alone | VERIFIED, never paid: the first payment needs a person | plan | `{"decision": "REQUIRES_APPROVAL", "actions": {"O1": "PAY"}, "requires": ["payee_not_yet_active"]}` | 4.5 | 100.11 | 94.3 | ✅ | call-back done, no PAID payout yet -> a person approves the first one |
| Y07 | 4 | who may be paid at all, and when alone | VERIFIED inside the cooling period after a bank change | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["payee_in_cooling_period"]}` | 3.7 | 93.1 | 93.6 | ✅ | now 23 Oct 12:00 < cooling_until 26 Oct 12:00 -> payee_in_cooling_period |
| Y08 | 4 | who may be paid at all, and when alone | the cooling period is over but the new details were never paid | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["payee_not_yet_active"]}` | 3.2 | 90.75 | 93.3 | ✅ | cooling ended one second ago -> not in cooling; still VERIFIED (never paid on these details) -> first payment by a person |
| Y09 | 4 | who may be paid at all, and when alone | cooling ends exactly now | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["payee_not_yet_active"]}` | 3.0 | 90.38 | 93.0 | ✅ | now == cooling_until is not before it -> the cooling period is over at that instant |
| Y10 | 4 | who may be paid at all, and when alone | one ACTIVE payee paid, one NEW payee escalated, in one plan | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY", "O2": "ESCALATE"}, "paid": ["O1"]}` | 3.4 | 89.37 | 88.4 | ✅ | the escalation does not stop the safe part of the plan |
| Y11 | 4 | who may be paid at all, and when alone | ACTIVE payee, but the invoice itself announces new bank details | plan | `{"decision": "APPROVED", "actions": {"O1": "ESCALATE"}, "paid": []}` | 3.9 | 119.75 | 94.3 | ✅ | the evidence flag escalates even when the registry still says ACTIVE (the registry is updated by the intake) |
| Y12 | 4 | who may be paid at all, and when alone | no registry attached: the lifecycle is not enforced | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "requires": []}` | 4.3 | 84.64 | 87.9 | ✅ | payees = null -> the control is off (plain plans and older suites unchanged) |
| Y13 | 4 | who may be paid at all, and when alone | a viewer cannot plan, whatever the payee | plan | `{"decision": "BLOCKED"}` | 2.8 | 89.58 | 97.4 | ✅ | role before everything |
| Y14 | 4 | who may be paid at all, and when alone | ACTIVE payee with a cooling_until in the past left on the record | plan | `{"decision": "APPROVED", "requires": []}` | 2.5 | 84.58 | 92.0 | ✅ | ACTIVE is ACTIVE: cooling only matters while VERIFIED |
| Z01 | 4 | who may be paid at all, and when alone | register a payee | payees | `{"status": "NEW", "bank_fingerprint": "bank:a1", "refused_n": 0, "payable": false}` | 0.0 | 1.49 | 5.3 | ✅ | registered -> NEW; not payable |
| Z02 | 4 | who may be paid at all, and when alone | the call-back on the registered number verifies | payees | `{"status": "VERIFIED", "cooling_until": null, "refused_n": 0, "payable": true, "autonomous": false}` | 0.0 | 1.2 | 3.1 | ✅ | finance_manager called the registered number -> VERIFIED; payable, but not alone yet |
| Z03 | 4 | who may be paid at all, and when alone | a call-back to a number from the request is refused | payees | `{"status": "NEW", "refused_n": 1, "last_refusal_contains": "registered number"}` | 0.0 | 0.87 | 3.1 | ✅ | the number must be the one on file; the record stays NEW |
| Z04 | 4 | who may be paid at all, and when alone | a treasury agent may not verify | payees | `{"status": "NEW", "refused_n": 1, "last_refusal_contains": "may not verify"}` | 0.0 | 1.03 | 3.3 | ✅ | only approver roles can confirm a payee |
| Z05 | 4 | who may be paid at all, and when alone | no registered number: no call-back possible | payees | `{"status": "NEW", "refused_n": 1, "last_refusal_contains": "no registered phone"}` | 0.0 | 1.87 | 4.0 | ✅ | register the number first |
| Z06 | 4 | who may be paid at all, and when alone | a bank change on an ACTIVE payee starts the cooling period | payees | `{"status": "CHANGED", "bank_fingerprint": "bank:b2", "cooling_until": "2026-10-26T12:00:00+00:00", "payable": false}` | 0.0 | 1.78 | 3.3 | ✅ | ACTIVE -> CHANGED; cooling_until = 23 Oct 12:00 + 72 h |
| Z07 | 4 | who may be paid at all, and when alone | the call-back after a change verifies but keeps the cooling period | payees | `{"status": "VERIFIED", "cooling_until": "2026-10-26T12:00:00+00:00", "payable": true, "autonomous": false}` | 0.1 | 1.12 | 3.1 | ✅ | VERIFIED with cooling_until unchanged |
| Z08 | 4 | who may be paid at all, and when alone | a payment inside the cooling period does not activate | payees | `{"status": "VERIFIED", "paid_count": 2, "autonomous": false}` | 0.0 | 1.41 | 3.6 | ✅ | paid 24 Oct < cooling_until 26 Oct -> stays VERIFIED (every payment still by a person) |
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

## Suite 9 — the approval matrix (four-eyes): who, and how many different people, must approve

Tiers by amount and by reason code, loosest first, the last applicable tier deciding (a plan that stops for a person at USD 1,000 needs one finance manager; at USD 25,000, or for a payee-change reason, two different people); no tier = one approver; no reason = no approval. The check the executor runs before money moves: an allowed role, the plan's own terms digest, two approvals from one person count once, and the requester may never approve their own plan.

| Path | Kind | Cases | Match | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|---|
| in-process | plan | 11 | 11 | 3.1 | 4.5 | 4.5 |
| endpoint | plan | 11 | 11 | 105.0 | 129.5 | 131.9 |
| UI form | plan | 11 | 11 | 92.7 | 128.9 | 172.6 |
| in-process | approvals | 15 | 15 | 0.0 | 0.0 | 0.0 |
| endpoint | approvals | 15 | 15 | 2.0 | 2.4 | 3.1 |
| UI form | approvals | 15 | 15 | 2.7 | 3.5 | 4.5 |

Server-side plan time (`duration_ms`, includes the live Airwallex balance check, the 72 h simulation and the counterfactuals): p50 90.5 ms, p95 112.1 ms.

### Cases

| ID | L | Pain (organizers) | Case | Kind | Expected | in-proc ms | endpoint ms | UI ms | Match | Derivation |
|---|---|---|---|---|---|---|---|---|---|---|
| Q01 | 4 | who may approve, and how many | a small plan that stops for a person: one finance manager | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["single_payment_above_limit"], "approval_tier": "one", "approvals_required": 1, "approval_roles": ["finance_manager"]}` | 3.8 | 106.4 | 172.6 | ✅ | USD 1,000 >= tier 'one' (0), < tier 'two' (25,000) -> one person |
| Q02 | 4 | who may approve, and how many | one cent below the two-person threshold | plan | `{"decision": "REQUIRES_APPROVAL", "approval_tier": "one", "approvals_required": 1}` | 2.8 | 131.91 | 128.9 | ✅ | 24,999.99 < 25,000 -> still one person |
| Q03 | 4 | who may approve, and how many | exactly the two-person threshold | plan | `{"decision": "REQUIRES_APPROVAL", "approval_tier": "two", "approvals_required": 2, "approval_roles": ["cfo", "finance_manager"]}` | 4.5 | 129.54 | 86.1 | ✅ | 25,000 >= 25,000 -> two different people |
| Q04 | 4 | who may approve, and how many | two payouts whose total crosses the threshold | plan | `{"decision": "REQUIRES_APPROVAL", "approval_tier": "two", "approvals_required": 2}` | 3.1 | 108.98 | 87.4 | ✅ | 13,000 + 12,000 = 25,000: the tier reads the plan's paying total, not one payout |
| Q05 | 4 | who may approve, and how many | the board tier | plan | `{"decision": "REQUIRES_APPROVAL", "approval_tier": "board", "approvals_required": 3, "approval_roles": ["cfo"]}` | 3.1 | 100.05 | 83.9 | ✅ | 100,000 >= tier 'board' -> three different CFO-role people; the last applicable tier wins |
| Q06 | 4 | who may approve, and how many | a small amount but a payee-change reason: the reason lifts the tier | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["payee_in_cooling_period"], "approval_tier": "two", "approvals_required": 2}` | 2.6 | 106.49 | 91.9 | ✅ | USD 1,000 would be tier 'one'; payee_in_cooling_period is listed on tier 'two' -> two people |
| Q07 | 4 | who may approve, and how many | no reason raised: no approval at all | plan | `{"decision": "APPROVED", "approvals_required": 0, "approval_tier": null}` | 2.3 | 105.03 | 95.6 | ✅ | USD 400 <= single limit 500 -> autonomous; the matrix is not consulted |
| Q08 | 4 | who may approve, and how many | no matrix in the policy: one approver from approver_roles | plan | `{"decision": "REQUIRES_APPROVAL", "approval_tier": "default", "approvals_required": 1, "approval_roles": ["finance_manager"]}` | 1.9 | 101.76 | 113.8 | ✅ | an empty matrix is the old rule: one finance manager |
| Q09 | 4 | who may approve, and how many | a matrix with only a two-person tier: small plans fall back to the default | plan | `{"decision": "REQUIRES_APPROVAL", "approval_tier": "default", "approvals_required": 1}` | 4.5 | 103.26 | 85.8 | ✅ | 1,000 < 25,000 and no payee reason -> no tier applies -> default |
| Q10 | 4 | who may approve, and how many | an escalated payout does not count toward the total | plan | `{"decision": "APPROVED", "actions": {"O1": "ESCALATE"}, "approvals_required": 0}` | 3.1 | 99.25 | 94.5 | ✅ | NEW payee -> ESCALATE, nothing paid, no reason raised -> no approval |
| Q11 | 4 | who may approve, and how many | a viewer: blocked before any tier | plan | `{"decision": "BLOCKED"}` | 2.9 | 97.95 | 92.7 | ✅ | role first |
| W01 | 4 | who may approve, and how many | two different people on the plan's terms | approvals | `{"ok": true, "tier": "two", "approvals_required": 2, "approved_by": ["Dana Finch", "Omar Reyes"], "problems_n": 0}` | 0.0 | 2.35 | 4.5 | ✅ | finance manager + CFO, both on digest D -> satisfied |
| W02 | 4 | who may approve, and how many | one approval where two are needed | approvals | `{"ok": false, "problems_n": 1, "first_problem_contains": "2 different people"}` | 0.0 | 1.97 | 3.4 | ✅ | 1 valid < 2 |
| W03 | 4 | who may approve, and how many | the same person twice | approvals | `{"ok": false, "problems_n": 2, "first_problem_contains": "approved twice"}` | 0.0 | 2.21 | 2.5 | ✅ | one person counts once -> 1 valid < 2 |
| W04 | 4 | who may approve, and how many | the same person twice, spelled differently | approvals | `{"ok": false, "first_problem_contains": "approved twice"}` | 0.0 | 1.65 | 2.8 | ✅ | names are compared normalised (case, spacing) |
| W05 | 4 | who may approve, and how many | the requester approves their own plan | approvals | `{"ok": false, "first_problem_contains": "may not approve it (four-eyes)"}` | 0.0 | 3.07 | 2.3 | ✅ | requested_by = Dana Finch -> her approval does not count -> 1 valid < 2 |
| W06 | 4 | who may approve, and how many | a role the tier does not allow | approvals | `{"ok": false, "first_problem_contains": "may not approve this tier"}` | 0.0 | 2.43 | 3.0 | ✅ | ap_clerk is not in [finance_manager, cfo] |
| W07 | 4 | who may approve, and how many | an approval on different terms | approvals | `{"ok": false, "first_problem_contains": "different terms"}` | 0.0 | 2.28 | 2.6 | ✅ | the CFO signed another plan's digest -> does not count |
| W08 | 4 | who may approve, and how many | one person is enough below the threshold | approvals | `{"ok": true, "tier": "one", "approvals_required": 1}` | 0.0 | 2.34 | 2.4 | ✅ | total 1,000 -> tier 'one' |
| W09 | 4 | who may approve, and how many | a small amount with a payee-change reason needs two | approvals | `{"ok": false, "tier": "two", "approvals_required": 2}` | 0.0 | 2.27 | 2.8 | ✅ | reason lifts the tier |
| W10 | 4 | who may approve, and how many | no reason: nothing to approve, any approvals are fine | approvals | `{"ok": true, "tier": null, "approvals_required": 0}` | 0.0 | 1.99 | 3.5 | ✅ | no reason raised -> no requirement |
| W11 | 4 | who may approve, and how many | the board tier needs three CFO-role people | approvals | `{"ok": false, "tier": "board", "approvals_required": 3, "problems_n": 2}` | 0.0 | 1.67 | 2.7 | ✅ | two CFOs valid, the finance manager's role is not allowed on this tier -> 2 < 3 |
| W12 | 4 | who may approve, and how many | the board tier satisfied | approvals | `{"ok": true, "approved_by": ["Omar Reyes", "Ana Souza", "Kim Park"]}` | 0.0 | 2.0 | 2.5 | ✅ | three different CFO-role people |
| W13 | 4 | who may approve, and how many | no matrix: the default tier takes one finance manager | approvals | `{"ok": true, "tier": "default", "approvals_required": 1}` | 0.0 | 1.86 | 3.4 | ✅ | approver_roles decides |
| W14 | 4 | who may approve, and how many | no matrix: a CFO is not in approver_roles | approvals | `{"ok": false, "first_problem_contains": "may not approve"}` | 0.0 | 1.47 | 2.3 | ✅ | the default tier allows only approver_roles |
| W15 | 4 | who may approve, and how many | a terms digest missing on the approval is accepted (the executor binds it) | approvals | `{"ok": true}` | 0.0 | 1.99 | 2.7 | ✅ | an approval without a digest is not 'different terms'; the CLI and API always set one |

## Suite 10 — FX exposure: open position per currency, daily conversion cap, minimum hedge ratio

The cash a plan leaves in a foreign currency (held + bought − paid) against a per-currency cap, exactly at and one cent over; the base sold in conversions over the last 24 h (from the journal, every book) plus this plan's conversions against a daily cap; foreign cash held plus bought against the foreign obligations due inside the planning horizon (a paid obligation is covered by construction, a deferred or escalated one is the exposure, beyond the horizon it is not one yet, nothing due means nothing to hedge); three reasons at once, each named; no limits = nothing changes.

| Path | Kind | Cases | Match | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|---|
| in-process | plan | 21 | 21 | 3.7 | 4.8 | 4.8 |
| endpoint | plan | 20 | 20 | 98.0 | 108.9 | 110.1 |
| UI form | plan | 20 | 20 | 102.0 | 137.6 | 266.7 |

Server-side plan time (`duration_ms`, includes the live Airwallex balance check, the 72 h simulation and the counterfactuals): p50 84.7 ms, p95 96.3 ms.

### Cases

| ID | L | Pain (organizers) | Case | Kind | Expected | in-proc ms | endpoint ms | UI ms | Match | Derivation |
|---|---|---|---|---|---|---|---|---|---|---|
| X01 | 4 | how much foreign currency may stay open, be sold, or stay unhedged | an exact conversion leaves no open position | plan | `{"decision": "APPROVED", "actions": {"O1": "CONVERT_AND_PAY"}, "requires": [], "conversions": {"EUR": 401285}}` | 3.8 | 93.89 | 266.7 | ✅ | EUR after = 0 + 4,012.85 − 4,012.85 = 0 <= cap 1,000 |
| X02 | 4 | how much foreign currency may stay open, be sold, or stay unhedged | the wallet already holds more EUR than the cap: a person decides | plan | `{"decision": "REQUIRES_APPROVAL", "actions": {"O1": "CONVERT_AND_PAY"}, "requires": ["open_position_above_cap"], "n_conversions": 0}` | 3.2 | 95.74 | 96.3 | ✅ | held 9,000 − paid 4,012.85 = 4,987.15 left > cap 1,000 -> the plan leaves the book over its cap (a foreign payout keeps the CONVERT_AND_PAY label even when the held EUR covers it: no conversion is booked) |
| X03 | 4 | how much foreign currency may stay open, be sold, or stay unhedged | open position exactly at the cap | plan | `{"decision": "APPROVED", "requires": []}` | 4.8 | 108.88 | 125.6 | ✅ | 5,012.85 − 4,012.85 = 1,000.00, not above 1,000 |
| X04 | 4 | how much foreign currency may stay open, be sold, or stay unhedged | one cent above the cap | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["open_position_above_cap"]}` | 2.8 | 99.75 | 100.4 | ✅ | 1,000.01 > 1,000 |
| X05 | 4 | how much foreign currency may stay open, be sold, or stay unhedged | the cap is per currency: GBP over, EUR fine | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["open_position_above_cap"]}` | 3.3 | 92.75 | 98.8 | ✅ | GBP 3,000 untouched by the plan > GBP cap 2,000; EUR 0 |
| X06 | 4 | how much foreign currency may stay open, be sold, or stay unhedged | no cap for a currency: nothing applies | plan | `{"decision": "APPROVED", "requires": []}` | 2.4 | 93.12 | 100.1 | ✅ | only EUR is capped |
| X07 | 4 | how much foreign currency may stay open, be sold, or stay unhedged | daily conversion cap: 24 h sales + now above it | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["daily_conversion_cap_exceeded"]}` | 2.7 | 102.32 | 103.6 | ✅ | sold 6,000 + now 4,353.95 (EUR 4,012.85 at 1.0850, rounded up) = 10,353.95 > 10,000 |
| X08 | 4 | how much foreign currency may stay open, be sold, or stay unhedged | daily conversion cap exactly reached | plan | `{"decision": "APPROVED", "requires": []}` | 4.5 | 95.83 | 92.0 | ✅ | 5,646.05 + 4,353.95 = 10,000.00, not above |
| X09 | 4 | how much foreign currency may stay open, be sold, or stay unhedged | no history: only this plan's conversions count | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["daily_conversion_cap_exceeded"]}` | 3.3 | 93.15 | 105.4 | ✅ | 0 + 4,353.95 > 4,000 |
| X10 | 4 | how much foreign currency may stay open, be sold, or stay unhedged | a plan without conversions does not touch the daily cap | plan | `{"decision": "APPROVED", "requires": []}` | 2.7 | 97.32 | 96.8 | ✅ | USD payout, nothing sold |
| X11 | 4 | how much foreign currency may stay open, be sold, or stay unhedged | hedge ratio: a paid foreign obligation is fully covered | plan | `{"decision": "APPROVED", "requires": []}` | 3.9 | 101.49 | 102.0 | ✅ | bought 4,012.85 / due 4,000 -> 100% (capped) >= 80% |
| X12 | 4 | how much foreign currency may stay open, be sold, or stay unhedged | hedge ratio: the second EUR obligation cannot be funded and stays unhedged | plan | `{"decision": "REQUIRES_APPROVAL", "actions": {"O1": "CONVERT_AND_PAY", "O2": "ESCALATE"}, "requires": ["hedge_ratio_below_minimum"]}` | 3.7 | 97.81 | 97.3 | ✅ | USD 10,000 − floor 5,000 funds one 4,353.95 conversion; covered 4,012.85 / due 8,000 = 50.16% < 80% |
| X13 | 4 | how much foreign currency may stay open, be sold, or stay unhedged | hedge ratio exactly at the minimum | plan | `{"decision": "APPROVED", "requires": []}` | 4.6 | 93.89 | 110.9 | ✅ | 401,285 × 10,000 // 800,000 = 5,016 bp, not below 5,016 -> autonomous |
| X14 | 4 | how much foreign currency may stay open, be sold, or stay unhedged | hedge ratio one bp below the minimum | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["hedge_ratio_below_minimum"]}` | 3.6 | 107.45 | 111.7 | ✅ | 5,016 < 5,017 -> a person |
| X15 | 4 | how much foreign currency may stay open, be sold, or stay unhedged | foreign cash already held counts as hedge | plan | `{"decision": "APPROVED", "actions": {"O1": "CONVERT_AND_PAY", "O2": "CONVERT_AND_PAY"}, "requires": []}` | 4.2 | 100.72 | 104.2 | ✅ | held EUR 4,000 + one conversion of EUR 4,025.70 (USD 4,367.89, inside the 5,000 above floor) pays both: covered 8,025.70 / due 8,000 -> 100% |
| X16 | 4 | how much foreign currency may stay open, be sold, or stay unhedged | an obligation beyond the horizon is not an exposure yet | plan | `{"decision": "APPROVED", "requires": []}` | 3.6 | 98.11 | 137.6 | ✅ | O2 due in 200 h > 72 h horizon -> due in horizon 4,000, covered 4,012.85 -> 100% |
| X17 | 4 | how much foreign currency may stay open, be sold, or stay unhedged | nothing due in a currency: nothing to hedge | plan | `{"decision": "APPROVED", "requires": []}` | 4.4 | 110.09 | 98.9 | ✅ | no foreign obligation -> coverage undefined -> no reason |
| X18 | 4 | how much foreign currency may stay open, be sold, or stay unhedged | three reasons at once, each named | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["daily_conversion_cap_exceeded", "hedge_ratio_below_minimum", "open_position_above_cap"]}` | 4.3 | 99.37 | 101.9 | ✅ | GBP 3,000 > 1,000; sold 4,353.95 > 4,000; EUR 50.16% < 80% |
| X19 | 4 | how much foreign currency may stay open, be sold, or stay unhedged | no FX limits: nothing changes | plan | `{"decision": "APPROVED", "actions": {"O1": "CONVERT_AND_PAY", "O2": "ESCALATE"}, "requires": []}` | 4.0 | 95.35 | 127.3 | ✅ | the control is off |
| X21 | 4 | how much foreign currency may stay open, be sold, or stay unhedged | a rejected duplicate is not an exposure to hedge | plan | `{"decision": "APPROVED", "actions": {"O1": "CONVERT_AND_PAY", "O2": "REJECT"}, "requires": []}` | 3.3 | - | - | ✅ | O2 repeats O1's invoice fingerprint -> REJECT; only O1's EUR 4,000 is due; bought 4,012.85 -> 100% (before the fix the duplicate counted: 50% < 80%) |
| X20 | 4 | how much foreign currency may stay open, be sold, or stay unhedged | a viewer: blocked before any FX rule | plan | `{"decision": "BLOCKED"}` | 4.8 | 102.51 | 94.4 | ✅ | role first |

## Suite 11 — invoice aging and dunning: how old what we owe is, who is reminding us, what lateness costs

Days overdue from the book's date and each due date, buckets current / 1-30 / 31-60 / 61-90 / 90+ per payee in the base currency; late interest accrued so far and projected over the horizon for what the plan leaves unpaid (the obligation's rate, else the policy default); a reminder, second reminder or final notice read from a payee's email raises the book's dunning level and never creates a second payable; an item overdue beyond the escalation days, a final notice, or a late-interest exposure above its cap left unpaid by the plan needs a person; a paid item is never a reason.

| Path | Kind | Cases | Match | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|---|
| in-process | plan | 15 | 15 | 3.7 | 4.9 | 5.0 |
| endpoint | plan | 15 | 15 | 95.4 | 101.5 | 118.4 |
| UI form | plan | 15 | 15 | 97.8 | 113.7 | 222.4 |
| in-process | intake | 5 | 5 | 2.1 | 2.2 | 2.7 |
| endpoint | intake | 5 | 5 | 6.2 | 6.6 | 8.8 |
| UI form | intake | 5 | 5 | 10.5 | 11.6 | 17.0 |
| in-process | aging | 10 | 10 | 0.0 | 0.1 | 0.1 |
| endpoint | aging | 10 | 10 | 2.0 | 2.2 | 2.8 |
| UI form | aging | 10 | 10 | 6.5 | 19.9 | 24.5 |

Server-side plan time (`duration_ms`, includes the live Airwallex balance check, the 72 h simulation and the counterfactuals): p50 85.6 ms, p95 91.2 ms.

### Cases

| ID | L | Pain (organizers) | Case | Kind | Expected | in-proc ms | endpoint ms | UI ms | Match | Derivation |
|---|---|---|---|---|---|---|---|---|---|---|
| G01 | 3 | what we owe, how late, what it costs | an overdue invoice the plan pays: accrued interest reported, no reason | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "requires": [], "aging_days": {"O1": 45}, "aging_buckets": {"O1": "31-60"}, "aging_accrued": {"O1": 9863}, "aging_exposure": 0}` | 5.0 | 90.16 | 222.4 | ✅ | 24 Oct − 9 Sep = 45 days -> bucket 31-60; accrued 10,000 × 8 % × 45/365 = 98.63; paid now -> exposure 0 |
| G02 | 3 | what we owe, how late, what it costs | an overdue invoice the plan cannot fund: escalated, and overdue beyond 30 days -> a person | plan | `{"decision": "REQUIRES_APPROVAL", "actions": {"O1": "ESCALATE"}, "requires": ["overdue_obligation_unpaid"], "aging_exposure": 10521}` | 4.2 | 118.4 | 103.7 | ✅ | USD 12,000 − floor 5,000 < 10,000 -> ESCALATE; 45 > 30 days -> overdue_obligation_unpaid; exposure = accrued 98.63 + 3 projected days 6.58 |
| G03 | 3 | what we owe, how late, what it costs | overdue but inside the escalation days: no reason | plan | `{"decision": "APPROVED", "actions": {"O1": "ESCALATE"}, "requires": [], "aging_days": {"O1": 14}, "aging_buckets": {"O1": "1-30"}}` | 3.0 | 99.81 | 105.0 | ✅ | 14 days <= 30 -> escalated to a person as unfundable, but not by this rule |
| G04 | 3 | what we owe, how late, what it costs | exactly at the escalation days | plan | `{"decision": "APPROVED", "requires": [], "aging_days": {"O1": 30}}` | 3.7 | 96.45 | 91.6 | ✅ | 30 is not above 30 |
| G05 | 3 | what we owe, how late, what it costs | a final notice left unpaid needs a person, whatever the age | plan | `{"decision": "REQUIRES_APPROVAL", "actions": {"O1": "ESCALATE"}, "requires": ["final_notice_unpaid"], "aging_days": {"O1": 0}, "aging_dunning": {"O1": 3}}` | 4.8 | 87.87 | 99.4 | ✅ | not even due yet, but the payee sent a final notice (L3) and the plan leaves it unpaid |
| G06 | 3 | what we owe, how late, what it costs | a second reminder is below the dunning threshold | plan | `{"decision": "APPROVED", "requires": []}` | 4.1 | 101.48 | 93.8 | ✅ | L2 < 3 |
| G07 | 3 | what we owe, how late, what it costs | a final notice the plan pays is not a reason | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "requires": []}` | 3.1 | 98.55 | 95.9 | ✅ | paid now -> never a reason |
| G08 | 3 | what we owe, how late, what it costs | late-interest exposure above its cap | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["late_interest_exposure_above_limit"], "aging_days": {"O1": 115}, "aging_buckets": {"O1": "90+"}, "aging_exposure": 25863}` | 3.2 | 97.65 | 92.3 | ✅ | 115 days: accrued 10,000 × 8 % × 115/365 = 252.05; projected 3 days 6.58; 258.63 > cap 200 |
| G09 | 3 | what we owe, how late, what it costs | exposure exactly at the cap | plan | `{"decision": "APPROVED", "requires": []}` | 3.6 | 86.39 | 97.8 | ✅ | 25,863 is not above 25,863 |
| G10 | 3 | what we owe, how late, what it costs | the obligation's own rate beats the policy default | plan | `{"decision": "APPROVED", "aging_accrued": {"O1": 14795}}` | 4.4 | 95.42 | 107.8 | ✅ | 10,000 × 12 % × 45/365 = 147.95 |
| G11 | 3 | what we owe, how late, what it costs | a foreign invoice is aged in the base currency | plan | `{"decision": "APPROVED", "actions": {"O1": "CONVERT_AND_PAY"}, "aging_accrued": {"O1": 4281}}` | 3.7 | 88.59 | 101.3 | ✅ | EUR 4,000 × 1.0850 = USD 4,340.00; × 8 % × 45/365 = 42.81 |
| G12 | 3 | what we owe, how late, what it costs | three reasons at once, each named | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["final_notice_unpaid", "late_interest_exposure_above_limit", "overdue_obligation_unpaid"]}` | 3.6 | 86.29 | 113.7 | ✅ | 115 d > 30; L3; 258.63 > 200 |
| G13 | 3 | what we owe, how late, what it costs | no thresholds: the table is reported, nothing stops | plan | `{"decision": "APPROVED", "actions": {"O1": "ESCALATE"}, "requires": [], "aging_days": {"O1": 115}}` | 2.9 | 99.64 | 96.0 | ✅ | the control reports; the policy sets no limit |
| G14 | 3 | what we owe, how late, what it costs | no as_of: nothing can be overdue | plan | `{"decision": "APPROVED", "requires": [], "aging_days": {"O1": 0}, "aging_buckets": {"O1": "current"}}` | 2.7 | 88.77 | 91.5 | ✅ | without the book's date nothing can be overdue: the item is current |
| G15 | 3 | what we owe, how late, what it costs | a viewer: blocked before any aging rule | plan | `{"decision": "BLOCKED"}` | 4.9 | 93.96 | 92.2 | ✅ | role first |
| H01 | 3 | what we owe, how late, what it costs | buckets and days per item | aging | `{"buckets": {"A": "current", "B": "1-30", "C": "61-90", "D": "90+"}, "days_overdue": {"A": 0, "B": 23, "C": 65, "D": 145}}` | 0.1 | 2.8 | 6.3 | ✅ | A due in 6 days; B 1 Oct -> 23 d; C 20 Aug -> 65 d; D 1 Jun -> 145 d; E is settled and left out |
| H02 | 3 | what we owe, how late, what it costs | per payee: overdue totals in base and the oldest item | aging | `{"payee_overdue": {"p1": 200000, "p2": 408500}, "payee_oldest": {"p1": 23, "p2": 145}}` | 0.0 | 1.75 | 8.2 | ✅ | p2: C 3,000 + D EUR 1,000 × 1.0850 = 1,085.00 |
| H03 | 3 | what we owe, how late, what it costs | accrued interest per item at the default 8 % | aging | `{"accrued": {"A": 0, "B": 1008, "C": 4274, "D": 3448}}` | 0.0 | 1.79 | 5.5 | ✅ | B 2,000 × 8 % × 23/365 = 10.08; C 3,000 × 65/365 × 8 % = 42.74; D 1,085 × 145/365 × 8 % = 34.48 |
| H04 | 3 | what we owe, how late, what it costs | projected interest only for what is left unpaid | aging | `{"projected": {"A": 0, "B": 0, "C": 197, "D": 71}, "exposure_minor": 7990}` | 0.0 | 1.93 | 4.7 | ✅ | 3 horizon days: C 3,000 × 8 % × 3/365 = 1.97; D 1,085 × 8 % × 3/365 = 0.71; exposure 79.90 |
| H05 | 3 | what we owe, how late, what it costs | reasons: escalation days, final notice, exposure cap | aging | `{"reasons": ["final_notice_unpaid", "late_interest_exposure_above_limit", "overdue_obligation_unpaid"]}` | 0.0 | 2.07 | 19.9 | ✅ | C and D unpaid beyond 30 d; D is L3; exposure 79.90 > 50 |
| H06 | 3 | what we owe, how late, what it costs | paying the old items clears every reason | aging | `{"reasons": [], "exposure_minor": 0}` | 0.0 | 1.93 | 14.1 | ✅ | paid now -> no projection, no reason |
| H07 | 3 | what we owe, how late, what it costs | a rejected item owes nothing | aging | `{"buckets": {"A": "current"}, "reasons": []}` | 0.0 | 2.11 | 6.3 | ✅ | duplicates / unverified items are left out of the table |
| H08 | 3 | what we owe, how late, what it costs | an item without a due date ages from due_in_hours | aging | `{"days_overdue": {"F": 0}, "buckets": {"F": "current"}}` | 0.0 | 2.16 | 6.7 | ✅ | as_of + 48 h is in the future |
| H09 | 3 | what we owe, how late, what it costs | the rate on the item overrides the default | aging | `{"accrued": {"G": 0}, "projected": {"G": 0}, "exposure_minor": 0}` | 0.0 | 1.84 | 24.5 | ✅ | a contract without late interest: 0 % beats the 8 % default |
| H10 | 3 | what we owe, how late, what it costs | no as_of: nothing is overdue | aging | `{"days_overdue": {"A": 0, "B": 0, "C": 0, "D": 0}, "reasons": []}` | 0.1 | 2.1 | 5.9 | ✅ | without a date the table cannot age anything |
| D01 | 3 | who is reminding us | a friendly reminder from a registered payee | intake | `{"status": "VERIFIED", "trust": "registered", "kind": "supplier_invoice", "dunning_level": 1, "flags": ["dunning_notice_L1"], "invoice_no": "CL-2041"}` | 2.7 | 8.81 | 17.0 | ✅ | registered mailbox; 'reminder' / 'overdue' -> L1; the invoice itself is new to the book -> a payable with dunning level 1 |
| D02 | 3 | who is reminding us | a second reminder | intake | `{"status": "VERIFIED", "dunning_level": 2, "flags": ["dunning_notice_L2"]}` | 2.2 | 6.64 | 7.4 | ✅ | 'second reminder' / 'remains unpaid' -> L2 |
| D03 | 3 | who is reminding us | a final notice | intake | `{"status": "VERIFIED", "dunning_level": 3, "flags": ["dunning_notice_L3"]}` | 1.8 | 5.42 | 10.5 | ✅ | 'final notice' / 'suspend your account' -> L3 (the highest phrase wins over 'past due') |
| D04 | 3 | who is reminding us | a final notice for an invoice already in the book is a duplicate that carries its level | intake | `{"status": "DUPLICATE", "dunning_level": 3, "duplicate_of": "SAAS-USD"}` | 2.1 | 6.24 | 7.8 | ✅ | the book already holds CL-2026-11 (SAAS-USD) -> never a second payable; the level travels to the existing obligation in to_state |
| D05 | 3 | who is reminding us | a final notice from an unregistered mailbox is quarantined, not believed | intake | `{"status": "QUARANTINED", "trust": "unknown_sender", "dunning_level": 3}` | 1.7 | 5.58 | 11.6 | ✅ | pressure from an unknown sender changes nothing: quarantined with the level recorded |

## Suite 12 — book and tenant segregation: whose book, which payees, whose signature, which policy

A book resolves against the tenant registry (exact id beats a glob pattern, the longest pattern wins, an unknown book goes to the default tenant unless the registry is strict, in which case the plan is BLOCKED); a payee the book's tenant did not allow is escalated and never paid, with or without an approval; the book's policy overrides apply on top of the shared policy; only the book's named approvers, in their roles, may approve its plans, on top of the approval matrix; an override of a policy field that does not exist is refused.

| Path | Kind | Cases | Match | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|---|
| in-process | plan | 9 | 9 | 2.9 | 4.0 | 4.2 |
| endpoint | plan | 9 | 9 | 96.7 | 108.2 | 112.7 |
| UI form | plan | 9 | 9 | 99.5 | 102.3 | 233.2 |
| in-process | approvals | 5 | 5 | 0.0 | 0.0 | 0.0 |
| endpoint | approvals | 5 | 5 | 1.6 | 1.8 | 1.8 |
| UI form | approvals | 5 | 5 | 4.4 | 4.8 | 5.1 |
| in-process | tenancy | 10 | 10 | 0.0 | 0.0 | 0.2 |
| endpoint | tenancy | 10 | 10 | 1.7 | 2.1 | 2.1 |
| UI form | tenancy | 10 | 10 | 4.8 | 5.2 | 5.3 |

Server-side plan time (`duration_ms`, includes the live Airwallex balance check, the 72 h simulation and the counterfactuals): p50 81.7 ms, p95 90.9 ms.

### Cases

| ID | L | Pain (organizers) | Case | Kind | Expected | in-proc ms | endpoint ms | UI ms | Match | Derivation |
|---|---|---|---|---|---|---|---|---|---|---|
| T01 | 4 | whose book, which payees, whose signature, which policy | the book pays one of its own payees | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "tenant": "acme", "book_known": true, "disallowed": []}` | 3.5 | 98.85 | 233.2 | ✅ | ben-saas-vendor is on ops-us's list -> paid |
| T02 | 4 | whose book, which payees, whose signature, which policy | a payee of another tenant is escalated, never paid | plan | `{"decision": "APPROVED", "actions": {"O1": "ESCALATE"}, "paid": [], "disallowed": ["O1"]}` | 2.8 | 108.19 | 97.1 | ✅ | ben-lumen-design is not a payee of ops-us -> policy exception -> ESCALATE; no approval can pay it |
| T03 | 4 | whose book, which payees, whose signature, which policy | one allowed, one not, in one plan | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY", "O2": "ESCALATE"}, "paid": ["O1"]}` | 2.6 | 95.51 | 101.0 | ✅ | segregation is per payout; the allowed one proceeds |
| T04 | 4 | whose book, which payees, whose signature, which policy | a wildcard book pays anyone | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "tenant": "lab"}` | 4.2 | 106.28 | 102.3 | ✅ | ['*'] allows every payee |
| T05 | 4 | whose book, which payees, whose signature, which policy | an unknown book under a strict registry is blocked | plan | `{"decision": "BLOCKED", "book_known": false, "failed_stages": ["Policy", "Tenancy \u00b7 book"]}` | 4.0 | 96.29 | 99.5 | ✅ | no tenant owns ghost-book and the registry is strict -> nothing may be spent from it |
| T06 | 4 | whose book, which payees, whose signature, which policy | no tenancy attached: the control is off | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}, "tenant": null}` | 2.9 | 93.25 | 101.2 | ✅ | plain plans and older suites unchanged |
| T07 | 4 | whose book, which payees, whose signature, which policy | the book's policy override lowers the single-payment limit | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["single_payment_above_limit"]}` | 2.5 | 94.97 | 99.2 | ✅ | ops-us overrides max_single_payment to USD 500 (attach merged it into the policy); USD 600 > 500 |
| T08 | 4 | whose book, which payees, whose signature, which policy | the book's override raises the reserve floor | plan | `{"decision": "APPROVED", "actions": {"O1": "ESCALATE"}}` | 3.0 | 112.67 | 95.8 | ✅ | USD 10,000 − floor 8,000 = 2,000 < 3,000 -> cannot be funded under this book's floor (5,000 would have paid it) |
| T09 | 4 | whose book, which payees, whose signature, which policy | a viewer: blocked before any tenancy rule | plan | `{"decision": "BLOCKED"}` | 2.4 | 96.74 | 98.6 | ✅ | role first |
| U01 | 4 | whose book, which payees, whose signature, which policy | an exact book id | tenancy | `{"known": true, "tenant": "acme", "matched_by": "exact", "allowed_payees": ["ben-saas-vendor", "payroll-group-us"]}` | 0.0 | 1.87 | 4.2 | ✅ | exact entry |
| U02 | 4 | whose book, which payees, whose signature, which policy | exact beats pattern | tenancy | `{"matched_by": "exact", "policy_overrides": {"max_single_payment_minor": 50000}}` | 0.0 | 1.82 | 4.7 | ✅ | ops-us also matches ops-*; the exact entry wins |
| U03 | 4 | whose book, which payees, whose signature, which policy | the longest matching pattern wins | tenancy | `{"matched_by": "pattern:ops-eu-*", "allowed_payees": ["ben-eu-components"], "approvers": [{"name": "Lena Vogt", "role": "finance_manager"}]}` | 0.2 | 1.63 | 4.9 | ✅ | ops-* and ops-eu-* both match; ops-eu-* is longer |
| U04 | 4 | whose book, which payees, whose signature, which policy | a shorter pattern for the rest | tenancy | `{"matched_by": "pattern:ops-*", "allowed_payees": ["ben-saas-vendor"]}` | 0.0 | 1.33 | 5.1 | ✅ | only ops-* matches |
| U05 | 4 | whose book, which payees, whose signature, which policy | an unknown book goes to the default tenant | tenancy | `{"known": true, "tenant": "lab", "matched_by": "default", "allowed_payees": ["*"]}` | 0.0 | 1.64 | 4.7 | ✅ | strict false -> lab |
| U06 | 4 | whose book, which payees, whose signature, which policy | an unknown book under a strict registry | tenancy | `{"known": false, "tenant": null, "matched_by": null}` | 0.0 | 1.63 | 4.6 | ✅ | strict and no catch-all pattern -> unknown |
| U07 | 4 | whose book, which payees, whose signature, which policy | a known book under a strict registry | tenancy | `{"known": true, "tenant": "acme", "strict": true}` | 0.0 | 1.67 | 5.2 | ✅ | strict changes nothing for known books |
| U08 | 4 | whose book, which payees, whose signature, which policy | allows() per payee | tenancy | `{"allows": {"ben-saas-vendor": true, "ben-lumen-design": false}}` | 0.0 | 2.06 | 5.3 | ✅ | list membership |
| U09 | 4 | whose book, which payees, whose signature, which policy | an override of a policy field that does not exist is refused | tenancy | `{"known": false, "error_contains": "unknown policy fields"}` | 0.0 | 2.01 | 4.2 | ✅ | the registry is validated against TreasuryPolicy |
| U10 | 4 | whose book, which payees, whose signature, which policy | no tenants at all, strict | tenancy | `{"known": false}` | 0.0 | 2.06 | 4.9 | ✅ | nothing to resolve against |
| J01 | 4 | whose book, which payees, whose signature, which policy | the book's named approver | approvals | `{"ok": true, "approved_by": ["Dana Finch"]}` | 0.0 | 1.84 | 4.0 | ✅ | name and role match the registry |
| J02 | 4 | whose book, which payees, whose signature, which policy | right role, not this book's person | approvals | `{"ok": false, "first_problem_contains": "not an approver of this book"}` | 0.0 | 1.8 | 5.1 | ✅ | a finance manager of another tenant cannot sign this book |
| J03 | 4 | whose book, which payees, whose signature, which policy | the right person in the wrong role | approvals | `{"ok": false, "first_problem_contains": "not an approver of this book"}` | 0.0 | 1.57 | 3.8 | ✅ | the registry names (person, role) pairs |
| J04 | 4 | whose book, which payees, whose signature, which policy | no book approvers configured: any approver of the right role | approvals | `{"ok": true}` | 0.0 | 1.5 | 4.8 | ✅ | an empty list defers to the matrix |
| J05 | 4 | whose book, which payees, whose signature, which policy | names are compared normalised | approvals | `{"ok": true, "approved_by": ["  dana   FINCH "]}` | 0.0 | 1.59 | 4.4 | ✅ | case and spacing do not matter |

## Suite 1 — the rules, one by one

37 treasury plans and 17 intake emails that exercise one rule each (fit / do not fit / exactly at the limit, priorities, evidence, duplicates, receipts, limits scaling with confidence, roles).

| Path | Kind | Cases | Match | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|---|
| in-process | plan | 37 | 37 | 3.4 | 5.9 | 7.4 |
| endpoint | plan | 37 | 37 | 90.8 | 94.7 | 97.6 |
| UI form | plan | 37 | 37 | 88.8 | 94.0 | 186.7 |
| in-process | intake | 17 | 17 | 2.1 | 3.9 | 4.2 |
| endpoint | intake | 17 | 17 | 4.4 | 5.2 | 5.3 |
| UI form | intake | 17 | 17 | 6.8 | 9.0 | 11.0 |

Server-side plan time (`duration_ms`, includes the live Airwallex balance check, the 72 h simulation and the counterfactuals): p50 82.3 ms, p95 89.9 ms.

### Cases

| ID | Case | Kind | Expected | in-proc ms | endpoint ms | UI ms | Match | Derivation |
|---|---|---|---|---|---|---|---|---|
| P01 | single obligation fits | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 3.4 | 94.11 | 186.7 | ✅ | headroom 5,000 >= 3,000 -> PAY; nothing needs a person |
| P02 | single obligation does not fit, no receipt | plan | `{"actions": {"O1": "ESCALATE"}, "not_blocked": true}` | 3.4 | 90.77 | 94.5 | ✅ | 6,000 > headroom 5,000 and no receipt -> ESCALATE |
| P03 | obligation equals the headroom exactly | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 4.0 | 94.65 | 88 | ✅ | 5,000 <= 5,000 -> PAY (cash after = floor, allowed) |
| P04 | one cent over the headroom | plan | `{"actions": {"O1": "ESCALATE"}}` | 5.7 | 88.48 | 89.3 | ✅ | 5,000.01 > 5,000 -> cannot be funded -> ESCALATE |
| P05 | two obligations both fit | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY", "O2": "PAY"}}` | 3.5 | 91.25 | 88.5 | ✅ | 2,000 + 2,500 = 4,500 <= 5,000 -> both PAY |
| P06 | two obligations, only the higher priority fits | plan | `{"actions": {"O1": "PAY", "O2": "ESCALATE"}}` | 3.9 | 86.85 | 88.8 | ✅ | priority 10 first: 3,000 fits; 3,000 more would breach -> ESCALATE |
| P07 | same priority, earlier due first | plan | `{"actions": {"O2": "PAY", "O1": "ESCALATE"}}` | 3.5 | 96.05 | 92.2 | ✅ | tie on priority -> due 12h paid, due 48h cannot be funded |
| P08 | duplicate invoice fingerprint | plan | `{"actions": {"O1": "PAY", "O2": "REJECT"}}` | 4.8 | 86.42 | 88.8 | ✅ | same fingerprint -> second copy REJECT, never paid twice |
| P09 | evidence rejected | plan | `{"actions": {"O1": "REJECT"}}` | 2.7 | 88.59 | 88.9 | ✅ | rejected evidence -> REJECT |
| P10 | evidence pending | plan | `{"actions": {"O1": "REJECT"}}` | 2.4 | 88.5 | 86.7 | ✅ | pending evidence -> REJECT |
| P11 | evidence missing | plan | `{"actions": {"O1": "REJECT"}}` | 2.2 | 91.68 | 83.1 | ✅ | no document -> REJECT |
| P12 | evidence names another obligation | plan | `{"actions": {"O1": "REJECT"}}` | 2.5 | 85.45 | 80.7 | ✅ | document is about another subject -> REJECT |
| P13 | already settled record | plan | `{"actions": {"O1": "REJECT"}}` | 2.1 | 92.43 | 86.1 | ✅ | settled -> not a payable -> REJECT |
| P14 | payee bank details changed | plan | `{"actions": {"O1": "ESCALATE"}}` | 4.0 | 90.85 | 82.5 | ✅ | escalate_flags -> ESCALATE even though it fits |
| P15 | viewer cannot plan | plan | `{"decision": "BLOCKED"}` | 2.8 | 85.69 | 92.3 | ✅ | actor_role viewer -> BLOCKED |
| P16 | finance manager plans like the agent | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 2.6 | 87.99 | 91.1 | ✅ | role only changes who may approve |
| P17 | does not fit now, reliable receipt lands before due | plan | `{"actions": {"O1": "DEFER"}}` | 3.5 | 93.3 | 90.2 | ✅ | 7,000 > 5,000; receipt 8,000 at 12h (90% >= 80%, 12h <= 72h) before due 48h -> DEFER |
| P18 | receipt not reliable (60%) | plan | `{"actions": {"O1": "ESCALATE"}}` | 3.4 | 93.57 | 89.8 | ✅ | 60% < 80% -> not relied on -> ESCALATE |
| P19 | receipt lands after due | plan | `{"actions": {"O1": "ESCALATE"}}` | 5.1 | 93.7 | 91.5 | ✅ | receipt at 48h > due 24h -> ESCALATE |
| P20 | receipt window ends after the horizon | plan | `{"actions": {"O1": "ESCALATE"}}` | 6.9 | 90.47 | 94 | ✅ | latest arrival 100h > 72h horizon -> not reliable -> ESCALATE |
| P21 | receipt covers one of two deferrals | plan | `{"actions": {"O1": "PAY", "O2": "ESCALATE"}}` | 3.7 | 89.06 | 90.2 | ✅ | O1 fits (4,000 <= 5,000); O2 needs 4,000, receipt only 3,000 -> ESCALATE |
| P22 | EUR payout converted inside the limit | plan | `{"decision": "APPROVED", "actions": {"O1": "CONVERT_AND_PAY"}, "n_conversions": 1}` | 4.8 | 89.62 | 88.6 | ✅ | EUR 1,000 + 12.85 fee = EUR 1,012.85 ~ USD 1,099; headroom 15,000; limit 10,000 (no receipt -> full) -> autonomous |
| P23 | conversion above the autonomous limit | plan | `{"decision": "REQUIRES_APPROVAL", "actions": {"O1": "CONVERT_AND_PAY"}, "requires": ["conversion_above_autonomous_limit"]}` | 2.8 | 87.86 | 85.2 | ✅ | EUR 12,012.85 ~ USD 13,034 > limit 10,000 (headroom 25,000 is fine) -> a person approves |
| P24 | limit zero: every conversion needs a person | plan | `{"decision": "REQUIRES_APPROVAL", "requires": ["conversion_above_autonomous_limit"]}` | 2.7 | 89.74 | 88 | ✅ | any conversion > 0 -> REQUIRES_APPROVAL |
| P25 | EUR wallet already covers the EUR payout | plan | `{"decision": "APPROVED", "n_conversions": 0, "paid": ["O1"]}` | 2.8 | 90.67 | 86.7 | ✅ | EUR 2,000 >= 1,012.85 -> no conversion, cost 0 |
| P26 | GBP wallet covers part: convert the shortfall only | plan | `{"decision": "APPROVED", "conversions": {"GBP": 60000}}` | 2.8 | 91.18 | 86.6 | ✅ | need GBP 1,000 (LOCAL, no fee), have 400 -> buy exactly 600 |
| P27 | stress eats the headroom | plan | `{"actions": {"O1": "ESCALATE"}}` | 7.4 | 97.61 | 90.1 | ✅ | headroom = 10,000 - 5,000 - 3,000 = 2,000 < 3,000 -> ESCALATE |
| P28 | stress leaves exactly enough (deterministic) but no margin (probabilistic) | plan | `{"decision": "REQUIRES_APPROVAL", "actions": {"O1": "PAY"}, "requires": ["reserve_breach_risk_above_tolerance"]}` | 5.9 | 91.11 | 86.2 | ✅ | deterministic: headroom = 10,000 - 5,000 - 2,000 = 3,000 = 3,000 -> PAY. Probabilistic: the 72h simulation draws the unexpected outflow with a lognormal size around 2,000, so landing exactly on the floor breaches it in about half the paths, far above the 5% tolerance -> a person approves. (The first hand answer missed the second rule; recorded as such.) |
| P29 | zero reserve floor | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 2.4 | 91.09 | 85.9 | ✅ | floor 0: cash 1,000 pays 1,000 |
| P30 | zero cash | plan | `{"paid": [], "not_blocked": true}` | 2.8 | 94.71 | 85.2 | ✅ | nothing can be paid; the plan is still a plan (escalates), not BLOCKED |
| P31 | twelve small obligations all fit | plan | `{"decision": "APPROVED", "paid": ["O1", "O2", "O3", "O4", "O5", "O6", "O7", "O8", "O9", "O10", "O11", "O12"]}` | 4.3 | 93.34 | 92.8 | ✅ | 12 x 100 = 1,200 <= 5,000 -> all PAY |
| P32 | large amounts, still inside the live sandbox wallet | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 2.5 | 88.35 | 88.3 | ✅ | USD 9,000,000 cash, USD 1,000,000 obligation, floor 5,000 -> PAY. (Amounts above the live wallet, e.g. USD 10bn, are BLOCKED by the live-balance check when the service is connected to the sandbox: the plan is grounded in real money.) |
| P33 | confidence 90%: limit is full | plan | `{"decision": "APPROVED", "limit": 1000000}` | 5.0 | 91.28 | 85.2 | ✅ | conf 90% >= 90% -> limit 10,000; conversion ~USD 7,734 < 10,000 -> autonomous |
| P34 | confidence 70%: limit halves | plan | `{"decision": "REQUIRES_APPROVAL", "limit": 500000, "requires": ["conversion_above_autonomous_limit"]}` | 3.4 | 93.37 | 88.6 | ✅ | limit = 10,000 x (70-50)/(90-50) = 5,000; conversion ~7,734 > 5,000 -> a person |
| P35 | confidence 40%: limit zero | plan | `{"decision": "REQUIRES_APPROVAL", "limit": 0}` | 5.2 | 90.02 | 91.9 | ✅ | conf 40% <= 50% -> limit 0 -> any conversion needs a person |
| P36 | priority 10 due now and priority 1 due at the horizon both fit | plan | `{"decision": "APPROVED", "actions": {"O1": "PAY", "O2": "PAY"}}` | 3.0 | 91.06 | 92.3 | ✅ | 4,000 <= 5,000 -> both PAY regardless of priority |
| P37 | cheapest obligation dropped, not the most urgent | plan | `{"actions": {"O1": "PAY", "O2": "ESCALATE"}}` | 3.2 | 88.52 | 88.9 | ✅ | 4,000 fits; 1,500 more = 5,500 > 5,000 -> O2 cannot be funded |
| I01 | registered mailbox, clean invoice | intake | `{"status": "VERIFIED", "trust": "registered", "kind": "supplier_invoice", "amount_minor": 125000, "currency": "USD", "invoice_no": "LD-90", "due_date": "2026-11-05"}` | 2.1 | 4.84 | 9 | ✅ | exact registered gmail address; amount + invoice quoted; no bank change |
| I02 | unknown mailbox with the same display name | intake | `{"status": "QUARANTINED", "trust": "unknown_sender", "claimed_name": "Lumen Design Studio"}` | 1.7 | 4.37 | 6.6 | ✅ | display name is not identity; unknown mailbox asking for money -> QUARANTINED |
| I03 | registered mailbox announcing new bank details | intake | `{"status": "NEEDS_REVIEW", "trust": "registered", "kind": "bank_change", "flags": ["payee_bank_details_changed"]}` | 1.7 | 3.99 | 7.1 | ✅ | registered sender but a bank change needs an out-of-band call-back -> NEEDS_REVIEW, never auto-payable |
| I04 | unknown mailbox, bank change, urgency | intake | `{"status": "QUARANTINED", "kind": "bank_change", "flags": ["payee_bank_details_changed", "urgency_pressure"]}` | 2.1 | 4.05 | 6.6 | ✅ | classic payment-diversion pattern -> QUARANTINED |
| I05 | registered mailbox but Reply-To elsewhere | intake | `{"status": "QUARANTINED", "trust": "reply_to_mismatch"}` | 2.7 | 5.26 | 6.2 | ✅ | Reply-To is not a registered mailbox -> reply_to_mismatch -> QUARANTINED |
| I06 | registered customer remittance (outlook) | intake | `{"status": "VERIFIED", "kind": "remittance_notice", "amount_minor": 200000, "due_date": "2026-10-30"}` | 2.0 | 5.18 | 5.6 | ✅ | registered payer mailbox + remittance language -> receivable |
| I07 | registered contractor timesheet (yahoo) | intake | `{"status": "VERIFIED", "kind": "contractor_claim", "amount_minor": 100000, "invoice_no": "OK-2026-11", "due_date": "2026-11-07"}` | 1.7 | 4.72 | 6.1 | ✅ | registered yahoo mailbox; amount is the total (largest figure); within 14 days of 24 Oct = 7 Nov |
| I08 | registered mailbox, no amount readable | intake | `{"status": "NEEDS_REVIEW", "trust": "registered"}` | 1.7 | 3.96 | 6.5 | ✅ | nothing to pay can be read -> a person looks |
| I09 | injection text cannot verify itself | intake | `{"status": "QUARANTINED", "trust": "unknown_sender"}` | 1.7 | 4.07 | 7.1 | ✅ | the text is data; trust comes from the registry only |
| I10 | registered sender with pressure language | intake | `{"status": "NEEDS_REVIEW", "flags": ["urgency_pressure"]}` | 1.9 | 3.27 | 6.9 | ✅ | registered, but pressure language -> a person confirms first |
| I11 | html body | intake | `{"status": "VERIFIED", "amount_minor": 32000, "invoice_no": "LD-97", "due_date": "2026-11-02"}` | 1.9 | 4.03 | 5.8 | ✅ | tags stripped; same reading |
| I12 | euro symbol and thousands separator | intake | `{"status": "VERIFIED", "amount_minor": 150000, "currency": "EUR", "due_date": "2026-11-10"}` | 3.9 | 4.7 | 7.1 | ✅ | € -> EUR; 1,500.00 -> 150000 minor |
| I13 | duplicate of an invoice already in the book | intake | `{"status": "DUPLICATE", "duplicate_of": "SHIPPING-UK"}` | 4.2 | 4.65 | 7.2 | ✅ | fingerprint thames-freight:TF-311 already in kit01_initial -> DUPLICATE |
| I14 | embargoed future date does not matter for triage, amount in subject-less body | intake | `{"status": "VERIFIED", "amount_minor": 1000, "invoice_no": "LD-99", "due_date": "2026-12-01"}` | 2.8 | 4.53 | 11 | ✅ | no subject; fields read from the body |
| I15 | corporate domain match (non-freemail) | intake | `{"status": "VERIFIED", "trust": "registered", "currency": "EUR", "amount_minor": 400000, "invoice_no": "INV-2026-0700"}` | 2.6 | 3.83 | 6.8 | ✅ | exact registered corporate mailbox -> registered |
| I16 | same corporate domain, different mailbox | intake | `{"status": "VERIFIED", "trust": "registered"}` | 2.5 | 4.61 | 7.4 | ✅ | registered domain (not freemail) -> registered |
| I17 | freemail lookalike of a corporate payee | intake | `{"status": "QUARANTINED", "trust": "unknown_sender", "claimed_name": "Rheinwerk Komponenten GmbH"}` | 2.4 | 3.89 | 6.8 | ✅ | freemail is never matched by domain |

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
