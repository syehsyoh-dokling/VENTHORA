# ruff: noqa: E501
"""Write data/groundtruth/approvals.json: the approval matrix (four-eyes) with hand-derived answers (src/venthora/proofcore/approvals.py).

Two kinds of case. `plan`: a treasury state whose policy carries an approval matrix; the gate decides which tier applies (by the
plan's paying total and by the reason codes raised; the last applicable tier wins; no tier = one approver; no reason = nothing).
`approvals`: the check the executor runs before money moves — allowed role, the plan's own terms digest, two approvals from one
person count once, the requester may never approve. Cash is ample (USD 1,000,000, floor 5,000) so only the matrix decides.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "groundtruth" / "approvals.json"

MATRIX = [{"name": "one", "min_total_minor": 0, "reasons": [], "approvals_required": 1, "roles": ["finance_manager"]},
          {"name": "two", "min_total_minor": 2_500_000, "reasons": ["payee_in_cooling_period", "payee_not_yet_active"], "approvals_required": 2, "roles": ["finance_manager", "cfo"]},
          {"name": "board", "min_total_minor": 10_000_000, "reasons": [], "approvals_required": 3, "roles": ["cfo"]}]
POLICY = {"reserve_floor_minor": 500_000, "autonomous_conversion_limit_minor": 10_000_000, "swift_fee_currency": "EUR",
          "swift_fee_minor": 1285, "planning_horizon_hours": 72, "max_single_payment_minor": 50_000, "approval_matrix": MATRIX}
NOW = "2026-10-23T12:00:00+00:00"


def ob(i, amount, ben="ben-saas-vendor", **kw):
    return {"obligation_id": f"O{i}", "amount_minor": amount, "currency": "USD", "priority": 5, "due_in_hours": 24,
            "beneficiary_id": ben, "payee_name": f"Payee {i}", "payout_method": "LOCAL", "evidence_ref": f"ev-O{i}",
            "invoice_fingerprint": f"fp-O{i}"} | kw


def ev(i):
    return {"ref": f"ev-O{i}", "subject_id": f"O{i}", "source": "ap:invoice", "status": "verified", "summary": f"inv {i}"}


def state(obligations, *, policy=None, payee_status=None, role="treasury_agent"):
    obs = list(obligations)
    s = {"case_id": "approvals", "book_id": "approvals", "base_currency": "USD", "balances": {"USD": 100_000_000, "EUR": 0, "GBP": 0},
         "fx_to_base": {"EUR": "1.0850", "GBP": "1.2700"}, "obligations": obs, "receivables": [],
         "evidence": [ev(int(o["obligation_id"][1:])) for o in obs], "scenarios": [], "policy": POLICY | (policy or {}), "actor_role": role}
    if payee_status:
        s["payees"] = {"as_of": NOW, "records": [{"beneficiary_id": "ben-saas-vendor", "status": payee_status, "registered_phone": "+1 650 555 0142",
                                                  "cooling_until": "2026-10-26T12:00:00+00:00" if payee_status == "VERIFIED" else None}]}
    return s


C = []


def add(id_, kind, title, inp, expect, derivation, pain="who may approve, and how many"):
    C.append({"id": id_, "kind": kind, "level": 4, "pain": pain, "title": title, "input": inp, "expect": expect, "derivation": derivation})


# -- which tier applies -------------------------------------------------------------------------------------------------------
add("Q01", "plan", "a small plan that stops for a person: one finance manager", state([ob(1, 100_000)]),
    {"decision": "REQUIRES_APPROVAL", "requires": ["single_payment_above_limit"], "approval_tier": "one", "approvals_required": 1, "approval_roles": ["finance_manager"]},
    "USD 1,000 >= tier 'one' (0), < tier 'two' (25,000) -> one person")
add("Q02", "plan", "one cent below the two-person threshold", state([ob(1, 2_499_999)]),
    {"decision": "REQUIRES_APPROVAL", "approval_tier": "one", "approvals_required": 1}, "24,999.99 < 25,000 -> still one person")
add("Q03", "plan", "exactly the two-person threshold", state([ob(1, 2_500_000)]),
    {"decision": "REQUIRES_APPROVAL", "approval_tier": "two", "approvals_required": 2, "approval_roles": ["cfo", "finance_manager"]}, "25,000 >= 25,000 -> two different people")
add("Q04", "plan", "two payouts whose total crosses the threshold", state([ob(1, 1_300_000), ob(2, 1_200_000, ben="ben-uk-logistics")]),
    {"decision": "REQUIRES_APPROVAL", "approval_tier": "two", "approvals_required": 2}, "13,000 + 12,000 = 25,000: the tier reads the plan's paying total, not one payout")
add("Q05", "plan", "the board tier", state([ob(1, 10_000_000)]),
    {"decision": "REQUIRES_APPROVAL", "approval_tier": "board", "approvals_required": 3, "approval_roles": ["cfo"]}, "100,000 >= tier 'board' -> three different CFO-role people; the last applicable tier wins")
add("Q06", "plan", "a small amount but a payee-change reason: the reason lifts the tier", state([ob(1, 100_000)], payee_status="VERIFIED"),
    {"decision": "REQUIRES_APPROVAL", "requires": ["payee_in_cooling_period"], "approval_tier": "two", "approvals_required": 2},
    "USD 1,000 would be tier 'one'; payee_in_cooling_period is listed on tier 'two' -> two people")
add("Q07", "plan", "no reason raised: no approval at all", state([ob(1, 40_000)]),
    {"decision": "APPROVED", "approvals_required": 0, "approval_tier": None}, "USD 400 <= single limit 500 -> autonomous; the matrix is not consulted")
add("Q08", "plan", "no matrix in the policy: one approver from approver_roles", state([ob(1, 5_000_000)], policy={"approval_matrix": []}),
    {"decision": "REQUIRES_APPROVAL", "approval_tier": "default", "approvals_required": 1, "approval_roles": ["finance_manager"]}, "an empty matrix is the old rule: one finance manager")
add("Q09", "plan", "a matrix with only a two-person tier: small plans fall back to the default", state([ob(1, 100_000)], policy={"approval_matrix": [MATRIX[1]]}),
    {"decision": "REQUIRES_APPROVAL", "approval_tier": "default", "approvals_required": 1}, "1,000 < 25,000 and no payee reason -> no tier applies -> default")
add("Q10", "plan", "an escalated payout does not count toward the total", state([ob(1, 2_500_000)], payee_status="NEW"),
    {"decision": "APPROVED", "actions": {"O1": "ESCALATE"}, "approvals_required": 0}, "NEW payee -> ESCALATE, nothing paid, no reason raised -> no approval")
add("Q11", "plan", "a viewer: blocked before any tier", state([ob(1, 2_500_000)], role="viewer"), {"decision": "BLOCKED"}, "role first")

# -- the check ---------------------------------------------------------------------------------------------------------------
D = "a" * 64
FM = {"approver": "Dana Finch", "role": "finance_manager", "terms_digest": D}
CFO = {"approver": "Omar Reyes", "role": "cfo", "terms_digest": D}


def chk(id_, title, approvals, expect, derivation, *, total=2_500_000, reasons=("single_payment_above_limit",), requested_by="Treasury agent run", matrix=MATRIX):
    add(id_, "approvals", title, {"approval_matrix": matrix, "approver_roles": ["finance_manager"], "reasons": list(reasons), "paying_total_minor": total,
                                   "approvals": approvals, "requested_by": requested_by, "terms_digest": D}, expect, derivation)


chk("W01", "two different people on the plan's terms", [FM, CFO], {"ok": True, "tier": "two", "approvals_required": 2, "approved_by": ["Dana Finch", "Omar Reyes"], "problems_n": 0},
    "finance manager + CFO, both on digest D -> satisfied")
chk("W02", "one approval where two are needed", [FM], {"ok": False, "problems_n": 1, "first_problem_contains": "2 different people"}, "1 valid < 2")
chk("W03", "the same person twice", [FM, FM], {"ok": False, "problems_n": 2, "first_problem_contains": "approved twice"}, "one person counts once -> 1 valid < 2")
chk("W04", "the same person twice, spelled differently", [FM, FM | {"approver": "  dana   FINCH "}], {"ok": False, "first_problem_contains": "approved twice"},
    "names are compared normalised (case, spacing)")
chk("W05", "the requester approves their own plan", [FM | {"approver": "Dana Finch"}, CFO], {"ok": False, "first_problem_contains": "may not approve it (four-eyes)"},
    "requested_by = Dana Finch -> her approval does not count -> 1 valid < 2", requested_by="Dana Finch")
chk("W06", "a role the tier does not allow", [FM, {"approver": "Pat Lee", "role": "ap_clerk", "terms_digest": D}], {"ok": False, "first_problem_contains": "may not approve this tier"},
    "ap_clerk is not in [finance_manager, cfo]")
chk("W07", "an approval on different terms", [FM, CFO | {"terms_digest": "b" * 64}], {"ok": False, "first_problem_contains": "different terms"},
    "the CFO signed another plan's digest -> does not count")
chk("W08", "one person is enough below the threshold", [FM], {"ok": True, "tier": "one", "approvals_required": 1}, "total 1,000 -> tier 'one'", total=100_000)
chk("W09", "a small amount with a payee-change reason needs two", [FM], {"ok": False, "tier": "two", "approvals_required": 2}, "reason lifts the tier", total=100_000, reasons=("payee_not_yet_active",))
chk("W10", "no reason: nothing to approve, any approvals are fine", [], {"ok": True, "tier": None, "approvals_required": 0}, "no reason raised -> no requirement", reasons=())
chk("W11", "the board tier needs three CFO-role people", [CFO, CFO | {"approver": "Ana Souza"}, FM], {"ok": False, "tier": "board", "approvals_required": 3, "problems_n": 2},
    "two CFOs valid, the finance manager's role is not allowed on this tier -> 2 < 3", total=10_000_000)
chk("W12", "the board tier satisfied", [CFO, CFO | {"approver": "Ana Souza"}, CFO | {"approver": "Kim Park"}], {"ok": True, "approved_by": ["Omar Reyes", "Ana Souza", "Kim Park"]},
    "three different CFO-role people", total=10_000_000)
chk("W13", "no matrix: the default tier takes one finance manager", [FM], {"ok": True, "tier": "default", "approvals_required": 1}, "approver_roles decides", matrix=[])
chk("W14", "no matrix: a CFO is not in approver_roles", [CFO], {"ok": False, "first_problem_contains": "may not approve"}, "the default tier allows only approver_roles", matrix=[])
chk("W15", "a terms digest missing on the approval is accepted (the executor binds it)", [FM | {"terms_digest": None}, CFO], {"ok": True},
    "an approval without a digest is not 'different terms'; the CLI and API always set one")

OUT.write_text(json.dumps({"_note": "Approval-matrix ground truth, hand-derived; see scripts/make_approvals_groundtruth.py.", "cases": C}, indent=1), encoding="utf-8")
print(f"{len(C)} approval cases -> {OUT}")
