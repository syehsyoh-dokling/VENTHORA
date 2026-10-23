# ruff: noqa: E501
"""Invoice aging and dunning: how old what we owe is, who has been reminding us, and what lateness costs.

Per payable obligation, as of the book's date (state.as_of) and its due date (due_date, else as_of + due_in_hours):
  days overdue   as_of − due_date (0 when not yet due); bucket current | 1-30 | 31-60 | 61-90 | 90+
  late interest  accrued = base × rate × days_overdue / 365 (the obligation's late_interest_bp, else the policy default);
                 projected = base × rate × horizon_days / 365 for an overdue obligation the plan leaves unpaid
Per payee: the bucketed totals (base currency), the oldest item, the highest dunning level received.
Reasons (mirrored in policy/treasury.rego):
  overdue_obligation_unpaid           overdue beyond overdue_escalation_days and not paid by this plan
  final_notice_unpaid                 dunning level at or above dunning_escalation_level and not paid by this plan
  late_interest_exposure_above_limit  accrued + projected late interest on the unpaid overdue items > max_late_interest_exposure
A rejected obligation (duplicate, settled, unverified) owes nothing and is left out; an escalated or deferred one is the exposure.
"""
from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

from .domain.models import ObligationDecision, TreasuryState

PAYING = ("PAY", "CONVERT_AND_PAY")
BUCKETS = ("current", "1-30", "31-60", "61-90", "90+")


def bucket_of(days_overdue: int) -> str:
    if days_overdue <= 0:
        return "current"
    if days_overdue <= 30:
        return "1-30"
    if days_overdue <= 60:
        return "31-60"
    if days_overdue <= 90:
        return "61-90"
    return "90+"


def to_base(amount_minor: int, currency: str, base: str, fx_to_base: dict[str, str]) -> int:
    if currency == base:
        return amount_minor
    rate = Decimal(str(fx_to_base[currency]))
    return int((Decimal(amount_minor) * rate).to_integral_value(rounding=ROUND_HALF_UP))


def interest(base_minor: int, rate_bp: int, days: int) -> int:
    if days <= 0 or rate_bp <= 0:
        return 0
    return int((Decimal(base_minor) * Decimal(rate_bp) * Decimal(days) / Decimal(10_000 * 365)).to_integral_value(rounding=ROUND_HALF_UP))


