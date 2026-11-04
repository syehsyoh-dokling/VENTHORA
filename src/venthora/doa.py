# ruff: noqa: E501
"""Delegation of Authority (DoA): the general policy the FCO delegates to VENTHORA's agent, by kind of payment.

The DoA holds no individual: no person, no vendor, no bank account, no amount. It says, per category, whether the
agent may pay on its own, when (schedule and pay day), and in which currencies. Who is paid, into which account and how much comes from the category's
source document: the payroll register for personnel (one line per employee, each with the employee's own registered
bank account and currency), the policy schedule for fringe benefits, the contract for consultants, the travel
authorisation for travel, the recurring contract for office costs and the activity budget for other direct costs.
Each line of a source document is then judged against the policy: the agent pays it on its own, prepares it for a
person, or never executes it.

Internal payments follow the cost categories of a donor-funded budget (2 CFR 200 cost principles: personnel §200.430,
fringe benefits §200.431, professional services §200.459, travel §200.475, other direct costs). External payments
are invoices: vendors (three-way match) and short-term specialists (contract terms); they run through the intake
queue and the standing mandate.

Every paid line carries the fingerprint doa:<category>:<period>:<line>, so the executor's duplicate lock never pays
a line twice.
"""
from __future__ import annotations

import calendar
import datetime as dt
import re
from typing import Any

from .domain.models import TreasuryState

PAYEE_OK = ("VERIFIED", "ACTIVE")
CURRENCIES = ("USD", "EUR", "GBP")  # a line in another currency than USD is bought by the agent in the same run
SCHEDULES = ("monthly", "quarterly", "annual", "per_document")
PAY_DAYS = ("last_working_day",) + tuple(str(d) for d in range(1, 29))

INTERNAL: list[dict[str, Any]] = [
    {"key": "personnel", "code": "01", "title": "Personnel", "basis": "Salaries and wages (2 CFR 200.430)", "priority": 10, "account": "6010",
     "source": "Payroll register: one line per employee, with the employee's registered bank account, currency and net pay; prepared by HR, approved by HR and the FCO",
     "connected": True,
     "auto": "On the pay date, every line of the approved register, to the employee's verified account, in an allowed currency.",
     "approve": "The first payroll under this delegation, and anyone new on the register.",
     "never": "A register not approved by both HR and the FCO; a line whose account is not verified, or does not receive the line's currency."},
    {"key": "fringe", "code": "02", "title": "Fringe benefits", "basis": "Insurance, pension, social security, leave (2 CFR 200.431)", "priority": 9, "account": "6020",
     "source": "The premium schedule of each policy or plan", "connected": False,
     "auto": "Each premium on its due date, to the registered insurer or fund.",
     "approve": "The first premium of a new policy, or a premium that is not on the schedule.",
     "never": "A premium to anyone but the registered insurer or fund."},
    {"key": "consultants", "code": "03", "title": "Experts and consultants", "basis": "Regular consultants on a retainer (2 CFR 200.459)", "priority": 7, "account": "6200",
     "source": "Each consultant's contract and the period's approved timesheet", "connected": False,
     "auto": "The monthly fee in the contract, inside the contract's ceiling and term.",
     "approve": "A fee above the contract's monthly amount, or the month that reaches the ceiling.",
     "never": "A fee with no active contract, or after the contract's end date."},
    {"key": "travel", "code": "04", "title": "Travel and transport", "basis": "Travel, per diem and transport (2 CFR 200.475)", "priority": 6, "account": "6400",
     "source": "An approved travel authorisation and the per diem rates", "connected": False,
     "auto": "Advances and per diem under an approved travel authorisation, at the per diem rates.",
     "approve": "Anything above the rates or outside the authorisation.",
     "never": "A payment with no travel authorisation; a new advance while an earlier one is not liquidated."},
    {"key": "office", "code": "05", "title": "Office and operations", "basis": "Rent, utilities, communications, software", "priority": 6, "account": "6300",
     "source": "Each recurring contract or subscription", "connected": False,
     "auto": "The recurring bill on its due date, at the contracted amount, to the registered provider.",
     "approve": "A bill that differs from the contracted amount.",
     "never": "A provider that is not registered and verified."},
    {"key": "other_direct", "code": "06", "title": "Other direct costs", "basis": "Programme activity costs not in another category", "priority": 5, "account": "6900",
     "source": "The approved activity budget or work plan", "connected": False,
     "auto": "Costs inside an approved activity budget.",
     "approve": "Anything outside the work plan or the activity's budget.",
     "never": "A charge code that is not active."},
]
EXTERNAL: list[dict[str, Any]] = [
    {"key": "vendors", "title": "Vendor invoices", "basis": "Supplies, services and equipment against a purchase order",
     "source": "The purchase order, proof of delivery and the invoice (three-way match)",
     "auto": "A verified vendor's invoice that quotes an open purchase order and matches it within the tolerance, up to the single-payment limit.",
     "approve": "Above a limit, a first payment to a new vendor, a vendor in its cooling period, a budget line exceeded.",
     "never": "No purchase order, a duplicate invoice, bank details changed by email, an unverified vendor."},
    {"key": "specialists", "title": "Short-term specialists and experts", "basis": "Non-regular consultants paid per deliverable (2 CFR 200.459)",
     "source": "The contract or statement of work and the accepted deliverable",
     "auto": "A deliverable's payment term in the contract, inside the contract's total, from the specialist's registered mailbox.",
     "approve": "An amount that is not a term of the contract, or the term that reaches the contract's total.",
     "never": "No contract on file, or payment beyond its total."},
]
BY_KEY = {c["key"]: c for c in INTERNAL}
ACCOUNTS = {"6010": ("Salaries and wages", "personnel"), "6020": ("Fringe benefits", "fringe"), "6900": ("Other direct costs", "other_direct")}

