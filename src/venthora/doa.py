# ruff: noqa: E501
"""Delegation of Authority (DoA): what the FCO delegates to VENTHORA's agent, by kind of payment.

Internal payments are the organisation's own recurring obligations, in the cost categories of a donor-funded budget
(2 CFR 200 cost principles: personnel §200.430, fringe benefits §200.431, professional services §200.459, travel
§200.475, other direct costs). The FCO sets one standing payment instruction per category: the payee, the charge
code, the schedule, where the amount comes from, the variance and the limit. On the pay date the agent pays on its
own whatever the instruction allows; the rest it prepares for a person, and some things it never does.

External payments are invoices: vendors (three-way match against a purchase order) and short-term, non-regular
specialists and experts (paid against their contract's terms). They run through the intake queue and the standing
mandate (src/venthora/mandate.py); this module only states their delegation.

Every run is recorded (kind doa_run, one per category and period), and every payout carries the fingerprint
doa:<category>:<period>, so the executor's duplicate lock never pays a period twice.
"""
from __future__ import annotations

import calendar
import datetime as dt
from typing import Any

from .domain.models import TreasuryState

PAYEE_OK = ("VERIFIED", "ACTIVE")
CURRENCIES = ("USD", "EUR", "GBP")  # the payout currency; the agent converts from base when it is not USD
SCHEDULES = ("monthly", "quarterly", "annual", "per_document")
SOURCES = ("register", "fixed", "document")
PAY_DAYS = ("last_working_day",) + tuple(str(d) for d in range(1, 29))

INTERNAL: list[dict[str, Any]] = [
    {"key": "personnel", "code": "01", "title": "Personnel", "basis": "Salaries and wages (2 CFR 200.430)",
     "evidence": "the payroll register for the period, prepared by HR and approved by the FCO", "account": "6010", "priority": 10,
     "auto": "On the pay date, the approved payroll register's total, within the variance against the last period, to a verified payroll account.",
     "approve": "A register with someone new on it, a total outside the variance or above the limit, a budget line it would exceed.",
     "never": "A register that is not approved by both HR and the FCO; a payroll account that is not verified."},
    {"key": "fringe", "code": "02", "title": "Fringe benefits", "basis": "Insurance, pension, social security, leave (2 CFR 200.431)",
     "evidence": "the premium schedule of the policy or plan", "account": "6020", "priority": 9,
     "auto": "Each premium on its due date, up to the instruction's amount, to the registered insurer or fund.",
     "approve": "A premium above the amount or the limit; the first premium of a new policy.",
     "never": "A premium to anyone but the registered insurer or fund."},
    {"key": "consultants", "code": "03", "title": "Experts and consultants", "basis": "Regular consultants on a retainer (2 CFR 200.459)",
     "evidence": "the consultant's contract and the period's approved timesheet", "account": "6200", "priority": 7,
     "auto": "The monthly fee in the contract, inside the contract's ceiling and term.",
     "approve": "A fee above the contract's monthly amount, or the last month before the ceiling is reached.",
     "never": "A fee with no active contract, or after the contract's end date."},
    {"key": "travel", "code": "04", "title": "Travel and transport", "basis": "Travel, per diem and transport (2 CFR 200.475)",
     "evidence": "an approved travel authorisation and the per diem rates", "account": "6400", "priority": 6,
     "auto": "Advances and per diem under an approved travel authorisation, at the per diem rates.",
     "approve": "Anything above the rates or outside the authorisation.",
     "never": "A payment with no travel authorisation; a new advance while an earlier one is not liquidated."},
    {"key": "office", "code": "05", "title": "Office and operations", "basis": "Rent, utilities, communications, software",
     "evidence": "the recurring contract or subscription", "account": "6300", "priority": 6,
     "auto": "The recurring bill on its due date, up to the instruction's amount, to the registered provider.",
     "approve": "A bill above the amount or the limit.",
     "never": "A provider that is not registered and verified."},
    {"key": "other_direct", "code": "06", "title": "Other direct costs", "basis": "Programme activity costs not in another category",
     "evidence": "the approved activity budget or work plan", "account": "6900", "priority": 5,
     "auto": "Costs inside an approved activity budget, up to the limit per activity.",
     "approve": "Anything outside the work plan or above the limit.",
     "never": "A charge code that is not active."},
]
EXTERNAL: list[dict[str, Any]] = [
    {"key": "vendors", "title": "Vendor invoices", "basis": "Supplies, services and equipment against a purchase order",
     "evidence": "the purchase order, proof of delivery and the invoice (three-way match)",
     "auto": "A verified vendor's invoice that quotes an open purchase order and matches it within the tolerance, up to the single-payment limit.",
     "approve": "Above a limit, a first payment to a new vendor, a vendor in its cooling period, a budget line exceeded.",
     "never": "No purchase order, a duplicate invoice, bank details changed by email, an unverified vendor."},
    {"key": "specialists", "title": "Short-term specialists and experts", "basis": "Non-regular consultants paid per deliverable (2 CFR 200.459)",
     "evidence": "the contract or statement of work and the accepted deliverable",
     "auto": "A deliverable's payment term in the contract, inside the contract's total, from the specialist's registered mailbox.",
     "approve": "An amount that is not a term of the contract, or the term that reaches the contract's total.",
     "never": "No contract on file, or payment beyond its total."},
]
BY_KEY = {c["key"]: c for c in INTERNAL}
ACCOUNTS = {"6010": "Salaries and wages", "6020": "Fringe benefits", "6900": "Other direct costs"}

