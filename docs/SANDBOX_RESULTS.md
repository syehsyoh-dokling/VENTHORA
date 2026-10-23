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

## Findings the sandbox revealed (and the fixes)

| Assumption (guide / fake) | Real sandbox | Fix |
|---|---|---|
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
