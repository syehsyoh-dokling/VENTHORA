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
    carry_forward, late_posted = [], []
    per_line: dict[str, dict] = {ln.line: {"paying": 0, "paying_ids": [], "owed": 0, "owed_ids": []} for ln in b.lines}
    paying_in_period = 0
    for d in decisions:
        o = by_id.get(d.obligation_id)
        if o is None or d.action == "REJECT":
            continue
        # Period cut-off: an obligation due after the period's end belongs to the next period (its commitment carries
        # forward and does not draw on this period's lines); one due before the period started is late-posted.
        placement = place_in_period(state, o)
        if placement == "next":
            carry_forward.append({"obligation_id": o.obligation_id, "line": o.budget_line, "due_date": due_date_of(state, o),
                                  "base_cost_minor": d.base_cost_minor, "action": d.action})
            continue
        if placement == "previous":
            late_posted.append({"obligation_id": o.obligation_id, "line": o.budget_line, "due_date": due_date_of(state, o),
                                "base_cost_minor": d.base_cost_minor, "action": d.action})
        if d.action in PAYING:
            paying_in_period += d.base_cost_minor
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
    if b.closed and paying_in_period:
        alerts.append({"line": "*", "utilisation_bp": 0, "alert_share_bp": 0, "requires_approval": True,
                       "message": f"period {b.period} is closed (cut-off report issued); paying against it needs a person"})
    return {"attached": True, "period": b.period, "period_start": b.period_start, "period_end": b.period_end,
            "closed": b.closed, "as_of": state.as_of, "lines": rows, "alerts": alerts, "requests": requests,
            "unbudgeted": unbudgeted, "carry_forward": carry_forward, "late_posted": late_posted,
            "paying_in_period_minor": paying_in_period, "problems": problems}


def due_date_of(state: TreasuryState, o) -> str | None:
    if o.due_date:
        return o.due_date
    if state.as_of:
        import datetime as dt

        return (dt.date.fromisoformat(state.as_of) + dt.timedelta(hours=o.due_in_hours)).isoformat()
    return None


def place_in_period(state: TreasuryState, o) -> str:
    """'current' | 'next' | 'previous' — by due date against the period's dates; 'current' when dates are unknown."""
    b = state.budget
    due = due_date_of(state, o)
    if b is None or due is None:
        return "current"
    if b.period_end and due > b.period_end:
        return "next"
    if b.period_start and due < b.period_start:
        return "previous"
    return "current"


def cutoff_report(state: TreasuryState, pipe: dict, realized_by_line: dict[str, int] | None = None) -> dict:
    """The period's cut-off statement: per line plan, realized up to the cut-off, open commitments, available, minus;
    what carries forward to the next period; what was posted late. Derived, never typed."""
    b = state.budget
    lines = []
    for r in pipe["lines"]:
        lines.append({"line": r["line"], "plan_minor": r["plan_minor"], "granted_minor": r["granted_minor"],
                      "realized_minor": (realized_by_line or {}).get(r["line"], r["realized_minor"]),
                      "open_commitments_minor": r["committed_minor"] + r["paying_now_minor"],
                      "available_minor": r["available_minor"], "minus_minor": r["minus_minor"], "status": r["status"]})
    return {"period": b.period, "period_start": b.period_start, "period_end": b.period_end, "as_of": state.as_of,
            "lines": lines, "carry_forward": pipe["carry_forward"], "late_posted": pipe["late_posted"],
            "carry_forward_total_minor": sum(c["base_cost_minor"] for c in pipe["carry_forward"]),
            "unbudgeted": pipe["unbudgeted"], "requests": pipe["requests"]}


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
            "unbudgeted_requires_approval": state.budget.unbudgeted_requires_approval,
            "period_closed": bool(state.budget.closed), "paying_in_period": pipe.get("paying_in_period_minor", 0)}


def _line_of(ln: BudgetLine) -> str:
    return ln.line