# the policy, per category: nothing here names a person or an amount owed
DEFAULTS: dict[str, dict] = {
    "personnel": {"enabled": True, "schedule": "monthly", "pay_day": "last_working_day", "currencies": ["USD", "EUR", "GBP"]},
    "fringe": {"enabled": True, "schedule": "monthly", "pay_day": "1", "currencies": ["USD"]},
    "consultants": {"enabled": True, "schedule": "monthly", "pay_day": "last_working_day", "currencies": ["USD", "EUR", "GBP"]},
    "travel": {"enabled": True, "schedule": "per_document", "pay_day": "1", "currencies": ["USD", "EUR", "GBP"]},
    "office": {"enabled": True, "schedule": "monthly", "pay_day": "1", "currencies": ["USD"]},
    "other_direct": {"enabled": False, "schedule": "per_document", "pay_day": "1", "currencies": ["USD"]},
}


class DoAError(ValueError):
    pass


def ensure_accounts(org) -> None:
    """The chart of accounts gets the DoA categories' accounts it may not have yet."""
    have = {a["code"] for a in org.list("account")}
    for code, (name, key) in ACCOUNTS.items():
        if code not in have:
            org._put("account", {"account_id": f"ac-{code}", "code": code, "name": name, "category": BY_KEY[key]["title"], "active": True}, "doa")


# -- schedule ---------------------------------------------------------------------------------------------------------
def _last_working_day(y: int, m: int) -> dt.date:
    d = dt.date(y, m, calendar.monthrange(y, m)[1])
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def _day(y: int, m: int, pay_day: str) -> dt.date:
    return _last_working_day(y, m) if pay_day == "last_working_day" else dt.date(y, m, int(pay_day))


def period_and_date(policy: dict, today: dt.date) -> tuple[str, dt.date] | None:
    """The period the policy pays now (or next) and its pay date; None when paid per document."""
    y, m = today.year, today.month
    if policy["schedule"] == "monthly":
        return f"{y}-{m:02d}", _day(y, m, policy["pay_day"])
    if policy["schedule"] == "quarterly":
        q = (m - 1) // 3 + 1
        return f"{y}-Q{q}", _day(y, q * 3, policy["pay_day"])
    if policy["schedule"] == "annual":
        return f"{y}", _day(y, 12, policy["pay_day"])
    return None


# -- the policy -------------------------------------------------------------------------------------------------------
def policy(org, key: str) -> dict:
    saved = {k: v for k, v in (org.get("doa_policy", key) or {}).items() if k in DEFAULTS[key] or k == "updated_by"}
    return DEFAULTS[key] | saved | {"_id": key, "category": key}


def save_policy(org, key: str, body: dict, by: str) -> dict:
    if key not in BY_KEY:
        raise DoAError(f"{key}: not an internal payment category")
    rec = policy(org, key) | {k: v for k, v in body.items() if k in DEFAULTS[key]}
    if rec["schedule"] not in SCHEDULES:
        raise DoAError(f"the schedule is one of: {', '.join(SCHEDULES)}")
    if rec["pay_day"] not in PAY_DAYS:
        raise DoAError("the pay day is a day from 1 to 28, or the last working day")
    if not rec["currencies"] or any(c not in CURRENCIES for c in rec["currencies"]):
        raise DoAError(f"allow at least one payout currency from: {', '.join(CURRENCIES)}")
    rec["currencies"] = [c for c in CURRENCIES if c in rec["currencies"]]
    rec["enabled"] = bool(rec["enabled"])
    rec["updated_by"] = by
    org._put("doa_policy", rec, by)
    return rec


