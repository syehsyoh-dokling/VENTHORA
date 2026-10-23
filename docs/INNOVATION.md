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
treasury allocation of USD 22,500. Reconciliation and the reserve check run on that managed budget,
which moves exactly as the live wallet moves. An agent with a budget is a different, safer object
than an agent with an account.
*Code:* `execution/live.py` (`ring_fence`), `Reconciliation.managed_after_minor`.

### 2. Evidence: what the model says is believed only if it can be cited

A language model reads the inbox; in the measured runs this was Qwen2.5-0.5B, local on the GPU.
The model only *proposes*, for example "the Acme receipt is delayed five days" or "Lumen changed
its bank details". A claim is accepted only if:

- the quoted sentence is found verbatim in the email
- the original Verity validator accepts the citation, so the document exists and predates the
  embargo (a message dated after the as-of date is never read)
- the fact is written in the cited span (the figure "five days"; bank and change terms)

If the model's claim fails, the deterministic reader re-reads the same passages. A claim that no
reader can ground in the email **has no effect**. On the real run, the 0.5B model invented a delay
before any delay email existed and a bank change for a supplier, three ungrounded claims a day,
and all were rejected. Confidence is then calibrated from the payer's track record, with a 90%
arrival window, and model output can never make a receipt earlier or more certain.
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

Measured on the Kit 01 story (real sandbox, live rates):

```text
day 2  recalculation: incremental; triggered by RCV-ACME forecast 24h/90% -> 144h/70%
         kept (unaffected): PAYROLL-NOV, SHIPPING-UK, SUPPLIER-EU   re-optimised: SAAS-USD
         CHANGED SAAS-USD: DEFER -> ESCALATE
deposit  recalculation: incremental; triggered by USD budget +USD 10,000.00, RCV-ACME received/settled
         kept (unaffected): PAYROLL-NOV, SHIPPING-UK, SUPPLIER-EU   re-optimised: SAAS-USD
         CHANGED SAAS-USD: ESCALATE -> PAY
```

*Code:* `domain/diff.py`, `Orchestrator.decide(previous=...)`, `POST /v1/plans/replan`.

### B. Counterfactual decision proof

Every plan carries proven answers to the questions a CFO asks before approving. Each answer comes
from the same independent projection and the same floor as the plan. Live sandbox rates, 23 Oct 2026:

```text
✓ Is the chosen plan safe?                       reserve kept with USD 438.42 headroom
✓ What if we pay nothing?                        misses PAYROLL-NOV, SHIPPING-UK (priority 10, due 24h), SUPPLIER-EU (priority 9)
✗ What if we pay every verified obligation now?  breaches the reserve floor by USD 764.58
✗ What if we also pay SUPPLIER-EU-RESENT?        pays invoice eu-components:INV-2026-0512 twice
✗ What if we also pay CONTRACTOR-US now?         policy exception (payee bank details changed): never auto-paid, even with cash
✗ What if we also pay SAAS-USD now?              breaches the reserve floor by USD 764.58
✗ What if RCV-ACME never arrives?                SAAS-USD would be unfunded at its due date; VENTHORA re-plans and escalates then
```

When even the critical obligations cannot be funded, the plan says so outright: **"not payable as
posed"**, with the minimum extra cash needed (SeismoForge's "not buildable as posed").

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
| Local LLM (Qwen2.5-0.5B) | pay PAYROLL-NOV, SUPPLIER-EU, SAAS-USD | **APPROVED**: safe, but it misses SHIPPING-UK, which halts operations if unpaid |
| Local LLM, next cycle | pay PAYROLL and SaaS **again** (already paid) | **BLOCKED**: settled, not payable; wallet negative; reserve breached |
| Naive assistant ("pay everything that is due") | pay all, including the duplicate and the changed-bank payee | **BLOCKED**: unverified obligation, policy exception, duplicate fingerprint, wallet negative, reserve breached |

The gate refuses what is unsafe, not what comes from an AI. Even when every model recommends
paying, VENTHORA keeps the authority to refuse.
*Code:* `engines/llm_planner.py`, `POST /v1/plans/challenge`.

### D. The organisation's plan is the agent's mandate (Budget · mandate)

