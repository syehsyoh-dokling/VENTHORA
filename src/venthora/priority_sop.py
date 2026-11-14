# ruff: noqa: E501
"""System Setting -> Standard Payment Priority Policy: what the FCO says is paid first when the money cannot cover
everything, and when it is paid (FIFO in real time, or high priority first over a planned horizon).

Each spending category gets a level (High, Medium, Low) and a place in the list. That becomes the priority (1-10)
every obligation carries into the plan for the horizon (72 h by default): High 10..8, Medium 7..5, Low 4..1, in list
order; inside the same number the earliest due date goes first. A priority never makes a payment allowed: the checks
(verified payee, PO, duplicate, DoA, budget, reserve floor) come first, whatever the level (docs/PAYMENT_PRIORITY_SOP.md).

Until the FCO saves the SOP the plan keeps each payee's own priority, so nothing changes by itself.
"""
from __future__ import annotations

import re

LEVELS = {"high": "High priority", "medium": "Medium priority", "low": "Low priority"}
BANDS = {"high": (10, 8), "medium": (7, 5), "low": (4, 1)}  # (first in the list, the floor of the band)
TOP_SHARED = 2  # the first two High categories both carry 10 (wages and the statutory withholdings by default)
LATE_INTEREST_BP = 800  # a contract late-payment interest at or above this (EU 2011/7: reference rate + 8 points) is "costly"

# key -> (label, what falls in it, the standard behind the default, default level)
ITEMS: dict[str, tuple[str, str, str, str]] = {
    "wages": ("Wages and salaries", "net pay of employees (payroll, DoA 01 Personnel)",
              "ILO C95 and C173: wages are paid regularly and rank ahead of other creditors", "high"),
    "statutory": ("Taxes and social security", "withheld payroll tax, social security, other taxes due",
                  "penalties and personal liability for unremitted withholdings", "high"),
    "critical_suppliers": ("Operations-critical suppliers", "suppliers whose non-payment stops operations (shipping, utilities, sole source); a payee marked priority 9-10",
                           "continuity: payroll first, then key suppliers", "high"),
    "fringe": ("Fringe benefits", "insurance, pension and benefits (DoA 02); cover lapses when unpaid", "IAS 19 employee benefits; continuity of cover", "high"),
    "debt_service": ("Loan and debt service", "instalments and interest under a loan covenant", "a missed instalment can trigger default and cross-default", "medium"),
    "consultants": ("Consultants and contracted services", "consultant contracts, one-off engagements, contractors (DoA 03)", "contract terms", "medium"),
    "suppliers": ("Suppliers and purchase orders", "goods and services against an open PO", "EU Directive 2011/7/EU: 30-60 day terms, interest when late", "medium"),
    "office": ("Office and operations", "rent, utilities, communications, software (DoA 05)", "continuity of the office", "medium"),
    "travel": ("Travel and transport", "travel claims and advances for approved trips (DoA 04)", "staff reimbursement", "medium"),
    "cash_advance": ("Cash advance, petty cash and slush fund", "advances to staff and petty cash floats (DoA 06)", "staff and office floats: held first when cash is short", "low"),
    "odc": ("Other direct costs", "programme activity costs (DoA 07)", "activity plan", "low"),
    "deferrable": ("Subscriptions and deferrable costs", "anything cheapest to defer (subscriptions, team gatherings and staff events, non-urgent services)", "least harm when late", "low"),
}
DEFAULT_ORDER = list(ITEMS)
DOA_KEY = {"personnel": "wages", "fringe": "fringe", "consultants": "consultants", "travel": "travel", "office": "office",
           "cash_advance": "cash_advance", "other_direct": "odc"}
LINE_KEY = {"payroll": "wages", "tax": "statutory", "taxes": "statutory", "social_security": "statutory", "loan": "debt_service", "debt": "debt_service",
            "contractors": "consultants", "consultants": "consultants", "saas": "deferrable", "subscriptions": "deferrable", "events": "deferrable", "team_events": "deferrable", "team_gathering": "deferrable",
            "cash_advance": "cash_advance", "petty_cash": "cash_advance", "slush_fund": "cash_advance", "benefits": "fringe", "fringe": "fringe", "insurance": "fringe", "pension": "fringe", "office": "office",
            "rent": "office", "utilities": "critical_suppliers", "logistics": "suppliers", "suppliers": "suppliers", "travel": "travel", "odc": "odc"}
