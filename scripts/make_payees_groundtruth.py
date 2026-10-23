# ruff: noqa: E501
"""Write data/groundtruth/payees.json: the payee lifecycle with hand-derived answers (src/venthora/payees.py).

Two kinds of case. `plan`: a treasury state with a payee registry attached, judged by the gate — NEW / CHANGED / SUSPENDED /
unregistered payees are ESCALATED (no payment, not even with an approval); a VERIFIED payee needs a person inside the cooling
period (payee_in_cooling_period) and until its first PAID payout (payee_not_yet_active); ACTIVE adds nothing. `payees`: the
state machine itself — register, the mandatory call-back (approver role, the registered number, nothing else), a bank change
that starts the cooling period, payments that promote or do not, suspend / reinstate.
Cash is ample (USD 100,000, floor 5,000) so only the lifecycle decides.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "groundtruth" / "payees.json"

POLICY = {"reserve_floor_minor": 500_000, "autonomous_conversion_limit_minor": 10_000_000, "swift_fee_currency": "EUR",
          "swift_fee_minor": 1285, "planning_horizon_hours": 72}
NOW = "2026-10-23T12:00:00+00:00"
PHONE = "+1 650 555 0142"


def ob(i, amount=300_000, ben="ben-saas-vendor", **kw):
    return {"obligation_id": f"O{i}", "amount_minor": amount, "currency": "USD", "priority": 5, "due_in_hours": 24,
            "beneficiary_id": ben, "payee_name": f"Payee {i}", "payout_method": "LOCAL", "evidence_ref": f"ev-O{i}",
            "invoice_fingerprint": f"fp-O{i}"} | kw


def ev(i):
    return {"ref": f"ev-O{i}", "subject_id": f"O{i}", "source": "ap:invoice", "status": "verified", "summary": f"inv {i}"}


def rec(ben="ben-saas-vendor", status="ACTIVE", cooling_until=None, paid_count=None, fp="bank:a1"):
    return {"beneficiary_id": ben, "status": status, "bank_fingerprint": fp, "registered_phone": PHONE,
            "cooling_until": cooling_until, "paid_count": (1 if status == "ACTIVE" else 0) if paid_count is None else paid_count}


def state(obligations, records, *, as_of=NOW, role="treasury_agent", registry=True):
    obs = list(obligations)
    s = {"case_id": "payees", "book_id": "payees", "base_currency": "USD", "balances": {"USD": 10_000_000, "EUR": 0, "GBP": 0},
         "fx_to_base": {"EUR": "1.0850", "GBP": "1.2700"}, "obligations": obs, "receivables": [],
         "evidence": [ev(int(o["obligation_id"][1:])) for o in obs], "scenarios": [], "policy": POLICY, "actor_role": role}
    if registry:
        s["payees"] = {"as_of": as_of, "records": records}
    return s


C = []


def add(id_, kind, title, inp, expect, derivation, pain="who may be paid at all, and when alone"):
    C.append({"id": id_, "kind": kind, "level": 4, "pain": pain, "title": title, "input": inp, "expect": expect, "derivation": derivation})


# -- plans against a registry ------------------------------------------------------------------------------------------------
add("Y01", "plan", "an ACTIVE payee is paid autonomously", state([ob(1)], [rec()]),
    {"decision": "APPROVED", "actions": {"O1": "PAY"}, "requires": []}, "ACTIVE: verified and paid before -> this control adds nothing")
add("Y02", "plan", "a NEW payee is never paid: escalated to a person", state([ob(1)], [rec(status="NEW")]),
    {"decision": "APPROVED", "actions": {"O1": "ESCALATE"}, "paid": []}, "NEW: nobody confirmed the bank details by call-back -> ESCALATE; no approval can pay it")
add("Y03", "plan", "a payee whose bank details changed is never paid until the call-back", state([ob(1)], [rec(status="CHANGED", cooling_until="2026-10-26T12:00:00+00:00")]),
    {"decision": "APPROVED", "actions": {"O1": "ESCALATE"}, "paid": []}, "CHANGED -> ESCALATE: paying the new account on the strength of an email is the classic fraud")
add("Y04", "plan", "a suspended payee is never paid", state([ob(1)], [rec(status="SUSPENDED")]),
    {"decision": "APPROVED", "actions": {"O1": "ESCALATE"}, "paid": []}, "SUSPENDED -> ESCALATE")
add("Y05", "plan", "an unregistered payee (no record at all) is never paid", state([ob(1, ben="ben-nobody")], [rec()]),
    {"decision": "APPROVED", "actions": {"O1": "ESCALATE"}, "paid": []}, "registry attached, no record for ben-nobody -> payee_unregistered -> ESCALATE")
add("Y06", "plan", "VERIFIED, never paid: the first payment needs a person", state([ob(1)], [rec(status="VERIFIED")]),
    {"decision": "REQUIRES_APPROVAL", "actions": {"O1": "PAY"}, "requires": ["payee_not_yet_active"]}, "call-back done, no PAID payout yet -> a person approves the first one")
add("Y07", "plan", "VERIFIED inside the cooling period after a bank change", state([ob(1)], [rec(status="VERIFIED", cooling_until="2026-10-26T12:00:00+00:00")]),
    {"decision": "REQUIRES_APPROVAL", "requires": ["payee_in_cooling_period"]}, "now 23 Oct 12:00 < cooling_until 26 Oct 12:00 -> payee_in_cooling_period")
add("Y08", "plan", "the cooling period is over but the new details were never paid", state([ob(1)], [rec(status="VERIFIED", cooling_until="2026-10-23T11:59:59+00:00")]),
    {"decision": "REQUIRES_APPROVAL", "requires": ["payee_not_yet_active"]}, "cooling ended one second ago -> not in cooling; still VERIFIED (never paid on these details) -> first payment by a person")
add("Y09", "plan", "cooling ends exactly now", state([ob(1)], [rec(status="VERIFIED", cooling_until=NOW)]),
    {"decision": "REQUIRES_APPROVAL", "requires": ["payee_not_yet_active"]}, "now == cooling_until is not before it -> the cooling period is over at that instant")
add("Y10", "plan", "one ACTIVE payee paid, one NEW payee escalated, in one plan", state([ob(1), ob(2, ben="ben-uk-logistics")], [rec(), rec(ben="ben-uk-logistics", status="NEW")]),
    {"decision": "APPROVED", "actions": {"O1": "PAY", "O2": "ESCALATE"}, "paid": ["O1"]}, "the escalation does not stop the safe part of the plan")
add("Y11", "plan", "ACTIVE payee, but the invoice itself announces new bank details", state([ob(1, policy_flags=["payee_bank_details_changed"])], [rec()]),
    {"decision": "APPROVED", "actions": {"O1": "ESCALATE"}, "paid": []}, "the evidence flag escalates even when the registry still says ACTIVE (the registry is updated by the intake)")
add("Y12", "plan", "no registry attached: the lifecycle is not enforced", state([ob(1, ben="ben-nobody")], [], registry=False),
    {"decision": "APPROVED", "actions": {"O1": "PAY"}, "requires": []}, "payees = null -> the control is off (plain plans and older suites unchanged)")
add("Y13", "plan", "a viewer cannot plan, whatever the payee", state([ob(1)], [rec(status="VERIFIED")], role="viewer"),
    {"decision": "BLOCKED"}, "role before everything")
add("Y14", "plan", "ACTIVE payee with a cooling_until in the past left on the record", state([ob(1)], [rec(status="ACTIVE", cooling_until="2026-10-20T00:00:00+00:00")]),
    {"decision": "APPROVED", "requires": []}, "ACTIVE is ACTIVE: cooling only matters while VERIFIED")

# -- the state machine --------------------------------------------------------------------------------------------------------
REG = {"kind": "register", "beneficiary_id": "ben-new", "bank_fingerprint": "bank:a1", "registered_phone": PHONE, "at": "2026-10-20T09:00:00+00:00"}
VERIFY = {"kind": "verify", "by": "D. Finance", "role": "finance_manager", "called_number": PHONE, "at": "2026-10-20T10:00:00+00:00"}


def sm(id_, title, events, expect, derivation, record=None, cooling_hours=72):
    inp = {"record": record, "events": events, "cooling_hours": cooling_hours}
    add(id_, "payees", title, inp, expect, derivation)


sm("Z01", "register a payee", [REG], {"status": "NEW", "bank_fingerprint": "bank:a1", "refused_n": 0, "payable": False}, "registered -> NEW; not payable")
sm("Z02", "the call-back on the registered number verifies", [REG, VERIFY], {"status": "VERIFIED", "cooling_until": None, "refused_n": 0, "payable": True, "autonomous": False},
   "finance_manager called the registered number -> VERIFIED; payable, but not alone yet")
sm("Z03", "a call-back to a number from the request is refused", [REG, VERIFY | {"called_number": "+1 650 555 0199"}],
   {"status": "NEW", "refused_n": 1, "last_refusal_contains": "registered number"}, "the number must be the one on file; the record stays NEW")
sm("Z04", "a treasury agent may not verify", [REG, VERIFY | {"role": "treasury_agent"}], {"status": "NEW", "refused_n": 1, "last_refusal_contains": "may not verify"},
   "only approver roles can confirm a payee")
sm("Z05", "no registered number: no call-back possible", [REG | {"registered_phone": None}, VERIFY], {"status": "NEW", "refused_n": 1, "last_refusal_contains": "no registered phone"},
   "register the number first")
sm("Z06", "a bank change on an ACTIVE payee starts the cooling period", [{"kind": "bank_change", "bank_fingerprint": "bank:b2", "source": "email", "at": "2026-10-23T12:00:00+00:00"}],
   {"status": "CHANGED", "bank_fingerprint": "bank:b2", "cooling_until": "2026-10-26T12:00:00+00:00", "payable": False}, "ACTIVE -> CHANGED; cooling_until = 23 Oct 12:00 + 72 h", record=rec())
sm("Z07", "the call-back after a change verifies but keeps the cooling period",
   [{"kind": "bank_change", "bank_fingerprint": "bank:b2", "source": "email", "at": "2026-10-23T12:00:00+00:00"}, VERIFY | {"at": "2026-10-23T15:00:00+00:00"}],
   {"status": "VERIFIED", "cooling_until": "2026-10-26T12:00:00+00:00", "payable": True, "autonomous": False}, "VERIFIED with cooling_until unchanged", record=rec())
sm("Z08", "a payment inside the cooling period does not activate",
   [{"kind": "bank_change", "bank_fingerprint": "bank:b2", "source": "email", "at": "2026-10-23T12:00:00+00:00"}, VERIFY | {"at": "2026-10-23T15:00:00+00:00"},
    {"kind": "paid", "at": "2026-10-24T09:00:00+00:00"}],
   {"status": "VERIFIED", "paid_count": 2, "autonomous": False}, "paid 24 Oct < cooling_until 26 Oct -> stays VERIFIED (every payment still by a person)", record=rec())
sm("Z09", "a payment after the cooling period activates",
   [{"kind": "bank_change", "bank_fingerprint": "bank:b2", "source": "email", "at": "2026-10-23T12:00:00+00:00"}, VERIFY | {"at": "2026-10-23T15:00:00+00:00"},
    {"kind": "paid", "at": "2026-10-26T12:00:00+00:00"}],
   {"status": "ACTIVE", "paid_count": 2, "autonomous": True}, "paid at exactly cooling_until -> not before it -> ACTIVE", record=rec())
sm("Z10", "a new payee: verified, then the first payment activates", [REG, VERIFY, {"kind": "paid", "at": "2026-10-21T09:00:00+00:00"}],
   {"status": "ACTIVE", "paid_count": 1, "autonomous": True}, "no cooling on a first verification -> ACTIVE at the first PAID payout")
sm("Z11", "the same bank details are not a change", [{"kind": "bank_change", "bank_fingerprint": "bank:a1", "source": "email", "at": NOW}],
   {"status": "ACTIVE", "refused_n": 1, "last_refusal_contains": "unchanged"}, "fingerprint on file == fingerprint announced -> refused, nothing changes", record=rec())
sm("Z12", "paying a NEW payee is refused by the machine itself", [REG, {"kind": "paid", "at": NOW}], {"status": "NEW", "paid_count": 0, "refused_n": 1, "last_refusal_contains": "cannot be paid"},
   "the gate never asks; if anything did, the record refuses")
sm("Z13", "suspend, then a payment is refused, then reinstate goes back to NEW",
   [{"kind": "suspend", "by": "D. Finance", "role": "finance_manager", "reason": "dispute", "at": NOW}, {"kind": "paid", "at": NOW},
    {"kind": "reinstate", "by": "D. Finance", "role": "finance_manager", "at": NOW}],
   {"status": "NEW", "refused_n": 1, "verified_by": None, "payable": False}, "SUSPENDED refuses the payment; reinstating requires another call-back (NEW)", record=rec())
sm("Z14", "a change with cooling_hours 0 still needs the call-back, then one approved payment",
   [{"kind": "bank_change", "bank_fingerprint": "bank:c3", "source": "portal", "at": NOW}, {"kind": "paid", "at": NOW}, VERIFY | {"at": NOW}, {"kind": "paid", "at": NOW}],
   {"status": "ACTIVE", "refused_n": 1, "cooling_until": NOW, "paid_count": 2}, "paid on CHANGED refused; verify -> VERIFIED (cooling ends at once); paid at cooling_until -> ACTIVE", record=rec(), cooling_hours=0)
sm("Z15", "a second bank change while CHANGED restarts the cooling period",
   [{"kind": "bank_change", "bank_fingerprint": "bank:b2", "source": "email", "at": "2026-10-23T12:00:00+00:00"},
    {"kind": "bank_change", "bank_fingerprint": "bank:c3", "source": "email", "at": "2026-10-24T12:00:00+00:00"}],
   {"status": "CHANGED", "bank_fingerprint": "bank:c3", "cooling_until": "2026-10-27T12:00:00+00:00"}, "the clock restarts from the latest change", record=rec())
sm("Z16", "a viewer may not suspend", [{"kind": "suspend", "by": "V", "role": "viewer", "at": NOW}], {"status": "ACTIVE", "refused_n": 1}, "role check on every person action", record=rec())
sm("Z17", "events on an unregistered payee are refused", [VERIFY], {"status": None, "refused_n": 1, "last_refusal_contains": "unregistered"}, "nothing to verify without a record")
sm("Z18", "verifying an ACTIVE payee is refused (nothing pending)", [VERIFY], {"status": "ACTIVE", "refused_n": 1, "last_refusal_contains": "nothing to verify"}, "a call-back is for NEW or CHANGED", record=rec())

OUT.write_text(json.dumps({"_note": "Payee-lifecycle ground truth, hand-derived; see scripts/make_payees_groundtruth.py.", "cases": C}, indent=1), encoding="utf-8")
print(f"{len(C)} payee cases -> {OUT}")
