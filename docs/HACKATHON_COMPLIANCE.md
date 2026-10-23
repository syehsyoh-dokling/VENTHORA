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
| Working demo | ✅ | `venthora-sandbox` on the real sandbox; four executed cycles, cycle 4 = the Kit 01 flow word for word |
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
| 52 | Thresholds in policy code, not prompts | ✅🧪 | `TreasuryPolicy`, `proofcore/policy.py`, `policy/treasury.rego`; Rego and Python agree on 990 inputs under a real OPA (`scripts/check_policy_mirror.py`) |
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
Verified on the fake sandbox and by tests (🧪); the real-sandbox run is listed in SANDBOX_RESULTS.md when done.

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

## Not applicable to this build

- **#14, #16–18, #23, #25–26, #28–29** (MCP setup and MCP-only tools): VENTHORA uses REST behind typed methods, which the guide allows (#17, #24).
- **#84, #86** apply only if other corridors are used. Only US/GB LOCAL and DE SWIFT are used.
- **#94, #107** (customer `x-on-behalf-of` accounts).
- **#108–111** (kits 9, 10 and 12).
