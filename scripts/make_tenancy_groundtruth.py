# ruff: noqa: E501
"""Write data/groundtruth/tenancy.json: book and tenant segregation with hand-derived answers (src/venthora/tenancy.py).

Three kinds of case. `plan`: a treasury state carrying its resolved tenancy (what the registry said about the book), judged by the
gate: an unknown book under a strict registry is BLOCKED; a payee the tenant did not allow is escalated and never paid; the book's
policy overrides are already in the policy. `tenancy`: pure resolution of a book id against a registry (exact beats pattern, longest
pattern wins, default tenant, strict, invalid override). `approvals`: the executor's check with the book's named approvers.
Cash is ample (USD 100,000, floor 5,000) unless an override says otherwise.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "groundtruth" / "tenancy.json"
POLICY = {"reserve_floor_minor": 500_000, "autonomous_conversion_limit_minor": 100_000_000, "swift_fee_currency": "EUR",
          "swift_fee_minor": 1285, "planning_horizon_hours": 72}
REG = {"strict": False, "default_tenant": "lab",
       "tenants": {"acme": {"name": "Acme", "books": {
                       "ops-us": {"allowed_payees": ["ben-saas-vendor", "payroll-group-us"], "approvers": [{"name": "Dana Finch", "role": "finance_manager"}], "policy_overrides": {"max_single_payment_minor": 50_000}},
                       "ops-*": {"allowed_payees": ["ben-saas-vendor"], "approvers": [], "policy_overrides": {}},
                       "ops-eu-*": {"allowed_payees": ["ben-eu-components"], "approvers": [{"name": "Lena Vogt", "role": "finance_manager"}], "policy_overrides": {"reserve_floor_minor": 800_000}}}},
                   "lab": {"name": "Lab", "books": {"*": {"allowed_payees": ["*"], "approvers": [], "policy_overrides": {}}}}}}
STRICT = {"strict": True, "tenants": {"acme": REG["tenants"]["acme"]}}  # no catch-all tenant


def ob(i, amount=100_000, ben="ben-saas-vendor", ccy="USD"):
    return {"obligation_id": f"O{i}", "amount_minor": amount, "currency": ccy, "priority": 5, "due_in_hours": 24,
            "beneficiary_id": ben, "payee_name": f"Payee {i}", "payout_method": "LOCAL" if ccy == "USD" else "SWIFT",
            "evidence_ref": f"ev-O{i}", "invoice_fingerprint": f"fp-O{i}"}


def ev(i):
    return {"ref": f"ev-O{i}", "subject_id": f"O{i}", "source": "ap:invoice", "status": "verified", "summary": f"inv {i}"}


def tenancy(tenant, book, allowed, approvers=(), overrides=None, known=True, strict=False, matched="exact"):
    cfg = {"tenant": tenant, "book_id": book, "allowed_payees": allowed, "approvers": list(approvers), "policy_overrides": overrides or {}} if known else None
    return {"known": known, "strict": strict, "config": cfg, "matched_by": matched if known else None}


def state(obligations, ten, *, book="ops-us", policy=None, usd=10_000_000, role="treasury_agent"):
    obs = list(obligations)
    s = {"case_id": "tenancy", "book_id": book, "base_currency": "USD", "balances": {"USD": usd, "EUR": 0, "GBP": 0},
         "fx_to_base": {"EUR": "1.0850", "GBP": "1.2700"}, "obligations": obs, "receivables": [],
         "evidence": [ev(int(o["obligation_id"][1:])) for o in obs], "scenarios": [], "policy": POLICY | (policy or {}), "actor_role": role}
    if ten is not None:
        s["tenancy"] = ten
    return s


C = []


def add(id_, kind, title, inp, expect, derivation, pain="whose book, which payees, whose signature, which policy"):
    C.append({"id": id_, "kind": kind, "level": 4, "pain": pain, "title": title, "input": inp, "expect": expect, "derivation": derivation})


OPS = tenancy("acme", "ops-us", ["ben-saas-vendor", "payroll-group-us"], [{"name": "Dana Finch", "role": "finance_manager"}])
add("T01", "plan", "the book pays one of its own payees", state([ob(1)], OPS), {"decision": "APPROVED", "actions": {"O1": "PAY"}, "tenant": "acme", "book_known": True, "disallowed": []},
    "ben-saas-vendor is on ops-us's list -> paid")
add("T02", "plan", "a payee of another tenant is escalated, never paid", state([ob(1, ben="ben-lumen-design")], OPS),
    {"decision": "APPROVED", "actions": {"O1": "ESCALATE"}, "paid": [], "disallowed": ["O1"]}, "ben-lumen-design is not a payee of ops-us -> policy exception -> ESCALATE; no approval can pay it")
add("T03", "plan", "one allowed, one not, in one plan", state([ob(1), ob(2, ben="ben-eu-components")], OPS),
    {"decision": "APPROVED", "actions": {"O1": "PAY", "O2": "ESCALATE"}, "paid": ["O1"]}, "segregation is per payout; the allowed one proceeds")
add("T04", "plan", "a wildcard book pays anyone", state([ob(1, ben="ben-lumen-design")], tenancy("lab", "*", ["*"], matched="default")),
    {"decision": "APPROVED", "actions": {"O1": "PAY"}, "tenant": "lab"}, "['*'] allows every payee")
add("T05", "plan", "an unknown book under a strict registry is blocked", state([ob(1)], tenancy(None, None, None, known=False, strict=True), book="ghost-book"),
    {"decision": "BLOCKED", "book_known": False, "failed_stages": ["Policy", "Tenancy · book"]}, "no tenant owns ghost-book and the registry is strict -> nothing may be spent from it")
add("T06", "plan", "no tenancy attached: the control is off", state([ob(1, ben="ben-lumen-design")], None), {"decision": "APPROVED", "actions": {"O1": "PAY"}, "tenant": None}, "plain plans and older suites unchanged")
add("T07", "plan", "the book's policy override lowers the single-payment limit", state([ob(1, 60_000)], OPS, policy={"max_single_payment_minor": 50_000}),
    {"decision": "REQUIRES_APPROVAL", "requires": ["single_payment_above_limit"]}, "ops-us overrides max_single_payment to USD 500 (attach merged it into the policy); USD 600 > 500")
add("T08", "plan", "the book's override raises the reserve floor", state([ob(1, 300_000)], tenancy("acme", "ops-eu-*", ["ben-saas-vendor"], overrides={"reserve_floor_minor": 800_000}, matched="pattern:ops-eu-*"),
                                                                      book="ops-eu-1", policy={"reserve_floor_minor": 800_000}, usd=1_000_000),
    {"decision": "APPROVED", "actions": {"O1": "ESCALATE"}}, "USD 10,000 − floor 8,000 = 2,000 < 3,000 -> cannot be funded under this book's floor (5,000 would have paid it)")
add("T09", "plan", "a viewer: blocked before any tenancy rule", state([ob(1)], OPS, role="viewer"), {"decision": "BLOCKED"}, "role first")

# -- resolution ---------------------------------------------------------------------------------------------------------------
def res(id_, title, book, expect, derivation, registry=REG, check=()):
    add(id_, "tenancy", title, {"registry": registry, "book_id": book, "check_payees": list(check)}, expect, derivation)


res("U01", "an exact book id", "ops-us", {"known": True, "tenant": "acme", "matched_by": "exact", "allowed_payees": ["ben-saas-vendor", "payroll-group-us"]}, "exact entry")
res("U02", "exact beats pattern", "ops-us", {"matched_by": "exact", "policy_overrides": {"max_single_payment_minor": 50_000}}, "ops-us also matches ops-*; the exact entry wins")
res("U03", "the longest matching pattern wins", "ops-eu-7", {"matched_by": "pattern:ops-eu-*", "allowed_payees": ["ben-eu-components"], "approvers": [{"name": "Lena Vogt", "role": "finance_manager"}]},
    "ops-* and ops-eu-* both match; ops-eu-* is longer")
res("U04", "a shorter pattern for the rest", "ops-apac", {"matched_by": "pattern:ops-*", "allowed_payees": ["ben-saas-vendor"]}, "only ops-* matches")
res("U05", "an unknown book goes to the default tenant", "random-book", {"known": True, "tenant": "lab", "matched_by": "default", "allowed_payees": ["*"]}, "strict false -> lab")
res("U06", "an unknown book under a strict registry", "random-book", {"known": False, "tenant": None, "matched_by": None}, "strict and no catch-all pattern -> unknown", registry=STRICT)
res("U07", "a known book under a strict registry", "ops-us", {"known": True, "tenant": "acme", "strict": True}, "strict changes nothing for known books", registry=STRICT)
res("U08", "allows() per payee", "ops-us", {"allows": {"ben-saas-vendor": True, "ben-lumen-design": False}}, "list membership", check=("ben-saas-vendor", "ben-lumen-design"))
res("U09", "an override of a policy field that does not exist is refused", "bad-book",
    {"known": False, "error_contains": "unknown policy fields"}, "the registry is validated against TreasuryPolicy",
    registry={"strict": True, "tenants": {"x": {"books": {"bad-book": {"policy_overrides": {"max_single_paymnt_minor": 1}}}}}})
res("U10", "no tenants at all, strict", "any", {"known": False}, "nothing to resolve against", registry={"strict": True, "tenants": {}})

# -- the book's approvers ------------------------------------------------------------------------------------------------------
D = "c" * 64
MATRIX = [{"name": "one", "min_total_minor": 0, "reasons": [], "approvals_required": 1, "roles": ["finance_manager", "cfo"]}]


def apr(id_, title, approvals, book_approvers, expect, derivation):
    add(id_, "approvals", title, {"approval_matrix": MATRIX, "approver_roles": ["finance_manager"], "reasons": ["single_payment_above_limit"], "paying_total_minor": 100_000,
                                  "approvals": approvals, "requested_by": "agent", "terms_digest": D, "book_approvers": book_approvers}, expect, derivation)


apr("J01", "the book's named approver", [{"approver": "Dana Finch", "role": "finance_manager", "terms_digest": D}], [{"name": "Dana Finch", "role": "finance_manager"}],
    {"ok": True, "approved_by": ["Dana Finch"]}, "name and role match the registry")
apr("J02", "right role, not this book's person", [{"approver": "Pat Lee", "role": "finance_manager", "terms_digest": D}], [{"name": "Dana Finch", "role": "finance_manager"}],
    {"ok": False, "first_problem_contains": "not an approver of this book"}, "a finance manager of another tenant cannot sign this book")
apr("J03", "the right person in the wrong role", [{"approver": "Dana Finch", "role": "cfo", "terms_digest": D}], [{"name": "Dana Finch", "role": "finance_manager"}],
    {"ok": False, "first_problem_contains": "not an approver of this book"}, "the registry names (person, role) pairs")
apr("J04", "no book approvers configured: any approver of the right role", [{"approver": "Pat Lee", "role": "finance_manager", "terms_digest": D}], [],
    {"ok": True}, "an empty list defers to the matrix")
apr("J05", "names are compared normalised", [{"approver": "  dana   FINCH ", "role": "finance_manager", "terms_digest": D}], [{"name": "Dana Finch", "role": "finance_manager"}],
    {"ok": True, "approved_by": ["  dana   FINCH "]}, "case and spacing do not matter")

OUT.write_text(json.dumps({"_note": "Book and tenant segregation ground truth, hand-derived; see scripts/make_tenancy_groundtruth.py.", "cases": C}, indent=1), encoding="utf-8")
print(f"{len(C)} tenancy cases -> {OUT}")