DEFAULTS: dict[str, dict] = {
    "personnel": {"enabled": True, "payee_id": "payroll-group-us", "charge_code": "US-200.OPS.6010", "schedule": "monthly", "pay_day": "last_working_day",
                  "source": "register", "currency": "USD", "amount_minor": None, "variance_bp": 200, "limit_minor": 50_000_000},
    "fringe": {"enabled": False, "payee_id": None, "charge_code": "US-200.OPS.6020", "schedule": "monthly", "pay_day": "1",
               "source": "fixed", "currency": "USD", "amount_minor": None, "variance_bp": 0, "limit_minor": 5_000_000},
    "consultants": {"enabled": True, "payee_id": "ben-contractor-okafor", "charge_code": "US-200.OPS.6200", "schedule": "monthly", "pay_day": "last_working_day",
                    "source": "fixed", "currency": "USD", "amount_minor": 320_000, "variance_bp": 0, "limit_minor": 500_000},
    "travel": {"enabled": False, "payee_id": None, "charge_code": "US-200.OPS.6400", "schedule": "per_document", "pay_day": "1",
               "source": "document", "currency": "USD", "amount_minor": None, "variance_bp": 0, "limit_minor": 300_000},
    "office": {"enabled": True, "payee_id": "ben-saas-vendor", "charge_code": "US-200.OPS.6300", "schedule": "monthly", "pay_day": "1",
               "source": "fixed", "currency": "USD", "amount_minor": 45_000, "variance_bp": 0, "limit_minor": 200_000},
    "other_direct": {"enabled": False, "payee_id": None, "charge_code": "US-200.OPS.6900", "schedule": "per_document", "pay_day": "1",
                     "source": "document", "currency": "USD", "amount_minor": None, "variance_bp": 0, "limit_minor": 1_000_000},
}


class DoAError(ValueError):
    pass


def ensure_accounts(org) -> None:
    """The chart of accounts gets the DoA categories' accounts it may not have yet."""
    have = {a["code"] for a in org.list("account")}
    for code, name in ACCOUNTS.items():
        if code not in have:
            org._put("account", {"account_id": f"ac-{code}", "code": code, "name": name, "category": BY_KEY[{"6010": "personnel", "6020": "fringe", "6900": "other_direct"}[code]]["title"], "active": True}, "doa")


# -- schedule ---------------------------------------------------------------------------------------------------------
def _last_working_day(y: int, m: int) -> dt.date:
    d = dt.date(y, m, calendar.monthrange(y, m)[1])
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def _day(y: int, m: int, pay_day: str) -> dt.date:
    return _last_working_day(y, m) if pay_day == "last_working_day" else dt.date(y, m, int(pay_day))


def period_and_date(ins: dict, today: dt.date) -> tuple[str, dt.date] | None:
    """The period the instruction pays now (or next) and its pay date; None for per-document instructions."""
    y, m = today.year, today.month
    if ins["schedule"] == "monthly":
        return f"{y}-{m:02d}", _day(y, m, ins["pay_day"])
    if ins["schedule"] == "quarterly":
        q = (m - 1) // 3 + 1
        return f"{y}-Q{q}", _day(y, q * 3, ins["pay_day"])
    if ins["schedule"] == "annual":
        return f"{y}", _day(y, 12, ins["pay_day"])
    return None


# -- records ----------------------------------------------------------------------------------------------------------
def instruction(org, key: str) -> dict:
    return DEFAULTS[key] | (org.get("doa_instruction", key) or {}) | {"_id": key, "category": key}


