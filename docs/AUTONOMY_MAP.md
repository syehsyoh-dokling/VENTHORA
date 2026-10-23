# Autonomy map: what the agent does alone, what it may do until a limit stops it, what only a person can do

Three tiers, decided by code (never by the model's narrative). "Alone" means no person is in the loop and the
action is still gated by proof. "Conditional" means the agent does the work but a named person must approve before
money moves, on the exact terms the agent showed. "Human only" means the agent cannot produce the action at all;
without the person it stops and says so.

## Tier 1 — the agent alone (gated by proof, logged, reversible or bounded)

| Step | What the agent does on its own | What still binds it |
|---|---|---|
| Read the book, the policy and the inbox | parses evidence, locates verbatim quotes, calibrates receipt confidence from payer history | an unquotable claim has no effect; embargoed / future documents are ignored |
| Verify obligations (Verity) | accepts evidence that names the obligation and is verified; rejects duplicates (per invoice and per instalment), settled records, pending / rejected evidence, instalments past the contract | — |
| Plan (Theorem) | chooses what to fund, convert, defer, escalate — exact optimizer, second candidate must agree | reserve floor + FX stress, per-budget-line cap, exact-amount conversions only |
| Prove (ProofCore) | invariants, FX-leg netting, policy in Python and Rego, live balances, 72 h simulation, counterfactuals, the worked frame | any FAIL blocks the plan |
| Execute inside the mandate | single-use quote, conversion, beneficiary schema → validate → create, transfer, poll to terminal, reconcile to the cent, learn fees, book the ledger and the spend ledger | only an APPROVED plan; only the book's own cash; idempotent request ids; sandbox only |
| Incremental re-plan | when a deposit lands or new information arrives: re-opens only the affected decisions, reports the diff | locked decisions stay |
| Incident: wait | a transfer still in flight inside the wait window | never replaces a transfer that may still clear |
| Incident: replace after a return | returned funds (typed failure in the replace class) → new request id, second fee budgeted, duplicate lock | only while the book can fund the second fee; max replacements |
| Alerts | budget line near its threshold, over-claims noted, unbudgeted obligations, carry-forward, late-posted | notifications, not permissions |
| Reports | monthly spend by payment instant, invoice statements, consolidation at recorded closing rates, cut-off statement | derived from the books, never typed |

## Tier 2 — the agent prepares, a person must approve (the plan stops at `REQUIRES_APPROVAL`)

| Trigger (reason code) | What the agent has already done | Who may approve, on what |
|---|---|---|
| `conversion_above_autonomous_limit` — FX above the limit that shrinks with receipt confidence | planned the exact conversion, named the evidence | finance manager, bound to the terms digest |
| `reserve_breach_risk_above_tolerance` — simulated P(breach) above 5 % | shows the probability and every counterfactual | finance manager |
| `budget_line_above_auto_share`, `budget_line_alert_requires_approval`, `budget_bypass_unsigned`, `unbudgeted_obligation` | pipeline per line, exact utilisation | finance manager; a bypass counts only when signed |
| `amount_mismatch` — invoice claims more than contract − paid so far, beyond tolerance | three-way match row with the exact difference | finance manager |
| `period_closed` — spend against a closed period | the cut-off statement already exists | finance manager |
| `single_payment_above_limit`, `payee_period_limit_exceeded`, `first_payment_to_new_payee_above_limit`, `daily_outflow_limit_exceeded`, `weekly_outflow_limit_exceeded` | the payouts, payees, history and totals behind each reason | finance manager |
| `payee_in_cooling_period`, `payee_not_yet_active` — a verified payee inside the cooling period after a bank change, or never paid on these details | the payee's record, cooling end, the payout | finance manager; the first PAID payout after cooling makes the payee ACTIVE |
| Incident: replacement blocked by funds (`max_replacements`, second fee) | decided REPLACE, computed the budget | a person re-plans or funds |

An approval is refused by code when: the role is not an approver for the tier, the terms digest differs from the plan
shown, the plan hash changed since review, or the book is locked by an open reconciliation case. The approval matrix
(`policy.approval_matrix`) decides how many **different** people a tier needs, by the plan's paying total and by the
reason codes raised; the same person twice counts once, and the requester may never approve their own plan.

## Tier 3 — human only (the agent cannot produce the action; it stops and names the person)

| Situation | What the agent does | What only a person can do |
|---|---|---|
| Obligation it cannot fund before due (`ESCALATE`) | reports it with the budget request / shortfall | fund it, grant mandate, or let it slip |
| Payee NEW, CHANGED (bank details changed), SUSPENDED or unregistered | escalates; never pays the account, with or without an approval; the executor refuses even a plan that saw it differently | the call-back on the registered number (any other number, any other role is refused), suspend / reinstate |
| Intake from an unknown mailbox, Reply-To diversion, pressure language | quarantines / holds for review | verify or reject; register the mailbox |
| Incident with wrong payee details or a person/control stop (`ESCALATE` class), unknown failure shape, wait window exceeded | escalates with the typed reason | chase the rail, correct the payee, decide |
| Reconciliation that does not add up | opens a case that locks the book | close the case with an explanation |
| Grant mandate on a budget line, sign a bypass, close a period, set closing rates, change the cut-off, adjust a book | the agent asks or reports | finance manager / admin, audited |
| Viewer role | `BLOCKED` | a treasury agent or finance manager runs it |
| Production URL, missing credentials, state changed since review | refused | — |

## Where the line is drawn, and why

The guide's rule is the line: *if the agent should make the same choice every time, write the rule in code*. Everything
in tier 1 is a choice the rules make the same way every time; tier 2 is where a rule says "not alone"; tier 3 is where
no rule can supply what is missing — a person's knowledge (a call-back), a person's authority (mandate, a signature),
or a person's judgement (an unexplained difference).
