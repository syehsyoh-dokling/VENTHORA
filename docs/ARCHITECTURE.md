# Architecture

## Principle

> AI proposes. Independent engines verify. Airwallex executes. The ledger reconciles.

In structural engineering, an AI may propose a design, but a physics solver decides whether the building stands. VENTHORA applies the same pattern to treasury. No single engine proves a banking decision, so ProofCore combines several authorities. Each one owns a different kind of truth:

| Authority | Truth it owns | Module |
|---|---|---|
| ProofCore invariants | Post-action balances, reserve floor, no duplicate or unverified payout | `proofcore/gate.py`, `domain/projection.py` |
| Optimizer (OR-Tools CP-SAT) | Feasibility and optimality *for the supplied model* | `engines/theorem.py` |
| Policy (local + OPA) | Company rules: who may act, autonomous limits, escalation | `proofcore/policy.py`, `policy/treasury.rego` |
| TigerBeetle (optional) | Internal ledger balance | `gateways/tigerbeetle.py` |
| Airwallex | Actual wallet balances and transaction status | `gateways/airwallex.py` |

## Decision loop

1. **Understand.** Verity filters obligations to those with matching verified evidence and unique invoice fingerprints.
2. **Anticipate.** Horizon runs at the same time as Verity. It builds a stress buffer and decides which receipts are reliable (confidence ≥ policy threshold, inside the planning horizon). Forecasts never count as spendable cash.
3. **Optimize.** Theorem maximizes strict priority and then earliest due date. The constraint is that base-currency spend plus conversion cost (rounded up, SWIFT fees included) fits within `balance − reserve − stress`. Unpaid items are deferred only if a reliable receipt arrives before they fall due. Otherwise they are escalated.
4. **Prove.** ProofCore recomputes the plan from raw state with separate code. Any `FAIL` gives `BLOCKED`. Policy reasons give `REQUIRES_APPROVAL`. Otherwise the result is `APPROVED`.
5. **Execute → reconcile** *(build phase)*. Fresh single-use quote, conversion, transfer, each with a persisted `request_id`. Then status and balances are re-read and the plan is closed only once they match.

## Fail-closed rules

- Remote engines can only **narrow**: Verity's answer is intersected with local eligibility, Horizon's stress takes the maximum, and Theorem's selection must be a subset of verified obligations.
- An unreachable remote engine, missing requested ECB data, an OPA error or an OPA/local disagreement, or a stale Airwallex balance each produce `BLOCKED`.
- Money is an integer in minor units. FX rates are integer ppm. Outflows round up and inflows round down.

## Audit

Each plan is appended to a SHA-256 hash-chained SQLite log (`/api/audit` verifies the chain). It is tamper-evident for this deployment. It does not replace a ledger.