def save_instruction(org, key: str, body: dict, by: str) -> dict:
    if key not in BY_KEY:
        raise DoAError(f"{key}: not an internal payment category")
    rec = instruction(org, key) | {k: v for k, v in body.items() if k in DEFAULTS[key]}
    if rec["currency"] not in CURRENCIES:
        raise DoAError(f"the payout currency is one of: {', '.join(CURRENCIES)}")
    if rec["schedule"] not in SCHEDULES:
        raise DoAError(f"the schedule is one of: {', '.join(SCHEDULES)}")
    if rec["source"] not in SOURCES or (rec["source"] == "register" and key != "personnel"):
        raise DoAError("the amount comes from a fixed amount or a document; only personnel uses the payroll register")
    if rec["pay_day"] not in PAY_DAYS:
        raise DoAError("the pay day is a day from 1 to 28, or the last working day")
    if rec["schedule"] == "per_document" and rec["source"] != "document":
        raise DoAError("an instruction paid per document takes its amount from the document")
    if rec["source"] == "fixed" and rec["enabled"] and not (isinstance(rec["amount_minor"], int) and rec["amount_minor"] > 0):
        raise DoAError("a fixed instruction needs its amount")
    for f in ("limit_minor", "variance_bp"):
        if not isinstance(rec[f], int) or rec[f] < 0:
            raise DoAError(f"{f}: a whole number of at least 0")
    org.account_code(rec["charge_code"])  # OrgError when the code is not valid
    rec["updated_by"] = by
    org._put("doa_instruction", rec, by)
    return rec


def register(org, period: str) -> dict | None:
    return org.get("payroll_register", period)


def save_register(org, period: str, lines: list[dict], currency: str, by: str) -> dict:
    """A payroll register for one period: employee, net amount, charge code. Saving it again resets its approvals."""
    if not lines:
        raise DoAError("a register needs at least one line")
    if currency not in CURRENCIES:
        raise DoAError(f"the register's currency is one of: {', '.join(CURRENCIES)}")
    for ln in lines:
        if not str(ln.get("employee", "")).strip() or not isinstance(ln.get("amount_minor"), int) or ln["amount_minor"] <= 0:
            raise DoAError("every line needs the employee and a net amount above zero")
        org.account_code(ln.get("charge_code") or instruction(org, "personnel")["charge_code"])
    rec = {"_id": period, "period": period, "currency": currency, "lines": lines, "total_minor": sum(ln["amount_minor"] for ln in lines),
           "status": "draft", "prepared_by": by, "hr_approved_by": None, "fco_approved_by": None}
    org._put("payroll_register", rec, by)
    return rec


def approve_register(org, period: str, step: str, by: str, role: str) -> dict:
    """HR approves first, then the FCO; the person who prepared it approves neither step."""
    rec = register(org, period)
    if rec is None:
        raise DoAError(f"no payroll register for {period}")
    who = by.split(" (")[0].strip().lower()
    if who == str(rec.get("prepared_by", "")).split(" (")[0].strip().lower():
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
        if who == str(rec["hr_approved_by"]).split(" (")[0].strip().lower():
            raise PermissionError("HR and the FCO approval come from two different people")
        rec |= {"fco_approved_by": by, "status": "approved"}
    else:
        raise DoAError("the step is hr or fco")
    org._put("payroll_register", rec, by)
    return rec


def _previous_register(org, period: str) -> dict | None:
    done = sorted((r for r in org.list("payroll_register") if r["status"] == "approved" and r["period"] < period), key=lambda r: r["period"])
    return done[-1] if done else None


def last_run(org, key: str) -> dict | None:
    runs = sorted((r for r in org.list("doa_run") if r["category"] == key and r["status"] in ("PAID", "IN_FLIGHT")), key=lambda r: r["period"])
    return runs[-1] if runs else None


