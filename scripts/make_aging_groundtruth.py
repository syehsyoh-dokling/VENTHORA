# ruff: noqa: E501
"""Write data/groundtruth/aging.json: invoice aging and dunning with hand-derived answers (src/venthora/aging.py).

Three kinds of case. `plan`: a book dated as_of with due dates in the past and the thresholds in the policy, judged by the gate.
`aging`: the pure aging table (buckets, days, interest, reasons) from plain obligations and actions. `intake`: reminder emails
from registered payees (level 1 / 2 / 3), and a final notice for an invoice already in the book (a duplicate that raises the
dunning level, never a second payable). Interest: base × rate × days / 365, rounded half up; the policy default is 8 % a year.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "groundtruth" / "aging.json"
AS_OF = "2026-10-24"
POLICY = {"reserve_floor_minor": 500_000, "autonomous_conversion_limit_minor": 100_000_000, "swift_fee_currency": "EUR",
          "swift_fee_minor": 1285, "planning_horizon_hours": 72}


def ob(i, amount, due, ccy="USD", ben="ben-saas-vendor", priority=5, **kw):
    return {"obligation_id": f"O{i}", "amount_minor": amount, "currency": ccy, "priority": priority, "due_in_hours": 24,
            "beneficiary_id": ben, "payee_name": f"Payee {i}", "payout_method": "LOCAL" if ccy == "USD" else "SWIFT",
            "evidence_ref": f"ev-O{i}", "invoice_fingerprint": f"fp-O{i}", "due_date": due} | kw


def ev(i):
    return {"ref": f"ev-O{i}", "subject_id": f"O{i}", "source": "ap:invoice", "status": "verified", "summary": f"inv {i}"}


def state(obligations, *, policy=None, usd=10_000_000, role="treasury_agent", as_of=AS_OF):
    obs = list(obligations)
    return {"case_id": "aging", "book_id": "aging", "base_currency": "USD", "balances": {"USD": usd, "EUR": 0, "GBP": 0},
            "fx_to_base": {"EUR": "1.0850", "GBP": "1.2700"}, "obligations": obs, "receivables": [],
            "evidence": [ev(int(o["obligation_id"][1:])) for o in obs], "scenarios": [], "policy": POLICY | (policy or {}),
            "actor_role": role, "as_of": as_of}


C = []


def add(id_, kind, title, inp, expect, derivation, pain="what we owe, how late, what it costs", **extra):
    C.append({"id": id_, "kind": kind, "level": 3, "pain": pain, "title": title, "input": inp, "expect": expect, "derivation": derivation} | extra)


# -- plans --------------------------------------------------------------------------------------------------------------------
# USD 10,000 overdue 45 days at 8 %: 1,000,000 × 800 × 45 / 3,650,000 = 9,863.01 -> 9,863 minor (USD 98.63)
add("G01", "plan", "an overdue invoice the plan pays: accrued interest reported, no reason", state([ob(1, 1_000_000, "2026-09-09")], policy={"overdue_escalation_days": 30}),
    {"decision": "APPROVED", "actions": {"O1": "PAY"}, "requires": [], "aging_days": {"O1": 45}, "aging_buckets": {"O1": "31-60"}, "aging_accrued": {"O1": 9863}, "aging_exposure": 0},
    "24 Oct − 9 Sep = 45 days -> bucket 31-60; accrued 10,000 × 8 % × 45/365 = 98.63; paid now -> exposure 0")
add("G02", "plan", "an overdue invoice the plan cannot fund: escalated, and overdue beyond 30 days -> a person", state([ob(1, 1_000_000, "2026-09-09")], usd=1_200_000, policy={"overdue_escalation_days": 30}),
    {"decision": "REQUIRES_APPROVAL", "actions": {"O1": "ESCALATE"}, "requires": ["overdue_obligation_unpaid"], "aging_exposure": 9863 + 658},
    "USD 12,000 − floor 5,000 < 10,000 -> ESCALATE; 45 > 30 days -> overdue_obligation_unpaid; exposure = accrued 98.63 + 3 projected days 6.58")
add("G03", "plan", "overdue but inside the escalation days: no reason", state([ob(1, 1_000_000, "2026-10-10")], usd=1_200_000, policy={"overdue_escalation_days": 30}),
    {"decision": "APPROVED", "actions": {"O1": "ESCALATE"}, "requires": [], "aging_days": {"O1": 14}, "aging_buckets": {"O1": "1-30"}}, "14 days <= 30 -> escalated to a person as unfundable, but not by this rule")
add("G04", "plan", "exactly at the escalation days", state([ob(1, 1_000_000, "2026-09-24")], usd=1_200_000, policy={"overdue_escalation_days": 30}),
    {"decision": "APPROVED", "requires": [], "aging_days": {"O1": 30}}, "30 is not above 30")
add("G05", "plan", "a final notice left unpaid needs a person, whatever the age", state([ob(1, 1_000_000, "2026-10-27", dunning_level=3)], usd=1_200_000, policy={"dunning_escalation_level": 3}),
    {"decision": "REQUIRES_APPROVAL", "actions": {"O1": "ESCALATE"}, "requires": ["final_notice_unpaid"], "aging_days": {"O1": 0}, "aging_dunning": {"O1": 3}},
    "not even due yet, but the payee sent a final notice (L3) and the plan leaves it unpaid")
add("G06", "plan", "a second reminder is below the dunning threshold", state([ob(1, 1_000_000, "2026-10-27", dunning_level=2)], usd=1_200_000, policy={"dunning_escalation_level": 3}),
    {"decision": "APPROVED", "requires": []}, "L2 < 3")
add("G07", "plan", "a final notice the plan pays is not a reason", state([ob(1, 1_000_000, "2026-10-27", dunning_level=3)], policy={"dunning_escalation_level": 3}),
    {"decision": "APPROVED", "actions": {"O1": "PAY"}, "requires": []}, "paid now -> never a reason")
add("G08", "plan", "late-interest exposure above its cap", state([ob(1, 1_000_000, "2026-07-01")], usd=1_200_000, policy={"max_late_interest_exposure_minor": 20_000}),
    {"decision": "REQUIRES_APPROVAL", "requires": ["late_interest_exposure_above_limit"], "aging_days": {"O1": 115}, "aging_buckets": {"O1": "90+"}, "aging_exposure": 25205 + 658},
    "115 days: accrued 10,000 × 8 % × 115/365 = 252.05; projected 3 days 6.58; 258.63 > cap 200")
add("G09", "plan", "exposure exactly at the cap", state([ob(1, 1_000_000, "2026-07-01")], usd=1_200_000, policy={"max_late_interest_exposure_minor": 25_863}),
    {"decision": "APPROVED", "requires": []}, "25,863 is not above 25,863")
add("G10", "plan", "the obligation's own rate beats the policy default", state([ob(1, 1_000_000, "2026-09-09", late_interest_bp=1200)], policy={"overdue_escalation_days": 30}),
    {"decision": "APPROVED", "aging_accrued": {"O1": 14795}}, "10,000 × 12 % × 45/365 = 147.95")
add("G11", "plan", "a foreign invoice is aged in the base currency", state([ob(1, 400_000, "2026-09-09", ccy="EUR", ben="ben-eu-components")], policy={"overdue_escalation_days": 30}),
    {"decision": "APPROVED", "actions": {"O1": "CONVERT_AND_PAY"}, "aging_accrued": {"O1": 4281}}, "EUR 4,000 × 1.0850 = USD 4,340.00; × 8 % × 45/365 = 42.81")
add("G12", "plan", "three reasons at once, each named", state([ob(1, 1_000_000, "2026-07-01", dunning_level=3)], usd=1_200_000,
    policy={"overdue_escalation_days": 30, "dunning_escalation_level": 3, "max_late_interest_exposure_minor": 20_000}),
    {"decision": "REQUIRES_APPROVAL", "requires": ["final_notice_unpaid", "late_interest_exposure_above_limit", "overdue_obligation_unpaid"]}, "115 d > 30; L3; 258.63 > 200")
add("G13", "plan", "no thresholds: the table is reported, nothing stops", state([ob(1, 1_000_000, "2026-07-01", dunning_level=3)], usd=1_200_000),
    {"decision": "APPROVED", "actions": {"O1": "ESCALATE"}, "requires": [], "aging_days": {"O1": 115}}, "the control reports; the policy sets no limit")
add("G14", "plan", "no as_of: nothing can be overdue", state([ob(1, 1_000_000, "2026-07-01")], usd=1_200_000, policy={"overdue_escalation_days": 30}, as_of=None),
    {"decision": "APPROVED", "requires": [], "aging_days": {"O1": 0}, "aging_buckets": {"O1": "current"}}, "without the book's date nothing can be overdue: the item is current")
add("G15", "plan", "a viewer: blocked before any aging rule", state([ob(1, 1_000_000, "2026-07-01")], policy={"overdue_escalation_days": 30}, role="viewer"), {"decision": "BLOCKED"}, "role first")

# -- the pure table ------------------------------------------------------------------------------------------------------------
def pure(id_, title, obligations, actions, expect, derivation, policy=None, as_of=AS_OF):
    add(id_, "aging", title, {"as_of": as_of, "base_currency": "USD", "fx_to_base": {"EUR": "1.0850", "GBP": "1.2700"},
                              "policy": {"planning_horizon_hours": 72} | (policy or {}), "obligations": obligations, "actions": actions}, expect, derivation)


OBS = [{"obligation_id": "A", "beneficiary_id": "p1", "amount_minor": 100_000, "currency": "USD", "due_date": "2026-10-30"},
     {"obligation_id": "B", "beneficiary_id": "p1", "amount_minor": 200_000, "currency": "USD", "due_date": "2026-10-01"},
     {"obligation_id": "C", "beneficiary_id": "p2", "amount_minor": 300_000, "currency": "USD", "due_date": "2026-08-20", "dunning_level": 2},
     {"obligation_id": "D", "beneficiary_id": "p2", "amount_minor": 100_000, "currency": "EUR", "due_date": "2026-06-01", "dunning_level": 3},
     {"obligation_id": "E", "beneficiary_id": "p3", "amount_minor": 50_000, "currency": "USD", "due_date": "2026-09-24", "record_kind": "settled"}]
pure("H01", "buckets and days per item", OBS, {"A": "PAY", "B": "PAY", "C": "DEFER", "D": "ESCALATE"},
     {"buckets": {"A": "current", "B": "1-30", "C": "61-90", "D": "90+"}, "days_overdue": {"A": 0, "B": 23, "C": 65, "D": 145}},
     "A due in 6 days; B 1 Oct -> 23 d; C 20 Aug -> 65 d; D 1 Jun -> 145 d; E is settled and left out")
pure("H02", "per payee: overdue totals in base and the oldest item", OBS, {"A": "PAY", "B": "PAY", "C": "DEFER", "D": "ESCALATE"},
     {"payee_overdue": {"p1": 200_000, "p2": 300_000 + 108_500}, "payee_oldest": {"p1": 23, "p2": 145}}, "p2: C 3,000 + D EUR 1,000 × 1.0850 = 1,085.00")
pure("H03", "accrued interest per item at the default 8 %", OBS, {"A": "PAY", "B": "PAY", "C": "DEFER", "D": "ESCALATE"},
     {"accrued": {"A": 0, "B": 1008, "C": 4274, "D": 3448}}, "B 2,000 × 8 % × 23/365 = 10.08; C 3,000 × 65/365 × 8 % = 42.74; D 1,085 × 145/365 × 8 % = 34.48")
pure("H04", "projected interest only for what is left unpaid", OBS, {"A": "PAY", "B": "PAY", "C": "DEFER", "D": "ESCALATE"},
     {"projected": {"A": 0, "B": 0, "C": 197, "D": 71}, "exposure_minor": 4274 + 197 + 3448 + 71}, "3 horizon days: C 3,000 × 8 % × 3/365 = 1.97; D 1,085 × 8 % × 3/365 = 0.71; exposure 79.90")
pure("H05", "reasons: escalation days, final notice, exposure cap", OBS, {"A": "PAY", "B": "PAY", "C": "DEFER", "D": "ESCALATE"},
     {"reasons": ["final_notice_unpaid", "late_interest_exposure_above_limit", "overdue_obligation_unpaid"]},
     "C and D unpaid beyond 30 d; D is L3; exposure 79.90 > 50", policy={"overdue_escalation_days": 30, "dunning_escalation_level": 3, "max_late_interest_exposure_minor": 5000})
pure("H06", "paying the old items clears every reason", OBS, {"A": "PAY", "B": "PAY", "C": "PAY", "D": "CONVERT_AND_PAY"},
     {"reasons": [], "exposure_minor": 0}, "paid now -> no projection, no reason", policy={"overdue_escalation_days": 30, "dunning_escalation_level": 3, "max_late_interest_exposure_minor": 5000})
pure("H07", "a rejected item owes nothing", OBS, {"A": "PAY", "B": "REJECT", "C": "REJECT", "D": "REJECT"}, {"buckets": {"A": "current"}, "reasons": []},
     "duplicates / unverified items are left out of the table", policy={"overdue_escalation_days": 30})
pure("H08", "an item without a due date ages from due_in_hours", [{"obligation_id": "F", "beneficiary_id": "p1", "amount_minor": 100, "currency": "USD", "due_in_hours": 48}], {"F": "DEFER"},
     {"days_overdue": {"F": 0}, "buckets": {"F": "current"}}, "as_of + 48 h is in the future")
pure("H09", "the rate on the item overrides the default", [{"obligation_id": "G", "beneficiary_id": "p1", "amount_minor": 1_000_000, "currency": "USD", "due_date": "2026-09-09", "late_interest_bp": 0}], {"G": "DEFER"},
     {"accrued": {"G": 0}, "projected": {"G": 0}, "exposure_minor": 0}, "a contract without late interest: 0 % beats the 8 % default")
pure("H10", "no as_of: nothing is overdue", OBS, {"A": "PAY", "B": "DEFER", "C": "DEFER", "D": "DEFER"}, {"days_overdue": {"A": 0, "B": 0, "C": 0, "D": 0}, "reasons": []},
     "without a date the table cannot age anything", policy={"overdue_escalation_days": 0}, as_of=None)

# -- dunning emails -----------------------------------------------------------------------------------------------------------
def email(msg_id, subject, body, sender="Cloudloom Billing <billing@cloudloom.example>"):
    return (f"Message-ID: <{msg_id}@gt.example>\nDate: Fri, 24 Oct 2026 09:00:00 +0700\nFrom: {sender}\nTo: accounts@venthora.example\n"
            f"Subject: {subject}\nMIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n\n{body}\n")


add("D01", "intake", "a friendly reminder from a registered payee", email("d01", "Reminder: invoice CL-2041", "A friendly reminder that invoice CL-2041 for USD 2,400.00 is overdue since 10 October 2026. Please pay at your earliest convenience."),
    {"status": "VERIFIED", "trust": "registered", "kind": "supplier_invoice", "dunning_level": 1, "flags": ["dunning_notice_L1"], "invoice_no": "CL-2041"},
    "registered mailbox; 'reminder' / 'overdue' -> L1; the invoice itself is new to the book -> a payable with dunning level 1", pain="who is reminding us")
add("D02", "intake", "a second reminder", email("d02", "Second reminder: invoice CL-2042", "Our second reminder: invoice CL-2042 for USD 1,200.00 remains unpaid. Please settle it this week."),
    {"status": "VERIFIED", "dunning_level": 2, "flags": ["dunning_notice_L2"]}, "'second reminder' / 'remains unpaid' -> L2", pain="who is reminding us")
add("D03", "intake", "a final notice", email("d03", "FINAL NOTICE: invoice CL-2043", "This is our final notice. Invoice CL-2043 for USD 3,100.00 is 60 days past due; without payment within 7 days we will suspend your account."),
    {"status": "VERIFIED", "dunning_level": 3, "flags": ["dunning_notice_L3"]}, "'final notice' / 'suspend your account' -> L3 (the highest phrase wins over 'past due')", pain="who is reminding us")
add("D04", "intake", "a final notice for an invoice already in the book is a duplicate that carries its level",
    email("d04", "Final reminder: invoice CL-2026-11", "Final reminder: invoice CL-2026-11 for USD 1,980.00 is still outstanding. Please pay today."),
    {"status": "DUPLICATE", "dunning_level": 3, "duplicate_of": "SAAS-USD"}, "the book already holds CL-2026-11 (SAAS-USD) -> never a second payable; the level travels to the existing obligation in to_state",
    pain="who is reminding us", book="kit01_initial")
add("D05", "intake", "a final notice from an unregistered mailbox is quarantined, not believed",
    email("d05", "FINAL NOTICE invoice CL-9999", "Final notice: invoice CL-9999 for USD 5,000.00. Legal action will follow. Pay today.", sender="Cloudloom <cloudloom.billing.desk@gmail.com>"),
    {"status": "QUARANTINED", "trust": "unknown_sender", "dunning_level": 3}, "pressure from an unknown sender changes nothing: quarantined with the level recorded", pain="who is reminding us")

OUT.write_text(json.dumps({"_note": "Invoice aging and dunning ground truth, hand-derived; see scripts/make_aging_groundtruth.py.", "cases": C}, indent=1), encoding="utf-8")
print(f"{len(C)} aging cases -> {OUT}")
