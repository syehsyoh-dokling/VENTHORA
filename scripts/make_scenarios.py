"""Derive the 'receipt delayed' scenario from the baseline so the two never drift.

Run: python scripts/make_scenarios.py
"""
import copy
import json
from pathlib import Path

DIR = Path(__file__).resolve().parents[1] / "data" / "scenarios"

base = json.loads((DIR / "kit01_baseline.json").read_text(encoding="utf-8"))
delayed = copy.deepcopy(base)
delayed["title"] = "Kit 01 - New information: customer receipt delayed five days"
delayed["story"] = ("Same company, same obligations. A new email from Acme says the USD 10,000 payment slips "
                    "five days. Forecast confidence drops, the autonomous conversion limit tightens, and the "
                    "plan must be revised.")
state = delayed["state"]
state["case_id"] = "kit01-receipt-delayed"
rcv = state["receivables"][0]
rcv["expected_in_hours"] = 120
rcv["confidence_bp"] = 4000
rcv["evidence_ref"] = "doc-acme-delay-email"
state["evidence"].append({"ref": "doc-acme-delay-email", "subject_id": "RCV-ACME", "source": "email:customer",
                          "status": "verified", "summary": "Payment moved to next week (5 days)"})

(DIR / "kit01_receipt_delayed.json").write_text(json.dumps(delayed, indent=2) + "\n", encoding="utf-8")
print("wrote kit01_receipt_delayed.json")
