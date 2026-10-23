# Scenario matrix report

Generated 2026-10-23 04:06 UTC by `python scripts/scenario_matrix.py`. **136 / 136 variants satisfy every invariant.** Each row is one real-world situation run through the full pipeline (inbox -> plan and proof -> approval -> execution on the stateful fake sandbox -> deposit / re-plan / Kit 03 incident). The JSON log beside this file holds every detail.

- machine: AMD64 / CPython 3.13.6
- reader: deterministic lexicon reader (no model), plus stub models for the adversarial inbox variants
- sandbox: stateful fake Airwallex sandbox with real-sandbox fees (SWIFT EUR 13.37, LOCAL USD 3.00, GBP 2.24)

## Summary

| Family | Variants | All invariants hold |
|---|---|---|
| normal | 7 | 7 |
| cash | 10 | 10 |
| obligations | 13 | 13 |
| fx | 8 | 8 |
| receivables | 10 | 10 |
| policy | 8 | 8 |
| inbox | 12 | 12 |
| rail | 18 | 18 |
| incident | 38 | 38 |
| load | 6 | 6 |
| roles | 2 | 2 |
| recalc | 4 | 4 |

Plan latency: p50 23.4 ms, p95 46.6 ms, max 403.7 ms (warm, includes the 72-hour simulation and the counterfactuals).
Execution on the fake sandbox: p50 319.5 ms, max 759.8 ms.

## Findings (what the matrix changed)

- **B10 (only foreign cash) found a real inconsistency, now fixed.** With USD 0, EUR 50k and GBP 10k the plan pays the EUR and GBP invoices from their own wallets at zero base cost; ProofCore passes it (the floor binds base *spend*) and the policy escalates the 100% breach risk to a person. The executor, however, refused at the pre-flight and again at reconciliation because it compared the absolute USD level with the floor. Both checks now apply the gate's rule: a run that does not touch the base wallet cannot breach the floor; a base balance already below the floor is reported as such, not blamed on the run (`tests/test_execution.py::test_plan_funded_from_foreign_wallets_runs_when_base_is_already_below_floor`).
- **A00 (cold start) is expected to fail on the rail and does so cleanly.** Before the first cycle the plan only knows the guide's fees (SWIFT EUR 12.85, LOCAL free); the real sandbox charges LOCAL GBP 2.24, so a wallet holding exactly the budget is GBP 2.24 short and Airwallex answers `balance_insufficient`. The executor closes the run (FAILED, reconciled, no money lost), the fee is learned, and every later variant plans with the real fees. This is the cycle-1 behaviour recorded in docs/SANDBOX_RESULTS.md.
- **Stale planning rates (H00, H01) stop before anything is booked**: the quoted sell amount above the 1% slippage limit halts the run with nothing converted; a rail that is cheaper than planned (H01b) completes with a favourable variance.
- **Every Airwallex failure_type (I01–I32) lands on the decision table**: 6 types with returned funds are replaced under the duplicate lock and resolved; 26 types that mean wrong payee details or intent/control are escalated; a transfer in flight waits up to the 48 h window (I33–I35) and is escalated after it (I36, I37); a replacement the wallet cannot fund (I99) is escalated instead of attempted.
- **Adversarial inbox (G06–G11)**: prompt injection, garbage and hallucinated model output never change a decision: claims that cannot be located verbatim in the cited document are rejected and the lexicon reader re-reads.
- **Load**: 100 sequential plans p50 ≈ 13–14 ms, p99 ≈ 20 ms, no drift (last fifth / first fifth ≈ 1.0); 20 concurrent plans give one identical hash; 10 executions on 10 books complete with at most one PAID per invoice; a chain of 10 re-plans stays incremental.

## How to read the Execution column

`COMPLETED` = converted, paid, polled to terminal and reconciled to the cent. `FAILED` = the executor closed the run before or after a refusal by the rail; nothing is half-done and the message says why. `IN_FLIGHT` = the transfer was deliberately left in flight (Kit 03 variants, or a conversion that did not settle) and the run is resumable. `-` = nothing to execute (BLOCKED, all deferred, or a planning-only load variant).

## Invariants

`no_crash`, `deterministic` (same state -> same plan hash), `reserve_respected`, `pays_only_verified`, `reconciled`, `one_paid_per_invoice`, `unique_request_ids`, `incident_finality`, `incident_decision_matches_table`, `latency`.