An agent with a budget is a different object from an agent with an account. The monthly or quarterly plan, line by
line, is the mandate: Theorem never proposes more than a line can still fund (mandate − realized, as a hard constraint
in CP-SAT and in the exact second candidate); what no longer fits is escalated **as a budget request with the exact
shortfall, the obligations behind it and their evidence**, so the agent asks for mandate the way a controller would,
and a person — never the agent — grants it (role-checked, hash-chained). Three thresholds per line decide when a
person is needed, in code and in Rego: the share spendable autonomously, an alert threshold that notifies (and can be
made a hard stop), and the mandate itself; a bypass counts only when signed. The pipeline per line — plan, granted,
realized (money that actually left), paying now, still owed, available, minus — is derived from the book and the
execution store, never typed in. Real-sandbox cycle 8 runs it end to end: pay inside the line, stop at it, grant,
continue, alert at 98%.

### E. The controls a finance team actually asks about

Four things a controller checks before trusting any payment system, each a rule in code with hand-derived ground
truth: an invoice settled in parts (a deposit, then the balance) is two payouts under a per-instalment duplicate lock
that can never exceed the contract total; every claim is three-way matched against contract minus what was already
paid, and an over-claim beyond tolerance goes to a person with the exact difference; the period has a cut-off — what
falls due after it carries forward, what was posted late is marked, and a closed period's statement (plan, realized,
open commitments, carry-forward) is issued once and never spent against autonomously again; and a reconciliation that
does not add up opens a case that locks the book until a person closes it with a note. None of these is a prompt.

## What each original system contributes (beyond speed)

The strengths below are the ones the portfolio describes for each system. Each one is now active
in VENTHORA and covered by tests:

| System | Strength in the portfolio | How it works in VENTHORA |
|---|---|---|
| **SeismoForge** | Intake rejects any value it cannot quote from the source text | Every model claim must be found verbatim in the email; a claim no reader can ground has no effect. On the real local model, 3 hallucinated claims a day were rejected |
| | An evidence gate re-simulates each design and refuses a verdict the physics contradicts | ProofCore re-derives every plan independently and refuses any plan or AI recommendation the arithmetic, policy or live bank state contradicts |
| | Declares a brief "not buildable as posed" | The frame states **"not payable as posed"** and the minimum extra cash needed when the critical obligations cannot all be funded |
| | A deterministic forge owns all numbers; the search policy is a tool surface | All amounts are integer code; the model gets only typed reading tasks |
| **Theorem** | Worked-problem frame (Given / Asked / Formula / Answer) and a checker that rejects incomplete frames | Every plan carries the frame; the stage `Theorem · decision frame` rejects missing sections, non-minimal conversions and identities that do not recompute |
| | Independent second candidate with a self-consistency tie-break | CP-SAT is cross-checked by exact enumeration at run time; disagreement blocks the plan; an exact tie goes to the cheaper selection |
| | Finance invariants checked before output is accepted | ProofCore invariants, plus the original `fx.net_currency_exposure` netting every FX leg |
| **Verity** | Every claim points to a real passage, with an entailment check | Original corpus, BM25 retriever and citation validator; a literal check of the fact (delay figure, bank-change terms) stands in where the original only measures overlap |
| | Date embargo | Messages dated after the as-of date are never read, as on the delay-day timeline |
| | 90% interval calibrated to evidence strength, widened when evidence is thin | Receipt confidence = Laplace posterior of the payer keeping promises; a 90% arrival window that widens with little history; reliance planned on its latest bound |
| **Horizon** | Deterministic seeded scenario engine | The original M0 engine draws joint FX paths; its p95 adverse move sizes the reserve buffer |
| | Text reshapes uncertainty, not just numbers, within bounds | The delay email moves the receipt *and* widens its arrival window, within bounded rules |
| | Self-check (schema, monotone quantiles, embargo) before writing | `horizon_m0` refuses non-finite draws, non-monotone quantiles or data after today, so the plan fails closed |
| | Deterministic lexicon fallback when the model is unavailable | The same fallback re-reads the email when the model is down **or** its claim does not verify |
| **Litmus** | A decision layer that never fails, over a measurement layer that may, degrading safely | Remote engines can only narrow; any failure fails closed; the model is optional |

## Mapping to the organizers' guidance

The complete, line-by-line matrix is in [HACKATHON_COMPLIANCE.md](HACKATHON_COMPLIANCE.md).

