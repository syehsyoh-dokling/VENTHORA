# ruff: noqa: E501
"""Write data/groundtruth/budget.json: Budget · mandate cases with hand-derived answers.

Rules (src/venthora/proofcore/budget.py, policy.budget_reasons, policy/treasury.rego, Theorem line caps):
  cap            Theorem never proposes more than mandate − realized on a line (signed bypass: no cap)
  auto_share     used (realized + paying now) above mandate × auto_share -> budget_line_above_auto_share (a person)
  alert_share    used at/above mandate × alert_share -> ALERT; with require_approval_on_alert -> a person
  unbudgeted     a paid obligation outside every line -> unbudgeted_obligation (when the budget says so)
  bypass         unsigned bypass keeps the cap and needs a person; signed bypass lifts the cap, no reason
  request        what is still owed beyond the mandate -> BUDGET REQUEST with the exact shortfall
  grant          granted_extra raises the mandate; realized lowers what is left
Cash is ample in every case (USD 100,000, floor 5,000) so the reserve never decides; the budget does.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "groundtruth" / "budget.json"

POLICY = {"reserve_floor_minor": 500_000, "autonomous_conversion_limit_minor": 10_000_000, "swift_fee_currency": "EUR",
          "swift_fee_minor": 1285, "planning_horizon_hours": 72}
BEN = {"USD": "ben-saas-vendor", "EUR": "ben-eu-components", "GBP": "ben-uk-logistics"}


def ob(i, amount, line="suppliers", ccy="USD", priority=5, due=24, **kw):
    return {"obligation_id": f"O{i}", "amount_minor": amount, "currency": ccy, "priority": priority, "due_in_hours": due,
            "beneficiary_id": BEN[ccy], "payee_name": f"Payee {i}", "payout_method": "LOCAL", "evidence_ref": f"ev-O{i}",
            "invoice_fingerprint": f"fp-O{i}", "budget_line": line} | kw


def ev(i):
    return {"ref": f"ev-O{i}", "subject_id": f"O{i}", "source": "ap:invoice", "status": "verified", "summary": f"inv {i}"}


def ln(line, amount, **kw):
    return {"line": line, "amount_minor": amount} | kw


def state(obligations, lines, *, cash=10_000_000, unbudgeted_requires_approval=True, role="treasury_agent"):
    obs = list(obligations)
    return {"case_id": "budget", "book_id": "budget", "base_currency": "USD", "balances": {"USD": cash, "EUR": 0, "GBP": 0},
            "fx_to_base": {"EUR": "1.0850", "GBP": "1.2700"}, "obligations": obs, "receivables": [],
            "evidence": [ev(int(o["obligation_id"][1:])) for o in obs], "scenarios": [], "policy": POLICY, "actor_role": role,
            "budget": {"period": "2026-11", "lines": lines, "unbudgeted_requires_approval": unbudgeted_requires_approval}}


C = []


def add(id_, title, st, expect, derivation, pain="the agent acts within its mandate"):
    C.append({"id": id_, "kind": "plan", "level": 4, "pain": pain, "title": title, "input": st, "expect": expect, "derivation": derivation})


add("B01", "inside the line, inside the auto share", state([ob(1, 300_000)], [ln("suppliers", 1_000_000)]),
    {"decision": "APPROVED", "actions": {"O1": "PAY"}, "budget_status": {"suppliers": "auto"}, "requires": []},
    "3,000 of 10,000 = 30% < alert 80%, < auto 100% -> PAY autonomously")
add("B02", "exactly the mandate", state([ob(1, 1_000_000)], [ln("suppliers", 1_000_000)]),
    {"actions": {"O1": "PAY"}, "budget_available": {"suppliers": 0}, "requires": ["budget_line_alert_requires_approval"] if False else []},
    "10,000 = mandate -> cap allows it (<=); utilisation 100% >= alert 80% -> ALERT, but the line does not require approval on alert -> APPROVED with an alert")
add("B03", "one cent over the mandate: the agent stops at the line", state([ob(1, 1_000_001)], [ln("suppliers", 1_000_000)]),
    {"actions": {"O1": "ESCALATE"}, "paid": [], "budget_requests": {"suppliers": 1}},
    "cap = 10,000; 10,000.01 does not fit -> ESCALATE with a budget request for the shortfall 0.01")
add("B04", "above the auto share needs a person", state([ob(1, 850_000)], [ln("suppliers", 1_000_000, auto_share_bp=8000)]),
    {"decision": "REQUIRES_APPROVAL", "actions": {"O1": "PAY"}, "requires": ["budget_line_above_auto_share"]},
    "8,500 > 10,000 x 80% = 8,000 -> payable, but a person approves")
add("B05", "exactly the auto share is still autonomous", state([ob(1, 800_000)], [ln("suppliers", 1_000_000, auto_share_bp=8000, alert_share_bp=9000)]),
    {"decision": "APPROVED", "actions": {"O1": "PAY"}, "requires": []}, "8,000 = 80% of 10,000: not above -> autonomous; 80% < alert 90% -> no alert")
add("B06", "alert threshold reached (notification, still autonomous)", state([ob(1, 820_000)], [ln("suppliers", 1_000_000, auto_share_bp=10000, alert_share_bp=8000)]),
    {"decision": "APPROVED", "budget_alerts": 1, "budget_status": {"suppliers": "alert"}}, "82% >= alert 80% -> ALERT raised; auto share 100% -> no approval needed")
add("B07", "alert threshold with require_approval_on_alert", state([ob(1, 820_000)], [ln("suppliers", 1_000_000, auto_share_bp=10000, alert_share_bp=8000, require_approval_on_alert=True)]),
    {"decision": "REQUIRES_APPROVAL", "requires": ["budget_line_alert_requires_approval"], "budget_alerts": 1},
    "82% >= 80% and the line demands a person at the alert -> REQUIRES_APPROVAL")
add("B08", "realized spend eats the line", state([ob(1, 300_000)], [ln("suppliers", 1_000_000, realized_minor=750_000)]),
    {"actions": {"O1": "ESCALATE"}, "budget_requests": {"suppliers": 1}}, "cap = 10,000 - 7,500 = 2,500 < 3,000 -> ESCALATE + request for 500")
add("B09", "a grant restores the mandate", state([ob(1, 300_000)], [ln("suppliers", 1_000_000, realized_minor=750_000, granted_extra_minor=100_000)]),
    {"decision": "APPROVED", "actions": {"O1": "PAY"}, "budget_available": {"suppliers": 50_000}},
    "mandate 11,000 - realized 7,500 = 3,500 >= 3,000 -> PAY; available after = 500")
add("B10", "two lines, one exhausted, the other fine", state([ob(1, 300_000, "suppliers"), ob(2, 200_000, "saas", priority=3)],
                                                               [ln("suppliers", 250_000), ln("saas", 500_000)]),
    {"actions": {"O1": "ESCALATE", "O2": "PAY"}, "budget_requests": {"suppliers": 1}},
    "suppliers cap 2,500 < 3,000 -> ESCALATE + request 500; saas 2,000 of 5,000 -> PAY")
add("B11", "priority within a line: the line pays the urgent one", state([ob(1, 600_000, priority=10, due=12), ob(2, 600_000, priority=5, due=48)], [ln("suppliers", 1_000_000)]),
    {"actions": {"O1": "PAY", "O2": "ESCALATE"}, "budget_requests": {"suppliers": 1}},
    "both fit the cash, only one fits the line (6,000 + 6,000 > 10,000): priority 10 paid, the other raises a request for 2,000")
add("B12", "unbudgeted obligation needs a person", state([ob(1, 100_000, line="marketing")], [ln("suppliers", 1_000_000)]),
    {"decision": "REQUIRES_APPROVAL", "actions": {"O1": "PAY"}, "requires": ["unbudgeted_obligation"]},
    "'marketing' is no line of 2026-11 -> payable by cash, but a person must place it")
add("B13", "unbudgeted allowed by the budget's setting", state([ob(1, 100_000, line="marketing")], [ln("suppliers", 1_000_000)], unbudgeted_requires_approval=False),
    {"decision": "APPROVED", "actions": {"O1": "PAY"}, "requires": []}, "the budget says unbudgeted obligations do not need a person")
add("B14", "bypass nobody signed keeps the cap", state([ob(1, 300_000)], [ln("suppliers", 200_000, bypass=True)]),
    {"actions": {"O1": "ESCALATE"}, "budget_requests": {"suppliers": 1}}, "unsigned bypass -> cap 2,000 applies -> ESCALATE + request")
add("B15", "signed bypass lifts the cap", state([ob(1, 300_000)], [ln("suppliers", 200_000, bypass=True, bypass_approved_by="Dana (CFO)")]),
    {"decision": "APPROVED", "actions": {"O1": "PAY"}, "budget_status": {"suppliers": "bypass"}, "requires": []},
    "signed bypass -> no cap, no approval reason; the line shows as bypass")
add("B16", "bypass signed but spending inside it is still recorded", state([ob(1, 300_000)], [ln("suppliers", 200_000, bypass=True, bypass_approved_by="Dana (CFO)", realized_minor=900_000)]),
    {"decision": "APPROVED", "actions": {"O1": "PAY"}}, "realized 9,000 on a 2,000 line with a signed bypass: still payable; the pipeline shows it, nobody stops it")
add("B17", "the whole November book against the November plan", state(
    [ob(1, 800_000, "payroll", priority=10, due=24), ob(2, 300_000, "logistics", priority=10, due=24), ob(3, 400_000, "suppliers", priority=9, due=48),
     ob(4, 300_000, "contractors", priority=6, due=72), ob(5, 120_000, "saas", priority=2, due=72)],
    [ln("payroll", 900_000, alert_share_bp=9000), ln("logistics", 400_000), ln("suppliers", 700_000, auto_share_bp=8000, alert_share_bp=7000),
     ln("contractors", 600_000, auto_share_bp=7000, alert_share_bp=6000, require_approval_on_alert=True), ln("saas", 150_000)]),
    {"decision": "APPROVED", "paid": ["O1", "O2", "O3", "O4", "O5"], "budget_alerts": 1, "requires": []},
    "every line fits: payroll 89% (< alert 90%), logistics 75%, suppliers 57%, contractors 50% (< 60% alert), saas 80% = alert -> one alert, no approval")
add("B18", "minus from what is still owed", state([ob(1, 700_000, priority=10), ob(2, 500_000, priority=5)], [ln("suppliers", 1_000_000)]),
    {"actions": {"O1": "PAY", "O2": "ESCALATE"}, "budget_minus": {"suppliers": 200_000}, "budget_requests": {"suppliers": 1}},
    "7,000 paid; 5,000 still owed; 12,000 - 10,000 = minus 2,000 -> request for 2,000")
add("B19", "viewer with a budget is still blocked", state([ob(1, 300_000)], [ln("suppliers", 1_000_000)], role="viewer"),
    {"decision": "BLOCKED"}, "actor_role viewer -> BLOCKED regardless of budget")
add("B20", "no budget attached: nothing changes", {k: v for k, v in state([ob(1, 300_000)], [ln("suppliers", 1)]).items() if k != "budget"},
    {"decision": "APPROVED", "actions": {"O1": "PAY"}, "budget_attached": False}, "without a budget the plan is the old plan")

OUT.write_text(json.dumps({"_note": "Budget · mandate ground truth, hand-derived; see scripts/make_budget_groundtruth.py.", "cases": C}, indent=1), encoding="utf-8")
print(f"{len(C)} budget cases -> {OUT}")