def age(obligations: list[dict], *, as_of: str | None, base: str, fx_to_base: dict[str, str], policy: dict,
        actions: dict[str, str]) -> dict:
    """Pure core on plain dicts. obligations: {obligation_id, beneficiary_id, amount_minor, currency, due_date?, due_in_hours?,
    record_kind?, dunning_level?, late_interest_bp?}; actions: obligation_id -> PAY | CONVERT_AND_PAY | DEFER | ESCALATE | REJECT."""
    day = dt.date.fromisoformat(as_of[:10]) if as_of else None
    horizon_days = -(-int(policy.get("planning_horizon_hours", 72)) // 24)
    default_rate = int(policy.get("late_interest_bp_default", 800))
    rows = []
    for o in obligations:
        if o.get("record_kind", "payable") != "payable":
            continue
        action = actions.get(o["obligation_id"], "DEFER")
        if action == "REJECT":
            continue
        due = o.get("due_date")
        if due is None and day is not None:
            due = (day + dt.timedelta(hours=int(o.get("due_in_hours", 0)))).isoformat()
        days_overdue = (day - dt.date.fromisoformat(due)).days if (day is not None and due) else 0
        days_overdue = max(0, days_overdue)
        base_minor = to_base(o["amount_minor"], o["currency"], base, fx_to_base)
        rate = o.get("late_interest_bp")
        rate = default_rate if rate is None else int(rate)
        paying = action in PAYING
        accrued = interest(base_minor, rate, days_overdue)
        projected = 0 if (paying or days_overdue == 0) else interest(base_minor, rate, horizon_days)
        rows.append({"obligation_id": o["obligation_id"], "beneficiary_id": o["beneficiary_id"], "amount_minor": o["amount_minor"],
                     "currency": o["currency"], "base_minor": base_minor, "due_date": due, "days_overdue": days_overdue,
                     "bucket": bucket_of(days_overdue), "action": action, "paying": paying,
                     "dunning_level": int(o.get("dunning_level", 0)), "late_interest_bp": rate,
                     "accrued_interest_minor": accrued, "projected_interest_minor": projected})
    payees: dict[str, dict] = {}
    for r in rows:
        p = payees.setdefault(r["beneficiary_id"], {"beneficiary_id": r["beneficiary_id"], "buckets": dict.fromkeys(BUCKETS, 0),
                                                    "overdue_minor": 0, "oldest_days": 0, "dunning_level": 0, "items": 0})
        p["buckets"][r["bucket"]] += r["base_minor"]
        p["items"] += 1
        if r["days_overdue"] > 0:
            p["overdue_minor"] += r["base_minor"]
        p["oldest_days"] = max(p["oldest_days"], r["days_overdue"])
        p["dunning_level"] = max(p["dunning_level"], r["dunning_level"])
    unpaid_overdue = [r for r in rows if r["days_overdue"] > 0 and not r["paying"]]
    exposure = sum(r["accrued_interest_minor"] + r["projected_interest_minor"] for r in unpaid_overdue)
    return {"as_of": as_of, "base_currency": base, "horizon_days": horizon_days, "rows": rows,
            "payees": sorted(payees.values(), key=lambda p: p["beneficiary_id"]),
            "overdue_total_minor": sum(r["base_minor"] for r in rows if r["days_overdue"] > 0),
            "unpaid_overdue_minor": sum(r["base_minor"] for r in unpaid_overdue),
            "accrued_interest_minor": sum(r["accrued_interest_minor"] for r in rows),
            "exposure_minor": exposure,
            "overdue_escalation_days": policy.get("overdue_escalation_days"),
            "dunning_escalation_level": policy.get("dunning_escalation_level"),
            "max_late_interest_exposure_minor": policy.get("max_late_interest_exposure_minor")}


def aging_reasons(f: dict) -> list[str]:
    """Mirrored one-to-one in policy/treasury.rego."""
    reasons = set()
    esc_days, esc_level, cap = f.get("overdue_escalation_days"), f.get("dunning_escalation_level"), f.get("max_late_interest_exposure_minor")
    for r in f.get("rows", []):
        if r["paying"]:
            continue
        if esc_days is not None and r["days_overdue"] > esc_days:
            reasons.add("overdue_obligation_unpaid")
        if esc_level is not None and r["dunning_level"] >= esc_level:
            reasons.add("final_notice_unpaid")
    if cap is not None and f.get("exposure_minor", 0) > cap:
        reasons.add("late_interest_exposure_above_limit")
    return sorted(reasons)


def active(f: dict) -> bool:
    return (f.get("overdue_escalation_days") is not None or f.get("dunning_escalation_level") is not None
            or f.get("max_late_interest_exposure_minor") is not None or f.get("overdue_total_minor", 0) > 0
            or any(r["dunning_level"] for r in f.get("rows", [])))


def aging_facts(state: TreasuryState, decisions: list[ObligationDecision]) -> dict:
    actions = {d.obligation_id: d.action for d in decisions}
    obligations = [o.model_dump() for o in state.obligations]
    policy = {"planning_horizon_hours": state.policy.planning_horizon_hours, "late_interest_bp_default": state.policy.late_interest_bp_default,
              "overdue_escalation_days": state.policy.overdue_escalation_days, "dunning_escalation_level": state.policy.dunning_escalation_level,
              "max_late_interest_exposure_minor": state.policy.max_late_interest_exposure_minor}
    return age(obligations, as_of=state.as_of, base=state.base_currency, fx_to_base=dict(state.fx_to_base), policy=policy, actions=actions)


def evaluate(inp: dict) -> dict:
    """Pure: {'as_of', 'base_currency'?, 'fx_to_base'?, 'policy'?, 'obligations': [...], 'actions': {id: action}} -> the aging table,
    the exposure and the reasons. Used by ground truth and the form."""
    f = age(list(inp.get("obligations") or []), as_of=inp.get("as_of"), base=inp.get("base_currency", "USD"),
            fx_to_base=dict(inp.get("fx_to_base") or {}), policy=dict(inp.get("policy") or {}), actions=dict(inp.get("actions") or {}))
    reasons = aging_reasons(f)
    buckets = {r["obligation_id"]: r["bucket"] for r in f["rows"]}
    return f | {"reasons": reasons, "buckets": buckets, "days_overdue": {r["obligation_id"]: r["days_overdue"] for r in f["rows"]},
                "accrued": {r["obligation_id"]: r["accrued_interest_minor"] for r in f["rows"]},
                "projected": {r["obligation_id"]: r["projected_interest_minor"] for r in f["rows"]},
                "payee_overdue": {p["beneficiary_id"]: p["overdue_minor"] for p in f["payees"]},
                "payee_oldest": {p["beneficiary_id"]: p["oldest_days"] for p in f["payees"]}}


def describe(f: dict, reasons: list[str]) -> str:
    bits = []
    overdue = [r for r in f["rows"] if r["days_overdue"] > 0]
    if overdue:
        bits.append(f"{len(overdue)} overdue item(s), {f['overdue_total_minor']} {f['base_currency']} ("
                    + ", ".join(f"{r['obligation_id']} {r['days_overdue']}d" + (f" L{r['dunning_level']}" if r["dunning_level"] else "") + (" paid now" if r["paying"] else "")
                                for r in overdue) + ")")
    if f["accrued_interest_minor"] or f["exposure_minor"]:
        bits.append(f"late interest accrued {f['accrued_interest_minor']}, exposure if left unpaid {f['exposure_minor']}")
    if "overdue_obligation_unpaid" in reasons:
        bits.append(f"overdue beyond {f['overdue_escalation_days']}d and still unpaid: " + ", ".join(
            r["obligation_id"] for r in f["rows"] if not r["paying"] and r["days_overdue"] > f["overdue_escalation_days"]))
    if "final_notice_unpaid" in reasons:
        bits.append("dunning level reached and unpaid: " + ", ".join(
            f"{r['obligation_id']} L{r['dunning_level']}" for r in f["rows"] if not r["paying"] and r["dunning_level"] >= f["dunning_escalation_level"]))
    if "late_interest_exposure_above_limit" in reasons:
        bits.append(f"exposure {f['exposure_minor']} > {f['max_late_interest_exposure_minor']}")
    return "; ".join(bits) if bits else "nothing overdue, no reminders"