# -- the personnel source document: the payroll register --------------------------------------------------------------
def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:30] or "line"


def register(org, period: str) -> dict | None:
    return org.get("payroll_register", period)


def save_register(org, period: str, lines: list[dict], by: str) -> dict:
    """A payroll register for one period. Each line: employee, the employee's payee (registered bank account),
    currency, net pay, charge code. Saving it again resets its approvals."""
    if not lines:
        raise DoAError("a register needs at least one line")
    ensure_accounts(org)
    seen = set()
    for ln in lines:
        if not str(ln.get("employee", "")).strip() or not isinstance(ln.get("amount_minor"), int) or ln["amount_minor"] <= 0:
            raise DoAError("every line needs the employee and a net pay above zero")
        if not ln.get("payee_id"):
            raise DoAError(f"{ln['employee']}: name the employee's registered bank account (payee)")
        if ln.get("currency") not in CURRENCIES:
            raise DoAError(f"{ln['employee']}: the currency is one of {', '.join(CURRENCIES)}")
        org.account_code(ln.get("charge_code") or "")
        if _slug(ln["employee"]) in seen:
            raise DoAError(f"{ln['employee']} is on the register twice")
        seen.add(_slug(ln["employee"]))
    rec = {"_id": period, "period": period, "lines": lines, "status": "draft", "prepared_by": by, "hr_approved_by": None, "fco_approved_by": None,
           "totals": {c: sum(ln["amount_minor"] for ln in lines if ln["currency"] == c) for c in CURRENCIES if any(ln["currency"] == c for ln in lines)}}
    org._put("payroll_register", rec, by)
    return rec


def _who(s: str | None) -> str:
    return str(s or "").split(" (")[0].strip().lower()


def approve_register(org, period: str, step: str, by: str, role: str) -> dict:
    """HR approves first, then the FCO; the person who prepared it approves neither step."""
    rec = register(org, period)
    if rec is None:
        raise DoAError(f"no payroll register for {period}")
    if _who(by) == _who(rec.get("prepared_by")):
        raise PermissionError("the person who prepared the register cannot approve it")
    if step == "hr":
        if role not in ("hq_admin", "admin", "super_admin", "finance_manager"):
            raise PermissionError(f"role {role} may not approve a register for HR")
        rec |= {"hr_approved_by": by, "status": "hr_approved"}
    elif step == "fco":
        if rec.get("hr_approved_by") is None:
            raise DoAError("HR approves the register first")
        if role not in ("super_admin", "cfo"):
            raise PermissionError(f"role {role} is not the FCO")
        if _who(by) == _who(rec["hr_approved_by"]):
            raise PermissionError("HR and the FCO approval come from two different people")
        rec |= {"fco_approved_by": by, "status": "approved"}
    else:
        raise DoAError("the step is hr or fco")
    org._put("payroll_register", rec, by)
    return rec


def _previous_register(org, period: str) -> dict | None:
    done = sorted((r for r in org.list("payroll_register") if r["status"] == "approved" and r["period"] < period), key=lambda r: r["period"])
    return done[-1] if done else None


# -- the verdict ------------------------------------------------------------------------------------------------------
def evaluate(org, payee_status, payees: dict, today: dt.date) -> list[dict]:
    """Each category this period: its policy, the source document's lines judged against it (auto, approve, never,
    with why), and whether the pay date has come. A category whose source document is not connected says so."""
    out = []
    for c in INTERNAL:
        pol = policy(org, c["key"])
        row = {"key": c["key"], "enabled": pol["enabled"], "period": None, "pay_date": None, "due": False, "lines": [], "summary": None, "reasons": []}
        pd = period_and_date(pol, today)
        if pd:
            row["period"], date = pd
            row["pay_date"], row["due"] = date.isoformat(), today >= date
        if not pol["enabled"]:
            row["summary"], row["reasons"] = "off", ["the delegation is switched off: every payment in this category waits for a person"]
        elif not c["connected"]:
            row["summary"], row["reasons"] = "not_connected", [f"the source document ({c['source'].split(':')[0].lower()}) is not connected yet; nothing is paid from this category"]
        elif c["key"] == "personnel":
            _personnel(org, pol, payee_status, payees, row)
        out.append(row)
    return out


