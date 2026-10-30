# ruff: noqa: E501
"""Budget allocation: which budget a payment draws on, and whether it still fits, checked again at execution.

Settings → Budget allocation holds the policy and the allocations (organisation store, kind ``allocation``):

* enforcement   ``per_allocation``: a payment is paid only within the allocation that governs its charge code;
                ``no_limit``: no unit is limited, payments are bounded by the balance (and every other check).
* period_type   monthly, quarterly, half_year, annual or no_limit (one allocation until it is used). The
                fiscal year starts in ``fiscal_year_start_month`` (Finance standards); Q1 is that month and the two
                after it, Q2 the next three, and so on. A fiscal year is named by the calendar year it starts in.
* cut-off       the reporting cut-off (day and time in the reporting time zone) decides which month, and so
                which budget period, a payment belongs to: paid after the cut-off counts in the next month.
* sync          a daily sync at a set time and a monthly sync on a set day re-read the connected sources
                (Settings → Sync API) and record the usage of every allocation.

Which allocation governs a payment: its charge code ``OFFICE.PROJECT.ACCOUNT`` names a unit and a project.
The most specific allocation in the active period wins: a project-based allocation of that project, then a
regular allocation of the unit itself (with the project, then without), then the nearest unit above it whose
allocation includes the units under it. Spend counts against the allocation that governed it. No allocation, an
allocation that the payment would exceed, or a currency it cannot be compared in: the payment is refused.
"""
from __future__ import annotations

import calendar
import datetime as dt
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

PeriodType = Literal["monthly", "quarterly", "half_year", "annual", "no_limit"]
PERIOD_LABEL = {"monthly": "Monthly", "quarterly": "Quarterly", "half_year": "Half-yearly (semester)",
                "annual": "Annually (fiscal year)", "no_limit": "No period (until used)"}


