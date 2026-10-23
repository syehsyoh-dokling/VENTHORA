# What VENTHORA adds

## The problem the organizers posed

Airwallex frames the hackathon around agents that already track balances, start payments and
manage risk without a person in the loop. Their line is: *"The rails underneath those agents are
yours to design."* The developer guide spells out what good rails look like:

- a decision that changes when a balance, deadline, piece of evidence or risk changes
- amounts, limits, reserves, validation, duplicate protection and state transitions in code
- typed tools instead of credentials
- approvals bound to the exact terms the approver saw
- money actually moving in the sandbox, ending in a financial action or an escalation to a person

Starter Kit 01 (Adaptive Treasury Controller) sets the scenario: five obligations in three
currencies within 72 hours, a reserve floor, a customer receipt that slips, a deposit that
arrives, one FX conversion and one supplier transfer.

**The scenario is not the innovation.** Many teams will build Kit 01. VENTHORA's contribution is
the rails around the agent. In one sentence:

> A treasury agent may act on its own **only within its mandate, only on evidence it can cite,
> and only with proof**, before the money moves and after it lands.

## The rails

### 1. Mandate: the agent spends its allocation, not the wallet

The live sandbox wallet holds USD/EUR/GBP 10,000,000 each. VENTHORA plans on its ring-fenced
treasury allocation of USD 20,000. Reconciliation and the reserve check run on that managed budget,
which moves exactly as the live wallet moves. An agent with a budget is a different, safer object
than an agent with an account.
*Code:* `execution/live.py` (`ring_fence`), `Reconciliation.managed_after_minor`.

### 2. Evidence: what the model says is believed only if it can be cited

A language model reads the inbox. In the measured runs this was Qwen2.5-0.5B, local on the GPU,
taking about 2.8 s per read and verified in 3 of 3 runs. The model only *proposes*, for example
"the Acme receipt is delayed five days". The claim is accepted only if:

- the quoted sentence is found verbatim in the email
- the original Verity validator accepts the citation, so the document exists and predates the
  embargo (a message dated after the as-of date is never read)
- the figure "five days" is written in the cited span

The trust is asymmetric. Unverified bad news still lowers reliance on the receipt. Model output can
never make a receipt earlier or more certain.
*Code:* `evidence/reader.py`, `originals/verity_core/`.

### 3. Proof before execution

ProofCore re-derives every plan from raw state with its own projection code. It checks:

- invariants: no wallet goes negative, the reserve plus Horizon's FX stress holds, no unverified
  or duplicate payout
- the original Theorem toolkit netting each FX leg
- policy, locally and in OPA, which must agree
- live Airwallex balances

At execution time:

- every payout is claimed before any money moves
- the reserve is re-checked at the *quoted* FX price before anything is booked
- each money-moving call carries a `request_id` that is persisted first, so a lost response is
  recovered rather than paid twice
- the approval is bound to the `plan_hash`; if anything changes, the approval no longer applies

*Code:* `proofcore/gate.py`, `execution/executor.py`.

### 4. Proof after execution

`SENT` is never treated as final. Every transfer is re-read from Airwallex, and expected balances
(before + conversions − `amount_payer_pays`) are reconciled to the cent against the live wallet.
The run is `COMPLETED` only when they match and the reserve holds.

## Three differentiators

These were added after reviewing the plan against Kit 01 and the original proposal.

### A. Recalculate only the affected decisions, with a decision diff

Kit 01 says: *"when a deposit arrives, it recalculates only the affected decisions"*. VENTHORA
handles this in three parts:

- Payments chosen earlier whose own inputs did not change are **locked**.
- Only open decisions (deferred, escalated, rejected, new) are re-optimised.
- If keeping the earlier payments no longer fits, for example because cash dropped, Theorem falls
  back to a full recalculation and says why.

Measured on the Kit 01 story: the Acme deposit lands. PAYROLL-USD, SUPPLIER-EU and SAAS-USD are
kept, and only VENDOR-UK is re-optimised:

```text
recalculation: incremental; triggered by USD budget +USD 10,000.00, RCV-ACME received/settled
  kept (unaffected): PAYROLL-USD, SAAS-USD, SUPPLIER-EU   re-optimised: VENDOR-UK
  CHANGED VENDOR-UK: ESCALATE -> CONVERT_AND_PAY
```

*Code:* `domain/diff.py`, `Orchestrator.decide(previous=...)`, `POST /v1/plans/replan`.

### B. Counterfactual decision proof

Every plan carries proven answers to the questions a CFO asks before approving. Each answer comes
from the same independent projection and the same floor as the plan. Live sandbox rates, 23 Oct 2026:

