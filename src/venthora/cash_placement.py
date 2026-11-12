# ruff: noqa: E501
"""Budget allocation -> Cash placement: the cash the organisation does not need, and where a person may place it.

Needed = the reserve floor + every open commitment still owed (POs and contracts, payroll, requests, invoices
waiting; the Total Spending recap, which already holds the approved bills not yet paid) + a safety buffer on top. Money already leaving
(processing) is already out of the available balance. Idle = available − needed. Above the minimum, VENTHORA proposes
a placement (a term deposit, a money market fund, another account). With a provider from FCO Authorize → Placement
providers, the FCO's approval sends the money by Airwallex transfer to the provider's checked account, and the
withdrawal comes back into the Airwallex Global Account (placement_providers.py). Without one, a person carries the
instruction out and marks it done; it stays on the record either way.
"""
from __future__ import annotations

import datetime as dt
import uuid

DEFAULT = {"buffer_pct": 10, "min_amount_minor": 100_000, "destination": "Term deposit, 1 month", "enabled": True,
           # the FCO's combinable rules (each applies only when ticked; every ticked rule must hold)
           "horizon_months": 3,
           "use_horizon_surplus": False, "surplus_min_minor": 0,      # budget for the next N months minus their estimated spending >= X
           "use_lower_bound": False, "lower_bound_minor": 0,          # the wallet never goes below this after a placement
           "use_upper_bound": False, "upper_bound_minor": 0}          # never more than this placed in total (open placements)
STATUSES = ("PROPOSED", "APPROVED", "DONE", "CANCELLED")
OPEN = ("PROPOSED", "APPROVED", "SENT", "PLACED", "DONE", "WITHDRAWAL_REQUESTED")


def policy(org) -> dict:
    return DEFAULT | {k: v for k, v in (org.get("cash_placement", "policy") or {}).items() if k in DEFAULT}


def save_policy(org, body: dict, by: str) -> dict:
    cur = policy(org)
    pct, low = int(body.get("buffer_pct", cur["buffer_pct"])), int(body.get("min_amount_minor", cur["min_amount_minor"]))
    if not 0 <= pct <= 100:
        raise ValueError("the buffer is a percentage between 0 and 100")
    if low < 0:
        raise ValueError("the minimum cannot be negative")
    rec = {"_id": "policy", "buffer_pct": pct, "min_amount_minor": low, "destination": str(body.get("destination") or cur["destination"])[:120],
           "enabled": bool(body.get("enabled", cur["enabled"])), "updated_by": by}
    months = int(body.get("horizon_months", cur["horizon_months"]))
    if not 1 <= months <= 24:
        raise ValueError("the horizon is 1 to 24 months")
    rec["horizon_months"] = months
    for flag, amount in (("use_horizon_surplus", "surplus_min_minor"), ("use_lower_bound", "lower_bound_minor"), ("use_upper_bound", "upper_bound_minor")):
        rec[flag] = bool(body.get(flag, cur[flag]))
        rec[amount] = int(body.get(amount, cur[amount]))
        if rec[amount] < 0:
            raise ValueError("an amount cannot be negative")
    if rec["use_upper_bound"] and rec["upper_bound_minor"] <= 0:
        raise ValueError("the upper bound must be above zero when it is used")
    org._put("cash_placement", rec, by)
    return policy(org)


def compute(*, available_minor: int, reserve_floor_minor: int, bills_minor: int, commitments_minor: int, pol: dict,
            horizon: dict | None = None, placed_minor: int = 0) -> dict:
    """What must stay, what is idle, and what to place (all in the base currency, minor units). The FCO's ticked rules
    each cap or block the placement; every ticked rule must hold."""
    # Approved bills not yet paid are invoices, and every invoice is already a line of the Total Spending recap (on its
    # own, or on its PO): they are part of the commitments, never added on top. Only a bill the recap has not reached
    # yet (bills above the commitments) adds to what must stay.
    bills_extra = max(max(bills_minor, 0) - max(commitments_minor, 0), 0)
    base_need = max(reserve_floor_minor, 0) + max(commitments_minor, 0) + bills_extra
    buffer = (base_need * int(pol["buffer_pct"]) + 99) // 100  # rounded up: the buffer never comes out short
    needed = base_need + buffer
    idle = available_minor - needed
    caps, rules = [("idle cash", idle)], []
    if pol.get("use_horizon_surplus") and horizon is not None:
        surplus = horizon["budget_minor"] - horizon["spending_minor"]
        ok = surplus >= int(pol["surplus_min_minor"])
        rules.append({"rule": f"budget for the next {pol['horizon_months']} month(s) minus their estimated spending ≥ {pol['surplus_min_minor'] / 100:,.2f}",
                      "value_minor": surplus, "holds": ok})
        caps.append((f"the {pol['horizon_months']}-month surplus", surplus if ok else 0))
    if pol.get("use_lower_bound"):
        room = available_minor - int(pol["lower_bound_minor"])
        rules.append({"rule": f"the wallet stays at or above {pol['lower_bound_minor'] / 100:,.2f}", "value_minor": room, "holds": room > 0})
        caps.append(("the lower bound", room))
    if pol.get("use_upper_bound"):
        room = int(pol["upper_bound_minor"]) - placed_minor
        rules.append({"rule": f"at most {pol['upper_bound_minor'] / 100:,.2f} placed in total (now {placed_minor / 100:,.2f})", "value_minor": room, "holds": room > 0})
        caps.append(("the upper bound", room))
    limit_name, limit = min(caps, key=lambda c: c[1])
    blocked = next((r for r in rules if not r["holds"]), None)
    place = limit if pol.get("enabled") and not blocked and limit >= int(pol["min_amount_minor"]) else 0
    why = ("placement is switched off" if not pol.get("enabled") else
           f"blocked by the rule: {blocked['rule']}" if blocked else
           "nothing idle: what is held does not cover what is needed" if idle <= 0 else
           f"below the minimum to place (limited by {limit_name})" if limit < int(pol["min_amount_minor"]) else
           f"a placement is proposed (limited by {limit_name})")
    return {"available_minor": available_minor, "reserve_floor_minor": reserve_floor_minor, "bills_minor": bills_minor,
            "commitments_minor": commitments_minor, "bills_extra_minor": bills_extra, "buffer_minor": buffer, "needed_minor": needed, "idle_minor": idle,
            "place_minor": place, "why": why, "rules": rules, "horizon": horizon, "placed_minor": placed_minor}