ACCOUNT_KEY = {"2200": "statutory", "2300": "debt_service", "6010": "wages", "6020": "fringe", "6200": "consultants", "6300": "office", "6700": "office", "6400": "travel",
               "1400": "cash_advance", "6900": "odc", "6100": "suppliers", "6500": "suppliers", "6600": "consultants", "7100": "suppliers"}


MODES = {"priority": "High priority first", "fifo": "First in, first out (FIFO): real time"}
HORIZONS = (24, 48, 72, 96, 120)
DEADLINE_HOUR_UTC = 17  # a payment is due by 17:00 UTC on its due date: "3 h before the deadline" is 14:00 UTC
DEEMED_BEFORE_H = 3  # no intervention this close to the deadline: the plan is deemed approved
FX_BEFORE_H = 2  # currencies are bought this close to the deadline, at the planned rate or not at all
POLICY_DEFAULT = {"mode": "priority", "horizon_hours": 72, "deemed_approval": False, "pay_what_fits": True,
                  "notify_fco": True, "cascade": True, "fx_at_plan": False, "run_times": [], "run_days": "every", "run_tz": "company"}
RUN_DAYS = {"every": "Every day", "weekdays": "Monday to Friday"}
RUN_TZ = {"company": "Company time", "UTC": "UTC"}
# scheduled payments (payroll and benefits from People & roles, on their pay date) go out at this UTC time on that date;
# everything else (invoices, bills, claims) waits for the policy's run times, so a person can change course each day
SCHEDULED_PAY_UTC = (0, 30)


def run_timezone(pol: dict, company_tz: str) -> str:
    return "UTC" if pol.get("run_tz") == "UTC" else company_tz


def scheduled_pay_open(now) -> bool:
    """Scheduled lines are paid from SCHEDULED_PAY_UTC on their pay date (a later minute the same day, or a missed day, still pays)."""
    return (now.hour, now.minute) >= SCHEDULED_PAY_UTC
RUN_WINDOW_MIN = 60  # a run time missed (the server was off) is still run within this many minutes, never later


def saved(org) -> dict | None:
    return org.get("priority_sop", "sop") if org is not None else None


def policy(org) -> dict:
    """The Standard Payment Priority Policy in force: how VENTHORA orders and times payments (defaults until saved)."""
    return POLICY_DEFAULT | ((saved(org) or {}).get("policy") or {})


def check_policy(p: dict) -> dict:
    p = POLICY_DEFAULT | {k: v for k, v in (p or {}).items() if k in POLICY_DEFAULT}
    if p["mode"] not in MODES:
        raise ValueError("the priority is fifo (first in, first out, real time) or priority (high priority first)")
    if p["horizon_hours"] not in HORIZONS:
        raise ValueError("the plan covers the next 24, 48, 72, 96 or 120 hours")
    for k in ("deemed_approval", "pay_what_fits", "notify_fco", "cascade", "fx_at_plan"):
        p[k] = bool(p[k])
    times = p["run_times"] if isinstance(p["run_times"], list) else str(p["run_times"] or "").replace(";", ",").split(",")
    clean = set()
    for t in (str(x).strip() for x in times):
        if not t:
            continue
        m = re.fullmatch(r"([01]?\d|2[0-3])[:.]([0-5]\d)", t)
        if not m:
            raise ValueError(f"payment run time {t!r}: write it as HH:MM, e.g. 09:00")
        clean.add(f"{int(m[1]):02d}:{m[2]}")
    if len(clean) > 6:
        raise ValueError("at most six payment run times a day")
    p["run_times"] = sorted(clean)
    if p["run_days"] not in RUN_DAYS:
        raise ValueError("payment runs are every day or Monday to Friday")
    if p["run_tz"] not in RUN_TZ:
        raise ValueError("payment run times are in company time or UTC")
    return p


def _slots(pol: dict, local, days: range):
    from datetime import datetime as _dt
    from datetime import timedelta

    for d in days:
        day = local.date() + timedelta(days=d)
        if pol.get("run_days") == "weekdays" and day.weekday() >= 5:
            continue
        for t in pol.get("run_times") or []:
            h, m = map(int, t.split(":"))
            yield _dt(day.year, day.month, day.day, h, m, tzinfo=local.tzinfo)


def next_run(pol: dict, now, tz: str):
    """The next payment run time after now, in the company's time zone; None when the policy runs continuously."""
    from zoneinfo import ZoneInfo

    local = now.astimezone(ZoneInfo(tz))
    return next((at for at in _slots(pol, local, range(0, 8)) if at > local), None)


