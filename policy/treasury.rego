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

# Limits and velocity (proofcore/limits.py). A null limit means no limit.
approval_reasons contains "single_payment_above_limit" if {
	input.limits.max_single_payment_minor != null
	some r in input.limits.payouts
	r.cost_minor > input.limits.max_single_payment_minor
}

approval_reasons contains "first_payment_to_new_payee_above_limit" if {
	input.limits.max_first_payment_minor != null
	some r in input.limits.payouts
	r.new_payee
	r.cost_minor > input.limits.max_first_payment_minor
}

approval_reasons contains "payee_period_limit_exceeded" if {
	input.limits.max_per_payee_period_minor != null
	some p in input.limits.payees
	p.total_minor > input.limits.max_per_payee_period_minor
}

approval_reasons contains "daily_outflow_limit_exceeded" if {
	input.limits.max_outflow_daily_minor != null
	input.limits.outflow_24h_minor + input.limits.paying_now_total_minor > input.limits.max_outflow_daily_minor
}

approval_reasons contains "weekly_outflow_limit_exceeded" if {
	input.limits.max_outflow_weekly_minor != null
	input.limits.outflow_7d_minor + input.limits.paying_now_total_minor > input.limits.max_outflow_weekly_minor
}

# Payee lifecycle (src/venthora/payees.py). NEW / CHANGED / SUSPENDED payees never reach a payout (Verity escalates them);
# a VERIFIED payee is paid by a person inside the cooling period after a bank change, and until its first PAID payout.
approval_reasons contains "payee_in_cooling_period" if {
	input.payees.attached
	some r in input.payees.rows
	r.status == "VERIFIED"
	r.in_cooling
}

approval_reasons contains "payee_not_yet_active" if {
	input.payees.attached
	some r in input.payees.rows
	r.status == "VERIFIED"
	not r.in_cooling
}

# FX exposure (proofcore/fx_exposure.py). A null cap / ratio does not apply; a currency with nothing due has nothing to hedge.
approval_reasons contains "open_position_above_cap" if {
	some r in input.fx.rows
	r.cap_minor != null
	r.position_after_minor > r.cap_minor
}

approval_reasons contains "hedge_ratio_below_minimum" if {
	input.fx.min_hedge_ratio_bp != null
	some r in input.fx.rows
	r.coverage_bp != null
	r.coverage_bp < input.fx.min_hedge_ratio_bp
}

approval_reasons contains "daily_conversion_cap_exceeded" if {
	input.fx.max_conversion_daily_minor != null
	input.fx.converted_24h_minor + input.fx.converting_now_minor > input.fx.max_conversion_daily_minor
}

# Approval matrix (proofcore/approvals.py): the last applicable tier decides; a tier applies when the paying total reaches
# its min_total_minor or when one of its reason codes was raised; no tier: one approver from approver_roles; no reason: null.
tier_applies(t) if {
	t.min_total_minor != null
	input.paying_total_minor >= t.min_total_minor
}

tier_applies(t) if {
	some r in t.reasons
	approval_reasons[r]
}

matching_tiers contains i if {
	some i
	tier_applies(input.approval_matrix[i])
}

approval := null if {
	count(approval_reasons) == 0
}

approval := {"tier": t.name, "approvals_required": t.approvals_required, "roles": t.roles, "distinct_people": t.approvals_required > 1} if {
	count(approval_reasons) > 0
	count(matching_tiers) > 0
	t := input.approval_matrix[max(matching_tiers)]
}

approval := {"tier": "default", "approvals_required": 1, "roles": input.approver_roles, "distinct_people": false} if {
	count(approval_reasons) > 0
	count(matching_tiers) == 0
}

decision := {
	"allow": input.actor_role != "viewer",
	"requires_approval": sort([r | some r in approval_reasons]),
	"conversion_limit_minor": conversion_limit,
	"escalations": input.escalations,
	"approval": approval,
}