def horizon(org, *, months: int, base: str, commitments_minor: int, to_base) -> dict:
    """The next `months` months: the budget allocated to them (each allocation spread evenly over the months it covers;
    one with no period counts whole) and their estimated spending (every open commitment, as if all fell due in the
    horizon, plus the payroll of people on payroll for those months). `to_base(minor, ccy)` converts."""
    from .budget_allocation import months_covered

    today = dt.date.today()
    want, y, m = [], today.year, today.month
    for _ in range(months):
        want.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    start = int(org.standards().get("fiscal_year_start_month", 1))
    budget = 0
    for a in org.list("allocation"):
        if a.get("no_limit"):
            continue
        cov = months_covered(a, start)
        share = a.get("amount_minor", 0) if cov is None else a.get("amount_minor", 0) * len(set(cov) & set(want)) // max(len(cov), 1)
        budget += to_base(share, a.get("currency") or base)
    payroll = sum(to_base(u.get("salary_minor") or 0, u.get("salary_currency") or base) for u in org.list("user")
                  if u.get("active", True) and u.get("salary_minor")) * months
    return {"months": want, "budget_minor": budget, "commitments_minor": commitments_minor, "payroll_minor": payroll,
            "spending_minor": commitments_minor + payroll}


def placed(org) -> int:
    """What is placed now (or on its way), in the base currency."""
    return sum(r.get("base_minor") or r["amount_minor"] for r in org.list("placement") if r.get("status") in OPEN)


def instruct(org, *, amount_minor: int, currency: str, destination: str, note: str, by: str, basis: dict,
             provider: dict | None = None, base_minor: int | None = None) -> dict:
    """`amount_minor` in `currency` (the provider's); `base_minor` its value in the base currency, checked against the
    idle cash (the same number when the currency is the base)."""
    if amount_minor <= 0:
        raise ValueError("the amount must be above zero")
    in_base = amount_minor if base_minor is None else base_minor
    if in_base > max(basis.get("idle_minor", 0), 0):
        raise ValueError("the amount is above the idle cash: it would touch money that is needed")
    if provider is not None:
        if not provider.get("active", True):
            raise ValueError(f"{provider['name']} is not an active provider")
        if amount_minor < provider.get("minimum_minor", 0):
            raise ValueError(f"{provider['name']} takes at least {provider['currency']} {provider['minimum_minor'] / 100:,.2f}")
        destination = destination or f"{provider['name']} · " + (f"{provider['tenor_days']}-day term deposit" if provider["kind"] == "TERM_DEPOSIT" else
                                                                 "money market fund" if provider["kind"] == "MONEY_MARKET_FUND" else "Airwallex Yield (flexible)")
    rec = {"_id": str(uuid.uuid4()), "amount_minor": amount_minor, "currency": currency, "base_minor": in_base, "destination": destination[:120], "note": note[:300],
           "status": "PROPOSED", "proposed_by": by, "proposed_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "basis": basis,
           "decided_by": None, "decided_at": None,
           "provider_id": provider["provider_id"] if provider else None, "provider_kind": provider["kind"] if provider else None,
           "indicative_rate_pct": provider.get("indicative_rate_pct") if provider else None,
           "history": [{"at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "by": by, "what": "proposed"}]}
    org._put("placement", rec, by)
    return rec


def decide(org, pid: str, status: str, by: str, role: str | None = None) -> dict:
    """Approve, mark done or cancel. A placement with a provider reached by transfer is approved by the FCO only and
    then goes on by itself (placement_providers.send); it is never marked done by hand."""
    rec = org.get("placement", pid)
    if rec is None:
        raise KeyError(pid)
    if status not in STATUSES[1:]:
        raise ValueError("a placement is approved, done or cancelled")
    by_transfer = rec.get("provider_id") and rec.get("provider_kind") != "AIRWALLEX_YIELD"
    allowed = {"PROPOSED": ("APPROVED", "CANCELLED"), "APPROVED": ("CANCELLED",) if by_transfer else ("DONE", "CANCELLED")}.get(rec["status"], ())
    if status not in allowed:
        raise ValueError(f"a {rec['status'].lower()} placement cannot become {status.lower()}")
    if status == "APPROVED" and by.split(" (")[0] == rec["proposed_by"].split(" (")[0]:
        raise ValueError("the person who proposed a placement cannot approve it")
    if status == "APPROVED" and rec.get("provider_id") and role != "super_admin":
        raise PermissionError("a placement with a provider is approved by the FCO (Super admin)")
    rec |= {"status": status, "decided_by": by, "decided_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
    rec.setdefault("history", []).append({"at": rec["decided_at"], "by": by, "what": status.lower()})
    org._put("placement", rec, by)
    return rec


def items(org) -> list[dict]:
    return sorted(org.list("placement"), key=lambda r: r.get("proposed_at", ""), reverse=True)
