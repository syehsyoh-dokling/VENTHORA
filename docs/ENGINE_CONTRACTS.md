# External engine contracts

Set `VENTHORA_VERITY_URL`, `VENTHORA_HORIZON_URL` or `VENTHORA_THEOREM_URL` to run an external engine behind the orchestrator. Every call is a JSON `POST`. All money values are integer minor units. A timeout, non-2xx response or malformed body fails closed (`BLOCKED`).

| Engine | Request | Response | How it is applied |
|---|---|---|---|
| Verity | `{"contract": "venthora.verity.v1", "state": TreasuryState}` | `{"eligible_ids": [str]}` | Intersected with local eligibility |
| Horizon | `{"contract": "venthora.horizon.v1", "state": ..., "local_stress_minor": int}` | `{"stress_minor": int}` | `max(local, remote)` |
| Theorem | `{"contract": "venthora.theorem.v1", "state": ..., "eligible_ids": [str], "budget_minor": int}` | `{"selected_ids": [str]}` | Must be unique and a subset of eligible; ProofCore re-verifies |

Keep these endpoints on a private network. They receive full treasury state.
