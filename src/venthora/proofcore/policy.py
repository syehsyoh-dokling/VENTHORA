"""Company treasury policy, evaluated in code. Mirrored 1:1 by policy/treasury.rego.

* The autonomous FX conversion limit is tied to forecast confidence (Kit 01): full when bank
  and invoice data support the receipt forecast, linear down to zero when an email contradicts
  it, so a person must approve the same conversion.
* Escalations (policy exceptions, unfunded obligations) go to a person but do not stop the
  rest of a safe plan from executing.
"""
from __future__ import annotations


def forecast_confidence(state) -> int | None:
    """The weakest receipt forecast still open; None when the plan relies on no receipt."""
    return min((r.confidence_bp for r in state.receivables), default=None)


def policy_input(state, conversion_base_minor: int, escalations: list[str],
                 breach_probability_bp: int | None = None, budget: dict | None = None,
                 paying_over_claims: list[str] | None = None, limits: dict | None = None,
                 payees: dict | None = None) -> dict:
    p = state.policy
    return {
        "paying_over_claims": sorted(paying_over_claims or []),
        "limits": limits or {},
        "payees": payees or {"attached": False, "rows": []},
        "budget": budget or {"attached": False, "lines": [], "unbudgeted_paying": 0, "unbudgeted_requires_approval": False},
        "actor_role": state.actor_role,
        "approver_roles": p.approver_roles,
        "conversion_base_minor": conversion_base_minor,
        "forecast_confidence_bp": forecast_confidence(state),
        "autonomous_conversion_limit_minor": p.autonomous_conversion_limit_minor,
        "full_confidence_bp": p.full_confidence_bp,
        "zero_limit_below_bp": p.zero_limit_below_bp,
        "escalations": sorted(escalations),
        "breach_probability_bp": breach_probability_bp,
        "max_breach_probability_bp": p.max_breach_probability_bp,
    }


def conversion_limit(inp: dict) -> int:
    full, c = inp["autonomous_conversion_limit_minor"], inp["forecast_confidence_bp"]
    hi, lo = inp["full_confidence_bp"], inp["zero_limit_below_bp"]
    if c is None or c >= hi:
        return full
    if c <= lo:
        return 0
    return full * (c - lo) // (hi - lo)


def evaluate(inp: dict) -> dict:
    """Return {"allow", "requires_approval": [reason codes], "conversion_limit_minor", "escalations"}."""
    limit = conversion_limit(inp)
    reasons = ["conversion_above_autonomous_limit"] if inp["conversion_base_minor"] > limit else []
    breach = inp.get("breach_probability_bp")
    if breach is not None and breach > inp["max_breach_probability_bp"]:
        reasons.append("reserve_breach_risk_above_tolerance")
    reasons += budget_reasons(inp.get("budget") or {})
    from .limits import limit_reasons

    reasons += limit_reasons(inp.get("limits") or {})
    from ..payees import payee_reasons

    reasons += payee_reasons(inp.get("payees") or {})
    # Three-way match: an invoice that claims more than the contract still allows is paid only by a person.
    if inp.get("paying_over_claims"):
        reasons.append("amount_mismatch")
    # Period cut-off: a plan drawn against a closed period cannot spend on it.
    if (inp.get("budget") or {}).get("period_closed") and (inp.get("budget") or {}).get("paying_in_period", 0) > 0:
        reasons.append("period_closed")
    return {"allow": inp["actor_role"] != "viewer", "requires_approval": sorted(set(reasons)),
            "conversion_limit_minor": limit, "escalations": inp["escalations"]}


def budget_reasons(b: dict) -> list[str]:
    """Budget · mandate, from the numbers (mirrored in Rego): a line paid beyond its autonomous share, beyond its
    mandate, past an alert threshold that demands a person, a bypass nobody signed, an obligation outside every line."""
    if not b.get("attached"):
        return []
    reasons = []
    for ln in b["lines"]:
        if ln["bypass_signed"]:
            continue
        mandate, used = ln["mandate_minor"], ln["used_minor"]
        if used > mandate:
            reasons.append("budget_line_exceeded")
        if used * 10_000 > mandate * ln["auto_share_bp"]:
            reasons.append("budget_line_above_auto_share")
        if ln["require_approval_on_alert"] and used > 0 and used * 10_000 >= mandate * ln["alert_share_bp"]:
            reasons.append("budget_line_alert_requires_approval")
        if ln["bypass"] and ln["paying_now_minor"] > 0:
            reasons.append("budget_bypass_unsigned")
    if b.get("unbudgeted_paying", 0) > 0 and b.get("unbudgeted_requires_approval"):
        reasons.append("unbudgeted_obligation")
    return sorted(set(reasons))
