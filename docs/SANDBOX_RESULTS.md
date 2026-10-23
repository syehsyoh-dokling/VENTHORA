# Real Airwallex sandbox results (23 Oct 2026)

These runs went against `https://api.sandbox.airwallex.com` using simulated money only. Run reports
are written to `data/runs/` (git-ignored; IDs below are copied from them). The approver was a
person acting as finance manager.

## Cycle 1: plan, approve, convert, pay, reconcile

| Step | Result |
|---|---|
| Inbox | Local Qwen2.5-0.5B read the Acme delay email; Verity verified the citation; receipt 24h/90% → 144h/40% |
| Plan | `REQUIRES_APPROVAL`: conversion above the low-confidence limit, VENDOR-UK escalated |
| Quote | EUR 4,012.85 for USD 4,551.54 at 1.134241 (within slippage of the 4,547.94 estimate) |
| Conversion | `87e36310-…` **SETTLED** |
| Transfers | PAYROLL-USD `82b441f6-…`, SUPPLIER-EU `5e333106-…` (SWIFT), SAAS-USD `dae0c920-…`: created as `SCHEDULED`, simulated SENT → **PAID** |
| Reconciliation | live balances equal to expected (no discrepancy); reserve held: USD 6,242.46 ≥ 6,135.03 |

## Cycle 2: what was learned, and a recommendation refused

- The paid invoices were recognised as settled. Learned fees were applied. The budget continued at
  USD 6,242.46.
- **The local LLM recommended paying PAYROLL-USD and SAAS-USD again. ProofCore BLOCKED it:** both
  are already settled and not payable, the USD wallet would go negative, and the reserve would be
  breached.
- A USD Global Account was created (`2d8a3a52-…`, ACTIVE). The simulated deposit was *not yet
  visible* when first checked, so it was correctly **not counted**.
- The plan moved no money (escalation only), so nothing was executed.

## Cycle 3: the delayed receipt lands, recalculate, execute

| Step | Result |
|---|---|
| Deposit | Airwallex deposit `f251d8f6-…` from Acme Retail Ltd, USD 10,000, **SETTLED**, found through `GET /api/v1/deposits` and not deposited twice |
| Recalculation | VENDOR-UK `ESCALATE → CONVERT_AND_PAY`; the plan is now `APPROVED` within the autonomous limit |
| Conversion | GBP 3,000 → `8a7e71dc-…` **SETTLED** |
| Transfer | VENDOR-UK `9e401900-…` **PAID** |
| Reconciliation | no discrepancy; reserve held: USD 12,177.51 ≥ 6,135.05 |

## Cycle 4: the November invoices, Kit 01 word for word (`venthora-sandbox --receipt`)

This cycle runs the Kit 01-aligned scenario, book `kit01-nov`, end to end on the real sandbox,
with the local Qwen2.5-0.5B reading the inbox. Approver: the project's finance manager.

| Step (guide order) | Result |
|---|---|
| Read balances and obligations | live wallet USD/EUR/GBP ~10M each; ring-fenced budget USD 22,500; rates EUR 1.132594, GBP 1.353267; learned fees applied |
| Day-1 inbox | Lumen's bank change verified and cited → CONTRACTOR-US is a **policy exception**; 3 ungrounded model claims rejected |
| Initial plan: **APPROVED** autonomously | shipping and payroll paid, **minimum** conversions for the supplier (EUR 4,013.37 incl. learned SWIFT fee) and shipping (GBP 2,502.24), SaaS (cheapest) **deferred**, contractor **escalated**; limit USD 10,000 at 90% confidence |
| AI challenger | naive "pay everything" **BLOCKED**; the model's own proposal passes the gate but misses SHIPPING-UK |
| Day-2 email (new information) | delay verified and cited; confidence 90% → **70%**, window 144–290 h; limit → USD 5,000; **the same conversion** needs a person (`REQUIRES_APPROVAL`); only SAAS-USD changes (DEFER → ESCALATE) |
| Deposit | Acme USD 10,000 to the USD Global Account; `SETTLED` 02:26:29Z; balances re-read |
| Recalculate | incremental: PAYROLL-NOV, SHIPPING-UK, SUPPLIER-EU kept; SAAS-USD ESCALATE → PAY; **APPROVED** |
| Beneficiaries | all four were **reused** from earlier cycles (stored Airwallex ids), so none was created here. The schema → validate → create path is covered by tests, and both endpoints were called directly on the real sandbox (schema: required fields present; validate: `"OK"`) |
| Conversions | EUR `06456e16-…` and GBP `5d4ab0e4-…`, both **SETTLED** |
| Transfers | PAYROLL-NOV `788b96e2-…`, SHIPPING-UK `4ec813fc-…`, SUPPLIER-EU `154cf79d-…`, SAAS-USD `e80a5b84-…`: SCHEDULED → SENT → **PAID** |
| Reconcile, confirm reserve | **no discrepancy**; managed USD 15,356.00 ≥ floor 6,127.46 |