```text
✓ Is the chosen plan safe?                       reserve kept with USD 116.72 headroom
✓ What if we pay nothing?                        misses PAYROLL-USD (priority 10, due 24h), SUPPLIER-EU (priority 9, due 48h)
✗ What if we pay every verified obligation now?  breaches the reserve floor by USD 3,945.23
✗ What if we also pay SUPPLIER-EU-RESENT?        pays invoice eu-components:INV-2026-0417 twice
✗ What if we also pay VENDOR-UK now?             breaches the reserve floor by USD 3,945.23
```

The agent chooses the action that satisfies the rules and the objective, and it shows why each
alternative is worse.
*Code:* `proofcore/counterfactual.py`.

### C. AI challenger: a wrong recommendation is blocked, a safe one is not

Any outside recommendation goes through `Orchestrator.challenge`. This can be the LLM's own
"which should we pay?", a person's suggestion, or a script's. Theorem is replaced by the proposal,
and Verity, Horizon, ProofCore and policy judge it exactly like a plan. Measured on the live
sandbox:

| Recommender | Proposal | Verdict |
|---|---|---|
| Local LLM (Qwen2.5-0.5B) | pay PAYROLL-USD, SAAS-USD | **APPROVED**: safe, but it misses SUPPLIER-EU, which VENTHORA pays |
| Naive assistant ("pay everything that is due") | pay all 5 | **BLOCKED**: unverified obligation, duplicate invoice fingerprint, USD wallet goes negative, reserve breached |

The gate refuses what is unsafe, not what comes from an AI. Even when every model recommends
paying, VENTHORA keeps the authority to refuse.
*Code:* `engines/llm_planner.py`, `POST /v1/plans/challenge`.

## Mapping to the organizers' guidance

| Guidance | Where VENTHORA shows it |
|---|---|
| Decision changes with balance, deadline, evidence or risk | delay email → revised plan → deposit → incremental recalculation |
| Controls in code, not prompts | policy code mirrored in Rego; the model never sets an amount |
| Typed tools, no credentials to the model | `gateways/airwallex.py`; the model only returns text |
| Approval bound to terms | `plan_hash` covers state, decisions and conversions |
| Duplicate protection, idempotency | payout claims, persisted `request_id`, used-quote registry, lost-response recovery |
| Ends in an action or an escalation | `REQUIRES_APPROVAL` escalation, then sandbox execution and reconciliation |
| Kit 01: limit tied to forecast confidence | 5,000 → 2,500 when the email contradicts the forecast |
| Kit 01: recalculate only affected decisions | differentiator A |

## What is *not* claimed

- The vendored original engines (Theorem FX netting, Horizon M0, Verity retrieval and validation)
  are small, focused components, not the product. They are cited for provenance, not as the pitch.
- Verity's original citation check measures term overlap, not entailment. The literal-figure check
  compensates for the delay use case only.
- The 0.5B local model is deliberately small. The point is that model quality does not decide
  safety. A stronger model (any OpenAI-compatible endpoint) improves recommendations, not authority.
- Real-sandbox evidence (three cycles, transfer IDs, findings) is in [SANDBOX_RESULTS.md](SANDBOX_RESULTS.md).

## Execution teaches the next plan

The real sandbox did not match the guide. SWIFT EUR cost 13.37, not 12.85, and LOCAL payouts were
not free (USD 3.00, GBP 2.24). A deposit settled after the first check, and a deposit to the HK
account landed in HKD. VENTHORA handles these in three ways:

- Reconciliation reports **plan-vs-actual variance** and any **budget overrun**, while the live
  wallet still matches to the cent.
- It **learns each real fee** per method and currency and applies it to the next plan.
- It continues the managed budget from the last reconciliation and marks paid invoices as settled.

In cycle 2 the local LLM tried to pay payroll a second time, and ProofCore blocked it.

## Speed

The decision loop runs in **2.7 ms at p50** and 4.5 ms at p99 (Verity, Horizon, CP-SAT, ProofCore
and counterfactuals). A re-plan against the live sandbox takes **85 ms**, about 77 ms of which is the
network. The remaining end-to-end time is spent waiting on Airwallex and on the language model,
not on deciding. Measured with `scripts/benchmark.py` and the run reports.

## The five demo moments

1. **Evidence:** the delay email is read and cited, and the future-dated email is embargoed.
2. **Revised decision:** VENDOR-UK is escalated and the conversion limit tightens, so a person
   must approve.
3. **Refusal:** the "pay everything" recommendation is blocked with four reasons; the counterfactuals
   show the USD breach.
4. **Recalculation:** the deposit lands and only VENDOR-UK changes; three decisions are kept.
5. **Proof after money moves:** quotes, conversions, transfers SENT → PAID, then reconciliation to
   the cent with the reserve confirmed.