class BudgetPolicy(BaseModel):
    enforcement: Literal["per_allocation", "no_limit"] = "no_limit"  # until a finance manager sets limits, every unit may pay
    period_type: PeriodType = "annual"
    daily_sync_time: str = Field(default="23:59", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    monthly_sync_day: int = Field(default=1, ge=1, le=31, description="clamped to the month's last day")
    sync_enabled: bool = True
    last_daily_sync_at: str | None = None
    last_monthly_sync_at: str | None = None
    last_sync_summary: dict | None = None


# -- periods ------------------------------------------------------------------------------------------

def _fy(year: int, month: int, start: int) -> tuple[int, int]:
    """(fiscal year, 0-based month index in it)."""
    return (year if month >= start else year - 1), (month - start) % 12


def period_of(month: str, period_type: str, start: int = 1) -> str:
    """The budget period a reporting month (YYYY-MM) falls in."""
    y, m = (int(x) for x in month.split("-"))
    fy, idx = _fy(y, m, start)
    return {"monthly": month, "quarterly": f"{fy}-Q{idx // 3 + 1}", "half_year": f"{fy}-H{idx // 6 + 1}",
            "annual": f"{fy}", "no_limit": "ALL"}[period_type]


def months_of(period: str, start: int = 1) -> list[str] | None:
    """The reporting months a budget period covers; None for ALL (every month)."""
    if period == "ALL":
        return None
    if len(period) == 7 and period[4] == "-" and period[5:].isdigit():
        return [period]
    fy = int(period[:4])
    if len(period) == 4:
        first, n = 0, 12
    elif period[5] == "Q":
        first, n = (int(period[6]) - 1) * 3, 3
    else:
        first, n = (int(period[6]) - 1) * 6, 6
    out = []
    for i in range(first, first + n):
        mm = (start - 1 + i) % 12 + 1
        yy = fy + (start - 1 + i) // 12
        out.append(f"{yy:04d}-{mm:02d}")
    return out


def period_options(period_type: str, start: int, month: str) -> list[str]:
    """The periods an allocation can be entered for: the fiscal year before, the current one and the next."""
    if period_type == "no_limit":
        return ["ALL"]
    fy = _fy(*(int(x) for x in month.split("-")), start)[0]
    out: list[str] = []
    for y in (fy - 1, fy, fy + 1):
        if period_type == "annual":
            out.append(f"{y}")
        elif period_type == "quarterly":
            out += [f"{y}-Q{q}" for q in range(1, 5)]
        elif period_type == "half_year":
            out += [f"{y}-H{h}" for h in (1, 2)]
        else:
            out += months_of(f"{y}", start)
    return out


def period_type_of(period: str) -> str:
    if period == "ALL":
        return "no_limit"
    if len(period) == 4:
        return "annual"
    return {"Q": "quarterly", "H": "half_year"}.get(period[5], "monthly")


def quarters(start: int) -> list[dict]:
    """Q1..Q4 of the fiscal year with their months, for the settings page."""
    name = calendar.month_abbr
    return [{"quarter": f"Q{q + 1}", "months": [name[(start - 1 + q * 3 + i) % 12 + 1] for i in range(3)]} for q in range(4)]


# -- which allocation governs a charge code -------------------------------------------------------------

def governing(allocs: list[dict], code: dict, period: str, chain: list[str]) -> tuple[dict | None, int]:
    """The most specific allocation of ``period`` covering a resolved charge code ({unit, project}); chain is
    the unit and its ancestors, nearest first. Returns (allocation, specificity)."""
    unit_id, pcode = code["unit"]["unit_id"], code["project"]["code"]
    best, score = None, -1
    for a in allocs:
        if a["period"] != period:
            continue
        if a.get("basis", "regular") == "project":
            s = 1000 if a.get("project_code") == pcode else -1
        else:
            if a.get("project_code") and a["project_code"] != pcode:
                continue
            if a.get("unit_id") == unit_id:
                s = 900
            elif a.get("include_children", True) and a.get("unit_id") in chain:
                s = 800 - 10 * chain.index(a["unit_id"])
            else:
                continue
            s += 50 if a.get("project_code") else 0
        if s > score:
            best, score = a, s
    return best, score


class Budget:
    """The allocations of one organisation and the spend already booked against them."""

    def __init__(self, org, store=None):
        self.org, self.store = org, store
        self.policy = org.budget_policy()
        self.start = int(org.standards().get("fiscal_year_start_month", 1))
        self.base = org.standards()["base_currency"]
        self.allocs = org.list("allocation")
        self._codes: dict[str, dict | None] = {}

    def resolve(self, account_code: str) -> dict | None:
        if account_code not in self._codes:
            try:
                self._codes[account_code] = self.org.account_code(account_code)
            except Exception:  # noqa: BLE001 - an unknown code is reported by the caller
                self._codes[account_code] = None
        return self._codes[account_code]

    def govern(self, account_code: str, period: str) -> dict | None:
        code = self.resolve(account_code)
        if code is None:
            return None
        chain = [u["unit_id"] for u in self.org.chain(code["unit"]["unit_id"])]
        return governing(self.allocs, code, period, chain)[0]

    def _amount_for(self, alloc: dict, currency: str, amount_minor: int, base_cost_minor: int) -> int | None:
        if alloc["currency"] == self.base:
            return base_cost_minor
        return amount_minor if currency == alloc["currency"] else None

    def spent(self, period: str) -> dict[str, int]:
        """alloc_id -> spend already paid in the period, counted against the allocation that governed it."""
        out: dict[str, int] = {}
        if self.store is None:
            return out
        months = months_of(period, self.start)
        for r in self.store.spend_rows(months):
            if period_of(r["reporting_period"], period_type_of(period), self.start) != period:
                continue
            a = self.govern(r["account_code"], period)
            if a is None:
                continue
            amt = self._amount_for(a, r["currency"], r["amount_minor"], r["base_cost_minor"])
            if amt is not None:
                out[a["alloc_id"]] = out.get(a["alloc_id"], 0) + amt
        return out

    def usage(self, period: str) -> list[dict]:
        spent = self.spent(period)
        rows = []
        for a in self.allocs:
            if a["period"] != period:
                continue
            used = spent.get(a["alloc_id"], 0)
            rows.append(a | {"spent_minor": used, "remaining_minor": None if a.get("no_limit") else a["amount_minor"] - used})
        return rows

    def check(self, items: list[dict], month: str) -> list[dict]:
        """items: {obligation_id, account_code, currency, amount_minor, base_cost_minor}. One verdict per item;
        items in the same batch add up against their allocation."""
        if self.policy["enforcement"] == "no_limit":
            return [{"obligation_id": i["obligation_id"], "ok": True, "reason": "budget allocation set to No limit"} for i in items]
        period = period_of(month, self.policy["period_type"], self.start)
        spent = self.spent(period)
        out = []
        for i in items:
            v = {"obligation_id": i["obligation_id"], "account_code": i["account_code"], "period": period, "ok": False}
            if self.resolve(i["account_code"]) is None:
                out.append(v | {"reason": f"charge code {i['account_code']} does not resolve to an active unit, project and account"})
                continue
            a = self.govern(i["account_code"], period)
            if a is None:
                out.append(v | {"reason": f"no budget allocation covers {i['account_code']} for {period}"})
                continue
            v |= {"alloc_id": a["alloc_id"], "allocation": _label(a)}
            if a.get("no_limit"):
                out.append(v | {"ok": True, "reason": f"{_label(a)} has No limit"})
                continue
            amt = self._amount_for(a, i["currency"], i["amount_minor"], i["base_cost_minor"])
            if amt is None:
                out.append(v | {"reason": f"{_label(a)} is in {a['currency']}; a {i['currency']} payment cannot be compared with it"})
                continue
            used = spent.get(a["alloc_id"], 0)
            left = a["amount_minor"] - used
            v |= {"currency": a["currency"], "limit_minor": a["amount_minor"], "spent_minor": used, "amount_minor": amt,
                  "remaining_after_minor": left - amt}
            if amt > left:
                out.append(v | {"reason": f"{_label(a)} has {left / 100:,.2f} {a['currency']} left for {period}; this payment needs {amt / 100:,.2f}"})
                continue
            spent[a["alloc_id"]] = used + amt
            out.append(v | {"ok": True, "reason": f"within {_label(a)} for {period}"})
        return out


def _label(a: dict) -> str:
    if a.get("basis") == "project":
        return f"project {a.get('project_code')} ({a['line']})"
    scope = a.get("unit_id") or "?"
    if a.get("project_code"):
        scope += f" / {a['project_code']}"
    return f"{scope} ({a['line']})"


# -- sync schedule ----------------------------------------------------------------------------------------

def _at(day: dt.date, hhmm: str, tz: ZoneInfo) -> dt.datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return dt.datetime(day.year, day.month, day.day, h, m, tzinfo=tz)


def last_scheduled(kind: str, policy: dict, now: dt.datetime, tz: str) -> dt.datetime:
    """The most recent instant (at or before now) the daily or monthly sync was due."""
    z = ZoneInfo(tz)
    local = now.astimezone(z)
    if kind == "daily":
        t = _at(local.date(), policy["daily_sync_time"], z)
        return t if t <= local else t - dt.timedelta(days=1)
    y, m = local.year, local.month
    for _ in range(2):
        d = min(policy["monthly_sync_day"], calendar.monthrange(y, m)[1])
        t = _at(dt.date(y, m, d), policy["daily_sync_time"], z)
        if t <= local:
            return t
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return t


def next_scheduled(kind: str, policy: dict, now: dt.datetime, tz: str) -> dt.datetime:
    z = ZoneInfo(tz)
    local = now.astimezone(z)
    if kind == "daily":
        t = _at(local.date(), policy["daily_sync_time"], z)
        return t if t > local else t + dt.timedelta(days=1)
    y, m = local.year, local.month
    for _ in range(2):
        d = min(policy["monthly_sync_day"], calendar.monthrange(y, m)[1])
        t = _at(dt.date(y, m, d), policy["daily_sync_time"], z)
        if t > local:
            return t
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return t


def due(kind: str, policy: dict, now: dt.datetime, tz: str) -> bool:
    """A sync is due when its latest scheduled instant passed after its last run (never run: not due, so a
    fresh start does not sync at once; the first run is the next scheduled one)."""
    last = policy.get(f"last_{kind}_sync_at")
    if not policy.get("sync_enabled", True) or not last:
        return False
    return dt.datetime.fromisoformat(last) < last_scheduled(kind, policy, now, tz)