**Plan vs actual, after learning the fees.** LOCAL USD 3.00 and GBP 2.24 were predicted exactly. The remaining variance was:

- **USD −6.28**: the quoted price versus the indicative planning rate (EUR +3.60, GBP +2.68, about 0.08%). This was inside the slippage limit, and the reserve was re-checked at the quoted price before booking.
- **EUR −0.01**: the SWIFT fee was 13.38 this time, against 13.37 before. The higher value is stored for the next plan.

Cycle 1's overrun (EUR 0.52, GBP 2.24, USD 6.00 of unplanned fees) did not recur.

## Cycle 5: Kit 03 incident on the real sandbox (`--scenario kit03_incident --incident SUPPLIER-EU-NOV:BENEFICIARY_BANK_RETURNED`)

Approved by the owner as finance manager; 23 Oct 2026, 11:19–11:23 WIB. Two runs, because the first one found a real
difference between the fake and the sandbox (see the findings table below).

| Step | What happened on the sandbox |
|---|---|
| Plan: **APPROVED** autonomously | EUR 1,500 SWIFT to Rheinwerk Komponenten GmbH, budget USD 8,000, floor USD 2,000 + stress 16.93; learned fees applied (SWIFT EUR 13.38); P(breach) 0.0% over 2,000 paths |
| Quote → conversion → transfer | quote `55a8d111-…`, conversion `eb61d5da-…`, transfer `9297602a-…` (`SCHEDULED`), reconciled: no discrepancy, floor held; transfer deliberately **left in flight** |
| Stage the failure | `SENT`, then `FAILED` with `failure_type=BENEFICIARY_BANK_RETURNED` via `/simulation/transfers/{id}/transition` |
| Run 1: re-read the original | status `FAILED`, **no `failure_type` field** → decision table saw `UNKNOWN` → **ESCALATE** (fail-closed; incident `ESCALATED`, nothing replaced) |
| Fix | the sandbox reports the failure as `failure.code` **90802** (+ `failure.details.type` `INCORRECT_ROUTING`) and moves the transfer to `CANCELLED`; the gateway now maps the code through Airwallex's failure-type table (`FAILURE_CODES`); the fake sandbox emits the real shape |
| Run 2, step 7 | the plan tried to pay the same invoice again: **`claim:SUPPLIER-EU-NOV REFUSED` — duplicate payment blocked before any money moved**, across two separate runs, on the real sandbox |
| Run 2, step 8 | original `9297602a-…` re-read: `CANCELLED` / `BENEFICIARY_BANK_RETURNED`, EUR 1,500 refunded (fee EUR 13.38 lost) → **REPLACE**: budget check (EUR 1,513.38 incl. the second fee), duplicate lock re-claimed with a **new request_id** `acd2bf5e-…`, replacement `60f3e3cd-…` → `PAID`; reconciliation EUR 10,001,499.47 → 9,999,986.09 = expected, discrepancy 0, refund observed |
| Incident | `3a5a4ca6-…` **RESOLVED** only after the original's CANCELLED status, failure type and refund were recorded |

Run reports: `data/runs/20261023-111947-kit03_incident.json` (run 1), `data/runs/20261023-112317-kit03_incident.json` (run 2).

## Cycle 6: requests from external users, end to end (`venthora-intake --as-of 2026-10-24 --execute --receipt`)

Approved by the owner as finance manager; 23 Oct 2026, 14:49–14:51 WIB. Five emails from mailboxes we do not control
(three Gmail, one Yahoo, one Outlook; `data/intake/`) go through intake, the book is grounded in the live sandbox and
what earlier cycles taught, and the loop runs.