def _personnel(org, pol, payee_status, payees, row) -> None:
    reg = register(org, row["period"]) if row["period"] else None
    if reg is None:
        row["summary"], row["reasons"] = "no_document", [f"no payroll register for {row['period']}"]
        return
    if reg["status"] != "approved":
        row["summary"], row["reasons"] = "never", [f"the payroll register for {row['period']} is {reg['status'].replace('_', ' ')}, not approved by HR and the FCO"]
        row["lines"] = [{"employee": ln["employee"], "verdict": "never", "reasons": ["register not approved"]} for ln in reg["lines"]]
        return
    prev = _previous_register(org, row["period"])
    whole = ["the first payroll paid under this delegation"] if prev is None else []
    known = {_slug(ln["employee"]) for ln in (prev or {}).get("lines", [])}
    for i, ln in enumerate(reg["lines"]):
        never, approve = [], []
        p = payees.get(ln["payee_id"]) or {}
        status = payee_status(ln["payee_id"])
        if not p:
            never.append("the bank account named is not a registered payee")
        elif status not in PAYEE_OK:
            never.append(f"the bank account is {str(status or 'not onboarded').lower()}, not verified")
        elif p.get("currency") and p["currency"] != ln["currency"]:
            never.append(f"the account receives {p['currency']}, not {ln['currency']}")
        if ln["currency"] not in pol["currencies"]:
            never.append(f"{ln['currency']} is not a payout currency the delegation allows")
        try:
            org.account_code(ln["charge_code"])
        except Exception as exc:  # noqa: BLE001 (OrgError, with the reason)
            never.append(f"charge code: {exc}")
        if prev is not None and _slug(ln["employee"]) not in known:
            approve.append("new on the register")
        approve += whole
        row["lines"].append({"line": i, "employee": ln["employee"], "payee_id": ln["payee_id"], "currency": ln["currency"], "amount_minor": ln["amount_minor"],
                             "charge_code": ln["charge_code"], "verdict": "never" if never else ("approve" if approve else "auto"), "reasons": never or approve,
                             "ref": f"doa:personnel:{row['period']}:{_slug(ln['employee'])}"})
    n = {v: sum(1 for x in row["lines"] if x["verdict"] == v) for v in ("auto", "approve", "never")}
    row["summary"] = "auto" if n["auto"] == len(row["lines"]) else ("never" if n["never"] == len(row["lines"]) else "mixed")
    row["reasons"] = [f"{n['auto']} line(s) the agent pays on its own, {n['approve']} prepared for a person, {n['never']} never executed"]


def book(base: TreasuryState, row: dict, lines: list[dict], payees: dict, today: dt.date) -> TreasuryState:
    """The book of the lines to pay now: one obligation per line, its evidence the approved source document."""
    c = BY_KEY[row["key"]]
    obls, ev = [], []
    for ln in lines:
        p = payees.get(ln["payee_id"]) or {}
        oid = f"DOA-{row['key'].upper()}-{row['period']}-{ln['line'] + 1:03d}"
        obls.append({"obligation_id": oid, "amount_minor": ln["amount_minor"], "currency": ln["currency"], "priority": c["priority"],
                     "due_in_hours": max(0, (dt.date.fromisoformat(row["pay_date"]) - today).days * 24), "beneficiary_id": ln["payee_id"],
                     "payee_name": p.get("name"), "payout_method": p.get("payout_method", "LOCAL"), "evidence_ref": ln["ref"],
                     "invoice_fingerprint": ln["ref"], "budget_line": p.get("budget_line"), "account_code": ln["charge_code"], "due_date": row["pay_date"]})
        ev.append({"ref": ln["ref"], "subject_id": oid, "source": f"doa:{row['key']}:{row['period']}", "status": "verified",
                   "summary": f"{c['title']} {row['period']}, {ln['employee']}: {c['source']}"})
    data = base.model_dump()
    data["obligations"], data["evidence"], data["receivables"] = obls, ev, []
    data["as_of"] = today.isoformat()
    return TreasuryState.model_validate(data)


def record_run(org, row: dict, ln: dict, status: str, by: str, execution_id: str | None, approved_by: str | None, message: str) -> dict:
    rec = {"_id": ln["ref"], "category": row["key"], "period": row["period"], "employee": ln["employee"], "amount_minor": ln["amount_minor"],
           "currency": ln["currency"], "status": status, "execution_id": execution_id, "approved_by": approved_by, "by": by, "message": message[:300],
           "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
    org._put("doa_run", rec, by)
    return rec


def autopilot(org) -> bool:
    return bool((org.get("doa_settings", "doa_settings") or {}).get("autopilot", False))
