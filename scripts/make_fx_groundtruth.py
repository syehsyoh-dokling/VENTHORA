# ruff: noqa: E501
"""Write data/groundtruth/fx.json: FX exposure limits with hand-derived answers (src/venthora/proofcore/fx_exposure.py).

Rules: the cash a plan leaves in a foreign currency (held + bought − paid) may not exceed max_open_position[ccy]; the base sold
in conversions over the last 24 h (journal) plus this plan's conversions may not exceed max_conversion_daily; foreign cash held
plus bought must cover at least min_hedge_ratio of the foreign obligations due inside the planning horizon (a paid obligation is
covered by construction; a deferred or escalated one is the exposure). Each breach is a reason the agent may not act alone.
EUR/USD 1.0850, GBP/USD 1.2700; SWIFT fee EUR 12.85 per EUR payout; cash is ample unless the case says otherwise.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "groundtruth" / "fx.json"

POLICY = {"reserve_floor_minor": 500_000, "autonomous_conversion_limit_minor": 100_000_000, "swift_fee_currency": "EUR",
          "swift_fee_minor": 1285, "planning_horizon_hours": 72}
HIST = {"as_of": "2026-10-23T12:00:00+00:00", "payees_paid_before": ["ben-eu-components", "ben-uk-logistics", "ben-saas-vendor"]}


def ob(i, amount, ccy="EUR", due=24, ben="ben-eu-components", method="SWIFT", priority=5):
    return {"obligation_id": f"O{i}", "amount_minor": amount, "currency": ccy, "priority": priority, "due_in_hours": due,
            "beneficiary_id": ben, "payee_name": f"Payee {i}", "payout_method": method, "evidence_ref": f"ev-O{i}",
            "invoice_fingerprint": f"fp-O{i}"}


def ev(i):
    return {"ref": f"ev-O{i}", "subject_id": f"O{i}", "source": "ap:invoice", "status": "verified", "summary": f"inv {i}"}


def state(obligations, *, policy=None, balances=None, history=None, role="treasury_agent"):
    obs = list(obligations)
    s = {"case_id": "fx", "book_id": "fx", "base_currency": "USD", "balances": {"USD": 10_000_000, "EUR": 0, "GBP": 0} | (balances or {}),
         "fx_to_base": {"EUR": "1.0850", "GBP": "1.2700"}, "obligations": obs, "receivables": [],
         "evidence": [ev(int(o["obligation_id"][1:])) for o in obs], "scenarios": [], "policy": POLICY | (policy or {}), "actor_role": role}
    if history is not None:
        s["history"] = HIST | history
    return s


C = []


def add(id_, title, st, expect, derivation, pain="how much foreign currency may stay open, be sold, or stay unhedged"):
    C.append({"id": id_, "kind": "plan", "level": 4, "pain": pain, "title": title, "input": st, "expect": expect, "derivation": derivation})


# One EUR 4,000 SWIFT payout needs EUR 4,012.85; bought exactly; USD sold 4,353.94.
add("X01", "an exact conversion leaves no open position", state([ob(1, 400_000)], policy={"max_open_position_minor": {"EUR": 100_000}}),
    {"decision": "APPROVED", "actions": {"O1": "CONVERT_AND_PAY"}, "requires": [], "conversions": {"EUR": 401_285}}, "EUR after = 0 + 4,012.85 − 4,012.85 = 0 <= cap 1,000")
add("X02", "the wallet already holds more EUR than the cap: a person decides", state([ob(1, 400_000)], balances={"EUR": 900_000}, policy={"max_open_position_minor": {"EUR": 100_000}}),
    {"decision": "REQUIRES_APPROVAL", "actions": {"O1": "CONVERT_AND_PAY"}, "requires": ["open_position_above_cap"], "n_conversions": 0},
    "held 9,000 − paid 4,012.85 = 4,987.15 left > cap 1,000 -> the plan leaves the book over its cap (a foreign payout keeps the "
    "CONVERT_AND_PAY label even when the held EUR covers it: no conversion is booked)")
add("X03", "open position exactly at the cap", state([ob(1, 400_000)], balances={"EUR": 501_285}, policy={"max_open_position_minor": {"EUR": 100_000}}),
    {"decision": "APPROVED", "requires": []}, "5,012.85 − 4,012.85 = 1,000.00, not above 1,000")
add("X04", "one cent above the cap", state([ob(1, 400_000)], balances={"EUR": 501_286}, policy={"max_open_position_minor": {"EUR": 100_000}}),
    {"decision": "REQUIRES_APPROVAL", "requires": ["open_position_above_cap"]}, "1,000.01 > 1,000")
add("X05", "the cap is per currency: GBP over, EUR fine", state([ob(1, 400_000)], balances={"GBP": 300_000}, policy={"max_open_position_minor": {"EUR": 100_000, "GBP": 200_000}}),
    {"decision": "REQUIRES_APPROVAL", "requires": ["open_position_above_cap"]}, "GBP 3,000 untouched by the plan > GBP cap 2,000; EUR 0")
add("X06", "no cap for a currency: nothing applies", state([ob(1, 400_000)], balances={"GBP": 300_000}, policy={"max_open_position_minor": {"EUR": 100_000}}),
    {"decision": "APPROVED", "requires": []}, "only EUR is capped")
add("X07", "daily conversion cap: 24 h sales + now above it", state([ob(1, 400_000)], policy={"max_conversion_daily_minor": 1_000_000}, history={"converted_24h_minor": 600_000}),
    {"decision": "REQUIRES_APPROVAL", "requires": ["daily_conversion_cap_exceeded"]}, "sold 6,000 + now 4,353.95 (EUR 4,012.85 at 1.0850, rounded up) = 10,353.95 > 10,000")
add("X08", "daily conversion cap exactly reached", state([ob(1, 400_000)], policy={"max_conversion_daily_minor": 1_000_000}, history={"converted_24h_minor": 564_605}),
    {"decision": "APPROVED", "requires": []}, "5,646.05 + 4,353.95 = 10,000.00, not above")
add("X09", "no history: only this plan's conversions count", state([ob(1, 400_000)], policy={"max_conversion_daily_minor": 400_000}),
    {"decision": "REQUIRES_APPROVAL", "requires": ["daily_conversion_cap_exceeded"]}, "0 + 4,353.95 > 4,000")
add("X10", "a plan without conversions does not touch the daily cap", state([ob(1, 400_000, ccy="USD", ben="ben-saas-vendor", method="LOCAL")], policy={"max_conversion_daily_minor": 1}, history={"converted_24h_minor": 0}),
    {"decision": "APPROVED", "requires": []}, "USD payout, nothing sold")
add("X11", "hedge ratio: a paid foreign obligation is fully covered", state([ob(1, 400_000)], policy={"min_hedge_ratio_bp": 8000}),
    {"decision": "APPROVED", "requires": []}, "bought 4,012.85 / due 4,000 -> 100% (capped) >= 80%")
add("X12", "hedge ratio: the second EUR obligation cannot be funded and stays unhedged",
    state([ob(1, 400_000, priority=9), ob(2, 400_000, priority=2)], balances={"USD": 1_000_000}, policy={"min_hedge_ratio_bp": 8000}),
    {"decision": "REQUIRES_APPROVAL", "actions": {"O1": "CONVERT_AND_PAY", "O2": "ESCALATE"}, "requires": ["hedge_ratio_below_minimum"]},
    "USD 10,000 − floor 5,000 funds one 4,353.95 conversion; covered 4,012.85 / due 8,000 = 50.16% < 80%")
add("X13", "hedge ratio exactly at the minimum", state([ob(1, 400_000, priority=9), ob(2, 400_000, priority=2)], balances={"USD": 1_000_000}, policy={"min_hedge_ratio_bp": 5016}),
    {"decision": "APPROVED", "requires": []}, "401,285 × 10,000 // 800,000 = 5,016 bp, not below 5,016 -> autonomous")
add("X14", "hedge ratio one bp below the minimum", state([ob(1, 400_000, priority=9), ob(2, 400_000, priority=2)], balances={"USD": 1_000_000}, policy={"min_hedge_ratio_bp": 5017}),
    {"decision": "REQUIRES_APPROVAL", "requires": ["hedge_ratio_below_minimum"]}, "5,016 < 5,017 -> a person")
add("X15", "foreign cash already held counts as hedge", state([ob(1, 400_000, priority=9), ob(2, 400_000, priority=2)], balances={"USD": 1_000_000, "EUR": 400_000}, policy={"min_hedge_ratio_bp": 8000}),
    {"decision": "APPROVED", "actions": {"O1": "CONVERT_AND_PAY", "O2": "CONVERT_AND_PAY"}, "requires": []},
    "held EUR 4,000 + one conversion of EUR 4,025.70 (USD 4,367.89, inside the 5,000 above floor) pays both: covered 8,025.70 / due 8,000 -> 100%")
add("X16", "an obligation beyond the horizon is not an exposure yet", state([ob(1, 400_000, priority=9), ob(2, 400_000, due=200, priority=2)], balances={"USD": 1_000_000}, policy={"min_hedge_ratio_bp": 8000}),
    {"decision": "APPROVED", "requires": []}, "O2 due in 200 h > 72 h horizon -> due in horizon 4,000, covered 4,012.85 -> 100%")
add("X17", "nothing due in a currency: nothing to hedge", state([ob(1, 400_000, ccy="USD", ben="ben-saas-vendor", method="LOCAL")], policy={"min_hedge_ratio_bp": 8000}),
    {"decision": "APPROVED", "requires": []}, "no foreign obligation -> coverage undefined -> no reason")
add("X18", "three reasons at once, each named", state([ob(1, 400_000, priority=9), ob(2, 400_000, priority=2)], balances={"USD": 1_000_000, "GBP": 300_000},
    policy={"min_hedge_ratio_bp": 8000, "max_conversion_daily_minor": 400_000, "max_open_position_minor": {"GBP": 100_000}}),
    {"decision": "REQUIRES_APPROVAL", "requires": ["daily_conversion_cap_exceeded", "hedge_ratio_below_minimum", "open_position_above_cap"]},
    "GBP 3,000 > 1,000; sold 4,353.95 > 4,000; EUR 50.16% < 80%")
add("X19", "no FX limits: nothing changes", state([ob(1, 400_000, priority=9), ob(2, 400_000, priority=2)], balances={"USD": 1_000_000, "GBP": 300_000}),
    {"decision": "APPROVED", "actions": {"O1": "CONVERT_AND_PAY", "O2": "ESCALATE"}, "requires": []}, "the control is off")
dup = ob(2, 400_000) | {"invoice_fingerprint": "fp-O1"}
add("X21", "a rejected duplicate is not an exposure to hedge", state([ob(1, 400_000), dup], policy={"min_hedge_ratio_bp": 8000}),
    {"decision": "APPROVED", "actions": {"O1": "CONVERT_AND_PAY", "O2": "REJECT"}, "requires": []},
    "O2 repeats O1's invoice fingerprint -> REJECT; only O1's EUR 4,000 is due; bought 4,012.85 -> 100% (before the fix the duplicate counted: 50% < 80%)")
add("X20", "a viewer: blocked before any FX rule", state([ob(1, 400_000)], policy={"max_conversion_daily_minor": 1}, role="viewer"), {"decision": "BLOCKED"}, "role first")

OUT.write_text(json.dumps({"_note": "FX-exposure ground truth, hand-derived; see scripts/make_fx_groundtruth.py.", "cases": C}, indent=1), encoding="utf-8")
print(f"{len(C)} fx cases -> {OUT}")
