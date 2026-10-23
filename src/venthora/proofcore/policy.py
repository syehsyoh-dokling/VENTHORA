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


def policy_input(state, conversion_base_minor: int, escalations: list[str]) -> dict:
    p = state.policy
    return {
        "actor_role": state.actor_role,
        "approver_roles": p.approver_roles,
        "conversion_base_minor": conversion_base_minor,
        "forecast_confidence_bp": forecast_confidence(state),
        "autonomous_conversion_limit_minor": p.autonomous_conversion_limit_minor,
        "full_confidence_bp": p.full_confidence_bp,
        "zero_limit_below_bp": p.zero_limit_below_bp,
        "escalations": sorted(escalations),
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
    return {"allow": inp["actor_role"] != "viewer", "requires_approval": reasons,
            "conversion_limit_minor": limit, "escalations": inp["escalations"]}
