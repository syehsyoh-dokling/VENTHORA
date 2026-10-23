# ruff: noqa: E501
"""Write data/groundtruth/limits.json: limits and velocity with hand-derived answers (src/venthora/proofcore/limits.py).

Rules (base-currency cost): a payout above max_single_payment; a payee whose paid-this-period + paying-now exceeds
max_per_payee_period; a payee never PAID before getting more than max_first_payment; rolling 24 h / 7 d outflow + paying
now above the daily / weekly limit — each is a reason the agent may not act alone (REQUIRES_APPROVAL). Null = no limit.
Cash is ample (USD 100,000, floor 5,000) so only the limit under test decides.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "groundtruth" / "limits.json"

POLICY = {"reserve_floor_minor": 500_000, "autonomous_conversion_limit_minor": 10_000_000, "swift_fee_currency": "EUR",
          "swift_fee_minor": 1285, "planning_horizon_hours": 72}


def ob(i, amount, ben="ben-saas-vendor", **kw):
    return {"obligation_id": f"O{i}", "amount_minor": amount, "currency": "USD", "priority": 5, "due_in_hours": 24,
            "beneficiary_id": ben, "payee_name": f"Payee {i}", "payout_method": "LOCAL", "evidence_ref": f"ev-O{i}",
            "invoice_fingerprint": f"fp-O{i}"} | kw


def ev(i):
    return {"ref": f"ev-O{i}", "subject_id": f"O{i}", "source": "ap:invoice", "status": "verified", "summary": f"inv {i}"}


def state(obligations, *, policy=None, history=None):
    obs = list(obligations)
    s = {"case_id": "limits", "book_id": "limits", "base_currency": "USD", "balances": {"USD": 10_000_000, "EUR": 0, "GBP": 0},
         "fx_to_base": {"EUR": "1.0850", "GBP": "1.2700"}, "obligations": obs, "receivables": [],
         "evidence": [ev(int(o["obligation_id"][1:])) for o in obs], "scenarios": [], "policy": POLICY | (policy or {}),
         "actor_role": "treasury_agent"}
    if history is not None:
        s["history"] = history
    return s


KNOWN = {"as_of": "2026-10-23T12:00:00+00:00", "paid_per_payee_period_minor": {}, "outflow_24h_minor": 0, "outflow_7d_minor": 0,
         "payees_paid_before": ["ben-saas-vendor"]}
C = []


def add(id_, title, st, expect, derivation, pain="how much, to whom, how often"):
    C.append({"id": id_, "kind": "plan", "level": 4, "pain": pain, "title": title, "input": st, "expect": expect, "derivation": derivation})


add("V01", "single payout inside the limit", state([ob(1, 400_000)], policy={"max_single_payment_minor": 500_000}, history=KNOWN),
    {"decision": "APPROVED", "actions": {"O1": "PAY"}, "requires": []}, "4,000 <= 5,000 -> autonomous")
add("V02", "single payout exactly at the limit", state([ob(1, 500_000)], policy={"max_single_payment_minor": 500_000}, history=KNOWN),
    {"decision": "APPROVED", "requires": []}, "5,000 is not above 5,000 -> autonomous")
add("V03", "single payout one cent above the limit", state([ob(1, 500_001)], policy={"max_single_payment_minor": 500_000}, history=KNOWN),
    {"decision": "REQUIRES_APPROVAL", "actions": {"O1": "PAY"}, "requires": ["single_payment_above_limit"]}, "5,000.01 > 5,000 -> a person")
add("V04", "two payouts each inside the single limit", state([ob(1, 400_000), ob(2, 400_000, ben="ben-uk-logistics")], policy={"max_single_payment_minor": 500_000}, history=KNOWN | {"payees_paid_before": ["ben-saas-vendor", "ben-uk-logistics"]}),
    {"decision": "APPROVED", "requires": []}, "the single limit is per payout, not per plan")
add("V05", "payee period limit: history + now exceeds it", state([ob(1, 300_000)], policy={"max_per_payee_period_minor": 600_000}, history=KNOWN | {"paid_per_payee_period_minor": {"ben-saas-vendor": 400_000}}),
    {"decision": "REQUIRES_APPROVAL", "requires": ["payee_period_limit_exceeded"]}, "paid 4,000 + now 3,000 = 7,000 > 6,000 -> a person")
add("V06", "payee period limit: history + now exactly at it", state([ob(1, 200_000)], policy={"max_per_payee_period_minor": 600_000}, history=KNOWN | {"paid_per_payee_period_minor": {"ben-saas-vendor": 400_000}}),
    {"decision": "APPROVED", "requires": []}, "4,000 + 2,000 = 6,000, not above -> autonomous")
add("V07", "payee period limit: two payouts to the same payee in one plan", state([ob(1, 350_000), ob(2, 350_000)], policy={"max_per_payee_period_minor": 600_000}, history=KNOWN),
    {"decision": "REQUIRES_APPROVAL", "requires": ["payee_period_limit_exceeded"]}, "3,500 + 3,500 = 7,000 to one payee > 6,000 even with no history")
add("V08", "a different payee is not affected by another's history", state([ob(1, 300_000, ben="ben-uk-logistics")], policy={"max_per_payee_period_minor": 600_000}, history=KNOWN | {"paid_per_payee_period_minor": {"ben-saas-vendor": 599_999}, "payees_paid_before": ["ben-saas-vendor", "ben-uk-logistics"]}),
    {"decision": "APPROVED", "requires": []}, "limits are per payee")
add("V09", "first payment to a new payee above the first-payment limit", state([ob(1, 300_000, ben="ben-uk-logistics")], policy={"max_first_payment_minor": 100_000}, history=KNOWN),
    {"decision": "REQUIRES_APPROVAL", "requires": ["first_payment_to_new_payee_above_limit"]}, "ben-uk-logistics never PAID before; 3,000 > 1,000 -> a person")
add("V10", "first payment to a new payee inside the first-payment limit", state([ob(1, 100_000, ben="ben-uk-logistics")], policy={"max_first_payment_minor": 100_000}, history=KNOWN),
    {"decision": "APPROVED", "requires": []}, "1,000 is not above 1,000 -> autonomous")
add("V11", "a known payee is not subject to the first-payment limit", state([ob(1, 300_000)], policy={"max_first_payment_minor": 100_000}, history=KNOWN),
    {"decision": "APPROVED", "requires": []}, "ben-saas-vendor was PAID before -> no first-payment cap")
add("V12", "no history at all: every payee is new", state([ob(1, 300_000)], policy={"max_first_payment_minor": 100_000}),
    {"decision": "REQUIRES_APPROVAL", "requires": ["first_payment_to_new_payee_above_limit"]}, "without history the books remember nobody -> first payment rule applies")
add("V13", "daily outflow: rolling 24 h + now above the limit", state([ob(1, 300_000)], policy={"max_outflow_daily_minor": 1_000_000}, history=KNOWN | {"outflow_24h_minor": 800_000}),
    {"decision": "REQUIRES_APPROVAL", "requires": ["daily_outflow_limit_exceeded"]}, "8,000 + 3,000 = 11,000 > 10,000")
add("V14", "daily outflow exactly at the limit", state([ob(1, 200_000)], policy={"max_outflow_daily_minor": 1_000_000}, history=KNOWN | {"outflow_24h_minor": 800_000}),
    {"decision": "APPROVED", "requires": []}, "8,000 + 2,000 = 10,000, not above")
add("V15", "weekly outflow above the limit while daily is fine", state([ob(1, 300_000)], policy={"max_outflow_daily_minor": 1_000_000, "max_outflow_weekly_minor": 2_000_000}, history=KNOWN | {"outflow_24h_minor": 100_000, "outflow_7d_minor": 1_900_000}),
    {"decision": "REQUIRES_APPROVAL", "requires": ["weekly_outflow_limit_exceeded"]}, "24h 1,000 + 3,000 = 4,000 <= 10,000; 7d 19,000 + 3,000 = 22,000 > 20,000")
add("V16", "several limits at once, every reason named", state([ob(1, 600_000, ben="ben-uk-logistics")], policy={"max_single_payment_minor": 500_000, "max_first_payment_minor": 100_000, "max_outflow_daily_minor": 1_000_000}, history=KNOWN | {"outflow_24h_minor": 900_000}),
    {"decision": "REQUIRES_APPROVAL", "requires": ["daily_outflow_limit_exceeded", "first_payment_to_new_payee_above_limit", "single_payment_above_limit"]},
    "6,000 > 5,000 single; new payee 6,000 > 1,000; 9,000 + 6,000 > 10,000 daily -> three reasons")
add("V17", "no limits set: nothing changes", state([ob(1, 5_000_000)], history=KNOWN), {"decision": "APPROVED", "requires": []}, "every limit null -> no reason")
add("V18", "an escalated payout does not count as paying now", state([ob(1, 600_000, policy_flags=["payee_bank_details_changed"])], policy={"max_single_payment_minor": 500_000}, history=KNOWN),
    {"actions": {"O1": "ESCALATE"}, "requires": []}, "the bank-change flag escalates it; nothing is paid now, so no limit reason")

OUT.write_text(json.dumps({"_note": "Limits & velocity ground truth, hand-derived; see scripts/make_limits_groundtruth.py.", "cases": C}, indent=1), encoding="utf-8")
print(f"{len(C)} limit cases -> {OUT}")