# -- the verdict ------------------------------------------------------------------------------------------------------
def evaluate(org, payee_status, payees: dict, today: dt.date) -> list[dict]:
    """Each internal instruction for its current period: the amount, the pay date, whether it is due, and the DoA's
    verdict: auto (the agent pays it on its own), approve (prepared for a person), never (with why)."""
    out = []
    for c in INTERNAL:
        ins = instruction(org, c["key"])
        p = payees.get(ins["payee_id"] or "") or {}
        row = {"key": c["key"], "title": c["title"], "enabled": ins["enabled"], "schedule": ins["schedule"], "payee_id": ins["payee_id"],
               "payee": p.get("name"), "payee_currency": p.get("currency"), "charge_code": ins["charge_code"], "currency": ins["currency"],
               "period": None, "pay_date": None, "due": False, "amount_minor": None, "verdict": None, "reasons": [], "run": None}
        pd = period_and_date(ins, today)
        if pd is None:
            row["verdict"], row["reasons"] = "per_document", ["paid per approved document, not on a schedule"]
            out.append(row)
            continue
        row["period"], date = pd
        row["pay_date"] = date.isoformat()
        row["due"] = today >= date
        row["run"] = org.get("doa_run", f"{c['key']}:{row['period']}")
        never, approve = [], []
        if not ins["enabled"]:
            never.append("the instruction is switched off")
        if not ins["payee_id"]:
            never.append("no payee is named")
        elif payee_status(ins["payee_id"]) not in PAYEE_OK:
            never.append(f"the payee is {str(payee_status(ins['payee_id']) or 'not registered').lower()}, not verified")
        elif p.get("currency") and p["currency"] != ins["currency"]:
            never.append(f"{p['name']}'s bank account receives {p['currency']}, not {ins['currency']}: register an account in "
                         f"{ins['currency']} or pay in {p['currency']}")
        try:
            org.account_code(ins["charge_code"])
        except Exception as exc:  # noqa: BLE001 (OrgError, with the reason)
            never.append(f"charge code: {exc}")
        amount = None
        if ins["source"] == "register":
            reg = register(org, row["period"])
            if reg is None:
                never.append(f"no payroll register for {row['period']}")
            elif reg["currency"] != ins["currency"]:
                never.append(f"the payroll register for {row['period']} is in {reg['currency']}, the instruction pays in {ins['currency']}")
            elif reg["status"] != "approved":
                never.append(f"the payroll register for {row['period']} is {reg['status'].replace('_', ' ')}, not approved by HR and the FCO")
            else:
                amount = reg["total_minor"]
                prev = _previous_register(org, row["period"])
                if prev is None:
                    approve.append("the first payroll paid under this instruction")
                else:
                    new = sorted({ln["employee"] for ln in reg["lines"]} - {ln["employee"] for ln in prev["lines"]})
                    if new:
                        approve.append("new on the register: " + ", ".join(new))
                    var = abs(amount - prev["total_minor"]) * 10_000 // max(prev["total_minor"], 1)
                    if var > ins["variance_bp"]:
                        approve.append(f"total {var / 100:.1f}% from {prev['period']}, the variance allowed is {ins['variance_bp'] / 100:.1f}%")
        elif ins["source"] == "fixed":
            amount = ins["amount_minor"]
        if amount is not None and amount > ins["limit_minor"]:
            approve.append(f"{ins['currency']} {amount / 100:,.2f} is above the limit of {ins['currency']} {ins['limit_minor'] / 100:,.2f}")
        row["amount_minor"] = amount
        row["verdict"] = "never" if never else ("approve" if approve else "auto")
        row["reasons"] = never or approve
        out.append(row)
    return out


def obligations_for(rows: list[dict], registry_payees: dict, today: dt.date) -> tuple[list[dict], list[dict]]:
    """The book's obligations and evidence for the rows to pay now."""
    obls, ev = [], []
    for r in rows:
        c, p = BY_KEY[r["key"]], registry_payees.get(r["payee_id"]) or {}
        oid = f"DOA-{r['key'].upper()}-{r['period']}"
        ref = f"doa:{r['key']}:{r['period']}"
        obls.append({"obligation_id": oid, "amount_minor": r["amount_minor"], "currency": r["currency"], "priority": c["priority"],
                     "due_in_hours": max(0, (dt.date.fromisoformat(r["pay_date"]) - today).days * 24), "beneficiary_id": r["payee_id"],
                     "payee_name": p.get("name"), "payout_method": p.get("payout_method", "LOCAL"), "evidence_ref": ref,
                     "invoice_fingerprint": ref, "budget_line": p.get("budget_line"), "account_code": r["charge_code"], "due_date": r["pay_date"]})
        ev.append({"ref": ref, "subject_id": oid, "source": f"doa:standing_instruction:{r['key']}", "status": "verified",
                   "summary": f"{c['title']} for {r['period']}: standing payment instruction set by the FCO; {c['evidence']}"})
    return obls, ev


def book(base: TreasuryState, rows: list[dict], registry_payees: dict, today: dt.date) -> TreasuryState:
    obls, ev = obligations_for(rows, registry_payees, today)
    data = base.model_dump()
    data["obligations"], data["evidence"], data["receivables"] = obls, ev, []
    data["as_of"] = today.isoformat()
    return TreasuryState.model_validate(data)


def record_run(org, row: dict, status: str, by: str, execution_id: str | None, approved_by: str | None, message: str) -> dict:
    rec = {"_id": f"{row['key']}:{row['period']}", "category": row["key"], "period": row["period"], "amount_minor": row["amount_minor"],
           "currency": row["currency"], "payee_id": row["payee_id"], "charge_code": row["charge_code"], "status": status,
           "execution_id": execution_id, "approved_by": approved_by, "by": by, "message": message[:300],
           "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
    org._put("doa_run", rec, by)
    return rec


def autopilot(org) -> bool:
    return bool((org.get("doa_settings", "doa_settings") or {}).get("autopilot", False))