| Step | What happened on the sandbox |
|---|---|
| Intake | `lumen.design.studio.billing@gmail.com` invoice LD-78 USD 1,800 → **VERIFIED**; `ld.studio.payments2026@gmail.com` "URGENT, new bank account for LD-77" → **QUARANTINED** (unknown mailbox claiming to be Lumen, bank change, pressure language); `thames.freight.accounts@gmail.com` reminder TF-311 → **DUPLICATE** of SHIPPING-UK; `mia.okafor.design@yahoo.com` timesheet USD 2,400 → **VERIFIED**; `acme.retail.ar@outlook.com` remittance USD 6,500 on 27 Oct → **VERIFIED** receivable |
| Grounding | learned fees applied; budget carried forward from cycle 4 (`3578bbbf`): **USD 15,356.00**; the five November invoices recognised as already paid (their transfer ids listed) and not re-proposed |
| Remittance lands | the settled USD 6,500 Acme deposit `c6946a55-…` (created by the first attempt, see below) was found **not yet counted and reused** instead of depositing again; budget USD 21,856.00; incremental re-plan: 2 decisions locked, no change |
| Plan: **APPROVED** autonomously | PAY REQ-OK-2026-10 USD 2,400 and REQ-LD-78 USD 1,800; REJECT REQ-LD-77 (evidence pending: the lookalike); ESCALATE CONTRACTOR-US (bank-details flag); the five settled invoices REJECT "already settled"; P(breach) 0.0% |
| Execute | beneficiaries created through schema → validate → create (`23467727-…` Okafor, `a15c4fad-…` Lumen); transfers `4147b10b-…` and `5acdf7b3-…` SENT → PAID; **reconciled: USD 17,650.00 = 21,856 − 4,200 − 2 × 3.00**, discrepancy 0 |

What was *not* paid: the lookalike's USD 3,000 to a new bank account, the duplicate TF-311, the flagged contractor, and
any of the five invoices paid in cycle 4.

Two things went wrong first, both recorded:
- The first attempt halted at the beneficiary step: the live corridor schema now marks **`payment_methods`** required,
  which the guide's `transfer_methods` alone does not satisfy (findings table). Nothing moved; the payout claims were
  released.
- An earlier fake-sandbox intake run had been pointed at the real execution store by mistake and left a `FAILED`
  record whose "managed budget" was the scenario allocation, not the live book; the budget carry-forward picked it up
  (USD 22,500 instead of 15,356). That record and the halted attempt's record were removed from the execution store
  before the rerun; the audit chain keeps every event. Store paths are now anchored at the project root so a fake run
  cannot land in the real store again.

Run report: `data/runs/20261023-145138-intake.json`.

## Cycle 7: both ground-truth suites against the real sandbox (`python scripts/groundtruth_real.py`)

Approved by the owner as finance manager; 23 Oct 2026, 16:18–16:25 WIB. Full detail in [GROUND_TRUTH.md](GROUND_TRUTH.md)
("Both suites on the real Airwallex sandbox") and `docs/groundtruth_log_real.json`.

**Part A — 84 plan / intake / incident cases through the live service.** 84 / 84 match the hand-derived answers; all 54
plans had their `Airwallex · live balances` stage read from the sandbox (none SKIPPED); plan round trip p50 105 ms.

**Part B — the execution anomalies on the real rail**, a fresh book per run (new obligation ids and invoice fingerprints):

| Case | On the real rail | Result |
|---|---|---|
| N04 stale planning rate | planning rate held at EUR 1.085 while the sandbox quoted ~1.13 | `EUR quote costs USD 4,540.70, above the slippage limit USD 4,398.07; nothing booked` — FAILED before any conversion ✅ |
| N26 / N27 / N28 approvals | wrong terms / viewer / none on a REQUIRES_APPROVAL plan | all three REFUSED, nothing moved ✅ |
| N41 same invoices twice | first run paid `9fb3a655-…`, `41559988-…`, `40b35ea3-…` (3 transfers, 2 conversions) | second run: `duplicate payment blocked before any money moved` ✅ |
| N42 stale state | allocation USD 20,000,000 against a ~9.98 M wallet | plan BLOCKED by the live-balance check, nothing executed ✅ |
| N44 Kit 03 end to end | transfer `66c8e69c-…` left in flight, failed with BENEFICIARY_BANK_RETURNED, replaced by `10220738-…` (run 1); `d62bc549-…` → `8033cd19-…` (run 2) | REPLACE under the duplicate lock; incident RESOLVED after a follow-up ✅ (see below) |
| N09, N29–N33, N39, N40, N45 | not reproducible on a real rail (a deposit that never settles, a lost response, a 429 storm, a reused request_id, a wallet drop between read and transfer, a conversion that never settles, a wallet of exactly EUR 1,512.85) | fake sandbox only; reasons recorded per case |

**What N44 found on the real rail.** Right after the FAILED transition the sandbox still reports the original as `FAILED`
without the typed failure; it moves to `CANCELLED` with `failure.code 90802` a few seconds later. The commander correctly
left the incident `REPLACED_AWAITING_ORIGINAL`. But when asked again, it looked up the invoice's live payout — now the
*replacement* — and opened a **second incident on the replacement** (PAID → "RESOLVE"), leaving the real one open. Fixed:
`IncidentCommander.handle` now finds the invoice's existing incident first; an incident awaiting its original re-reads
the original and closes only when its terminal status, typed failure and refund are on file; asking again returns the
same incident (`test_asking_again_returns_the_same_incident_not_a_new_one_on_the_replacement`). With the fix both
real incidents (`1027e8f3-…`, `ded29b6e-…`) were closed as RESOLVED / REPLACE; the one record the bug created was removed.

