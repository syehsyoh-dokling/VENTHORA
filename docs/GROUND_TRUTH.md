# Ground truth: hand-derived answers vs the live service

Generated 2026-10-23 15:39 by `python scripts/groundtruth_report.py`.

**What this is.** `data/groundtruth/cases.json` holds 54 cases (37 treasury plans, 17 intake emails) whose answers were derived *by hand* from the published rules in `scripts/make_groundtruth.py` (each case carries its derivation), not by running VENTHORA. Every case was then submitted three ways and judged by one comparator (`venthora.testing.groundtruth.compare`):

1. **in-process** (`tests/test_groundtruth.py`, runs in CI);
2. **via the HTTP endpoints** of the running service (`scripts/groundtruth_bench.py`: `POST /v1/plans`, `POST /v1/intake/emails?dry_run=true`), round trip measured by the client, 3 repeats, median kept;
3. **via the dashboard form** (`Submit a case` at `/`), each case picked and submitted through the UI in a browser, round trip measured by the browser's `performance.now()` beside the server's own `duration_ms`.

## Results

| Path | Kind | Cases | Match | p50 ms | p95 ms | max ms |
|---|---|---|---|---|---|---|
| endpoint | plan | 37 | 37 | 90.8 | 94.7 | 97.6 |
| endpoint | intake | 17 | 17 | 4.4 | 5.2 | 5.3 |
| UI form | plan | 37 | 37 | 88.8 | 94.0 | 186.7 |
| UI form | intake | 17 | 17 | 6.8 | 9.0 | 11.0 |

Server-side plan time (`duration_ms`, includes the live Airwallex balance check, the 72 h simulation and the counterfactuals): p50 82.3 ms, p95 89.9 ms. The difference to the round trip (about 10 ms) is HTTP, JSON and the audit append. Without the live Airwallex call (offline / tests) a plan takes about 13 ms; the balance check against the sandbox accounts for most of the rest.

## Cases

