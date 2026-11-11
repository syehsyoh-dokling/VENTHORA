# ruff: noqa: E501
"""Agent plan -> the next three months: the cash VENTHORA expects to go out and come in, month by month, and the
deposit it suggests so the balance never falls under the reserve floor.

What goes out: the payroll of every active person in People & roles (one pay per month, at the net pay on file);
every payment request that waits (by its due date, an overdue one in the first month); what is left on every open
purchase order (spread over the months of the order that fall in the forecast). What comes in: the customer receipts
expected, weighted by how sure they are. Everything in the base currency at the rates of the plan; an amount in a
currency without a rate is listed apart, never guessed. Nothing is paid or moved by reading it.
"""
from __future__ import annotations

import datetime as dt
import math

from .aging import to_base

PAYABLE = ("supplier_invoice", "contractor_claim", "expense_claim")
OPEN_STATUSES = ("VERIFIED", "NEEDS_REVIEW", "PENDING")
DONE = ("REJECTED", "DUPLICATE")


def _windows(today: dt.date, months: int) -> list[tuple[dt.date, dt.date]]:
    return [(today + dt.timedelta(days=30 * i), today + dt.timedelta(days=30 * (i + 1))) for i in range(months)]


def _idx(windows, day: dt.date) -> int | None:
    if day < windows[0][0]:
        return 0  # overdue: first month
    return next((i for i, (lo, hi) in enumerate(windows) if lo <= day < hi), None)


def _date(s) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def build(org, requests: list, receivables: list, base: str, fx_to_base: dict, opening_minor: int, floor_minor: int,
          today: dt.date, months: int = 3, horizon_short_minor: int = 0) -> dict:
    win = _windows(today, months)
    rows = [{"from": lo.isoformat(), "to": (hi - dt.timedelta(days=1)).isoformat(), "out": {"Payroll": 0, "Payment requests": 0, "Open purchase orders": 0},
             "in": 0, "items": 0} for lo, hi in win]
    no_rate: dict[str, int] = {}

    def add(i, key, amount, ccy):
        if i is None or not amount:
            return
        try:
            v = to_base(int(amount), ccy or base, base, fx_to_base)
        except (KeyError, ValueError, TypeError):
            no_rate[ccy] = no_rate.get(ccy, 0) + int(amount)
            return
        if key == "in":
            rows[i]["in"] += v
        else:
            rows[i]["out"][key] += v
        rows[i]["items"] += 1

    # payroll: every active person with a net pay, once a month
    staff = [u for u in org.list("user") if u.get("active", True) and u.get("salary_minor")]
    for i in range(months):
        for u in staff:
            add(i, "Payroll", u["salary_minor"], u.get("salary_currency") or base)
    # payment requests that wait, by due date
    used: dict[str, int] = {}
    for q in requests:
        if q.status in DONE or not q.amount_minor:
            continue
        if q.po_number:
            used[q.po_number] = used.get(q.po_number, 0) + q.amount_minor
        if q.payment or q.kind not in PAYABLE or q.status not in OPEN_STATUSES:
            continue
        add(_idx(win, _date(q.due_date) or today), "Payment requests", q.amount_minor, q.currency)
    # open purchase orders: what is left, spread over the order's months inside the forecast
    for po in org.list("po"):
        if str(po.get("status", "OPEN")).upper() != "OPEN":
            continue
        left = int(po.get("total_minor") or 0) - used.get(po.get("po_id"), 0)
        end = _date(po.get("expiry_date")) or win[-1][1]
        start = max(_date(po.get("start_date")) or today, today)
        if left <= 0 or end < today:
            continue
        idx = [i for i, (lo, hi) in enumerate(win) if lo <= end and start < hi] or [0]
        for i in idx:
            add(i, "Open purchase orders", left // len(idx), po.get("currency") or base)
    # customer receipts expected, weighted by confidence
    for v in receivables:
        day = today + dt.timedelta(hours=v.expected_in_hours)
        i = _idx(win, day)
        add(i, "in", v.amount_minor * v.confidence_bp // 10_000, v.currency)

    bal, low, low_at, first_under = opening_minor, opening_minor, None, None
    for i, r in enumerate(rows):
        r["out_minor"] = sum(r["out"].values())
        r["opening_minor"] = bal
        bal = bal + r["in"] - r["out_minor"]
        r["closing_minor"] = bal
        r["under_floor"] = bal < floor_minor
        if bal < low:
            low, low_at = bal, i
        if r["under_floor"] and first_under is None:
            first_under = i
    need = max(0, floor_minor - low, horizon_short_minor)
    step = 100_000  # rounded up to 1,000.00 of the base currency
    suggest = math.ceil(need / step) * step if need else 0
    by = None
    if suggest:
        start = win[first_under][0] if first_under is not None else today
        by = start - dt.timedelta(days=2)
        while by.weekday() >= 5:
            by -= dt.timedelta(days=1)
        by = max(by, today)
    monthly_out = sum(r["out_minor"] for r in rows) / months if months else 0
    idle = max(0, low - floor_minor - int(monthly_out)) if not suggest else 0  # above one month of outflow, after the low point
    if suggest:
        why = (f"the balance would fall to {low / 100:,.2f} {base} in month {(low_at or 0) + 1}, under the reserve floor of {floor_minor / 100:,.2f}"
               if low < floor_minor else f"the next {horizon_short_minor and 'plan'} needs it now")
        if horizon_short_minor and not low < floor_minor:
            why = f"the payments due in the plan's horizon are {horizon_short_minor / 100:,.2f} {base} short now"
    else:
        why = f"the balance stays above the reserve floor; its lowest point is {low / 100:,.2f} {base}"
    return {"base_currency": base, "as_of": today.isoformat(), "months": rows, "opening_minor": opening_minor, "reserve_floor_minor": floor_minor,
            "lowest_minor": low, "lowest_month": (low_at or 0) + 1, "staff_paid": len(staff), "no_rate": no_rate,
            "suggestion": {"deposit_minor": suggest, "deposit_by": by.isoformat() if by else None, "why": why, "idle_minor": idle,
                           "idle_note": "above one month of outflow at the lowest point: it may be placed (FCO Authorize -> Placement providers)" if idle else None}}