| Guidance | Where VENTHORA shows it |
|---|---|
| Decision changes with balance, deadline, evidence or risk | day-1 plan → delay email → revised plan → deposit → incremental recalculation |
| Controls in code, not prompts | policy code mirrored in Rego; the model never sets an amount |
| Typed tools, no credentials to the model | `gateways/airwallex.py`; the model only returns text |
| Approval bound to terms | `approval_terms` (amount, currency, counterparty, evidence) under `plan_hash` |
| Duplicate protection, idempotency | payout claims, persisted `request_id`, used-quote registry, lost-response recovery |
| Ends in an action or an escalation | executed conversions and transfers, plus a policy exception escalated with a model-written note |
| Kit 01: limit tied to forecast confidence | full at ≥90%, linear to zero at ≤50%; 90% → 70% halves it to 5,000 and the same conversion needs a person |
| Kit 01: recalculate only affected decisions | differentiator A |

## The fourth strength: speed, applied where it means something

The author's Pulse/fastsim kernel runs millions of simulator events per second (4–9M/s kernel-only on
the Agenthon board). That kernel is **not** in VENTHORA: a single treasury decision is bound by the
bank's round trip (~77 ms) and the language model, and the decision loop itself already runs in
milliseconds. Speed matters here in one place, and that is where it is used: the **72-hour
discrete-event reserve simulation**. For the plan and for every counterfactual, thousands of paths
run the window hour by hour: receipts arrive with probability equal to their calibrated confidence
inside their 90% window, FX moves along the original Horizon M0 joint paths, deferred obligations
are paid at due only if the path stays above the floor, and the unexpected outflow lands at a random
hour with an uncertain size (lognormal, median = the budgeted amount). The result is a
**probabilistic proof**: P(reserve breached within 72 h) and P(a deferred obligation is unfunded at
its due date), reported next to the deterministic proof, with a policy tolerance (5%) mirrored in
Rego and verified by OPA.

Measured honestly: the event loop is vectorised numpy, not C. Throughput is reported as
path-events per second over the loop's wall clock only (1.1–3.7M/s on this laptop; FX setup is
cached per day and excluded). The whole plan, with the simulation and seven simulated
counterfactuals, runs at **p50 12.9 ms** (was 2.8 ms without them).

On the Kit 01 story the simulation adds a fact the deterministic plan cannot see: on day 2, with the
receipt pushed outside the window, the chosen plan's breach risk rises above 5%, which is the second
reason a person is asked to approve.

## Kit 03 in the same agent: plan → pay → incident

The Kit 01 executor already owned finality, idempotency and the duplicate lock, so the Payment Ops
Incident Commander is the same rails applied after the money left: a typed failure code decides
WAIT / REPLACE / ESCALATE (wrong payee details are never auto-replaced), a replacement is a new
operation with its own `request_id` under the same invoice lock, the second SWIFT fee is budgeted and
refused if it does not fit, and the incident is not closed when the replacement is PAID but when the
original's fate is recorded too. One agent, one ledger of claims, across the whole payment lifecycle.

## What is *not* claimed

- The vendored original engines (Theorem FX netting, Horizon M0, Verity retrieval and validation)
  are small, focused components, not the product. They are cited for provenance, not as the pitch.
- Verity's original overlap check is effectively bypassed (the located sentence is passed as the claim);
  what Verity contributes is existence, embargo and span resolution. The fact check is VENTHORA's literal
  check, written for two claim types. Details and other limitations: [AUDIT.md](AUDIT.md).
- The simulation's stress-size distribution (lognormal, sigma 0.5) and uniform arrival inside the 90% window are
  modelling assumptions, stated in `proofcore/simulate.py`; the breach probabilities are only as good as them.
- The path-events/s figure is for a vectorised Python loop; the Pulse C kernel is not used.
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

## The demo moments (Kit 01 flow, guide order)

1. **Goal and initial plan (day 1):** shipping and payroll are paid, the minimum is converted for the
   supplier, the cheapest bill is deferred, and the changed-bank payee is escalated with a cited email
   and a model-written note. The plan is `APPROVED` autonomously at 90% confidence.
2. **Refusal:** the "pay everything" recommendation is blocked with its reasons; the counterfactuals
   show the breach.
3. **New information (day 2):** the delay email is cited; confidence drops from 90% to 70% and the
   window widens; the limit halves, and **the same conversion** now needs a finance manager. Only
   SaaS changes.
4. **The deposit settles:** it is confirmed on `/deposits` and balances are re-read. Only SaaS is
   recalculated (→ PAY), and the plan is `APPROVED`.
5. **Proof after the money moves:** schema → validate → create beneficiaries; quotes, conversions,
   transfers SENT → PAID; reconciliation to the cent; reserve confirmed; real fees learned.
