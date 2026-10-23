# ruff: noqa: E501
"""Write data/groundtruth/controls.json: instalments, three-way match, period cut-off and reconciliation cases.

Rules (hand-derived; see engines/verity.py, proofcore/match.py, proofcore/budget.py, execution/executor.py):
  instalments   duplicate lock per (invoice, instalment); parts may never exceed the contract total
  three-way     expected = contract − paid so far; claimed > expected + tolerance -> amount_mismatch (a person);
                claimed < expected − tolerance -> under-claim, noted; tolerance = max(5.00, 0.5% of expected)
  cut-off       due after period_end -> carry-forward (next period's commitment, not this period's line);
                due before period_start -> late-posted; a closed period -> period_closed (a person)
  recon case    a reconciliation mismatch opens a case and locks the book: the next execution is REFUSED until a
                person closes the case with a note (execution kind, fake sandbox)
Cash is ample (USD 100,000, floor 5,000) so only the control under test decides.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "groundtruth" / "controls.json"

POLICY = {"reserve_floor_minor": 500_000, "autonomous_conversion_limit_minor": 10_000_000, "swift_fee_currency": "EUR",
          "swift_fee_minor": 1285, "planning_horizon_hours": 72}


def ob(i, amount, **kw):
    return {"obligation_id": f"O{i}", "amount_minor": amount, "currency": "USD", "priority": 5, "due_in_hours": 24,
            "beneficiary_id": "ben-saas-vendor", "payee_name": f"Payee {i}", "payout_method": "LOCAL", "evidence_ref": f"ev-O{i}",
            "invoice_fingerprint": f"fp-O{i}", "budget_line": "suppliers"} | kw


def ev(i):
    return {"ref": f"ev-O{i}", "subject_id": f"O{i}", "source": "ap:invoice", "status": "verified", "summary": f"inv {i}"}


def state(obligations, *, budget=None, as_of=None, policy=None):
    obs = list(obligations)
    s = {"case_id": "controls", "book_id": "controls", "base_currency": "USD", "balances": {"USD": 10_000_000, "EUR": 0, "GBP": 0},
         "fx_to_base": {"EUR": "1.0850", "GBP": "1.2700"}, "obligations": obs, "receivables": [],
         "evidence": [ev(int(o["obligation_id"][1:])) for o in obs], "scenarios": [], "policy": POLICY | (policy or {}),
         "actor_role": "treasury_agent"}
    if budget:
        s["budget"] = budget
    if as_of:
        s["as_of"] = as_of
    return s


NOV = {"period": "2026-11", "period_start": "2026-11-01", "period_end": "2026-11-30", "lines": [{"line": "suppliers", "amount_minor": 5_000_000}]}
C = []


def add(id_, kind, title, inp, expect, derivation, pain, **kw):
    C.append({"id": id_, "kind": kind, "level": 4, "pain": pain, "title": title, "input": inp, "expect": expect, "derivation": derivation} | kw)


# ---- instalments ------------------------------------------------------------------------------------------------------
add("K01", "plan", "deposit and balance of one invoice are two payouts", state([
    ob(1, 300_000, invoice_fingerprint="fp-INV9", installment="deposit", contract_total_minor=1_000_000),
    ob(2, 700_000, invoice_fingerprint="fp-INV9", installment="balance", contract_total_minor=1_000_000, paid_so_far_minor=300_000)]),
    {"decision": "APPROVED", "actions": {"O1": "PAY", "O2": "PAY"}}, "same invoice, different instalments -> both payable; 3,000 + 7,000 = contract 10,000", "a deposit then the balance")
add("K02", "plan", "the balance sent twice is a duplicate", state([
    ob(1, 700_000, invoice_fingerprint="fp-INV9", installment="balance", contract_total_minor=1_000_000, paid_so_far_minor=300_000),
    ob(2, 700_000, invoice_fingerprint="fp-INV9", installment="balance", contract_total_minor=1_000_000, paid_so_far_minor=300_000)]),
    {"actions": {"O1": "PAY", "O2": "REJECT"}}, "same (invoice, instalment) -> the second copy is REJECT", "paying twice")
add("K03", "plan", "a balance that overshoots the contract is rejected", state([
    ob(1, 800_000, invoice_fingerprint="fp-INV9", installment="balance", contract_total_minor=1_000_000, paid_so_far_minor=300_000)]),
    {"actions": {"O1": "REJECT"}}, "3,000 paid + 8,000 claimed = 11,000 > contract 10,000 -> REJECT before anything else", "over-claim on the balance")
add("K04", "plan", "whole-invoice payout after a deposit is a duplicate of the invoice", state([
    ob(1, 300_000, invoice_fingerprint="fp-INV9", installment="deposit", contract_total_minor=1_000_000),
    ob(2, 1_000_000, invoice_fingerprint="fp-INV9", contract_total_minor=1_000_000, paid_so_far_minor=300_000)]),
    {"actions": {"O1": "PAY", "O2": "PAY"}, "decision": "REQUIRES_APPROVAL", "requires": ["amount_mismatch"]},
    "O2 claims the whole 10,000 although 3,000 is paid: expected 7,000, claimed 10,000 -> over-claim -> a person (amount_mismatch); O1 unaffected", "double-billing after a deposit")

# ---- three-way match ---------------------------------------------------------------------------------------------------
add("K05", "plan", "invoice equals the contract", state([ob(1, 1_000_000, contract_total_minor=1_000_000)]),
    {"decision": "APPROVED", "actions": {"O1": "PAY"}, "requires": []}, "expected 10,000 = claimed -> matched", "three-way match")
add("K06", "plan", "invoice inside tolerance (+0.4%)", state([ob(1, 1_004_000, contract_total_minor=1_000_000)]),
    {"decision": "APPROVED", "requires": []}, "diff 40 <= tolerance max(5, 0.5% of 10,000 = 50) -> matched", "rounding and small variances")
add("K07", "plan", "invoice above tolerance (+1%)", state([ob(1, 1_010_000, contract_total_minor=1_000_000)]),
    {"decision": "REQUIRES_APPROVAL", "actions": {"O1": "PAY"}, "requires": ["amount_mismatch"]}, "diff 100 > 50 -> over-claim -> a person", "over-claim")
add("K08", "plan", "invoice below the contract (under-claim) is fine", state([ob(1, 900_000, contract_total_minor=1_000_000)]),
    {"decision": "APPROVED", "requires": []}, "the payee bills less; paying less is never unsafe; noted as under-claim", "under-claim")
add("K09", "plan", "over-claim that is escalated anyway needs no second reason", state([ob(1, 1_010_000, contract_total_minor=1_000_000, policy_flags=["payee_bank_details_changed"])]),
    {"actions": {"O1": "ESCALATE"}, "requires": []}, "the bank-change flag already sends it to a person; the plan pays nothing on it, so no amount_mismatch", "controls compose")
add("K10", "plan", "tolerance set to zero: one cent over needs a person", state([ob(1, 1_000_001, contract_total_minor=1_000_000)], policy={"amount_tolerance_bp": 0, "amount_tolerance_minor": 0}),
    {"decision": "REQUIRES_APPROVAL", "requires": ["amount_mismatch"]}, "tolerance 0 -> any excess is an over-claim", "strict books")

# ---- period cut-off ----------------------------------------------------------------------------------------------------
add("K11", "plan", "due inside the period draws on the line", state([ob(1, 300_000)], budget=NOV, as_of="2026-11-10"),
    {"decision": "APPROVED", "actions": {"O1": "PAY"}, "budget_carry_forward": 0, "budget_status": {"suppliers": "auto"}},
    "as_of 10 Nov + 24h = 11 Nov, inside Nov -> counted on 'suppliers'", "cut-off")
add("K12", "plan", "due after the period end carries forward", state([ob(1, 300_000, due_in_hours=72)], budget=NOV, as_of="2026-11-29"),
    {"decision": "APPROVED", "actions": {"O1": "PAY"}, "budget_carry_forward": 1, "budget_paying_in_period": 0},
    "29 Nov + 72h = 2 Dec > 30 Nov -> next period's commitment; it is still payable from cash but draws on no November line", "cut-off")
add("K13", "plan", "explicit due_date after the period end", state([ob(1, 300_000, due_date="2026-12-05")], budget=NOV, as_of="2026-11-20"),
    {"budget_carry_forward": 1}, "due_date wins over due_in_hours; 5 Dec is after 30 Nov -> carry-forward", "cut-off")
add("K14", "plan", "late-posted: due before the period started", state([ob(1, 300_000, due_date="2026-10-28")], budget=NOV, as_of="2026-11-03"),
    {"budget_late_posted": 1, "budget_carry_forward": 0, "actions": {"O1": "PAY"}}, "28 Oct < 1 Nov -> late-posted on the November statement, still payable", "cut-off")
add("K15", "plan", "closed period: spending against it needs a person", state([ob(1, 300_000)], budget=NOV | {"closed": True}, as_of="2026-11-10"),
    {"decision": "REQUIRES_APPROVAL", "requires": ["period_closed"]}, "the statement was issued; a payment that draws on the closed period goes to a person", "cut-off")
add("K16", "plan", "closed period but the payment carries forward: no reason", state([ob(1, 300_000, due_in_hours=72)], budget=NOV | {"closed": True}, as_of="2026-11-29"),
    {"decision": "APPROVED", "requires": []}, "nothing is paid against November (carry-forward) -> period_closed does not apply", "cut-off")

# ---- reconciliation case (execution, fake sandbox) --------------------------------------------------------------------
KIT = json.loads((ROOT / "data" / "scenarios" / "kit01_initial.json").read_text(encoding="utf-8"))["state"]
KIT_FEES = {**KIT, "policy": {**KIT["policy"], "payout_fees_minor": {"SWIFT:EUR": 1337, "LOCAL:USD": 300, "LOCAL:GBP": 224}}}
add("K17", "execution", "a mismatch opens a case and the book is locked for the next run",
    {"state": KIT_FEES, "hidden_charge": {"EUR": "0.63"}, "twice": True},
    {"execution": "REFUSED", "message_contains": "locked by open reconciliation case"},
    "the rail debits EUR 0.63 it never reports on the transfer -> live EUR differs from expected by 63 minor -> RECONCILIATION_MISMATCH opens a case -> the second run is REFUSED before anything moves", "reconciliation mismatch")
add("K18", "execution", "closing the case unlocks the book",
    {"state": KIT_FEES, "hidden_charge": {"EUR": "0.63"}, "twice": True, "close_cases": True},
    {"execution": "FAILED", "message_contains": "duplicate payment blocked"},
    "with the case closed by a person (note required), the next run reaches the duplicate lock as usual (the invoices were paid in run 1)", "reconciliation mismatch")

OUT.write_text(json.dumps({"_note": "Controls ground truth, hand-derived; see scripts/make_controls_groundtruth.py.", "cases": C}, indent=1), encoding="utf-8")
print(f"{len(C)} control cases -> {OUT}")
