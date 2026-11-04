# ruff: noqa: E501
"""System Setting -> Payment Priority SOP: what the FCO says is paid first when the money cannot cover everything.

Each spending category gets a level (High, Medium, Low) and a place in the list. That becomes the priority (1-10)
every obligation carries into the plan for the horizon (72 h by default): High 10..8, Medium 7..5, Low 4..1, in list
order; inside the same number the earliest due date goes first. A priority never makes a payment allowed: the checks
(verified payee, PO, duplicate, DoA, budget, reserve floor) come first, whatever the level (docs/PAYMENT_PRIORITY_SOP.md).

Until the FCO saves the SOP the plan keeps each payee's own priority, so nothing changes by itself.
"""
from __future__ import annotations

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
    "cash_advance": ("Cash advance, petty cash and slush fund", "advances to staff and petty cash floats (DoA 06)", "staff and office floats", "medium"),
    "odc": ("Other direct costs", "programme activity costs (DoA 07)", "activity plan", "low"),
    "deferrable": ("Subscriptions and deferrable costs", "anything cheapest to defer (subscriptions, non-urgent services)", "least harm when late", "low"),
}
DEFAULT_ORDER = list(ITEMS)
DOA_KEY = {"personnel": "wages", "fringe": "fringe", "consultants": "consultants", "travel": "travel", "office": "office",
           "cash_advance": "cash_advance", "other_direct": "odc"}
LINE_KEY = {"payroll": "wages", "tax": "statutory", "taxes": "statutory", "social_security": "statutory", "loan": "debt_service", "debt": "debt_service",
            "contractors": "consultants", "consultants": "consultants", "saas": "deferrable", "subscriptions": "deferrable", "office": "office",
            "rent": "office", "utilities": "critical_suppliers", "logistics": "suppliers", "suppliers": "suppliers", "travel": "travel", "odc": "odc"}
ACCOUNT_KEY = {"2200": "statutory", "2300": "debt_service", "6010": "wages", "6020": "fringe", "6200": "consultants", "6300": "office", "6700": "office", "6400": "travel",
               "1400": "cash_advance", "6900": "odc", "6100": "suppliers", "6500": "suppliers", "6600": "consultants", "7100": "suppliers"}


def saved(org) -> dict | None:
    return org.get("priority_sop", "sop") if org is not None else None


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


def save(org, items: list[dict], by: str, late_cost: bool = True) -> dict:
    """items: every category, in the order the FCO wants, each with its level."""
    keys = [str(i.get("key")) for i in items]
    if sorted(keys) != sorted(ITEMS):
        raise ValueError("the SOP lists every category once: " + ", ".join(ITEMS))
    bad = [i["key"] for i in items if i.get("level") not in LEVELS]
    if bad:
        raise ValueError(f"{bad[0]}: the level is high, medium or low")
    rec = {"_id": "sop", "items": [{"key": i["key"], "level": i["level"]} for i in items], "order": keys, "updated_by": by,
           "late_cost": bool(late_cost)}
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
    as_of = str(state.as_of)[:10] if getattr(state, "as_of", None) else None
    obls = []
    for o in state.obligations:
        _, p, _ = priority_for(org, o.model_dump(), payees, as_of)
        obls.append(o.model_copy(update={"priority": p}))
    return state.model_copy(update={"obligations": obls})
