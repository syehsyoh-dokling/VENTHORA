# Compliance matrix: every requirement, line by line

Sources: [airwallexdev.com/guide](https://airwallexdev.com/guide) and [airwallex.hackerearth.com](https://airwallex.hackerearth.com/),
re-read 23 Oct 2026. The guide was split into 114 numbered items. Every item that applies to a
Kit 01 build is listed below. Items for other kits (4, 9–12), MCP-only workflows and customer
(`x-on-behalf-of`) accounts are listed at the end as not applicable, with the reason.

Legend:
- ✅ met and verified on the **real** sandbox ([SANDBOX_RESULTS.md](SANDBOX_RESULTS.md))
- 🧪 met and covered by automated tests
- 📝 stated in the docs
- ↔ deliberate deviation, with the reason given

No oracle: no production code branches on scenario ids or demo values; the later scenarios are generated
by reading the inbox (`scripts/make_scenarios.py`); `scripts/oracle_audit.py` mutates the data and checks the
agent reacts to the rules only. Audit report and known limitations: [AUDIT.md](AUDIT.md).

## Submission (hackerearth)

| Requirement | Status | Evidence |
|---|---|---|
| Working demo | ✅ | `venthora-sandbox` on the real sandbox; nine recorded cycles (SANDBOX_RESULTS.md), cycle 4 = the Kit 01 flow word for word; `python scripts/real_suite.py` runs the whole set on the real sandbox in one command, 8 / 8 steps ([REAL_SUITE.md](REAL_SUITE.md)) |
| Video walkthrough under 5 minutes | ⬜ | script: [DEMO_SCRIPT.md](DEMO_SCRIPT.md) |
| Repository link with setup instructions | ✅ | https://github.com/syehsyoh-dokling/VENTHORA, README quick start |
| Built on Airwallex APIs | ✅ | balances, rates, quotes, conversions, beneficiaries (schema/validate/create), transfers, deposits, simulation |

## (1) General conventions

| # | Requirement | Status | Where |
|---|---|---|---|
| 1 | Sandbox only, no real money | ✅🧪 | executor refuses non-sandbox URLs; simulation calls are sandbox-only (`test_simulation_and_execution_are_sandbox_only`) |
| 2 | Base `https://api.sandbox.airwallex.com`, paths under `/api/v1` | ✅ | `gateways/airwallex.py` `SANDBOX_URL` |
| 3–4 | Login with `x-client-id`/`x-api-key`, then a Bearer token | ✅🧪 | `_auth_headers`; token cached and refreshed 60 s before expiry; retried once on 401 |
| 5 | Amounts in major units | ✅🧪 | `to_major`/`to_minor` at the boundary; integer minor units inside |
| 6–8 | Fresh UUID `request_id` per operation; reuse only on retry; `duplicate_request_id` | ✅🧪 | persisted before each call (`execution/store.py`); a lost response is recovered by `request_id`, never resent with a new id |
| 9 | Credentials in env, never committed | ✅ | `.env` (git-ignored, `AWX_*` names as in the guide); pushes were scanned (0 matches) |
| 10 | No `x-api-version` on FX calls | ✅ | never sent |
| 11 | Exact payload fields from the API reference | ✅ | fields checked against the reference and the real sandbox |
| 12 | Synthetic data | ✅ | `data/scenarios`, `data/inbox`, `data/beneficiaries.json` |
| 13 / 19 | Verify setup by listing Global Accounts; fund one if none | ✅ | `scripts/sandbox_check.py`; CLI finds or creates a USD account |
| 15 | Business name "Sandbox Business" | ✅ | account approved |
| 20 | Stop when setup fails | ✅ | CLI exits with the reason (missing credentials, non-sandbox) |
| 21–22 | Rate limits; back off on 429; no parallel bursts | ✅🧪 | retries with backoff on 429/5xx; execution is sequential |
| 24 | Each REST call behind one function | ✅ | one typed method per endpoint |
| 27 | REST for conversions and beneficiary validation | ✅ | `create_conversion`, `validate_beneficiary` |
| 30 | Validate over `POST /beneficiaries/validate`, not a dry-run flag | ✅🧪 | `validate_beneficiary` before every create |

## (2) Build around a financial decision

| # | Requirement | Status | Where |
|---|---|---|---|
| 31 | A repeated finance task with a decision inside | ✅ | which obligations to pay, convert for, defer or escalate |
| 32 | Choose differently when balance, deadline, evidence or risk change | ✅🧪 | delay email → revised plan; deposit → recalculation; FX stress; bank-change evidence |
| 33 | Agent moves money; state which actions need a person | ✅📝 | approval table in the README; `REQUIRES_APPROVAL` reasons and escalations |
| 34 | Demo shows goal, initial plan, new information, revised decision, resulting balance/payment/status | ✅ | CLI steps 1–7 (`docs/DEMO_SCRIPT.md`) |
| 35 | Amounts, limits, reserves, validation, duplicate protection, state transitions in code | ✅🧪 | ProofCore, `proofcore/policy.py` + Rego, executor state machine |
| 36 | Model reads text, compares evidence, proposes a plan, explains exceptions | ✅🧪 | `evidence/reader.py` (read + compare), `engines/llm_planner.py` (propose), `evidence/explain.py` (explain) |
| 37 | Typed tools, not credentials | ✅ | the model only returns text; credentials stay in the gateway |
| 38 | Approval bound to amount, currency, counterparty, terms, evidence | ✅🧪 | the terms are shown to the approver at approval time; the approval carries a `terms_digest` and execution refuses an approval given on different terms (audit gap, closed) |
| 39 | Simulation calls behind one replaceable interface | ✅ | `simulate_deposit`, `simulate_transfer_transition` on the gateway |
| 40 | End with a financial action or an escalation to a person | ✅ | both: conversions/transfers executed and policy exceptions escalated |
| 41 | A rule that always gives the same answer belongs in code | ✅ | limits, deferral, escalation rules in code; the model never decides |

## (3) Starter Kit 01: Adaptive Treasury Controller

| # | Requirement | Status | Where |
|---|---|---|---|
| 42 | Treasury lead who cannot fund everything at once | ✅ | USD 22,500 allocation; the obligations exceed it above the reserve |
| 43 | Five obligations, three currencies, within 72 h | ✅ | PAYROLL-NOV, SHIPPING-UK, SUPPLIER-EU, CONTRACTOR-US, SAAS-USD; USD/EUR/GBP; due 24–72 h (a re-sent copy of the supplier invoice is a duplicate record, not a sixth obligation) |
| 44 | Cash covers only part after the reserve floor | ✅🧪 | counterfactual "pay everything" breaches the reserve; the 72-hour simulation gives P(breach) for the plan and every alternative |
| 45 | Fund the shipping invoice whose non-payment halts operations | ✅🧪 | SHIPPING-UK (priority 10) is paid |
| 46 | Convert only the minimum for a time-sensitive supplier | ✅🧪 | buy = shortfall incl. fee; frame checker `minimum_conversions` |
| 47 | Defer the cheapest obligation | ✅🧪 | SAAS-USD deferred on the reliable receipt |
| 48 | Escalate a policy exception | ✅🧪 | CONTRACTOR-US: payee bank details changed (cited email) → escalated, never auto-paid |
| 49 | After a deposit, recalculate only the decisions it affects | ✅🧪 | incremental recalculation with locked decisions and a diff |
| 50 | Execution limit tied to forecast confidence; strong data allows a larger autonomous conversion | ✅🧪 | limit = full at ≥90%, linear to 0 at ≤50% |
| 51 | A contradicting customer email lowers the limit; a person approves the same conversion | ✅🧪 | 90% → 70% → limit 5,000 < conversion 7,931 → `REQUIRES_APPROVAL` for identical conversions; a contradicted forecast is capped below the full band whatever the payer record (audit mutation M6) |
| 52 | Thresholds in policy code, not prompts | ✅🧪 | `TreasuryPolicy`, `proofcore/policy.py`, `policy/treasury.rego`; Rego and Python agree on 9,598 inputs under a real OPA 0.70.0, budget-line thresholds included (`scripts/check_policy_mirror.py`, 23 Oct 2026) |
| 53 | Read balances and obligations | ✅ | `GET /balances/current`; scenario obligations |
| 54 | Plan in code | ✅ | Theorem (CP-SAT + exact second candidate) |
| 55 | Lower forecast confidence for one receipt | ✅ | RCV-ACME 90% → 70% (calibrated from payer history) |
| 56 | Simulate the deposit | ✅ | `POST /simulation/deposit/create`; counted only when `/deposits` says SETTLED |
| 57 | Recalculate | ✅ | incremental re-plan |
| 58 | Execute one FX conversion and one supplier transfer | ✅ | conversions EUR + GBP; supplier, shipping, payroll and SaaS transfers |
| 59 | Confirm the reserve that remains | ✅ | reconciliation confirms the managed reserve |
| 60–68 | Endpoints: balances, rates, quotes, conversions, beneficiaries/schema, beneficiaries/create, transfers, simulation deposit, simulation transition | ✅ | all used; `beneficiaries/schema` checked before every create |
| 69 | Fund a wallet first | ✅ | sandbox pre-funded; a USD Global Account was created |
| 70 | Add EUR 12.85 per SWIFT payout to the conversion amount; LOCAL has no fee | ↔✅ | applied by default; the **real** sandbox charged EUR 13.37 SWIFT and USD 3.00 / GBP 2.24 LOCAL, so observed fees override the guide and plan-vs-actual variance is reported |
| 71 | No `x-api-version` on FX | ✅ | |
| 72 | Book each quote once with one request ID; fresh quote to convert again | ✅🧪 | used-quote registry, persisted `request_id` (the sandbox returns `MULTI_USE`; we still use each quote once) |
| 73 | Book the conversion over REST | ✅ | |

## (4) Beneficiaries, transfers, FX, deposits

| # | Requirement | Status | Where |
|---|---|---|---|
| 74 | Call `/beneficiaries/schema` before building a payload | ✅🧪 | required fields compared; a missing one fails before create (real sandbox: schema and validate called directly and accepted; the executor path is covered by tests, because the cycle-4 beneficiaries already existed) |
| 75–76 | `{"beneficiary": …, "transfer_methods": […]}`; not `payment_methods` | ✅ | |
| 77–79 | US/USD/LOCAL: `aba`, exact `Checking`, full address | ✅ | `data/beneficiaries.json` |
| 80 | GB/GBP/LOCAL: `bank_name`, lowercase `sort_code` | ✅ | |
| 81 | DE/EUR/SWIFT: IBAN + `swift_code`, no routing fields | ✅ | |
| 82–83 | Synthetic names; real routing codes, valid IBAN checksum | ✅ | accepted by the real sandbox |
| 85 | Bank details validated at creation | ✅ | validate + create; errors halt before any transfer |
| 87–88 | `transfer_method` LOCAL/SWIFT; `transfer_currency` + `transfer_amount` | ✅ | |
| 89 | `request_id` 10–50 chars | ✅ | UUID (36) |
| 92 | Failed transfer ends CANCELLED; read `failure_type`/`failure_reason` | ✅🧪 | treated as failed, refund reconciled, payout released for retry |
| 93 | Poll to a terminal state; SENT is not final | ✅🧪 | `_poll`; only PAID/CANCELLED/FAILED are terminal |
| 95 | `rate` from a rate check vs `client_rate` from a quote | ✅ | indicative rate for planning, `client_rate` for booking |
| 96–98 | Conversion fields; `quote_id` locks the rate; settles immediately | ✅ | non-SETTLED conversion holds transfers |
| 99 | Insufficient-funds codes differ by endpoint | ✅ | any error halts that step and is reported with its code |
| 100 | List Global Accounts first; reuse one with local USD | ✅ | primary-currency USD only |
| 102 | Deposit lands in the account's currency | ✅ | learned on the real sandbox (HKD); fixed |
| 104 | For a missing currency, fund USD and convert | ✅ | EUR/GBP come from conversions |
| 105 | Deposit says PENDING, balance appears immediately | ↔✅ | the real sandbox settled after a delay; we wait for SETTLED on `/deposits` |
| 106 | Re-read balances after a deposit before recalculating | ✅ | CLI step 5 re-reads balances, then recalculates |
| 112–114 | Every demo: goal → plan → new info → revised decision → result; ends in an action or an escalation; credentials out of model context | ✅ | |

## (5) Starter Kit 03: Payment Ops Incident Commander (second track)

Built on the Kit 01 executor; `venthora-sandbox --scenario kit03_incident --incident <OBLIGATION>:<FAILURE_TYPE>`.
Verified on the fake sandbox and by tests (🧪) and **on the real sandbox on 23 Oct 2026** (SANDBOX_RESULTS.md, cycle 5:
original `9297602a-…` CANCELLED / BENEFICIARY_BANK_RETURNED, replacement `60f3e3cd-…` PAID under the duplicate lock,
incident RESOLVED; a second payment of the same invoice was refused before any money moved).

| Requirement (guide) | Status | Where |
|---|---|---|
| A sent transfer did not arrive; the agent decides to wait, replace or escalate | 🧪 | `execution/incidents.py`: `decide()` table over Airwallex `failure_type`; 11 cases tested |
| Code enforces finality: a transfer that may still clear is never replaced | 🧪 | non-terminal → WAIT inside `incident_wait_hours`, ESCALATE beyond; only CANCELLED/FAILED can be replaced |
| Idempotency: the replacement is its own operation | 🧪 | new `request_id` persisted before the call; original and replacement ids asserted different |
| Duplicate lock: the company cannot pay twice | 🧪 | one live payout per invoice fingerprint; the original is recorded FAILED before the key is re-claimed; a third payment is refused |
| CANCELLED does not mean a person cancelled; use the failure type | 🧪 | the typed `failure_type` drives the decision; wrong-payee types escalate, returned-funds types replace, intent/control types escalate |
| Demo flow: send, leave in an intermediate state, fail with a chosen type, decide, replace under the lock | 🧪 | CLI step 7 leaves the transfer in flight when `--incident` is set; step 8 simulates SENT → FAILED(type) and runs the commander |
| Endpoints: beneficiary schema/create, transfer create, transitions, transfer listing | 🧪 | reused from Kit 01; `GET /transfers/{id}` re-read after every transition |
| Simulate SENT before PAID; the failure type applies only after SENT | 🧪 | commander transitions to SENT first |
| Prefer a FAILED transition with a type over a bare cancellation | 🧪 | always `FAILED` + `failure_type` |
| Original and replacement get different request IDs | 🧪 | asserted in `test_replace_under_duplicate_lock_with_second_fee_and_resolution` |
| Budget for two SWIFT fees | 🧪 | the learned fee is added to the replacement cost; if the wallet cannot cover it the replacement is refused and escalated (`test_replacement_without_funds_for_the_second_fee_is_refused`); reconciliation expects the original's fee to be lost |
| Do not close the incident because the replacement is PAID; record the original too | 🧪 | status `REPLACED_AWAITING_ORIGINAL` until the original's CANCELLED status, failure type and refund are recorded; then `RESOLVED` |
| Every Airwallex `failure_type` handled | 🧪 | scenario matrix I01–I32: all 32 types in the decision table, plus the wait window (0/47/48/49/720 h) and an unfundable replacement; `docs/SCENARIO_MATRIX.md` |

## (6) Robustness: the scenario matrix

`python scripts/scenario_matrix.py` runs 136 real-world variants (normal days, cash and reserve extremes, obligation
structure, FX extremes, receivables, policy knobs, adversarial inbox and model output, rail faults, every Kit 03
failure type, load and stability, roles and recalculation) through the full pipeline and ten invariants, and writes
[`docs/SCENARIO_MATRIX.md`](SCENARIO_MATRIX.md) plus a JSON log under `data/runs/matrix/`. The same variants run in
CI (`tests/test_scenario_matrix.py`). Result on 23 Oct 2026: 136 / 136; one product bug found and fixed (B10, see AUDIT.md §3).

## (7) Intake from external users and the dashboard

`venthora-intake` / `POST /v1/intake/emails`: payment requests arrive as email from mailboxes we do not control
(three Gmail, one Yahoo, one Outlook in `data/intake/`). The sender is verified against the exact registered mailbox
(`data/payees.json`), never the display name or the provider; amount, invoice, due date, bank-change and urgency
language are extracted with verbatim quotes; the status (VERIFIED / NEEDS_REVIEW / QUARANTINED / DUPLICATE) is decided
by code. Verified requests join the book as payable obligations (or a receivable with the payer's history); everything
else is visible in the plan and can never be paid; a duplicate of an invoice already in the book is never paid twice.
Tested end to end on the fake sandbox (`tests/test_intake.py`) and **run on the real sandbox on 23 Oct 2026**
(SANDBOX_RESULTS.md, cycle 6): the registered invoices were paid (`4147b10b-…`, `5acdf7b3-…`) and reconciled to the
cent; the lookalike "new bank account, urgent" request, the duplicate reminder and the flagged contractor were not. The dashboard at `/` shows the plan with its
counterfactual breach probabilities, the intake queue with role-checked verify/reject, executions, incidents, run
reports and the scenario-matrix summary.

## (8) Ground truth and the submission form

Two suites whose answers were derived by hand from the published rules, never by running the service, each case with
its derivation ([GROUND_TRUTH.md](GROUND_TRUTH.md)):

- `data/groundtruth/cases.json` — 54 cases (37 plans, 17 intake emails), one rule each (`scripts/make_groundtruth.py`).
- `data/groundtruth/anomalies.json` — **46 anomalies, no normal day**, the organizers' own failure modes ordered from
  the smallest to the largest (`scripts/make_anomaly_groundtruth.py`): fees and FX buffers that break a reserve, stale
  quotes, receipts that slip or fall outside the horizon, PENDING deposits, lookalike mailboxes, injection, Reply-To
  diversion, duplicates, bank changes, limits scaling with confidence, approvals on wrong terms or by the wrong role,
  lost responses, 429 storms, `duplicate_request_id`, `balance_insufficient` at the rail, conversions that never settle,
  every Kit 03 failure class, bank returns after SENT, paying the same invoices twice, stale state, a base wallet already
  below the floor, and everything at once.

All 100 are reproduced in-process (CI); the 84 that do not move money also through the HTTP endpoints
(`scripts/groundtruth_bench.py`) and through the dashboard's `Submit a case` form in a browser, with latency on every path.
**Both suites were then run against the real sandbox** (`scripts/groundtruth_real.py`, SANDBOX_RESULTS.md cycle 7): 84 / 84
through the live service with live balances read for every plan, and the 7 execution anomalies a real rail can reproduce
(stale rate, three approval refusals, the same invoices twice, stale state, Kit 03 end to end) 7 / 7; the other 9 are
fake-only with the reason recorded. The real rail found one more product bug (a follow-up on a replaced incident opened a
second incident on the replacement), fixed and tested.

## (9) Budget · mandate: the organisation's plan is the agent's mandate

The guide's "amounts, limits, reserves … in code" and "the agent acts within its mandate", made concrete: `data/budgets.json`
holds the monthly / quarterly plan per line (`payroll`, `logistics`, `suppliers`, `contractors`, `saas`) with the share the
agent may spend on its own (`auto_share`), an alert threshold (`alert_share`, optionally a hard stop), and a `bypass` that
counts only when a person signed it. Obligations name their line (intake-created ones inherit it from the payee registry).

| Rule | Where |
|---|---|
| Theorem never proposes more than mandate − realized on a line; what no longer fits is ESCALATED with a **budget request** stating the exact shortfall and the obligations behind it | `engines/theorem.py` line caps, `proofcore/budget.py` |
| Above the auto share, above the mandate, an alert that demands a person, an unsigned bypass, an unbudgeted obligation → `REQUIRES_APPROVAL` with the reason code | `proofcore/policy.py::budget_reasons` mirrored in `policy/treasury.rego`; proven on a real OPA 0.70.0: 2,688 budget inputs around every threshold (auto share, alert, mandate, signed/unsigned bypass, unbudgeted) inside the 7,638-input grid, 0 disagreements |
| An alert is raised at the threshold even when the plan stays autonomous (a notification, in the plan, the CLI and the dashboard) | stage `Budget · mandate`, `PlanResult.budget.alerts` |
| Realized spend per line comes from money that actually left (recorded at reconciliation), granted mandate from a role-checked, audited grant; both feed the next plan | `execution/store.py` (`budget_spend`, `budget_grants`), `execution/facts.py`, `POST /v1/budgets/{period}/{line}/grant` |
| Pipeline per line: plan · granted · realized · paying now · still owed · available · minus · utilisation · status | dashboard section `Budget · mandate`, `GET /api/budget` |

Ground truth: 20 hand-derived cases (`scripts/make_budget_groundtruth.py`), 20/20 in-process, through the endpoints and
through the form; real sandbox cycle 8 ran the mandate end to end (pay inside the line, stop at it with a request, grant,
continue, alert near the end).

## (10) Finance controls: instalments, three-way match, period cut-off, reconciliation cases

| Control | Rule, in code | Where |
|---|---|---|
| Instalments (deposit, balance, term-N) | one payout per *(invoice, instalment)* under the duplicate lock; the parts may never exceed the contract total (Verity rejects the part that would) | `engines/verity.py`, `execution/executor.py::_payout_key`, `execution/facts.py` |
| Three-way match | expected = contract − paid so far; an invoice claiming more than that beyond tolerance (max(USD 5, 0.5 %)) → `amount_mismatch`, a person; under-claims noted, never unsafe | `proofcore/match.py`, stage `Verity · three-way match`, `policy.py` + `treasury.rego` |
| Period cut-off | obligations due after `period_end` carry forward to the next period (not this period's lines); due before `period_start` are late-posted; `POST /v1/budgets/{period}/close` issues the cut-off statement (plan, realized, open commitments, carry-forward) and closes the period; spend against a closed period → `period_closed`, a person | `proofcore/budget.py` (`place_in_period`, `cutoff_report`), `api.py` |
| Reconciliation cases | a reconciliation that does not add up opens a case that **locks the book**: every later execution is REFUSED until a finance manager closes the case with a note (audited) | `execution/store.py` (`recon_cases`), `executor.py::_preflight`, `POST /v1/recon-cases/{id}/close`, dashboard |

Ground truth suite 4 (`scripts/make_controls_groundtruth.py`): 18 hand-derived cases, 18/18 in-process; the 16 that do not
move money also 16/16 through the endpoints and the dashboard form. Rego mirror re-proven on a real OPA with the two new
reasons.

## (11) Reporting periods: the admin's cut-off, three layers, one reporting currency

| Rule | Where |
|---|---|
| Cut-off = admin's day + time + timezone (`data/reporting.json`, `PUT /v1/reporting/config`, admin only); a document posted at or before the cut-off instant reports in that month, after it in the next; the due date still decides when it is paid | `reporting.py::reporting_period` |
| Every payment is booked to the report period of its **payment instant** (ledger `spend_ledger`, written at reconciliation from the transfer's timestamp) | `execution/executor.py`, `store.py` |
| Layer 1 monthly spend by currency and line · layer 2 invoice statements as of the period's end (an instalment invoice is RECONCILED only when its parts add up to the contract, in the month of the last part) · layer 3 consolidation to the reporting currency at the period's closing rates (snapshot from Airwallex or set by the admin, recorded with source), beside the base cost actually paid (FX difference); missing rates flagged | `GET /v1/reporting/periods/{period}`, `POST …/rates`, dashboard section |

Ground truth suite 5: 18 hand-derived cases, 18/18 in-process, through the endpoint (`POST /v1/reporting/eval`) and through
the form; real sandbox: the ledger holds today's real instalments (deposit + balance, both booked to 2026-10, invoice
RECONCILED); the rate snapshot found that the sandbox does not quote IDR (`unsupported_currency` / `invalid_parameter`), so the admin's fallback
rates are recorded with that source — a finding, not a guess (SANDBOX_RESULTS.md cycle 11).

## (12) The book ledger: double-entry per book on a shared wallet

| Rule | Where |
|---|---|
| Every book has its own accounts per currency — cash (available), committed (in flight), expense, fees, fx, funding — and every entry is balanced per currency | `ledger.py`, `store.journal` |
| Postings follow the rail: open (allocation), convert, commit (transfer created: amount + fee leave cash), settle (PAID), refund (returned: amount back, fee stays), deposit, grant, adjust (a person, audited) | `execution/executor.py`, `incidents.py`, `POST /v1/ledger/{book}/adjust` |
| **A book never spends another book's money**: before any money moves, every conversion and payout still to be made must fit the book's own cash, whatever the shared wallet holds; a post-fact overdraft (e.g. a fee larger than planned) is recorded and reported, never hidden | `Executor._ledger_preflight`, `store.post(strict=…)` |
| Reconciliation on a shared wallet: the wallet's movement since the run's `before` read must equal the sum of the cash lines of **every** book since then; only the remainder is a discrepancy (closes the cycle-9 resume limit) | step `ledger · wallet-vs-books` |
| The next plan starts from the book's ledger cash, with money in flight excluded | `execution/facts.py` |
| TigerBeetle remains the optional external mirror; the agent proves against this ledger without it | `gateways/tigerbeetle.py` |

Ground truth suite 6: 15 hand-derived cases, 15/15 in-process, through `POST /v1/ledger/eval` and through the form; real
sandbox cycle 12: two books reconciled on one wallet with nothing unexplained, a third refused before money moved.

## (13) Limits and velocity: how much, to whom, how often

| Rule (base-currency cost; null = no limit) | Reason code |
|---|---|
| one payout above `max_single_payment` | `single_payment_above_limit` |
| paid to a payee this report period + paying now above `max_per_payee_period` (two payouts in one plan count together) | `payee_period_limit_exceeded` |
| a payee never PAID before getting more than `max_first_payment` (no history = everyone is new) | `first_payment_to_new_payee_above_limit` |
| rolling 24 h / 7 d outflow + paying now above `max_outflow_daily` / `max_outflow_weekly` | `daily_outflow_limit_exceeded` / `weekly_outflow_limit_exceeded` |

History comes from the spend ledger (`store.payment_history`, filled by `facts.py` as `state.history`); the stage
`Policy · limits` names the payouts, payees and totals behind every reason; `proofcore/limits.py` mirrored in
`policy/treasury.rego` (OPA grid: 9,598 inputs, 0 disagreements). Ground truth suite 7: 18 cases, 18/18 on three paths;
real sandbox cycle 13: a known payee paid inside the limit, the same payee over its period limit stopped for a person
and then paid, a single payout above the limit refused without approval and executed with it.

See [AUTONOMY_MAP.md](AUTONOMY_MAP.md) for the three tiers: what the agent does alone, what a person must approve,
what only a person can do.

## (14) Payee lifecycle: NEW → VERIFIED → ACTIVE, cooling after a bank change, the call-back is mandatory

The guide's payee-fraud case (an email announces new bank details) becomes a state machine the agent cannot talk its
way around (`src/venthora/payees.py`, persisted in the execution store, seeded from `data/payees.json` `lifecycle` and
the fingerprint of the bank details in `data/beneficiaries.json`):

| Status | How it is reached | What the plan does with a payout to it |
|---|---|---|
| `NEW` | registered, never confirmed | **ESCALATE**: no payment, not even with an approval |
| `VERIFIED` | a finance manager's call-back **on the registered number** (any other number, any other role: refused) | `REQUIRES_APPROVAL payee_in_cooling_period` until `cooling_until`, then `payee_not_yet_active` until the first PAID payout |
| `ACTIVE` | a PAID payout at or after `cooling_until` | nothing from this control |
| `CHANGED` | bank details changed (intake email flag, portal, API): cooling starts, `policy.payee_cooling_hours` (72) | **ESCALATE** until the call-back; a second change restarts the clock |
| `SUSPENDED` | a finance manager | **ESCALATE**; reinstating goes back to `NEW` |

Verity turns NEW / CHANGED / SUSPENDED / unregistered into a policy exception (the obligation is escalated, the rest of
the plan runs); the stage `Payee · lifecycle` and `policy/treasury.rego` carry the two approval reasons (OPA grid:
9,598 inputs, 0 disagreements); the executor re-reads the live record before money moves and refuses a payee the plan
saw differently; a PAID payout promotes the record. Endpoints `/v1/payees`, `/verify`, `/bank-change`, `/suspend`,
`/reinstate`, `/eval`; dashboard section with the call-back and bank-change actions. Ground truth suite 8: 32 cases
(14 plans, 18 state-machine), 32/32 on three paths; real sandbox cycle 14 (SANDBOX_RESULTS.md).

## (15) The approval matrix (four-eyes): who, and how many different people, must approve

`policy.approval_matrix` is a list of tiers (`ApprovalTier`: `min_total_minor`, `reasons`, `approvals_required`,
`roles`), loosest first. When the plan stops for a person, the last applicable tier decides: a tier applies when the
plan's paying total (base currency) reaches its threshold or when one of its reason codes was raised. No tier applies:
one approver from `approver_roles`; no reason raised: nothing (`proofcore/approvals.py`, mirrored in
`policy/treasury.rego` as `decision.approval`; OPA grid 9,598 inputs, 0 disagreements). The plan carries the
requirement (`PlanResult.approval_requirement`, stage `Policy · approvals`, the dashboard's Decision tile).

Before money moves the executor checks every approval given (`ExecutionRequest.approvals`, `requested_by`): an allowed
role for the tier, the plan's own terms digest, one person counts once however the name is spelled, and **the requester
may never approve their own plan**; fewer valid approvals than the tier needs is refused with the reason. Ground truth
suite 9: 26 cases (11 plans, 15 checks), 26/26 on three paths; real sandbox cycle 15: one finance manager for USD
1,000; at USD 2,000 one approval, the same person twice and the requester were refused, a finance manager plus a CFO
executed.

## Not applicable to this build

- **#14, #16–18, #23, #25–26, #28–29** (MCP setup and MCP-only tools): VENTHORA uses REST behind typed methods, which the guide allows (#17, #24).
- **#84, #86** apply only if other corridors are used. Only US/GB LOCAL and DE SWIFT are used.
- **#94, #107** (customer `x-on-behalf-of` accounts).
- **#108–111** (kits 9, 10 and 12).
