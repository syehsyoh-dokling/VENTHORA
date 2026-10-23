# ruff: noqa: E501
"""The approval matrix (four-eyes): who, and how many, must approve a plan that stopped for a person.

Requirement (mirrored in policy/treasury.rego): from the policy's tiers, the plan's paying total (base currency) and the
reason codes raised, the LAST applicable tier decides — a tier applies when the total reaches its min_total_minor or when
any of its reason codes was raised. No tier applicable: one approver from approver_roles. No reason raised: nothing.

Check (the executor, before money moves): every approval must come from an allowed role, on the plan's own terms digest;
the requester never counts; approvals_required > 1 means that many DIFFERENT people — the same person twice is one.
"""
from __future__ import annotations


def requirement(inp: dict, reasons: list[str]) -> dict | None:
    """Mirrored one-to-one in policy/treasury.rego (decision.approval)."""
    if not reasons:
        return None
    tiers = inp.get("approval_matrix") or []
    total = inp.get("paying_total_minor", 0)
    matching = [i for i, t in enumerate(tiers)
                if (t.get("min_total_minor") is not None and total >= t["min_total_minor"]) or set(t.get("reasons") or []) & set(reasons)]
    if matching:
        t = tiers[max(matching)]
        return {"tier": t["name"], "approvals_required": t["approvals_required"], "roles": list(t["roles"]),
                "distinct_people": t["approvals_required"] > 1}
    return {"tier": "default", "approvals_required": 1, "roles": list(inp.get("approver_roles") or []), "distinct_people": False}


def _person(name: str | None) -> str:
    return " ".join(str(name or "").split()).lower()


def check(req: dict | None, approvals: list[dict], requested_by: str | None, terms_digest: str | None) -> dict:
    """{'ok', 'problems', 'approved_by'}: which approvals count and why the rest do not."""
    if not req:
        return {"ok": True, "problems": [], "approved_by": [], "required": 0}
    problems: list[str] = []
    counted: dict[str, str] = {}  # person -> approver as written
    for a in approvals:
        who, role = a.get("approver"), a.get("role")
        if role not in req["roles"]:
            problems.append(f"role {role!r} of {who} may not approve this tier; allowed: {req['roles']}")
            continue
        if terms_digest and a.get("terms_digest") and a["terms_digest"] != terms_digest:
            problems.append(f"the approval by {who} was given on different terms (amount, counterparty or evidence changed)")
            continue
        if requested_by and _person(who) == _person(requested_by):
            problems.append(f"{who} requested this plan and may not approve it (four-eyes)")
            continue
        if _person(who) in counted:
            problems.append(f"{who} approved twice; one person counts once")
            continue
        counted[_person(who)] = str(who)
    need = req["approvals_required"]
    if len(counted) < need:
        problems.append(f"tier {req['tier']!r} needs {need} approval(s) from {need} different " + ("people" if need > 1 else "person")
                        + f" ({', '.join(req['roles'])}); {len(counted)} valid")
    return {"ok": len(counted) >= need, "problems": problems, "approved_by": list(counted.values()), "required": need}


def evaluate(inp: dict) -> dict:
    """Pure: {'approval_matrix', 'approver_roles', 'reasons', 'paying_total_minor', 'approvals', 'requested_by', 'terms_digest'}
    -> the requirement and the check. Used by ground truth and the form."""
    req = requirement({"approval_matrix": inp.get("approval_matrix") or [], "approver_roles": inp.get("approver_roles") or ["finance_manager"],
                       "paying_total_minor": inp.get("paying_total_minor", 0)}, list(inp.get("reasons") or []))
    out = check(req, list(inp.get("approvals") or []), inp.get("requested_by"), inp.get("terms_digest"))
    return {"requirement": req, "tier": req["tier"] if req else None, "approvals_required": req["approvals_required"] if req else 0,
            "ok": out["ok"], "problems": out["problems"], "problems_n": len(out["problems"]),
            "first_problem": out["problems"][0] if out["problems"] else None, "approved_by": out["approved_by"]}


def describe(req: dict | None) -> str:
    if not req:
        return "no approval needed"
    n = req["approvals_required"]
    return (f"tier {req['tier']}: {n} approval(s) from {n} different " + ("people" if n > 1 else "person")
            + f" ({', '.join(req['roles'])}); the requester may not approve")
