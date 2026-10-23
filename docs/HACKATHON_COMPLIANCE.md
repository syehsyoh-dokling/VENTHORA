# Hackathon compliance checklist

Sources: [airwallex.hackerearth.com](https://airwallex.hackerearth.com/), [Airwallex developer guide](https://airwallexdev.com/guide), [Airwallex API reference](https://www.airwallex.com/docs/api/introduction), checked 23 Oct 2026. The organizers do not publish judging weights.

Legend: ✅ done and verified on the real Airwallex sandbox (see [SANDBOX_RESULTS.md](SANDBOX_RESULTS.md)) · ⬜ not started

## Submission deliverables (due 13 Nov 2026)

| Required | Status |
|---|---|
| Working demo | ✅ Plan → approve → quote → convert → pay → settle → reconcile, three cycles on the real sandbox |
| Video walkthrough under 5 minutes | ⬜ Script ready: [DEMO_SCRIPT.md](DEMO_SCRIPT.md) |
| Repository link with setup instructions | ✅ https://github.com/syehsyoh-dokling/VENTHORA |
| Built on Airwallex APIs | ✅ Auth, balances, rates, quotes, conversions, beneficiaries, transfers, simulation |

## Starter Kit 01: Adaptive Treasury Controller

| Kit requirement | Where | Status |
|---|---|---|
| Five obligations across three currencies over 72 hours | `data/scenarios/kit01_*.json` | ✅ |
| Partial payment while keeping a reserve floor | Theorem budget + ProofCore floor invariant | ✅ |
| Read balances and obligations | `Airwallex.balances_minor`, `/live-state` | ✅ |
| Plan in code | `engines/theorem.py` (CP-SAT / exact) | ✅ |
| Lower forecast confidence for one receipt, then recalculate | `kit01_receipt_delayed` scenario | ✅ |
| Simulate the deposit (`POST /simulation/deposit/create`) | `POST /api/sandbox/deposits` | ✅ |
| Execute one FX conversion (`/fx/quotes/create`, `/fx/conversions/create`) | `execution/executor.py` | ✅ |
| Execute one supplier transfer (`/beneficiaries/create`, `/transfers/create`) | `execution/executor.py`, `data/beneficiaries.json` | ✅ |
| Confirm the remaining reserve after execution | Reconciliation re-reads live balances | ✅ |
| Include flat EUR 12.85 fee per SWIFT payout | `TreasuryPolicy.swift_fee_minor`, in the conversion amount | ✅ |
| Every FX quote single-use, booked once with one request ID | `used_quotes` + persisted `request_id` | ✅ |
| Autonomous conversion limit in code, tied to forecast confidence | `proofcore/policy.py` + `policy/treasury.rego` | ✅ |

## Build guidance ("Build around a financial decision")

| Guidance | Status |
|---|---|
| Show goal, initial plan, new information, revised decision, resulting balance/payment/status | ✅ plan + revision; ✅ resulting payment and balance |
| Finish with a financial action or an escalation to a person | ✅ escalation; ✅ executed action after approval |
| Amounts, limits, reserves, validation, duplicate protection, state transitions in code | ✅ |
| Model reads unstructured text, compares evidence, proposes, explains | ✅ inbox reader (local Qwen2.5-0.5B on GPU, verified 3/3 runs) + original Verity citation check |
| Typed tools, not unrestricted credentials | ✅ |
| State which actions require a person's approval | ✅ policy reason codes; approval bound to `plan_hash` |

## Verified on the real sandbox, 23 Oct 2026

Beneficiaries for the US and GB LOCAL corridors and the DE SWIFT corridor were accepted; the `transfer_reason` values in `data/beneficiaries.json` were accepted; `GET /api/v1/deposits` is the authoritative receipt record. Real fees differ from the guide: EUR 13.37 SWIFT, USD 3.00 and GBP 2.24 LOCAL (see SANDBOX_RESULTS.md).

## Still open

- `GET /transfers?request_id=` and `GET /fx/conversions?request_id=` (lost-response recovery) are exercised only against the fake sandbox; no response was lost on the real one.
- LLM evidence reader on a larger model; the local 0.5B model is enough for the demo inbox.