## Cycle 8: Budget · mandate on the real sandbox (`python scripts/budget_real.py`)

Approved by the owner as finance manager; 23 Oct 2026, 16:53 WIB. A fresh book (`budget-real-165336`) with its own
period and one line, so realized spend starts at zero: line `suppliers`, mandate USD 5,000; O1 USD 3,000 (priority 10)
and O2 USD 3,000 (priority 5); learned LOCAL fee USD 3.00.

| Run | What the agent did | On the sandbox |
|---|---|---|
| 1 | O1 **PAY**, O2 **ESCALATE** — "budget line 'suppliers' cannot fund it; a budget request is raised": mandate 5,000, paying now 3,003, still owed 3,003, available 1,997, **minus 1,006** → BUDGET REQUEST for USD 1,006 | transfer `56bc82bc-…` PAID; reconciled; **realized spend 3,003 recorded on the line** |
| 2 | the book re-planned from the store: O1 already paid (REJECT, settled), O2 still ESCALATE; the line shows realized 3,003 / available 1,997; the request stands | nothing moved |
| grant | a finance manager grants USD 1,100 on `suppliers` (`246cdb28-…`, role-checked, audited) | — |
| 3 | mandate 6,100 − realized 3,003 = 3,097 ≥ 3,003 → O2 **PAY**; utilisation 98% ≥ alert 80% → **ALERT** "budget line 'suppliers' at 98% of its mandate after this plan" (the line does not require approval on alert, so APPROVED) | transfer `76eaf883-…` PAID; reconciled |

