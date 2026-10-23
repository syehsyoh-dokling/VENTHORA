package venthora.treasury

# Mirrors src/venthora/proofcore/policy.py. ProofCore requires both to agree.

import rego.v1

full := input.autonomous_conversion_limit_minor

# Autonomous FX conversion limit tied to forecast confidence (Kit 01).
conversion_limit := full if input.forecast_confidence_bp == null

conversion_limit := full if {
	input.forecast_confidence_bp != null
	input.forecast_confidence_bp >= input.full_confidence_bp
}

conversion_limit := 0 if {
	input.forecast_confidence_bp != null
	input.forecast_confidence_bp <= input.zero_limit_below_bp
}

conversion_limit := floor((full * (input.forecast_confidence_bp - input.zero_limit_below_bp)) / (input.full_confidence_bp - input.zero_limit_below_bp)) if {
	input.forecast_confidence_bp != null
	input.forecast_confidence_bp > input.zero_limit_below_bp
	input.forecast_confidence_bp < input.full_confidence_bp
}

approval_reasons contains "conversion_above_autonomous_limit" if {
	input.conversion_base_minor > conversion_limit
}

# The simulated probability of breaching the reserve within the window must stay within tolerance.
approval_reasons contains "reserve_breach_risk_above_tolerance" if {
	input.breach_probability_bp != null
	input.breach_probability_bp > input.max_breach_probability_bp
}

# Budget · mandate (src/venthora/proofcore/budget.py + policy.budget_reasons). Signed bypass lines are exempt.
approval_reasons contains "budget_line_exceeded" if {
	input.budget.attached
	some ln in input.budget.lines
	not ln.bypass_signed
	ln.used_minor > ln.mandate_minor
}

approval_reasons contains "budget_line_above_auto_share" if {
	input.budget.attached
	some ln in input.budget.lines
	not ln.bypass_signed
	ln.used_minor * 10000 > ln.mandate_minor * ln.auto_share_bp
}

approval_reasons contains "budget_line_alert_requires_approval" if {
	input.budget.attached
	some ln in input.budget.lines
	not ln.bypass_signed
	ln.require_approval_on_alert
	ln.used_minor > 0
	ln.used_minor * 10000 >= ln.mandate_minor * ln.alert_share_bp
}

approval_reasons contains "budget_bypass_unsigned" if {
	input.budget.attached
	some ln in input.budget.lines
	not ln.bypass_signed
	ln.bypass
	ln.paying_now_minor > 0
}

approval_reasons contains "unbudgeted_obligation" if {
	input.budget.attached
	input.budget.unbudgeted_paying > 0
	input.budget.unbudgeted_requires_approval
}

# Three-way match (proofcore/match.py): an invoice claiming more than contract − paid so far, beyond tolerance.
approval_reasons contains "amount_mismatch" if {
	count(input.paying_over_claims) > 0
}

# Period cut-off: spending on a closed period needs a person (the cut-off report was issued).
approval_reasons contains "period_closed" if {
	input.budget.attached
	input.budget.period_closed
	input.budget.paying_in_period > 0
}

decision := {
	"allow": input.actor_role != "viewer",
	"requires_approval": sort([r | some r in approval_reasons]),
	"conversion_limit_minor": conversion_limit,
	"escalations": input.escalations,
}
