# ruff: noqa: E501
"""Write data/scenarios/demo_live.json: the Kit 01 book scaled for the live demo (USD 30,000 allocation, USD 10,000 floor).

Same obligations, payees, evidence, receivable and inbox as kit01_baseline; four changes, all stated in the scenario:
  * the payout fees the real sandbox charged in earlier cycles (SWIFT EUR 13.43, LOCAL GBP 2.25, LOCAL USD 3.00)
  * allocation USD 30,000 and reserve floor USD 10,000 (the room above the floor is USD 20,000)
  * SAAS-USD USD 5,000 instead of 1,200, so that after payroll, shipping and the EU supplier (about USD 15,529 with fees
    and conversion) it still does not fit and must wait for Acme's USD 10,000 receipt, as in the guide's story
  * the book's own November budget, scaled so every line has room for its obligation (SaaS 5,000 of 6,000 = 83 %, past
    the 80 % alert: the dashboard shows the alert, the agent still acts alone)
The day-1 / day-2 inbox (data/inbox/kit01_inbox.json) supplies the new information: Lumen's bank change and Acme's delay.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
base = json.loads((ROOT / "data" / "scenarios" / "kit01_baseline.json").read_text(encoding="utf-8"))
st = base["state"]
st["case_id"] = "demo-live"
st["book_id"] = "demo-live"
st["balances"] = {"USD": 3_000_000, "EUR": 0, "GBP": 0}
st["policy"]["reserve_floor_minor"] = 1_000_000
# The fees the real sandbox charged in earlier cycles (SANDBOX_RESULTS.md), so a demo on a clean store does not repeat the
# cold-start shortfall of cycle 1 (planning with the guide's fees, then a few cents short in EUR and GBP).
st["policy"]["payout_fees_minor"] = {"SWIFT:EUR": 1343, "LOCAL:GBP": 225, "LOCAL:USD": 300}
for o in st["obligations"]:
    if o["obligation_id"] == "SAAS-USD":
        o["amount_minor"] = 500_000
st["budget"] = {"period": "2026-11", "unbudgeted_requires_approval": True, "period_start": "2026-11-01", "period_end": "2026-11-30",
                "lines": [{"line": "payroll", "amount_minor": 1_200_000, "auto_share_bp": 10_000, "alert_share_bp": 9_000},
                          {"line": "logistics", "amount_minor": 500_000, "auto_share_bp": 10_000, "alert_share_bp": 8_000},
                          {"line": "suppliers", "amount_minor": 900_000, "auto_share_bp": 8_000, "alert_share_bp": 7_000},
                          {"line": "contractors", "amount_minor": 600_000, "auto_share_bp": 7_000, "alert_share_bp": 6_000, "require_approval_on_alert": True},
                          {"line": "saas", "amount_minor": 600_000, "auto_share_bp": 10_000, "alert_share_bp": 8_000}]}
out = {"title": "Live demo - Kit 01 at USD 30,000 with a USD 10,000 reserve floor",
       "story": ("The user's goal: pay what is due in the next 72 hours in USD, EUR and GBP without the wallet ever falling below "
                 "USD 10,000. Day 1: Acme promises USD 10,000 tomorrow and Lumen announces new bank details. Day 2: Acme's "
                 "payment slips five days. Then the deposit lands on Airwallex."),
       "goal": {"text": "Pay every obligation due in the next 72 hours on time, in USD, EUR and GBP, without the wallet ever falling below the USD 10,000 reserve floor; never pay an invoice twice; never pay a changed bank account without a person.",
                "allocation_minor": 3_000_000, "reserve_floor_minor": 1_000_000, "currency": "USD"},
       "inbox": "kit01_inbox.json",
       "state": st}
path = ROOT / "data" / "scenarios" / "demo_live.json"
path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
print(f"-> {path}")