## Results

| ID | Family | Severity | Situation | Plan | Breach | Execution | Incident | Invariants | ms |
|---|---|---|---|---|---|---|---|---|---|
| A00 | normal | high | cold start: fees not yet learned, wallet holds exactly the budget | APPROVED | 3.4% | FAILED | - | ✅ all | 534 |
| A01 | normal | normal | cash 22,500, EUR 1.0850, GBP 1.2700, stress 1,000 | APPROVED | 3.4% | COMPLETED | - | ✅ all | 288 |
| A02 | normal | normal | cash 19,000, EUR 1.1200, GBP 1.3300, stress 500 | APPROVED | 0.6% | COMPLETED | - | ✅ all | 419 |
| A03 | normal | normal | cash 30,000, EUR 1.0500, GBP 1.2000, stress 0 | APPROVED | 0.0% | COMPLETED | - | ✅ all | 457 |
| A04 | normal | normal | cash 26,000, EUR 1.1500, GBP 1.3800, stress 1,500 | APPROVED | 1.2% | COMPLETED | - | ✅ all | 468 |
| A05 | normal | normal | cash 21,000, EUR 1.0850, GBP 1.2700, stress 2,000 | APPROVED | 4.5% | COMPLETED | - | ✅ all | 405 |
| A06 | normal | normal | cash 40,000, EUR 1.0000, GBP 1.2500, stress 1,000 | APPROVED | 0.0% | COMPLETED | - | ✅ all | 448 |
| B01 | cash | extreme | zero cash | REQUIRES_APPROVAL (reserve_breach_risk_above_tolerance) | 100.0% | - | - | ✅ all | 98 |
| B02 | cash | extreme | cash exactly the reserve floor | REQUIRES_APPROVAL (reserve_breach_risk_above_tolerance) | 44.5% | - | - | ✅ all | 108 |
| B03 | cash | high | cash = floor + 1 cent | REQUIRES_APPROVAL (reserve_breach_risk_above_tolerance) | 44.5% | - | - | ✅ all | 121 |
| B04 | cash | extreme | cash below the reserve floor | REQUIRES_APPROVAL (reserve_breach_risk_above_tolerance) | 100.0% | - | - | ✅ all | 106 |
| B05 | cash | extreme | USD 10 billion (max minor units) | APPROVED | 0.0% | COMPLETED | - | ✅ all | 523 |
| B06 | cash | high | zero reserve floor | APPROVED | 0.0% | COMPLETED | - | ✅ all | 493 |
| B07 | cash | extreme | reserve floor above all cash | REQUIRES_APPROVAL (reserve_breach_risk_above_tolerance) | 100.0% | - | - | ✅ all | 103 |
| B08 | cash | extreme | stress larger than all cash | REQUIRES_APPROVAL (reserve_breach_risk_above_tolerance) | 97.6% | - | - | ✅ all | 111 |
| B09 | cash | high | stress eats exactly the headroom | APPROVED | 4.5% | COMPLETED | - | ✅ all | 374 |
| B10 | cash | high | only foreign cash: USD 0, EUR 50k, GBP 10k | REQUIRES_APPROVAL (reserve_breach_risk_above_tolerance) | 100.0% | COMPLETED | - | ✅ all | 306 |
| C01 | obligations | normal | single obligation | APPROVED | 0.0% | COMPLETED | - | ✅ all | 168 |
| C02 | obligations | high | twelve obligations (model maximum), mixed currencies | REQUIRES_APPROVAL (reserve_breach_risk_above_tolerance) | 11.3% | COMPLETED | - | ✅ all | 879 |
| C03 | obligations | extreme | everything critical and due now (0 h) | REQUIRES_APPROVAL (reserve_breach_risk_above_tolerance) | 9.2% | COMPLETED | - | ✅ all | 444 |
| C04 | obligations | moderate | everything due at the horizon (72 h) | APPROVED | 1.2% | COMPLETED | - | ✅ all | 432 |
| C05 | obligations | high | all SWIFT in EUR (fee on every payout) | REQUIRES_APPROVAL (conversion_above_autonomous_limit) | 0.0% | COMPLETED | - | ✅ all | 469 |
| C06 | obligations | extreme | five copies of the same invoice | APPROVED | 0.0% | COMPLETED | - | ✅ all | 191 |
| C07 | obligations | high | all obligations already settled | APPROVED | 0.0% | - | - | ✅ all | 70 |
| C08 | obligations | extreme | one-cent obligations | APPROVED | 0.0% | COMPLETED | - | ✅ all | 284 |
| C09 | obligations | extreme | one obligation of USD 1 billion | APPROVED | 0.0% | - | - | ✅ all | 81 |
| C10 | obligations | high | each obligation alone breaks the reserve | APPROVED | 0.0% | - | - | ✅ all | 84 |
| C11 | obligations | extreme | no eligible evidence at all (every document rejected) | APPROVED | 0.0% | - | - | ✅ all | 64 |
| C12 | obligations | high | evidence points at the wrong obligation | APPROVED | 0.0% | COMPLETED | - | ✅ all | 406 |
| C13 | obligations | moderate | evidence pending (not yet verified) for payroll | APPROVED | 0.0% | COMPLETED | - | ✅ all | 437 |
| D01 | fx | extreme | EUR worth 0.0001 USD | APPROVED | 0.0% | COMPLETED | - | ✅ all | 559 |
| D02 | fx | extreme | EUR worth 100 USD | APPROVED | 0.0% | COMPLETED | - | ✅ all | 414 |
| D03 | fx | high | rate with 10 decimals | APPROVED | 3.4% | COMPLETED | - | ✅ all | 436 |
| D04 | fx | moderate | EUR wallet already covers the EUR payout (no conversion) | APPROVED | 0.0% | COMPLETED | - | ✅ all | 593 |
| D05 | fx | high | GBP wallet covers half the GBP payout (partial conversion) | APPROVED | 1.6% | COMPLETED | - | ✅ all | 604 |
| D06 | fx | high | ECB stress requested but no history on disk (must fail closed) | BLOCKED: Horizon · risk: stress buffer 100000 minor; 1/1 receipts reliable | 3.4% | - | - | ✅ all | 154 |
| D07 | fx | moderate | ECB stress with the empirical model on a synthetic history | APPROVED | 5.0% | COMPLETED | - | ✅ all | 976 |
| D08 | fx | moderate | Horizon M0 paths on a synthetic history | REQUIRES_APPROVAL (reserve_breach_risk_above_tolerance) | 9.2% | COMPLETED | - | ✅ all | 705 |
| E01 | receivables | moderate | no receivables at all | REQUIRES_APPROVAL (reserve_breach_risk_above_tolerance) | 7.0% | COMPLETED | - | ✅ all | 461 |
| E02 | receivables | high | fifty small receivables | APPROVED | 0.9% | COMPLETED | - | ✅ all | 557 |
| E03 | receivables | extreme | receipt confidence 0% | REQUIRES_APPROVAL (conversion_above_autonomous_limit, reserve_breach_risk_above_tolerance) | 8.3% | COMPLETED | - | ✅ all | 468 |
| E04 | receivables | high | receipt confidence 100% | APPROVED | 2.9% | COMPLETED | - | ✅ all | 490 |
| E05 | receivables | high | arrival window ends after the horizon | REQUIRES_APPROVAL (reserve_breach_risk_above_tolerance) | 7.7% | COMPLETED | - | ✅ all | 467 |
| E06 | receivables | extreme | a USD 1 billion receipt | APPROVED | 3.4% | COMPLETED | - | ✅ all | 447 |
| E07 | receivables | moderate | payer history 0 kept / 100 missed | APPROVED | 3.4% | COMPLETED | - | ✅ all | 426 |
| E08 | receivables | moderate | payer history 100 kept / 0 missed | APPROVED | 3.4% | COMPLETED | - | ✅ all | 443 |
| E09 | receivables | moderate | receipt in GBP | APPROVED | 3.4% | COMPLETED | - | ✅ all | 533 |
| E10 | receivables | high | receipt expected right now (0 h) | APPROVED | 0.6% | COMPLETED | - | ✅ all | 494 |
| F01 | policy | extreme | autonomous conversion limit 0 (every conversion needs a person) | REQUIRES_APPROVAL (conversion_above_autonomous_limit) | 3.4% | COMPLETED | - | ✅ all | 445 |
| F02 | policy | extreme | autonomous limit 10^13 | APPROVED | 3.4% | COMPLETED | - | ✅ all | 428 |
| F03 | policy | high | confidence band one basis point wide | APPROVED | 3.4% | COMPLETED | - | ✅ all | 411 |
| F04 | policy | moderate | no escalation flags (bank changes are not exceptions) | APPROVED · inbox: 0 verified / 0 unverified receipt updates, 1 policy exceptio | 3.4% | COMPLETED | - | ✅ all | 494 |
| F05 | policy | high | breach tolerance 0% (any simulated risk needs a person) | REQUIRES_APPROVAL (reserve_breach_risk_above_tolerance) | 3.4% | COMPLETED | - | ✅ all | 472 |
| F06 | policy | moderate | 100 simulation paths (coarse) | APPROVED | 3.0% | COMPLETED | - | ✅ all | 440 |
| F07 | policy | high | planning horizon 1 hour | REQUIRES_APPROVAL (reserve_breach_risk_above_tolerance) | 8.3% | COMPLETED | - | ✅ all | 376 |
| F08 | policy | moderate | reliance threshold 100% confidence | APPROVED | 3.4% | COMPLETED | - | ✅ all | 414 |
| G01 | inbox | normal | empty inbox | APPROVED · inbox: 0 verified / 0 unverified receipt updates, 0 policy exceptio | 3.4% | COMPLETED | - | ✅ all | 473 |
| G02 | inbox | moderate | only embargoed (future) messages | APPROVED · inbox: 0 verified / 0 unverified receipt updates, 0 policy exceptio | 3.4% | COMPLETED | - | ✅ all | 444 |
| G03 | inbox | high | delay of 90 days | REQUIRES_APPROVAL (conversion_above_autonomous_limit, reserve_breach_risk_above_tolerance) · inbox: 0 verified / 1 unverified receipt updates, 1 policy exceptio | 8.2% | COMPLETED | - | ✅ all | 454 |
| G04 | inbox | moderate | delay of 'a week' | REQUIRES_APPROVAL (conversion_above_autonomous_limit, reserve_breach_risk_above_tolerance) · inbox: 1 verified / 0 unverified receipt updates, 1 policy exceptio | 8.3% | COMPLETED | - | ✅ all | 512 |
| G05 | inbox | high | delay of 400 days (beyond the sanity limit) | REQUIRES_APPROVAL (conversion_above_autonomous_limit, reserve_breach_risk_above_tolerance) · inbox: 0 verified / 1 unverified receipt updates, 1 policy exceptio | 8.2% | COMPLETED | - | ✅ all | 552 |
| G06 | inbox | extreme | prompt injection in the email | APPROVED · inbox: 0 verified / 0 unverified receipt updates, 1 policy exceptio | 3.4% | COMPLETED | - | ✅ all | 569 |
| G07 | inbox | extreme | 20,000-character email | REQUIRES_APPROVAL (conversion_above_autonomous_limit, reserve_breach_risk_above_tolerance) · inbox: 1 verified / 0 unverified receipt updates, 1 policy exceptio | 8.3% | COMPLETED | - | ✅ all | 467 |
| G08 | inbox | moderate | unicode payer name in the email | REQUIRES_APPROVAL (conversion_above_autonomous_limit, reserve_breach_risk_above_tolerance) · inbox: 1 verified / 0 unverified receipt updates, 1 policy exceptio | 8.3% | COMPLETED | - | ✅ all | 409 |
| G09 | inbox | extreme | model answers garbage (not JSON) | REQUIRES_APPROVAL (conversion_above_autonomous_limit, reserve_breach_risk_above_tolerance) · inbox: 1 verified / 0 unverified receipt updates, 1 policy exceptio | 8.3% | COMPLETED | - | ✅ all | 422 |
| G10 | inbox | extreme | model hallucinates a 2-day delay and a bank change for every payee | REQUIRES_APPROVAL (conversion_above_autonomous_limit, reserve_breach_risk_above_tolerance) · inbox: 1 verified / 0 unverified receipt updates, 1 policy exceptio | 8.3% | COMPLETED | - | ✅ all | 413 |
| G11 | inbox | high | model claims a delay citing the wrong document | REQUIRES_APPROVAL (conversion_above_autonomous_limit, reserve_breach_risk_above_tolerance) · inbox: 1 verified / 0 unverified receipt updates, 1 policy exceptio | 8.3% | COMPLETED | - | ✅ all | 478 |
| G12 | inbox | moderate | contradictory: delay and release on the same day | REQUIRES_APPROVAL (conversion_above_autonomous_limit, reserve_breach_risk_above_tolerance) · inbox: 1 verified / 0 unverified receipt updates, 1 policy exceptio | 8.3% | COMPLETED | - | ✅ all | 451 |
| H00 | rail | high | planning rate stale: rail EUR 3% dearer than the plan assumed | APPROVED | 3.4% | FAILED | - | ✅ all | 193 |
| H01 | rail | high | quote 5% worse than the planning rate (slippage) | APPROVED | 3.4% | FAILED | - | ✅ all | 276 |
| H01b | rail | moderate | planning rate stale: rail EUR 3% cheaper than the plan assumed | APPROVED | 3.4% | COMPLETED | - | ✅ all | 498 |
| H02 | rail | high | quote 0.9% worse (inside the 1% slippage limit) | APPROVED | 3.4% | COMPLETED | - | ✅ all | 437 |
| H03 | rail | high | conversion response lost | APPROVED | 3.4% | COMPLETED | - | ✅ all | 472 |
| H04 | rail | high | transfer response lost | APPROVED | 3.4% | COMPLETED | - | ✅ all | 493 |
| H05 | rail | extreme | 429 storm: three rate-limit errors on transfer create | APPROVED | 3.4% | COMPLETED | - | ✅ all | 502 |
| H06 | rail | high | 500 then OK on quotes | APPROVED | 3.4% | COMPLETED | - | ✅ all | 487 |
| H07 | rail | high | token expires once (401) mid-run | APPROVED | 3.4% | COMPLETED | - | ✅ all | 505 |
| H08 | rail | extreme | wallet holds less than the plan assumes (stale state) | BLOCKED: Airwallex · live balances: plan uses more cash than Airwallex shows | 3.4% | - | - | ✅ all | 117 |
| H09 | rail | high | beneficiary rejected by the corridor schema | APPROVED | 3.4% | FAILED | - | ✅ all | 309 |
| H10 | rail | high | conversion stays SCHEDULED (does not settle) | APPROVED | 3.4% | IN_FLIGHT disc={'USD': -435451, 'EUR': 401337} | - | ✅ all | 269 |
| H11 | rail | high | deposit never settles (receipt must not be counted) | APPROVED · receipt landed=False | 3.4% | COMPLETED | - | ✅ all | 504 |
| H12 | rail | moderate | deposit settles, plan recalculated | APPROVED · receipt landed=True | 3.4% | COMPLETED | - | ✅ all | 581 |
| H13 | rail | extreme | duplicate_request_id returned by the rail | APPROVED | 3.4% | FAILED | - | ✅ all | 342 |
| H14 | rail | extreme | insufficient funds at transfer (rail disagrees with wallet read) | APPROVED | 3.4% | FAILED | - | ✅ all | 349 |
| H15 | rail | high | shipping payout fails after SENT (bank return) | APPROVED | 3.4% | FAILED | - | ✅ all | 478 |
| H16 | rail | high | every payout fails after SENT | APPROVED | 3.4% | FAILED | - | ✅ all | 468 |
| I01 | incident | moderate | failure ACCOUNT_CLOSED (escalate) | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED (ACCOUNT_CLOSED) | ✅ all | 243 |
| I02 | incident | moderate | failure ACCOUNT_CURRENCY_MISMATCH (escalate) | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED (ACCOUNT_CURRENCY_MISMATCH) | ✅ all | 288 |
| I03 | incident | moderate | failure ACCOUNT_INACTIVE_OR_DORMANT (escalate) | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED (ACCOUNT_INACTIVE_OR_DORMANT) | ✅ all | 256 |
| I04 | incident | moderate | failure ACCOUNT_UNDER_RESTRICTION (escalate) | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED (ACCOUNT_UNDER_RESTRICTION) | ✅ all | 239 |
| I05 | incident | high | failure BENEFICIARY_BANK_RETURNED (replace) | APPROVED | 0.0% | IN_FLIGHT | REPLACE → RESOLVED (BENEFICIARY_BANK_RETURNED) | ✅ all | 337 |
| I06 | incident | moderate | failure BENEFICIARY_NAME_MISMATCH (escalate) | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED (BENEFICIARY_NAME_MISMATCH) | ✅ all | 250 |
| I07 | incident | moderate | failure BENEFICIARY_REQUESTED (escalate) | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED (BENEFICIARY_REQUESTED) | ✅ all | 238 |
| I08 | incident | moderate | failure CARD_ISSUER_ERROR (escalate) | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED (CARD_ISSUER_ERROR) | ✅ all | 245 |
| I09 | incident | high | failure CHANNEL_POLICY (replace) | APPROVED | 0.0% | IN_FLIGHT | REPLACE → RESOLVED (CHANNEL_POLICY) | ✅ all | 334 |
| I10 | incident | high | failure CHANNEL_TIMEOUT (replace) | APPROVED | 0.0% | IN_FLIGHT | REPLACE → RESOLVED (CHANNEL_TIMEOUT) | ✅ all | 339 |
| I11 | incident | moderate | failure CLIENT_REQUESTED (escalate) | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED (CLIENT_REQUESTED) | ✅ all | 265 |
| I12 | incident | moderate | failure DUPLICATION_RETURN (escalate) | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED (DUPLICATION_RETURN) | ✅ all | 267 |
| I13 | incident | moderate | failure EXCEEDED_TRANSACTION_AMOUNT_OR_LIMIT (escalate) | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED (EXCEEDED_TRANSACTION_AMOUNT_OR_LIMIT) | ✅ all | 283 |
| I14 | incident | moderate | failure INBOUND_2B_CARD_NOT_SUPPORTED (escalate) | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED (INBOUND_2B_CARD_NOT_SUPPORTED) | ✅ all | 241 |
| I15 | incident | moderate | failure INBOUND_BENFICIARY_VALIDATION_ERROR (escalate) | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED (INBOUND_BENFICIARY_VALIDATION_ERROR) | ✅ all | 264 |
| I16 | incident | moderate | failure INBOUND_COMPANY_REGISTRATION_NUMBER_ERROR (escalate) | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED (INBOUND_COMPANY_REGISTRATION_NUMBER_ERROR) | ✅ all | 239 |
| I17 | incident | moderate | failure INBOUND_ORDER_ERROR (escalate) | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED (INBOUND_ORDER_ERROR) | ✅ all | 244 |
| I18 | incident | high | failure INSUFFICIENT_FUNDS (replace) | APPROVED | 0.0% | IN_FLIGHT | REPLACE → RESOLVED (INSUFFICIENT_FUNDS) | ✅ all | 322 |
| I19 | incident | moderate | failure INVALID_ACCOUNT_NAME_OR_NUMBER (escalate) | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED (INVALID_ACCOUNT_NAME_OR_NUMBER) | ✅ all | 277 |
| I20 | incident | moderate | failure INVALID_BANK_INFORMATION (escalate) | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED (INVALID_BANK_INFORMATION) | ✅ all | 269 |
| I21 | incident | moderate | failure INVALID_BANK_OR_BRANCH_CODE (escalate) | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED (INVALID_BANK_OR_BRANCH_CODE) | ✅ all | 252 |
| I22 | incident | moderate | failure INVALID_BENEFICIARY_DETAILS (escalate) | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED (INVALID_BENEFICIARY_DETAILS) | ✅ all | 259 |
| I23 | incident | moderate | failure INVALID_CORRESPONDENT_BANK_INFORMATION (escalate) | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED (INVALID_CORRESPONDENT_BANK_INFORMATION) | ✅ all | 294 |
| I24 | incident | moderate | failure INVALID_PAYMENT_PURPOSE (escalate) | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED (INVALID_PAYMENT_PURPOSE) | ✅ all | 253 |
| I25 | incident | moderate | failure INVALID_SPECIAL_CHARACTER (escalate) | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED (INVALID_SPECIAL_CHARACTER) | ✅ all | 249 |
| I26 | incident | moderate | failure INVALID_SWIFT_BIC_CODE (escalate) | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED (INVALID_SWIFT_BIC_CODE) | ✅ all | 229 |
| I27 | incident | moderate | failure OTHER (escalate) | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED (OTHER) | ✅ all | 228 |
| I28 | incident | moderate | failure RECALL_REQUESTED (escalate) | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED (RECALL_REQUESTED) | ✅ all | 237 |
| I29 | incident | high | failure SYSTEM_ERROR (replace) | APPROVED | 0.0% | IN_FLIGHT | REPLACE → RESOLVED (SYSTEM_ERROR) | ✅ all | 307 |
| I30 | incident | moderate | failure TM_SUSPENDED (escalate) | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED (TM_SUSPENDED) | ✅ all | 252 |
| I31 | incident | moderate | failure TRANSACTION_AMOUNT_NOT_COVERING_FEE (escalate) | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED (TRANSACTION_AMOUNT_NOT_COVERING_FEE) | ✅ all | 238 |
| I32 | incident | high | failure UNABLE_TO_APPLY (replace) | APPROVED | 0.0% | IN_FLIGHT | REPLACE → RESOLVED (UNABLE_TO_APPLY) | ✅ all | 292 |
| I33 | incident | high | still in flight after 0 h | APPROVED | 0.0% | IN_FLIGHT | WAIT → WAITING | ✅ all | 230 |
| I34 | incident | high | still in flight after 47 h | APPROVED | 0.0% | IN_FLIGHT | WAIT → WAITING | ✅ all | 234 |
| I35 | incident | high | still in flight after 48 h | APPROVED | 0.0% | IN_FLIGHT | WAIT → WAITING | ✅ all | 230 |
| I36 | incident | high | still in flight after 49 h | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED | ✅ all | 240 |
| I37 | incident | high | still in flight after 720 h | APPROVED | 0.0% | IN_FLIGHT | ESCALATE → ESCALATED | ✅ all | 220 |
| I99 | incident | extreme | replacement with no money for the second fee | APPROVED | 0.0% | IN_FLIGHT | REPLACE → ESCALATED (BENEFICIARY_BANK_RETURNED) | ✅ all | 246 |
| J01 | load | extreme | 12 obligations x 20,000 simulation paths | REQUIRES_APPROVAL (conversion_above_autonomous_limit) | 1.3% | - | - | ✅ all | 96 |
| J02 | load | high | 50 receivables x 10,000 paths | APPROVED | 1.4% | - | - | ✅ all | 249 |
| J03 | load | high | 100 sequential plans (latency p50/p99, drift) | n=100, p50_ms=24.75, p99_ms=40.46, mean_first_fifth_ms=26.34, mean_last_fifth_ms=32.93, drift_ratio=1.25, stable=True | - | - | - | ✅ all | 2740 |
| J04 | load | high | 20 concurrent plans on one orchestrator | n=20, wall_ms=619.4, plans_per_s=32.3, all_same_hash=True, stable=True | - | - | - | ✅ all | 672 |
| J05 | load | high | 10 executions in a row on separate books (no double pay, all reconciled) | n=10, all_completed=True, max_paid_per_invoice=1, stable=True | - | - | - | ✅ all | 3754 |
| J06 | load | moderate | chain of 10 re-plans as cash grows | n=10, modes=['incremental', 'incremental', 'incremental', 'incremental', 'incremental', 'incremental', 'incremental', 'incremental', 'incremental', 'incremental | - | - | - | ✅ all | 265 |
| K01 | roles | high | viewer tries to plan | BLOCKED: Policy: role viewer may not plan payments | 3.4% | - | - | ✅ all | 108 |
| K02 | roles | normal | finance manager plans directly | APPROVED | 3.4% | COMPLETED | - | ✅ all | 438 |
| K03 | recalc | moderate | deposit of half the receipt | APPROVED · replan incremental [('SAAS-USD', 'DEFER', 'PAY')] | 3.4% | COMPLETED | - | ✅ all | 467 |
| K04 | recalc | high | cash drops by half after the plan | APPROVED · replan full [('PAYROLL-NOV', 'PAY', 'DEFER'), ('SUPPLIER-EU', 'CONVERT_AND_PAY', 'ESCALATE'), ('SAAS-USD', 'DEFER', 'PAY')] | 3.4% | COMPLETED | - | ✅ all | 480 |
| K05 | recalc | moderate | payroll amount re-issued higher | APPROVED · replan incremental [('PAYROLL-NOV', 'PAY', 'ESCALATE'), ('SAAS-USD', 'DEFER', 'PAY')] | 3.4% | COMPLETED | - | ✅ all | 452 |
| K06 | recalc | normal | identical state re-planned | APPROVED · replan incremental [] | 3.4% | COMPLETED | - | ✅ all | 470 |