def due_slot(pol: dict, now, tz: str, done: set[str]) -> str | None:
    """The run time that is due now and not yet run ("YYYY-MM-DD HH:MM"), or None."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    local = now.astimezone(ZoneInfo(tz))
    for at in _slots(pol, local, range(-1, 1)):
        key = at.strftime("%Y-%m-%d %H:%M")
        if at <= local < at + timedelta(minutes=RUN_WINDOW_MIN) and key not in done:
            return key
    return None


def current(org) -> list[dict]:
    """The list in force: each category with its level, place and the priority it gives."""
    rec = saved(org) or {}
    levels = {i["key"]: i["level"] for i in rec.get("items", [])}
    order = [k for k in rec.get("order", []) if k in ITEMS] + [k for k in DEFAULT_ORDER if k not in rec.get("order", [])]
    rows = [{"key": k, "label": ITEMS[k][0], "detail": ITEMS[k][1], "standard": ITEMS[k][2], "level": levels.get(k, ITEMS[k][3]),
             "default_level": ITEMS[k][3]} for k in order]
    for lvl, (top, floor) in BANDS.items():
        for n, r in enumerate([r for r in rows if r["level"] == lvl]):
            r["priority"] = max(top - max(n - (TOP_SHARED - 1), 0) if lvl == "high" else top - n, floor)
    return rows


def late_cost_on(org) -> bool:
    """Inside a level, a payment that costs more when late (overdue, a second reminder or final notice, high late
    interest) moves up one step. On unless the FCO switched it off."""
    return bool((saved(org) or {}).get("late_cost", True))


def priorities(org) -> dict[str, int] | None:
    """category -> priority, or None while the FCO has not saved an SOP (each payee's own priority applies)."""
    if saved(org) is None:
        return None
    return {r["key"]: r["priority"] for r in current(org)}


def save(org, items: list[dict], by: str, late_cost: bool = True, policy_in: dict | None = None) -> dict:
    """items: every category, in the order the FCO wants, each with its level; policy_in: the ordering and timing
    rules (mode, horizon, deemed approval, short balance, cascade, FX timing)."""
    keys = [str(i.get("key")) for i in items]
    if sorted(keys) != sorted(ITEMS):
        raise ValueError("the SOP lists every category once: " + ", ".join(ITEMS))
    bad = [i["key"] for i in items if i.get("level") not in LEVELS]
    if bad:
        raise ValueError(f"{bad[0]}: the level is high, medium or low")
    rec = {"_id": "sop", "items": [{"key": i["key"], "level": i["level"]} for i in items], "order": keys, "updated_by": by,
           "late_cost": bool(late_cost), "policy": check_policy(policy_in if policy_in is not None else policy(org))}
    org._put("priority_sop", rec, by)
    return rec


def classify(o: dict, payees: dict) -> str:
    """The SOP category of an obligation, from what it is (a DoA line, a claim), its account or its payee."""
    oid = str(o.get("obligation_id") or "")
    if oid.startswith("DOA-"):
        key = oid.split("-")[1].lower()
        return DOA_KEY.get({"other": "other_direct", "cash": "cash_advance"}.get(key, key), "suppliers")
    if oid.startswith("TRAVEL-"):
        return "travel"
    if oid.startswith("ADVANCE-"):
        return "cash_advance"
    payee = payees.get(o.get("beneficiary_id") or "") or {}
    if (payee.get("default_priority") or 0) >= 9 and (payee.get("budget_line") or "") != "payroll":
        return "critical_suppliers"
    acct = str(o.get("account_code") or "").split(".")
    if len(acct) == 3 and acct[2] in ACCOUNT_KEY:
        return ACCOUNT_KEY[acct[2]]
    line = str(o.get("budget_line") or payee.get("budget_line") or "").lower()
    return LINE_KEY.get(line, "suppliers")


def costly_when_late(o: dict, as_of: str | None) -> list[str]:
    """Why an obligation costs more when it is late: overdue, reminders, high contractual late interest."""
    why = []
    if o.get("due_date") and as_of and o["due_date"] < as_of:
        why.append("overdue")
    if (o.get("dunning_level") or 0) >= 2:
        why.append("second reminder or final notice")
    if (o.get("late_interest_bp") or 0) >= LATE_INTEREST_BP:
        why.append(f"late interest {o['late_interest_bp'] / 100:g}% a year")
    return why


def level_top(org, key: str) -> int:
    lvl = next(r["level"] for r in current(org) if r["key"] == key)
    return BANDS[lvl][0]


def priority_for(org, o: dict, payees: dict, as_of: str | None) -> tuple[str, int, list[str]]:
    """(category, priority, why it moved up) for one obligation under the saved SOP."""
    pr = {r["key"]: r["priority"] for r in current(org)}
    k = classify(o, payees)
    p, why = pr[k], []
    if late_cost_on(org):
        why = costly_when_late(o, as_of)
        if why and p < level_top(org, k):
            p += 1
    return k, p, why


def apply(state, org, payees: dict):
    """The state with every obligation's priority taken from the SOP (unchanged while none is saved)."""
    if priorities(org) is None:
        return state
    pol = policy(org)
    if pol["mode"] == "priority":  # the plan covers the horizon the policy sets
        state = state.model_copy(update={"policy": state.policy.model_copy(update={"planning_horizon_hours": pol["horizon_hours"]})})
    as_of = str(state.as_of)[:10] if getattr(state, "as_of", None) else None
    obls = []
    for o in state.obligations:
        _, p, _ = priority_for(org, o.model_dump(), payees, as_of)
        obls.append(o.model_copy(update={"priority": p}))
    return state.model_copy(update={"obligations": obls})


# -- the policy at run time: deadlines, a person's hold (the intervention), the planned rate, notices to the FCO ----------

def deadline(o: dict, now):
    """When a payment is due: DEADLINE_HOUR_UTC on its due date, else now + its due_in_hours."""
    import datetime as dt

    if o.get("due_date"):
        try:
            d = dt.date.fromisoformat(str(o["due_date"])[:10])
            return dt.datetime(d.year, d.month, d.day, DEADLINE_HOUR_UTC, tzinfo=dt.UTC)
        except ValueError:
            pass
    return now + dt.timedelta(hours=int(o.get("due_in_hours") or 0))


def hours_left(o: dict, now) -> float:
    return (deadline(o, now) - now).total_seconds() / 3600


def hold(org, obligation_id: str, by: str, note: str = "", until: str | None = None, rejected: bool = False) -> dict:
    """A person's intervention: this payment is not paid by the policy (no deemed approval, no autonomous run) until
    the hold is released."""
    import datetime as dt

    rec = {"_id": obligation_id, "by": by, "note": note[:300], "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
           "until": until, "rejected": rejected}
    org._put("plan_hold", rec, by)
    return rec


def release(org, obligation_id: str, by: str) -> None:
    rec = org.get("plan_hold", obligation_id)
    if rec is not None:
        org._put("plan_hold", rec | {"released_by": by, "released": True}, by)


def holds(org) -> dict[str, dict]:
    """The payments a person holds now: released holds, and a pending one whose new date has come, no longer count."""
    import datetime as dt

    today = dt.datetime.now(dt.UTC).date().isoformat()
    return {r["_id"]: r for r in org.list("plan_hold") if not r.get("released") and not (r.get("until") and r["until"] <= today)}


def planned_rate(org, obligation_id: str, currency: str, rate: str | None) -> str | None:
    """The rate a currency payment was first planned at (kept once): it is bought at that value or better, never above."""
    rec = org.get("fx_plan", obligation_id)
    if rec is None and rate:
        import datetime as dt

        rec = {"_id": obligation_id, "currency": currency, "rate": str(rate), "planned_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
        org._put("fx_plan", rec, "VENTHORA (plan)")
    return rec["rate"] if rec else None


def notify(org, key: str, subject: str, body: str, refs: list[str] | None = None, to_role: str = "super_admin") -> dict:
    """A notice for the FCO (once per key while it is open): shown in Agent plan and on the policy page."""
    import datetime as dt

    old = org.get("notification", key)
    if old is not None and old.get("status") == "open":
        return old
    rec = {"_id": key, "to_role": to_role, "subject": subject[:200], "body": body[:1000], "refs": list(refs or [])[:20],
           "status": "open", "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
    org._put("notification", rec, "VENTHORA (policy)")
    return rec


def notices(org, status: str | None = "open") -> list[dict]:
    rows = [r for r in org.list("notification") if status is None or r.get("status") == status]
    return sorted(rows, key=lambda r: r.get("at") or "", reverse=True)
