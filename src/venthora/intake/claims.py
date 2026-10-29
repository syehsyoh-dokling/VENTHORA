# ruff: noqa: E501
"""Expense and reimbursement claims from staff, checked against the organisation's own settings.

A claim is staff's only when the sender is the exact, active mailbox of a person in User management (never a
domain match: anyone at the company domain is not anyone in the company). Code then checks the claim against
Finance standards and Unit prices and names who may approve it:

* category   one of the finance standards' expense categories, quoted from the email or its PDF
* deadline   the expense date is within the standards' claim deadline
* unit price a per-day price for that category in the claimant's unit chain caps days x price
* approvers  active finance roles in the claimant's unit or above, with an approval limit that covers the amount
             (compared in the base currency only), nearest unit first, never the claimant

A claim always waits for one of those approvers; the agent never approves its own spending.
"""
from __future__ import annotations

import datetime as dt
import re

from .models import Quote

APPROVER_ROLES = ("country_finance_lead", "finance_manager", "cfo")
DAYS_RE = re.compile(r"\b(?P<n>\d{1,3})\s*(?P<u>days?|nights?)\b", re.IGNORECASE)


def employee(org, sender: str) -> dict | None:
    if org is None:
        return None
    sender = sender.lower()
    return next((u for u in org.list("user") if u.get("active", True) and u["email"].lower() == sender), None)


def unit_chain(org, unit_id: str | None) -> list[dict]:
    """The unit and its ancestors, nearest first."""
    out, seen = [], set()
    while unit_id and unit_id not in seen:
        seen.add(unit_id)
        unit = org.get("unit", unit_id)
        if unit is None:
            break
        out.append(unit)
        unit_id = unit.get("parent_id")
    return out


def category(text: str, subject: str, categories: list[str]) -> Quote | None:
    for src, offset in ((subject, False), (text, True)):
        low = src.lower()
        hits = [(low.find(c.lower()), c) for c in categories if re.search(rf"\b{re.escape(c.lower())}\b", low)]
        if hits:
            i, c = min(hits)
            return Quote(field="category", value=c, quote=src[i:i + len(c)], start=i if offset else -1, end=i + len(c) if offset else -1)
    return None


def expense_date(text: str, received: dt.date, any_date_re, parse_date) -> Quote | None:
    """The first date in the claim on or before the day it was sent (when the money was spent)."""
    for m in any_date_re.finditer(text):
        d = parse_date(m.group("date"), received)
        if d and d <= received:
            return Quote(field="expense_date", value=d.isoformat(), quote=m.group(0), start=m.start(), end=m.end())
    return None


def unit_price_cap(org, chain: list[dict], cat: str, currency: str, text: str) -> tuple[int, str, Quote] | None:
    """(cap in minor units, explanation, quoted days) when a per-day price applies to this category and unit chain."""
    m = DAYS_RE.search(text)
    if not m:
        return None
    ids = [u["unit_id"] for u in chain]
    prices = [p for p in org.list("price") if p["category"].lower() == cat.lower() and p["currency"] == currency
              and "day" in p["unit"].lower() and (p.get("unit_id") in ids or p.get("unit_id") is None)]
    if not prices:
        return None
    # the nearest unit's price applies; a company-wide price only when no unit in the chain has one
    prices.sort(key=lambda p: ids.index(p["unit_id"]) if p.get("unit_id") in ids else len(ids))
    p, n = prices[0], int(m.group("n"))
    q = Quote(field="quantity", value=f"{n} {m.group('u').lower()}", quote=m.group(0), start=m.start(), end=m.end())
    return n * p["price_minor"], f"{n} x {p['item']} at {currency} {p['price_minor'] / 100:,.2f}", q


def approvers(org, claimant: dict, chain: list[dict], amount_minor: int, currency: str, base: str) -> tuple[list[dict], str | None]:
    """Eligible approvers, nearest unit first; and a note when the limit could not be compared."""
    order = {u["unit_id"]: i for i, u in enumerate(chain)}
    note = None if currency == base else f"approval limits are set in {base}; a {currency} claim is not compared against them"
    people = [u for u in org.list("user") if u.get("active", True) and u["role"] in APPROVER_ROLES and u["unit_id"] in order
              and u["user_id"] != claimant["user_id"] and (note is not None or u.get("approval_limit_minor", 0) >= amount_minor)]
    people.sort(key=lambda u: (order[u["unit_id"]], APPROVER_ROLES.index(u["role"])))
    return people, note
