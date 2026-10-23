# ruff: noqa: E501
"""Budget · mandate: the organisation's plan for the period is the agent's mandate, line by line.

For every line:  plan (+ granted) − realized (already paid this period) − paying now = available after this plan;
                 committed = open obligations on the line that are NOT paid now (still owed);
                 minus = how far realized + paying now + committed exceeds the mandate (projected overrun).
Thresholds, all decided in code and mirrored in Rego:
  utilisation > alert_share     -> ALERT (a notification; a person is required too if the line says so)
  utilisation > auto_share      -> budget_line_above_auto_share  (REQUIRES_APPROVAL)
  utilisation > 100%            -> budget_line_exceeded          (REQUIRES_APPROVAL; Theorem never proposes it)
  obligation outside every line -> unbudgeted_obligation         (REQUIRES_APPROVAL when the budget says so)
  bypass without a signature    -> budget_bypass_unsigned        (REQUIRES_APPROVAL; the cap stays)
A line that cannot fund what is still owed raises a BUDGET REQUEST: the agent asks for mandate with the exact shortfall,
the obligations behind it and their evidence. Nothing here moves money.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..config import ROOT
from ..domain.models import Budget, BudgetLine, ObligationDecision, TreasuryState

BUDGETS_FILE = ROOT / "data" / "budgets.json"
PAYING = ("PAY", "CONVERT_AND_PAY")
OWED = ("DEFER", "ESCALATE")  # still a liability after this plan


def load_budget(period: str | None = None, path: Path = BUDGETS_FILE) -> Budget | None:
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    budgets = data["budgets"]
    chosen = next((b for b in budgets if b["period"] == period), None) if period else (budgets[0] if budgets else None)
    return Budget.model_validate(chosen) if chosen else None


def attach_budget(state: TreasuryState, period: str | None = None, *, realized: dict[str, int] | None = None,
                  grants: dict[str, int] | None = None) -> TreasuryState:
    """Attach the period's budget (data/budgets.json) to a book whose obligations name budget lines, with what
    the execution store knows: realized spend per line and mandate granted by a person."""
    if state.budget is None:
        budget = load_budget(period)
        if budget is None or not any(o.budget_line for o in state.obligations):
            return state
    else:
        budget = state.budget
    lines = []
    for ln in budget.lines:
        upd = {}
        if realized and ln.line in realized:
            upd["realized_minor"] = max(ln.realized_minor, realized[ln.line])
        if grants and ln.line in grants:
            upd["granted_extra_minor"] = ln.granted_extra_minor + grants[ln.line]
        lines.append(ln.model_copy(update=upd) if upd else ln)
    return state.model_copy(update={"budget": budget.model_copy(update={"lines": lines})})


def line_caps(state: TreasuryState) -> dict[str, int | None]:
    """Theorem's hard cap per line: mandate − realized (None = uncapped: a signed bypass)."""
    if state.budget is None:
        return {}
    return {ln.line: (None if ln.bypass_signed else max(0, ln.mandate_minor - ln.realized_minor)) for ln in state.budget.lines}


