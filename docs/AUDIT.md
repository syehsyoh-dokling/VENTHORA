# Adversarial audit (23 Oct 2026)

An audit of our own claims, done against the code rather than the documents we wrote. Anyone can
repeat it:

```bash
python scripts/oracle_audit.py          # behaviour comes from rules, not from the demo data
python scripts/check_policy_mirror.py   # Rego == Python policy, evaluated by a real OPA (Docker, or OPA_BIN=<official binary>)
pytest -q                               # 118 tests
```

## 1. Oracle hunt

**Question:** does the Kit 01 behaviour (shipping paid, minimum converted, cheapest deferred,
exception escalated, limit lowered by the email) come from the rules, or was the demo data tuned
to produce it?

**Method:** mutation. Change the data in ways that should change the answer, and in ways that
should not, and check the agent reacts to the former only.

| Mutation | Expected | Result |
|---|---|---|
| M1 rename every obligation id and payee | identical decisions | PASS |
| M2 swap the priorities of SHIPPING-UK and SAAS-USD | their roles swap | PASS: SaaS paid, shipping deferred |
| M3 make SAAS-USD the largest amount but lowest priority | still the one deferred | PASS: "cheapest" is the lowest cost of non-payment (priority), not the smallest amount |
| M4 delete the bank-change email | no policy exception | PASS: the contractor is decided on cash (deferred) |
| M5 edit the email to "two days" | forecast follows the text | PASS: 72 h, window 72–130 h |
| M6 spotless payer history (60/0) | the contradicting email still lowers the limit | **FAILED before the fix**, PASS after (see §3) |
| M7 change `case_id` | identical plan | PASS |

Additional static checks: no production module references a scenario id or demo value; the
"receipt delayed" scenario is generated from the inbox by `scripts/make_scenarios.py`, not written
by hand; the test that matters for safety is randomized (40 random states must never breach the
reserve), not a fixed expected output.

## 2. Claims in the compliance matrix, verified in code

