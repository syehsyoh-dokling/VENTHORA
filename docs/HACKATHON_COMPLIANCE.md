# Hackathon compliance checklist

Sources: [airwallex.hackerearth.com](https://airwallex.hackerearth.com/), [Airwallex developer guide](https://airwallexdev.com/guide), checked 23 Oct 2026. The organizers do not publish judging weights.

## Submission deliverables (due 13 Nov 2026)

| Required | Status |
|---|---|
| Working demo | 🟡 Planning + verification loop works end to end; Airwallex execution pending |
| Video walkthrough under 5 minutes | ⬜ Script ready: [DEMO_SCRIPT.md](DEMO_SCRIPT.md) |
| Repository link with setup instructions | 🟡 README complete; push to a public GitHub repo |
| Built on Airwallex APIs | 🟡 Auth + balances implemented; quotes, conversions, transfers, simulation pending |

## Starter Kit 01: Adaptive Treasury Controller

| Kit requirement | Where | Status |
|---|---|---|
| Five obligations across three currencies over 72 hours | `data/scenarios/kit01_*.json` | ✅ |
| Partial payment while keeping a reserve floor | Theorem budget + ProofCore floor invariant | ✅ |
| Read balances and obligations | `GET /balances/current` in `gateways/airwallex.py` | ✅ |
| Plan in code | `engines/theorem.py` (CP-SAT / exact) | ✅ |
| Lower forecast confidence for one receipt, then recalculate | `kit01_receipt_delayed` scenario | ✅ |
| Simulate the deposit (`POST /simulation/deposit/create`) | | ⬜ |
| Execute one FX conversion (`/fx/quotes/create`, `/fx/conversions/create`) | | ⬜ |
| Execute one supplier transfer (`/beneficiaries/create`, `/transfers/create`) | | ⬜ |
| Confirm the remaining reserve after execution | Projection done; post-execution re-read pending | 🟡 |
| Include flat EUR 12.85 fee per SWIFT payout | `TreasuryPolicy.swift_fee_minor` | ✅ |
| Every FX quote single-use, booked once with one request ID | | ⬜ |
| Autonomous conversion limit in code, tied to forecast confidence | `proofcore/policy.py` + `policy/treasury.rego` | ✅ |

## Build guidance ("Build around a financial decision")

| Guidance | Status |
|---|---|
| Show goal, initial plan, new information, revised decision, resulting balance/payment/status | ✅ plan + revision; 🟡 resulting payment after execution |
| Finish with a financial action or an escalation to a person | ✅ escalation (`REQUIRES_APPROVAL`); ⬜ executed action |
| Amounts, limits, reserves, validation, duplicate protection, state transitions in code | ✅ |
| Model reads unstructured text, compares evidence, proposes, explains | ⬜ LLM evidence reader |
| Typed tools, not unrestricted credentials | ✅ credentials server-side; typed client methods |
| State which actions require a person's approval | ✅ policy reason codes |
