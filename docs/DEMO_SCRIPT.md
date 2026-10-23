# Demo video script (target 4:30, limit 5:00)

The organizers ask that the demo show the goal, the initial plan, the new information, the revised
decision and the resulting balance, payment or status, and end with a financial action or an
escalation to a person. The whole video is a single run:

```bash
python scripts/local_llm_server.py --port 8001            # local LLM (separate terminal)
venthora-sandbox --receipt --approve "<your name>"        # real Airwallex sandbox
```

| Time | Screen (CLI step) | Say |
|---|---|---|
| 0:00–0:30 | Problem slide | "USD 22,500, a USD 5,000 reserve, five obligations in three currencies due within 72 hours. Cash can't cover them all." |
| 0:30–1:20 | Steps 1–3: live balances (ring-fenced), inbox day 1, initial plan | "Shipping would halt operations, so it's paid. The EU supplier is time-sensitive, so it converts only the minimum. The cheapest bill is deferred. The contractor changed bank details by email: that's a policy exception, escalated with the cited sentence. Approved autonomously at 90% confidence." |
| 1:20–1:50 | Step 3b: AI challenger | "A naive assistant says pay everything. ProofCore blocks it: duplicate invoice, changed-bank payee, negative wallet, reserve breach." |
| 1:50–2:40 | Step 4: delay email, revised plan | "Acme says five days late. Their track record puts confidence at 70%, so the limit halves and the same conversion now needs me. Only the SaaS decision changes." |
| 2:40–3:10 | Step 5: deposit | "The deposit is confirmed SETTLED by Airwallex, and balances are re-read. Only the SaaS bill is recalculated: now paid." |
| 3:10–4:20 | Step 7: execution | "Schema, validate, create the beneficiaries. Fresh quotes, conversions, transfers SENT then PAID. Reconciled to the cent; the reserve is confirmed, and the real fees are learned for next time." |
| 4:20–4:30 | Close | "AI proposes. Independent engines verify. Airwallex executes. The ledger reconciles." |

Rehearsal checklist:

- No credentials on screen; sandbox only.
- Use a fresh book or invoice set so nothing is already settled. The execution DB remembers paid invoices and will (correctly) refuse to pay them again.
- The local model runs on the GPU (`MODEL_ENDPOINT=http://127.0.0.1:8001`).