def pipeline(state: TreasuryState, decisions: list[ObligationDecision]) -> dict:
    """The rows the dashboard shows and the policy judges. Base-currency minor units throughout."""
    b = state.budget
    if b is None:
        return {"attached": False, "lines": [], "alerts": [], "requests": [], "unbudgeted": [], "problems": []}
    by_id = {o.obligation_id: o for o in state.obligations}
    rows, alerts, requests, unbudgeted, problems = [], [], [], [], []
    per_line: dict[str, dict] = {ln.line: {"paying": 0, "paying_ids": [], "owed": 0, "owed_ids": []} for ln in b.lines}
    for d in decisions:
        o = by_id.get(d.obligation_id)
        if o is None or d.action == "REJECT":
            continue
        ln = b.line(o.budget_line)
        if ln is None:
            if d.action in PAYING:
                unbudgeted.append({"obligation_id": o.obligation_id, "line": o.budget_line, "base_cost_minor": d.base_cost_minor})
            continue
        bucket = per_line[ln.line]
        if d.action in PAYING:
            bucket["paying"] += d.base_cost_minor
            bucket["paying_ids"].append(o.obligation_id)
        elif d.action in OWED:
            bucket["owed"] += d.base_cost_minor
            bucket["owed_ids"].append(o.obligation_id)
    for ln in b.lines:
        bucket = per_line[ln.line]
        mandate = ln.mandate_minor
        used = ln.realized_minor + bucket["paying"]
        available = mandate - used
        minus = max(0, used + bucket["owed"] - mandate)
        util_bp = (used * 10_000) // mandate if mandate else (10_000 if used else 0)
        status = "auto"
        if ln.bypass_signed:
            status = "bypass"
        elif used > mandate:
            status = "exceeded"
            problems.append(f"line {ln.line}: paying {used} against a mandate of {mandate}")
        elif used * 10_000 > mandate * ln.auto_share_bp:
            status = "needs_approval"
        elif util_bp >= ln.alert_share_bp and used:
            status = "alert"
        if not ln.bypass_signed and mandate and util_bp >= ln.alert_share_bp and used:
            alerts.append({"line": ln.line, "utilisation_bp": util_bp, "alert_share_bp": ln.alert_share_bp,
                           "requires_approval": ln.require_approval_on_alert,
                           "message": f"budget line '{ln.line}' at {util_bp / 100:.0f}% of its mandate after this plan"
                                      + (" (approval required by the line's setting)" if ln.require_approval_on_alert else "")})
        if ln.bypass and not ln.bypass_signed and bucket["paying"]:
            alerts.append({"line": ln.line, "utilisation_bp": util_bp, "alert_share_bp": ln.alert_share_bp, "requires_approval": True,
                           "message": f"budget line '{ln.line}' is marked bypass but nobody signed it; the cap applies"})
        if minus and not ln.bypass_signed:
            requests.append({"line": ln.line, "period": b.period, "shortfall_minor": minus,
                             "obligations": bucket["owed_ids"], "mandate_minor": mandate, "realized_minor": ln.realized_minor,
                             "paying_now_minor": bucket["paying"], "committed_minor": bucket["owed"],
                             "message": f"line '{ln.line}' cannot fund what is still owed: mandate {mandate}, used {used}, "
                                        f"owed {bucket['owed']}; a grant of {minus} would cover it"})
        rows.append({"line": ln.line, "plan_minor": ln.amount_minor, "granted_minor": ln.granted_extra_minor,
                     "mandate_minor": mandate, "realized_minor": ln.realized_minor, "paying_now_minor": bucket["paying"],
                     "paying_ids": bucket["paying_ids"], "committed_minor": bucket["owed"], "committed_ids": bucket["owed_ids"],
                     "available_minor": available, "minus_minor": minus, "utilisation_bp": util_bp,
                     "auto_share_bp": ln.auto_share_bp, "alert_share_bp": ln.alert_share_bp, "bypass": ln.bypass,
                     "bypass_signed": ln.bypass_signed, "status": status})
    return {"attached": True, "period": b.period, "lines": rows, "alerts": alerts, "requests": requests,
            "unbudgeted": unbudgeted, "problems": problems}


def policy_facts(state: TreasuryState, pipe: dict) -> dict:
    """The numbers the policy (Python and Rego) reasons from."""
    if not pipe.get("attached"):
        return {"attached": False, "lines": [], "unbudgeted_paying": 0, "unbudgeted_requires_approval": False}
    return {"attached": True,
            "lines": [{"line": r["line"], "mandate_minor": r["mandate_minor"], "used_minor": r["realized_minor"] + r["paying_now_minor"],
                       "paying_now_minor": r["paying_now_minor"], "auto_share_bp": r["auto_share_bp"], "alert_share_bp": r["alert_share_bp"],
                       "require_approval_on_alert": next(ln.require_approval_on_alert for ln in state.budget.lines if ln.line == r["line"]),
                       "bypass": r["bypass"], "bypass_signed": r["bypass_signed"]} for r in pipe["lines"]],
            "unbudgeted_paying": len(pipe["unbudgeted"]),
            "unbudgeted_requires_approval": state.budget.unbudgeted_requires_approval}


def _line_of(ln: BudgetLine) -> str:
    return ln.line