What this shows: the agent never proposed beyond the line (Theorem's per-line cap), said exactly how much mandate was
missing and for which obligation, a person — not the agent — raised the mandate, and the alert fired on the way to the
line's end. Log: `docs/budget_log_real.json`.

## Cycle 9: the whole set in one command (`python scripts/real_suite.py --approve "…"`)

Approved by the owner as finance manager; 23 Oct 2026, 17:32–19:55 WIB (two attempts). Eight steps, each on a fresh
book, the runner stopping at the first failure. Report: [REAL_SUITE.md](REAL_SUITE.md), full output per step in
`docs/real_suite_log.json`.

| Step | Result |
|---|---|
| 1 Kit 01 cycle, fresh book `kit01-nov-1023173244` | ✅ plan → approve → EUR/GBP conversions → three transfers → deposit lands → incremental re-plan → reconciled (51 s) |
| 2 Kit 03 incident, fresh book | attempt 1 ❌ (see below); attempt 2 ✅ original `e59198af-…` CANCELLED / BENEFICIARY_BANK_RETURNED, replacement `bfb116c8-…` PAID, reconciled with `refund_timing` recorded, incident `040de823-…` RESOLVED (15 s) |
| 3 Intake of the five external emails, fresh book | ✅ two verified requests paid (`aa9690db-…`, `e750f983-…`); the lookalike, the duplicate and the flagged contractor not; the sandbox's simulation transition failed once, so one transfer was left **in flight** (resumable) — see below |
| 4 Ground truth suites 1 + 2 on the real sandbox | ✅ 84 / 84 through the live service, 7 / 7 reproducible execution anomalies, 9 fake-only with reasons |
| 5a Budget suite through the live service | ✅ 20 / 20, round trip p50 122 ms |
| 5b Budget mandate cycle on the rail | ✅ pay inside the line, stop with a request, grant, continue, alert at 98% |
| 6a Rego mirror on a real OPA 0.70.0 | ✅ 7,638 inputs, 0 disagreements |
| 6b Oracle audit | ✅ 7 / 7 mutations change the answer |

**Attempt 1 stopped at step 2 — a real-rail timing the fake never showed.** The original's EUR 1,500 refund landed
*between* the balance read before the replacement and the read after it; the reconciliation expected the refund to be in
the wallet already and reported a discrepancy of exactly EUR 1,500, so the incident stayed `REPLACED_AWAITING_ORIGINAL`
and the CLI treated that correct open state as a failure. Fixed: the replacement's reconciliation accepts both timings
of the same money and records which it saw; the CLI follows up until the original's fate is on file and treats the
open state as success. The attempt-1 incident (`bc5ffe06-…`) was then closed RESOLVED by the follow-up.

**Step 3's transfer left in flight** was resumed afterwards (`POST /v1/executions/{id}/resume` semantics): the rail
reported it PAID and the payout record was settled. The resumed run's *reconciliation* then reported a mismatch, because
its "before" balances date from the original run and steps 4–6 had since moved money in the same shared wallet. That is a
known limit, now written down: on a shared wallet a resume can only reconcile to the cent when nothing else moved money
in between (a book-level ledger — TigerBeetle, optional here — is the real answer). The payouts themselves are correct.

## Cycle 10: finance controls on the real sandbox

Approved by the owner as finance manager; 23 Oct 2026, ~20:30 WIB.

- **All four ground-truth suites through the live service** (`scripts/groundtruth_real.py`): **120 / 120** plan / intake /
  incident cases match, every one of the 90 plans with its `Airwallex · live balances` stage read from the sandbox (plan
  round trip p50 90 ms); the 7 reproducible execution anomalies 7 / 7 on the real rail again. Log: `docs/groundtruth_log_real.json`.
- **Instalments on the rail** (`scripts/controls_real.py`, log `docs/controls_log_real.json`): one invoice, contract
  USD 1,000 — run 1 paid the **deposit** (USD 300) and the **balance** (USD 700) as two payouts of the same invoice under the
  per-instalment duplicate lock, three-way matched (deposit: expected 1,000, balance: expected 700), reconciled; run 2
  presented the balance again → `duplicate payment blocked before any money moved`.
- The Rego mirror was re-proven on the real OPA with the two new reasons (`amount_mismatch`, `period_closed`):
  7,646 inputs, 0 disagreements.

## Cycle 11: reporting periods on the real sandbox

23 Oct 2026, ~21:40 WIB. The instalments cycle (`scripts/controls_real.py`) ran again so the new spend ledger holds real
payments: deposit USD 300 and balance USD 700 of invoice `cloudloom:CT-203634`, both PAID today → both booked to report
period **2026-10** (cut-off day 25 23:59 Asia/Jakarta, so 23 Oct is October's report). `GET /v1/reporting/periods/2026-10`:

| Layer | What it shows |
|---|---|
| 1 · monthly spend | 2 payments, USD 1,000.00 paid, base cost USD 1,006.00 (the two learned LOCAL fees) |
| 2 · invoice statements | `cloudloom:CT-203634` **RECONCILED** in 2026-10: deposit + balance = contract USD 1,000 |
| 3 · consolidation | IDR 15,800,000 at the October closing rate USD 15,800; FX difference −IDR 94,800 (the fees, at the base closing rate) |

**Finding.** `POST /v1/reporting/periods/2026-10/rates` tried to snapshot USD/EUR/GBP → IDR from Airwallex and the sandbox
answered `unsupported_currency` (and on a later call `invalid_parameter`) for IDR. The endpoint now falls back **per currency** to the admin's rates and records the
source of each rate (`admin fallback (unsupported_currency)`), rather than failing or guessing; with a reporting currency
the sandbox quotes (e.g. EUR) the snapshot comes from Airwallex.

## Cycle 12: the book ledger on the real sandbox (`python scripts/ledger_real.py`)

Approved by the owner as finance manager; 23 Oct 2026, 20:54 WIB. Three fresh Kit 01 books on the one sandbox wallet.

| Book | What happened |
|---|---|
| A `kit01-nov-A205429` | plan → execution COMPLETED; the wallet moved by USD −15,930.86 / EUR −0.02 and the book's own cash lines explain exactly that: **unexplained {}** |
| B `kit01-nov-B205429` | same, on the same wallet, right after A: its reconciliation is against *its* `before` read and *its* cash lines; A's money is not its concern: **unexplained {}** |
| C `kit01-nov-C205429` | its ledger was opened with the allocation and an earlier recorded spend, leaving USD 100 — while the wallet holds ~USD 9.9 M. The pre-flight refused the first conversion **before any money moved**: `book cannot fund convert … in USD: the book's own cash is short, whatever the shared wallet holds` |

Every book's accounts net to zero per currency (double entry); cash is what the book may still spend, committed is money
in flight, expense what left for good, fees what the rail charged. This closes the cycle-9 limit: a resumed run, or a run
that overlaps another book's, now explains the wallet's movement by the cash lines of *every* book since its own `before`
read, and only what no book knows about is a discrepancy. (The interleaved case is covered by `tests/test_ledger.py`;
on the sandbox A and B ran back to back.) Log: `docs/ledger_log_real.json`.

## Cycle 13: limits and velocity on the real sandbox (`python scripts/limits_real.py`)

Approved by the owner as finance manager; 23 Oct 2026, ~21:50 WIB. The books' own history decides what the agent may
do alone. The per-payee period limit is set relative to what the spend ledger already held for `ben-saas-vendor` today
(USD 5,112.00 across earlier cycles): limit = that + USD 1,300; single-payout limit USD 2,500.

| Run | Plan | Execution |
|---|---|---|
| 1 · USD 1,200 to the known payee | **APPROVED**, no reason: paid this period 5,112 + now 1,203 = 6,315 ≤ limit | COMPLETED, autonomous |
| 2 · USD 1,500 more to the same payee | **REQUIRES_APPROVAL** `payee_period_limit_exceeded` (6,315 + 1,503 = 7,818 > limit) | a finance manager approved → COMPLETED |
| 3a · USD 2,600 single payout, nobody approves | **REQUIRES_APPROVAL** `payee_period_limit_exceeded`, `single_payment_above_limit` | **REFUSED**: "plan requires a person's approval" — nothing moved |
| 3b · the same, approved | same reasons, approval bound to the terms | COMPLETED |

**Finding.** The first attempt saw every payee as *new* (`first_payment_to_new_payee_above_limit` on a payee paid
many times today): the spend-ledger rows booked before the `beneficiary_id` column existed had no payee. The store now
backfills that column from each execution's stored plan on start-up (8 rows recovered), and every new row carries it.
Log: `docs/limits_log_real.json`.

## Cycle 14: payee lifecycle on the real sandbox (`python scripts/payees_real.py`)

Approved by the owner as finance manager; 23 Oct 2026, ~21:44 WIB; payee `ben-saas-vendor` (Cloudloom, ACTIVE since
onboarding), fresh book, USD 1,000 invoices. Log: `docs/payees_log_real.json`.

| Step | Registry | Plan | Execution |
|---|---|---|---|
| 1 · "new IBAN" change recorded (as an intake email would) | ACTIVE → **CHANGED**, cooling until +72 h | invoice **ESCALATED**, nothing to pay | COMPLETED with 0 transfers, even with a finance manager's approval attached |
| 2a · call-back to the number printed in the email | **REFUSED** ("must be made to the registered number") | | |
| 2b · call-back by a viewer | **REFUSED** (role) | | |
| 3 · call-back on the registered number, finance manager | CHANGED → **VERIFIED**, cooling kept | `REQUIRES_APPROVAL payee_in_cooling_period` | 3a nobody approves → **REFUSED**; 3b approved → COMPLETED; payee stays VERIFIED |
| 4 · second change with cooling 0 h, call-back | CHANGED → VERIFIED, cooling over at once | `REQUIRES_APPROVAL payee_not_yet_active` | approved → COMPLETED → payee **ACTIVE** |
| 5 · next invoice | ACTIVE | **APPROVED**, no reason | COMPLETED, autonomous |

**Finding.** In the first attempt the sandbox's settlement simulation answered HTTP 500 for two transfers, which stayed
SCHEDULED (IN_FLIGHT). The payee correctly did **not** become ACTIVE: a transfer in flight is not a PAID payout, so the
first autonomous plan still asked for a person (`payee_not_yet_active`). The rerun settled, and the two in-flight
executions were resumed afterwards (both PAID, COMPLETED; the payee's paid count includes them). Five much older
IN_FLIGHT executions left over from fake-sandbox cycles were resumed in the same pass and closed as FAILED by the
duplicate lock, no money moved.

## Cycle 15: the approval matrix on the real sandbox (`python scripts/approvals_real.py`)

Approved by the owner as finance manager (and, for the second signature, as CFO); 23 Oct 2026, ~22:05 WIB; fresh book,
payee `ben-saas-vendor`. Policy: every payout above USD 500 stops for a person; matrix: one finance manager up to
USD 1,500 of paying total, two different people (finance manager or CFO) from USD 1,500. Log: `docs/approvals_log_real.json`.

| Run | Tier the plan announced | Approvals given | Execution |
|---|---|---|---|
| 1 · USD 1,000 | `one`: 1 finance manager | finance manager | COMPLETED |
| 2a · USD 2,000 | `two`: 2 different people | finance manager only | **REFUSED**: "needs 2 approval(s) from 2 different people; 1 valid" |
| 2b · USD 2,000 | `two` | the same finance manager twice | **REFUSED**: "approved twice; one person counts once" |
| 2c · USD 2,000 | `two` | finance manager + CFO, but the finance manager had requested the plan | **REFUSED**: "requested this plan and may not approve it (four-eyes)" |
| 2d · USD 2,000 | `two` | finance manager + CFO | COMPLETED; the authorization step names both |

## Cycle 16: FX exposure limits on the real sandbox (`python scripts/fx_real.py`)

Approved by the owner as finance manager; 24 Oct 2026, ~05:02 WIB; EUR 1,000 SWIFT invoices to `ben-eu-components`,
USD sold at the live sandbox quote (EUR 1,013.43 for USD 1,149.82); one book per run. Log: `docs/fx_log_real.json`.

| Run | Plan | Execution |
|---|---|---|
| 1 · daily cap above today's sales + this conversion | **APPROVED**; 24 h sales USD 18,157.14 + now 1,149.82 inside the cap | COMPLETED, one conversion, autonomous |
| 2a · daily cap = today's sales + USD 500 | **REQUIRES_APPROVAL** `daily_conversion_cap_exceeded` (19,307.86 + 1,149.82 > cap) | **REFUSED** without a person |
| 2b · the same, approved | same reason | COMPLETED |
| 3 · two EUR invoices, USD 6,500 funds one conversion above the floor, min hedge 80 % | first `CONVERT_AND_PAY`, second `ESCALATE`; coverage 50.7 % → `hedge_ratio_below_minimum` | approved → COMPLETED, one transfer; the second invoice is a person's problem |
| 4 · open-position cap EUR 1.00, book allocated EUR 300 | ring-fenced to the EUR the wallet held (3.00): position after the plan 0 → **no reason**, as the rule says | plan only |

**Finding.** In the first attempt runs 1 and 2 shared a book. Run 1's real SWIFT fee was EUR 13.43 against the
assumed 13.37, so the exact conversion left the book EUR 0.02 short; the fee was learned at once, but run 2 on the same
book was refused by the ledger pre-flight ("the book's own cash is short, whatever the shared wallet holds"). That is
the rule working: a book never spends another book's money, and a person adjusts it (`POST /v1/ledger/{book}/adjust`).
The plan's notes now say so (`facts.py`).

## Cycle 17: invoice aging and dunning on the real sandbox (`python scripts/aging_real.py`)

Approved by the owner as finance manager; 24 Oct 2026, ~05:31 WIB; books dated today, USD 1,000 invoices to
`ben-saas-vendor`; policy: escalation at 30 days overdue, interest exposure cap USD 5.00, dunning escalation at level 3.
Log: `docs/aging_log_real.json`.

| Run | Aging | Plan | Execution |
|---|---|---|---|
| 1 · due 45 days ago, fundable | bucket 31-60, accrued interest USD 9.86 (8 % a year) | **APPROVED**, paid now | COMPLETED |
| 2a · the same on a book that cannot fund it | accrued 9.86 + 3 projected days 0.66 = exposure 10.52 | `ESCALATE`; **REQUIRES_APPROVAL** `overdue_obligation_unpaid`, `late_interest_exposure_above_limit` | **REFUSED** without a person |
| 2b · the same, approved | | same | COMPLETED with 0 transfers: an escalation is never paid, the person now owns it |
| 3 · a final-notice email from the registered Cloudloom mailbox about the invoice in book 3 (intake, dry run) | **DUPLICATE**, dunning level 3, tied to the existing obligation; the book still holds one payable | | |
| 3b · that book, unfundable (plan only) | level 3, current | `ESCALATE`; `final_notice_unpaid` | |
| 3a · that book, fundable | level 3 | **APPROVED**, paid, no reason | COMPLETED |

**Finding.** In the first attempt 3a ran before 3b: once 3a had paid the invoice, the books marked it settled and 3b's
plan rejected it ("already paid"), so the order was swapped. The duplicate lock and the settled-record rule held across
the two books, as they should.

## Cycle 18: book and tenant segregation on the real sandbox (`python scripts/tenancy_real.py`)

Approved by the owner as finance manager; 24 Oct 2026, ~05:48 WIB. The registry gives the pattern `tenant-real-*` to
Acme: payees `ben-saas-vendor` only, approver "finance manager (owner)", override `max_single_payment` USD 500 (so every
payout here stops for a person). Fresh books. Log: `docs/tenancy_log_real.json`.

| Run | Tenancy | Plan | Execution |
|---|---|---|---|
| 1 · the book's payee, the book's approver | acme-group (pattern), override applied | `REQUIRES_APPROVAL single_payment_above_limit` (USD 1,000 > the book's 500) | COMPLETED |
| 2 · the same, approved by "Pat Lee", finance manager of nobody's book | | same | **REFUSED**: "not an approver of this book; the tenant registry names finance manager (owner)" |
| 3 · another tenant's payee (`ben-eu-components`), approval attached | disallowed: the obligation | **ESCALATE**, nothing to pay | COMPLETED with 0 transfers |
| 4 · the same book id under a strict registry without the pattern (plan only) | unknown | **BLOCKED** by Policy and Tenancy stages | |

**Finding.** The first attempt's run 1 stayed IN_FLIGHT (the sandbox settlement simulation answered late again); the
transfer existed, the control had done its work, and the execution was resumed to COMPLETED afterwards. The script now
counts an in-flight transfer as money moved.

## Findings the sandbox revealed (and the fixes)

| Assumption (guide / fake) | Real sandbox | Fix |
|---|---|---|
| A fee surprise of a few cents is harmless | it leaves the book short in that currency; the next payout in it is refused (cycle 16) | by design; the plan's notes name the overdraft and the adjustment a person must make |
| The sandbox settlement simulation always answers | HTTP 500 on two transfers (cycle 14); they stayed in flight | nothing to fix: an in-flight payout proves nothing, the payee stayed VERIFIED; `resume` settles later |
| History rows always name their payee | rows booked before the column existed had none, so a well-known payee looked new | `beneficiary_id` backfilled from stored plans at start-up; carried on every new row |
| Airwallex quotes any reporting currency | the sandbox answers `unsupported_currency` for IDR | closing-rate snapshot falls back per currency to the admin's figures, source recorded per rate |
| A resumed execution reconciles against its original "before" balances | other runs moved money in the shared wallet between the run and its resume → mismatch reported although every payout is PAID and recorded | stated as a limit (cycle 9); the book-level ledger (TigerBeetle, optional) is the proper fix |
| The original's refund is in the wallet before the replacement is made | on the real rail the refund can land **between** the balance read before the replacement and the read after it (cycle 9, step 2: discrepancy of exactly EUR 1,500) | the replacement's reconciliation accepts both timings of the same money and records which one it saw (`refund_timing`); the CLI treats `REPLACED_AWAITING_ORIGINAL` as a correct open state and follows up until the original is on file |
| A follow-up on an incident finds the same incident | after a replacement, the invoice's live payout *is* the replacement; a follow-up opened a second incident on it | the commander looks up the invoice's existing incident before anything else; see cycle 7 |
| Beneficiary payload needs `transfer_methods` | the corridor schema (`POST /beneficiaries/schema`) marks **`payment_methods`** required and relevant (23 Oct 2026) | both fields are sent for every registered payee; the schema check caught it before anything was created |
| A failed transfer carries `failure_type` | status **`CANCELLED`**, failure in `failure.code` (**90802**) and `failure.details.type` (`INCORRECT_ROUTING`); the transition response carries the detail type, not the enum sent | `failure_type_of()` maps the code through the documented table first, then `failure_reason` ("90802: …"), then the typed fields; unknown shapes still fail closed to ESCALATE |
| Flat EUR 12.85 per SWIFT payout | **EUR 13.37** | fees observed per `METHOD:CCY` are stored and used by later plans |
| LOCAL payouts are free | **USD 3.00**, **GBP 2.24** per LOCAL payout | same; plan-vs-actual variance reported in the reconciliation |
| Deposit balance appears immediately | the deposit settled **after** the first check | the receipt counts only when the deposit record says `SETTLED` (`/api/v1/deposits`), with polling and reuse of a settled, uncounted deposit |
| Deposit lands in USD | to the HK Global Account it landed in **HKD** (the account's primary currency) | only accounts whose primary currency is USD are used; a US USD account is created if none exists |
| New transfer is `PROCESSING` | `SCHEDULED` | treated as in flight, like every non-terminal status |
| Quote is single-use | returned as `MULTI_USE` | VENTHORA still books each quote once (used-quote registry) |

The fee difference showed up as exactly what reconciliation is for: the live wallet matched to
the cent, while the **managed budget** ended a few cents to a few dollars below the plan (EUR
−0.52, GBP −2.24). VENTHORA reports this as `budget exceeded by …`, learns the real fee, and
the next plan includes it.

## Speed

| Path | Measured |
|---|---|
| Local decision loop: Verity → Horizon → Theorem (CP-SAT) → ProofCore → counterfactuals | **p50 2.7 ms, p99 4.5 ms** (`python scripts/benchmark.py`, 300 runs) |
| Re-plan on the real sandbox (live balances read included) | **85 ms**, of which about 77 ms is the Airwallex network round trip |
| Horizon M0 FX stress | 428 ms the first time per day (ECB file); **0.04 ms** cached afterwards; warmed at API start |
| Local LLM inbox read (Qwen2.5-0.5B, RTX 4060) | about 2.8 s |
| End-to-end execution (quotes, conversion, beneficiaries, transfers, settlement, reconciliation) | seconds: bounded by sequential Airwallex calls and status polling |

The decision itself runs at millisecond speed. What remains is time spent waiting on the bank and
on the language model.