Spot checks by `grep`, not by re-reading the matrix: no `x-api-version` header is sent (#10);
429/5xx retry with exponential backoff (#21–22); terminal transfer statuses are exactly
PAID/CANCELLED/FAILED (#92–93); `request_id` is a 36-character UUID persisted before the call
(#6–8, #89); only PAY/CONVERT_AND_PAY actions ever reach the executor, so an escalation can never
be paid (#48); learned fees keep the maximum observed (#70).

**Policy mirror.** `policy/treasury.rego` and `proofcore/policy.py` were evaluated on a grid of
9,598 inputs (confidence null/0–10000, three limits, three roles, with and without escalations,
five breach probabilities, five conversion sizes, and 2,688 budget-line inputs around every
threshold: auto share, alert with and without a required approval, the mandate itself, bypass
signed or not, unbudgeted obligations) by a real OPA 0.70.0 (the official binary, SHA-256
verified; the same script runs the Docker image): **0 disagreements** (23 Oct 2026).

## 3. Gaps the audit found, now closed

| Gap | Found by | Fix |
|---|---|---|
| **#38 approval terms were computed but never shown to the approver.** The plan hash bound the approval, but the person was never presented the amount, counterparty and evidence they were approving. | grep for `approval_terms` in the CLI and API: none | The CLI prints every payout and conversion with its evidence at approval time; the approval carries a `terms_digest`; execution refuses an approval given on different terms (`test_approval_on_different_terms_is_refused`) |
| **#51 a contradicting email did not lower the limit for a payer with a spotless record.** Confidence = min(previous, posterior); for 60 kept / 0 missed the posterior stayed at 97%, so the limit stayed full. | mutation M6 | A forecast its own payer has contradicted is capped below the fully-supported band (`contradicted_confidence_ceiling_bp`, default 70%), so the limit always drops; the payer's record then modulates below that |
| Test-level: the approval-terms test asserted after a later execution | test failure | fixed the assertion order |
| **Executor and gate disagreed on what the reserve floor binds.** ProofCore and Theorem bind the floor to base *spend*; the executor's pre-flight and reconciliation compared the absolute USD level with the floor, so a plan paid entirely from EUR/GBP wallets with USD already below floor was approved by a person and then refused by the executor. | scenario matrix variant B10 (`docs/SCENARIO_MATRIX.md`) | both executor checks now use the gate's rule; a base wallet untouched by the run is reported as "already below the floor (not caused by this run)" (`test_plan_funded_from_foreign_wallets_runs_when_base_is_already_below_floor`) |

### Closed since: the shared-wallet reconciliation limit

Cycle 9 recorded that a resumed execution on a shared wallet could not reconcile to the cent when other runs had moved
money in between. The book ledger (cycle 12) closes it: the wallet's movement is explained by the cash lines of every
book since the run's own `before` read, and a book that cannot fund its next step from its own cash is refused before
money moves.

## 4. Limitations found, stated rather than fixed

- **Verity's own "carries the claim" check is neutralised.** The original validator measures term
  overlap between claim and span, which is neither stem- nor punctuation-aware ("delay" vs
  "delayed."). VENTHORA passes the located sentence itself as the claim, so that check is always
  satisfied; what Verity still contributes is document existence, the date embargo and span
  resolution. The actual fact check (the figure "five days", bank + change terms) is VENTHORA's
  literal check, which only covers the two claim types it was written for.
- **The frame checker is not fully independent.** `frame.build` and `frame.check` share the
  projection primitives (`obligation_needs`, `project`) with ProofCore. It catches a wrong or
  tampered answer, not a bug in those primitives. The independent cross-checks are Theorem's
  `net_currency_exposure` (different code) and the exact-enumeration second candidate (different
  algorithm).
- **"Second candidate" is a deterministic cross-check**, not Theorem's original two-LLM
  self-consistency. Two solvers maximising the same objective are expected to agree; the check
  catches solver or encoding bugs, nothing more.
- **Cycle 4 on the real sandbox reused beneficiaries** created in earlier cycles. The
  schema → validate → create path through the executor is covered by tests; on the real sandbox
  only the direct schema and validate calls were exercised.
- **The challenger model's own proposal misses SHIPPING-UK** and is still APPROVED by the gate.
  That is by design: the gate refuses what is unsafe, not what is sub-optimal. The comparison
  line in the output makes the difference visible.
- **No video yet, no dashboard.** The demo is a CLI run. Both are listed as open in the
  compliance matrix.

## 5. How strong is the differentiation?

**What most Kit 01 entries will look like.** The guide steers everyone the same way: an LLM with
MCP or typed tools, limits in code, an approval step, a simulated deposit, one conversion and one
transfer, probably a web dashboard. Teams that follow the guide carefully will also have
`request_id` idempotency and a plan hash. Those are table stakes, and VENTHORA's versions of them
are thorough but not novel.

**What is unlikely to appear elsewhere** (each one is tested and was exercised on the real sandbox):

| Differentiator | Why it is more than the standard build |
|---|---|
| Counterfactual decision proof | every alternative (pay nothing, pay everything, also pay X, the receipt never arrives) is proven on the same invariants as the plan, with the breach in dollars |
| Incremental recalculation with locked decisions and a diff | the literal Kit 01 sentence; most entries will recompute everything and show a new list |
| Evidence grounding | verbatim quote, date embargo, literal fact check; an ungrounded model claim has **no effect**; the deterministic reader re-reads the email. Demonstrated live with a 0.5B model that hallucinated three claims a day |
| Calibrated forecast confidence | Laplace posterior from the payer's record, a 90% arrival window, reliance on its latest bound; contradicted forecasts capped. Most entries will hard-code 90% → 40% |
| Approval bound to terms the approver saw | terms digest + plan hash; an approval on different terms is refused |
| Execution teaches the next plan | real fees differed from the guide (13.37 vs 12.85; LOCAL not free); variance is reported and the fees are learned |
| Ring-fenced mandate | the agent spends its allocation, never the wallet, and reconciles its budget against the live wallet to the cent |
| AI challenger | any recommendation, including the LLM's own, goes through the same gate |
| Rego mirror verified by OPA | policy in two engines that must agree, proven on 9,598 inputs including the budget mandate |
| Safety independent of model quality | shown with a small local model; a stronger model improves proposals, not authority |

**Honest weaknesses against the field:** no UI (judges from fintech and VC respond to a
dashboard), the base currency is USD only, no webhooks (polling), no multi-tenant story, the
foundations for Kit 03 incident handling exist but the replacement-transfer flow is not built,
and the video is not recorded. The rigour is the differentiator; the presentation is not yet.
