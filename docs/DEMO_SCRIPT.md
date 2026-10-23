# Demo video script (target 4:30, limit 5:00)

The organizers ask the demo to show: goal → initial plan → new information → revised decision → resulting balance/payment/status, ending in a financial action or an escalation to a person.

| Time | Show | Say |
|---|---|---|
| 0:00–0:35 | Problem slide: USD 20k, reserve 5k, five obligations in USD/EUR/GBP within 72h | "Cash is limited, obligations are cross-currency, and a receipt we count on might slip." |
| 0:35–1:30 | Run `kit01_baseline`; walk through stages | "Verity rejects the re-sent duplicate invoice. Theorem pays payroll and the EUR supplier, which costs USD 4,353.95 including the EUR 12.85 SWIFT fee. It defers the UK vendor because the Acme receipt lands first." |
| 1:30–2:30 | Customer email: payment slips five days → run `kit01_receipt_delayed` | "Confidence drops to 40%. The conversion limit tightens. The vendor can't be safely deferred, so the agent escalates to a person instead of guessing." |
| 2:30–3:30 | ProofCore refusing a bad plan (remote engine tries to pay the duplicate) → `BLOCKED` | "Even if every AI recommends paying, ProofCore can refuse." |
| 3:30–4:30 | Airwallex sandbox: approve → quote → conversion → transfer → simulate → re-read balances; audit chain verified | "We confirm the result in Airwallex, not just from the first API response." |

Rehearsal checklist: no real credentials on screen; sandbox only; record from a clean `data/` folder so the audit chain starts at GENESIS.