| ID | Case | Expected | Endpoint ms | UI ms | Server ms | Match | Derivation |
|---|---|---|---|---|---|---|---|
| P01 | single obligation fits | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 94.11 | 186.7 | 174 | ✅ | headroom 5,000 >= 3,000 -> PAY; nothing needs a person |
| P02 | single obligation does not fit, no receipt | `{"actions": {"O1": "ESCALATE"}, "not_blocked": true}` | 90.77 | 94.5 | 82.9 | ✅ | 6,000 > headroom 5,000 and no receipt -> ESCALATE |
| P03 | obligation equals the headroom exactly | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 94.65 | 88 | 77.5 | ✅ | 5,000 <= 5,000 -> PAY (cash after = floor, allowed) |
| P04 | one cent over the headroom | `{"actions": {"O1": "ESCALATE"}}` | 88.48 | 89.3 | 78.7 | ✅ | 5,000.01 > 5,000 -> cannot be funded -> ESCALATE |
| P05 | two obligations both fit | `{"decision": "APPROVED", "actions": {"O1": "PAY", "O2": "PAY"}}` | 91.25 | 88.5 | 78.3 | ✅ | 2,000 + 2,500 = 4,500 <= 5,000 -> both PAY |
| P06 | two obligations, only the higher priority fits | `{"actions": {"O1": "PAY", "O2": "ESCALATE"}}` | 86.85 | 88.8 | 77 | ✅ | priority 10 first: 3,000 fits; 3,000 more would breach -> ESCALATE |
| P07 | same priority, earlier due first | `{"actions": {"O2": "PAY", "O1": "ESCALATE"}}` | 96.05 | 92.2 | 81.7 | ✅ | tie on priority -> due 12h paid, due 48h cannot be funded |
| P08 | duplicate invoice fingerprint | `{"actions": {"O1": "PAY", "O2": "REJECT"}}` | 86.42 | 88.8 | 78.1 | ✅ | same fingerprint -> second copy REJECT, never paid twice |
| P09 | evidence rejected | `{"actions": {"O1": "REJECT"}}` | 88.59 | 88.9 | 78.9 | ✅ | rejected evidence -> REJECT |
| P10 | evidence pending | `{"actions": {"O1": "REJECT"}}` | 88.5 | 86.7 | 72.5 | ✅ | pending evidence -> REJECT |
| P11 | evidence missing | `{"actions": {"O1": "REJECT"}}` | 91.68 | 83.1 | 73.7 | ✅ | no document -> REJECT |
| P12 | evidence names another obligation | `{"actions": {"O1": "REJECT"}}` | 85.45 | 80.7 | 70.3 | ✅ | document is about another subject -> REJECT |
| P13 | already settled record | `{"actions": {"O1": "REJECT"}}` | 92.43 | 86.1 | 73.8 | ✅ | settled -> not a payable -> REJECT |
| P14 | payee bank details changed | `{"actions": {"O1": "ESCALATE"}}` | 90.85 | 82.5 | 72.4 | ✅ | escalate_flags -> ESCALATE even though it fits |
| P15 | viewer cannot plan | `{"decision": "BLOCKED"}` | 85.69 | 92.3 | 80.3 | ✅ | actor_role viewer -> BLOCKED |
| P16 | finance manager plans like the agent | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 87.99 | 91.1 | 79.7 | ✅ | role only changes who may approve |
| P17 | does not fit now, reliable receipt lands before due | `{"actions": {"O1": "DEFER"}}` | 93.3 | 90.2 | 79.5 | ✅ | 7,000 > 5,000; receipt 8,000 at 12h (90% >= 80%, 12h <= 72h) before due 48h -> DEFER |
| P18 | receipt not reliable (60%) | `{"actions": {"O1": "ESCALATE"}}` | 93.57 | 89.8 | 79 | ✅ | 60% < 80% -> not relied on -> ESCALATE |
| P19 | receipt lands after due | `{"actions": {"O1": "ESCALATE"}}` | 93.7 | 91.5 | 80.7 | ✅ | receipt at 48h > due 24h -> ESCALATE |
| P20 | receipt window ends after the horizon | `{"actions": {"O1": "ESCALATE"}}` | 90.47 | 94 | 81.9 | ✅ | latest arrival 100h > 72h horizon -> not reliable -> ESCALATE |
| P21 | receipt covers one of two deferrals | `{"actions": {"O1": "PAY", "O2": "ESCALATE"}}` | 89.06 | 90.2 | 78.5 | ✅ | O1 fits (4,000 <= 5,000); O2 needs 4,000, receipt only 3,000 -> ESCALATE |
| P22 | EUR payout converted inside the limit | `{"decision": "APPROVED", "actions": {"O1": "CONVERT_AND_PAY"}, "n_conversions": 1}` | 89.62 | 88.6 | 78.6 | ✅ | EUR 1,000 + 12.85 fee = EUR 1,012.85 ~ USD 1,099; headroom 15,000; limit 10,000 (no receipt -> full) -> autonomous |
| P23 | conversion above the autonomous limit | `{"decision": "REQUIRES_APPROVAL", "actions": {"O1": "CONVERT_AND_PAY"}, "requires": ["conversion_above_autonomous_limit"]}` | 87.86 | 85.2 | 75.1 | ✅ | EUR 12,012.85 ~ USD 13,034 > limit 10,000 (headroom 25,000 is fine) -> a person approves |
| P24 | limit zero: every conversion needs a person | `{"decision": "REQUIRES_APPROVAL", "requires": ["conversion_above_autonomous_limit"]}` | 89.74 | 88 | 77.1 | ✅ | any conversion > 0 -> REQUIRES_APPROVAL |
| P25 | EUR wallet already covers the EUR payout | `{"decision": "APPROVED", "n_conversions": 0, "paid": ["O1"]}` | 90.67 | 86.7 | 76.8 | ✅ | EUR 2,000 >= 1,012.85 -> no conversion, cost 0 |
| P26 | GBP wallet covers part: convert the shortfall only | `{"decision": "APPROVED", "conversions": {"GBP": 60000}}` | 91.18 | 86.6 | 75.4 | ✅ | need GBP 1,000 (LOCAL, no fee), have 400 -> buy exactly 600 |
| P27 | stress eats the headroom | `{"actions": {"O1": "ESCALATE"}}` | 97.61 | 90.1 | 79.2 | ✅ | headroom = 10,000 - 5,000 - 3,000 = 2,000 < 3,000 -> ESCALATE |
| P28 | stress leaves exactly enough (deterministic) but no margin (probabilistic) | `{"decision": "REQUIRES_APPROVAL", "actions": {"O1": "PAY"}, "requires": ["reserve_breach_risk_above_tolerance"]}` | 91.11 | 86.2 | 75.4 | ✅ | deterministic: headroom = 10,000 - 5,000 - 2,000 = 3,000 = 3,000 -> PAY. Probabilistic: the 72h simulation draws the unexpected outflow with a lognormal size around 2,000, so landing exactly on the floor breaches it in about half the paths, far above the 5% tolerance -> a person approves. (The first hand answer missed the second rule; recorded as such.) |
| P29 | zero reserve floor | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 91.09 | 85.9 | 75.5 | ✅ | floor 0: cash 1,000 pays 1,000 |
| P30 | zero cash | `{"paid": [], "not_blocked": true}` | 94.71 | 85.2 | 73.6 | ✅ | nothing can be paid; the plan is still a plan (escalates), not BLOCKED |
| P31 | twelve small obligations all fit | `{"decision": "APPROVED", "paid": ["O1", "O2", "O3", "O4", "O5", "O6", "O7", "O8", "O9", "O10", "O11", "O12"]}` | 93.34 | 92.8 | 79.7 | ✅ | 12 x 100 = 1,200 <= 5,000 -> all PAY |
| P32 | large amounts, still inside the live sandbox wallet | `{"decision": "APPROVED", "actions": {"O1": "PAY"}}` | 88.35 | 88.3 | 77.5 | ✅ | USD 9,000,000 cash, USD 1,000,000 obligation, floor 5,000 -> PAY. (Amounts above the live wallet, e.g. USD 10bn, are BLOCKED by the live-balance check when the service is connected to the sandbox: the plan is grounded in real money.) |
| P33 | confidence 90%: limit is full | `{"decision": "APPROVED", "limit": 1000000}` | 91.28 | 85.2 | 72.5 | ✅ | conf 90% >= 90% -> limit 10,000; conversion ~USD 7,734 < 10,000 -> autonomous |
| P34 | confidence 70%: limit halves | `{"decision": "REQUIRES_APPROVAL", "limit": 500000, "requires": ["conversion_above_autonomous_limit"]}` | 93.37 | 88.6 | 77.3 | ✅ | limit = 10,000 x (70-50)/(90-50) = 5,000; conversion ~7,734 > 5,000 -> a person |
| P35 | confidence 40%: limit zero | `{"decision": "REQUIRES_APPROVAL", "limit": 0}` | 90.02 | 91.9 | 79.8 | ✅ | conf 40% <= 50% -> limit 0 -> any conversion needs a person |
| P36 | priority 10 due now and priority 1 due at the horizon both fit | `{"decision": "APPROVED", "actions": {"O1": "PAY", "O2": "PAY"}}` | 91.06 | 92.3 | 77.9 | ✅ | 4,000 <= 5,000 -> both PAY regardless of priority |
| P37 | cheapest obligation dropped, not the most urgent | `{"actions": {"O1": "PAY", "O2": "ESCALATE"}}` | 88.52 | 88.9 | 78 | ✅ | 4,000 fits; 1,500 more = 5,500 > 5,000 -> O2 cannot be funded |
| I01 | registered mailbox, clean invoice | `{"status": "VERIFIED", "trust": "registered", "kind": "supplier_invoice", "amount_minor": 125000, "currency": "USD", "invoice_no": "LD-90", "due_date": "2026-11-05"}` | 4.84 | 9 | - | ✅ | exact registered gmail address; amount + invoice quoted; no bank change |
| I02 | unknown mailbox with the same display name | `{"status": "QUARANTINED", "trust": "unknown_sender", "claimed_name": "Lumen Design Studio"}` | 4.37 | 6.6 | - | ✅ | display name is not identity; unknown mailbox asking for money -> QUARANTINED |
| I03 | registered mailbox announcing new bank details | `{"status": "NEEDS_REVIEW", "trust": "registered", "kind": "bank_change", "flags": ["payee_bank_details_changed"]}` | 3.99 | 7.1 | - | ✅ | registered sender but a bank change needs an out-of-band call-back -> NEEDS_REVIEW, never auto-payable |
| I04 | unknown mailbox, bank change, urgency | `{"status": "QUARANTINED", "kind": "bank_change", "flags": ["payee_bank_details_changed", "urgency_pressure"]}` | 4.05 | 6.6 | - | ✅ | classic payment-diversion pattern -> QUARANTINED |
| I05 | registered mailbox but Reply-To elsewhere | `{"status": "QUARANTINED", "trust": "reply_to_mismatch"}` | 5.26 | 6.2 | - | ✅ | Reply-To is not a registered mailbox -> reply_to_mismatch -> QUARANTINED |
| I06 | registered customer remittance (outlook) | `{"status": "VERIFIED", "kind": "remittance_notice", "amount_minor": 200000, "due_date": "2026-10-30"}` | 5.18 | 5.6 | - | ✅ | registered payer mailbox + remittance language -> receivable |
| I07 | registered contractor timesheet (yahoo) | `{"status": "VERIFIED", "kind": "contractor_claim", "amount_minor": 100000, "invoice_no": "OK-2026-11", "due_date": "2026-11-07"}` | 4.72 | 6.1 | - | ✅ | registered yahoo mailbox; amount is the total (largest figure); within 14 days of 24 Oct = 7 Nov |
| I08 | registered mailbox, no amount readable | `{"status": "NEEDS_REVIEW", "trust": "registered"}` | 3.96 | 6.5 | - | ✅ | nothing to pay can be read -> a person looks |
| I09 | injection text cannot verify itself | `{"status": "QUARANTINED", "trust": "unknown_sender"}` | 4.07 | 7.1 | - | ✅ | the text is data; trust comes from the registry only |
| I10 | registered sender with pressure language | `{"status": "NEEDS_REVIEW", "flags": ["urgency_pressure"]}` | 3.27 | 6.9 | - | ✅ | registered, but pressure language -> a person confirms first |
| I11 | html body | `{"status": "VERIFIED", "amount_minor": 32000, "invoice_no": "LD-97", "due_date": "2026-11-02"}` | 4.03 | 5.8 | - | ✅ | tags stripped; same reading |
| I12 | euro symbol and thousands separator | `{"status": "VERIFIED", "amount_minor": 150000, "currency": "EUR", "due_date": "2026-11-10"}` | 4.7 | 7.1 | - | ✅ | € -> EUR; 1,500.00 -> 150000 minor |
| I13 | duplicate of an invoice already in the book | `{"status": "DUPLICATE", "duplicate_of": "SHIPPING-UK"}` | 4.65 | 7.2 | - | ✅ | fingerprint thames-freight:TF-311 already in kit01_initial -> DUPLICATE |
| I14 | embargoed future date does not matter for triage, amount in subject-less body | `{"status": "VERIFIED", "amount_minor": 1000, "invoice_no": "LD-99", "due_date": "2026-12-01"}` | 4.53 | 11 | - | ✅ | no subject; fields read from the body |
| I15 | corporate domain match (non-freemail) | `{"status": "VERIFIED", "trust": "registered", "currency": "EUR", "amount_minor": 400000, "invoice_no": "INV-2026-0700"}` | 3.83 | 6.8 | - | ✅ | exact registered corporate mailbox -> registered |
| I16 | same corporate domain, different mailbox | `{"status": "VERIFIED", "trust": "registered"}` | 4.61 | 7.4 | - | ✅ | registered domain (not freemail) -> registered |
| I17 | freemail lookalike of a corporate payee | `{"status": "QUARANTINED", "trust": "unknown_sender", "claimed_name": "Rheinwerk Komponenten GmbH"}` | 3.89 | 6.8 | - | ✅ | freemail is never matched by domain |

## What the exercise changed

- **P28** — the hand answer first said APPROVED (deterministic headroom exactly covers the payment). The service answered REQUIRES_APPROVAL: the 72-hour simulation draws the unexpected outflow with a lognormal size, so a plan that lands exactly on the floor breaches it in about half the paths, above the 5% tolerance. The second rule is the documented one; the expectation was corrected and the case kept as a record.
- **P32** — USD 10 bn cash was BLOCKED by the live service (the sandbox wallet holds about USD 10 M) while passing in-process: a connected service grounds every plan in real balances. The case now uses amounts inside the wallet and the note stays.
- **I04** — the lookalike's invoice LD-77 was already in the live intake queue, so the live service answered DUPLICATE instead of QUARANTINED: the queue is shared state and the duplicate lock applies to it. The case uses another invoice number; the behaviour is the intended one.
