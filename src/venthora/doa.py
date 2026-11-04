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
# Payout currencies in Airwallex's payout network (SWIFT and local clearing, from airwallex.com/guides/payout and the
# market-coverage docs; a currency may be limited to some destinations, which the beneficiary schema checks).
CURRENCIES = ("USD", "EUR", "GBP", "AED", "AUD", "BDT", "CAD", "CHF", "CNY", "CZK", "DKK", "HKD", "HUF", "IDR", "ILS", "INR", "JPY",
              "KRW", "LKR", "MXN", "MYR", "NOK", "NPR", "NZD", "PHP", "PKR", "PLN", "RON", "SAR", "SEK", "SGD", "THB", "TRY", "VND", "ZAR")
# what VENTHORA's treasury engine converts and pays today; a line in another allowed currency waits for a person
ENGINE_CURRENCIES = ("USD", "EUR", "GBP")
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
     "source": "The consultant's payment request, by email from the senders chosen below, with every document ticked attached", "connected": True,
     "auto": "A request from an allowed sender, with every required document attached, inside the consultant's contract value (unless exceeding claims are ignored).",
     "approve": "A request while the delegation is switched off.",
     "never": "A request from any other sender, or with a required document missing: it is rejected, not held."},
    {"key": "travel", "code": "04", "title": "Travel and transport", "basis": "Travel, per diem and transport (2 CFR 200.475)", "priority": 6, "account": "6400",
     "source": "The traveller's request, with the documents ticked below, for a trip between the places chosen, at the organisation's travel rates", "connected": True,
     "auto": "A complete request for a trip from an allowed origin to an allowed destination, at or below that city's lodging and per diem rates.",
     "approve": "A trip to a destination with no travel rate on file, or an amount above the rates.",
     "never": "A trip outside the places chosen, a request with a required document missing, or one from a channel the delegation does not accept."},
    {"key": "office", "code": "05", "title": "Office and operations", "basis": "Rent, utilities, communications, software", "priority": 6, "account": "6300",
     "source": "The vendor's bill without a purchase order, by email from the senders chosen below, carrying everything ticked below", "connected": True,
     "auto": "In real time, as soon as a bill arrives from an allowed sender and carries everything ticked below; paid to the vendor's registered account.",
     "approve": "A bill that misses something ticked below, or arrives while the delegation is off.",
     "never": "A vendor that is not registered and verified. A bill that quotes a purchase order is an external vendor invoice instead."},
    {"key": "cash_advance", "code": "06", "title": "Cash advance and slush fund", "basis": "Advances to staff and imprest (petty cash) floats, settled later against receipts", "priority": 6, "account": "1400",
     "source": "An instruction by email from the one person chosen below, naming the person to pay and the amount", "connected": True,
     "auto": "In real time, when the instruction comes from the person chosen, names someone in People and roles with a registered bank account, and states the amount.",
     "approve": "An instruction for someone without a registered bank account, or in a currency their account does not receive.",
     "never": "An instruction from anyone else. The advance is booked as a receivable from the person until it is settled."},
    {"key": "other_direct", "code": "07", "title": "Other direct costs (ODC)", "basis": "Programme activity costs not in another category", "priority": 5, "account": "6900",
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

# Fringe benefits by IAS 19 class (Employee Benefits). paid_to: who receives the money. A benefit paid to a third party
# (an insurer, a fund, an authority) is paid from this category; one paid to the employee is paid with the payroll.
FREQUENCIES = ("monthly", "quarterly", "semi_annual", "annual", "on_event")
BENEFIT_GROUPS = {"short_term": "Short-term employee benefits", "post_employment": "Post-employment benefits",
                  "long_term": "Other long-term benefits", "termination": "Termination benefits", "international": "International assignments"}
BENEFITS: list[dict[str, str]] = [
    {"key": "social_security", "group": "short_term", "label": "Social security and state pension, employer share", "paid_to": "authority", "frequency": "monthly"},
    {"key": "national_health", "group": "short_term", "label": "National health insurance, employer share", "paid_to": "authority", "frequency": "monthly"},
    {"key": "workers_comp", "group": "short_term", "label": "Workers' compensation (work accident) insurance", "paid_to": "insurer", "frequency": "monthly"},
    {"key": "unemployment", "group": "short_term", "label": "Unemployment insurance", "paid_to": "authority", "frequency": "quarterly"},
    {"key": "health", "group": "short_term", "label": "Group health insurance", "paid_to": "insurer", "frequency": "monthly"},
    {"key": "dental_vision", "group": "short_term", "label": "Dental and vision insurance", "paid_to": "insurer", "frequency": "monthly"},
    {"key": "life", "group": "short_term", "label": "Group life insurance", "paid_to": "insurer", "frequency": "annual"},
    {"key": "short_disability", "group": "short_term", "label": "Short-term disability and accident insurance", "paid_to": "insurer", "frequency": "annual"},
    {"key": "paid_leave", "group": "short_term", "label": "Paid leave: annual, sick, parental", "paid_to": "employee", "frequency": "monthly"},
    {"key": "bonus", "group": "short_term", "label": "Bonuses and profit-sharing", "paid_to": "employee", "frequency": "annual"},
    {"key": "thirteenth", "group": "short_term", "label": "Thirteenth-month or religious holiday allowance", "paid_to": "employee", "frequency": "annual"},
    {"key": "housing", "group": "short_term", "label": "Housing provided or housing allowance", "paid_to": "vendor", "frequency": "monthly"},
    {"key": "vehicle", "group": "short_term", "label": "Company vehicle or transport allowance", "paid_to": "vendor", "frequency": "monthly"},
    {"key": "meals_phone", "group": "short_term", "label": "Meals, communications and other benefits in kind", "paid_to": "vendor", "frequency": "monthly"},
    {"key": "training", "group": "short_term", "label": "Education and training assistance", "paid_to": "vendor", "frequency": "on_event"},
    {"key": "childcare_wellness", "group": "short_term", "label": "Childcare, wellness and employee assistance programme", "paid_to": "vendor", "frequency": "monthly"},
    {"key": "pension_dc", "group": "post_employment", "label": "Pension, defined contribution (employer contribution)", "paid_to": "fund", "frequency": "monthly"},
    {"key": "pension_db", "group": "post_employment", "label": "Pension, defined benefit (funding contribution)", "paid_to": "fund", "frequency": "quarterly"},
    {"key": "retiree_medical", "group": "post_employment", "label": "Post-employment medical and life insurance", "paid_to": "insurer", "frequency": "annual"},
    {"key": "long_service", "group": "long_term", "label": "Long-service leave, sabbatical, jubilee awards", "paid_to": "employee", "frequency": "on_event"},
    {"key": "long_disability", "group": "long_term", "label": "Long-term disability insurance", "paid_to": "insurer", "frequency": "annual"},
    {"key": "severance", "group": "termination", "label": "Severance and termination payments", "paid_to": "employee", "frequency": "on_event"},
    {"key": "post_differential", "group": "international", "label": "Post differential, hardship and danger pay", "paid_to": "employee", "frequency": "monthly"},
    {"key": "expat_allowances", "group": "international", "label": "Housing, education and cost-of-living allowances abroad", "paid_to": "employee", "frequency": "monthly"},
    {"key": "medevac_dba", "group": "international", "label": "Medical evacuation and overseas workers' insurance (e.g. Defense Base Act)", "paid_to": "insurer", "frequency": "annual"},
]
BENEFIT_KEYS = {b["key"] for b in BENEFITS}

# Experts and consultants: who may send a payment request, and what it must carry
CONSULTANT_SENDERS = {"consultant": "The individual consultant, from their registered mailbox", "procurement": "Procurement team", "finance": "Finance team"}
CONSULTANT_DOCUMENTS = {  # keys are document types the document validation recognises (src/venthora/intake/validation.py)
    "invoice": "Invoice, signed, quoting the contract",
    "timesheet": "Timesheet for the period, approved",
    "progress_report": "Progress or deliverable report",
    "deliverable_acceptance": "Deliverable acceptance by the technical lead",
    "expense_report": "Expense report with receipts, for reimbursable costs",
    "tax_registration": "Tax registration (W-9, W-8BEN, NPWP or VAT number)",
}
PAID_TO = {"authority": "a government authority", "insurer": "an insurer", "fund": "a pension fund", "vendor": "a provider", "employee": "the employee, with the payroll"}
ACCOUNTS = {"6010": ("Salaries and wages", "personnel"), "6020": ("Fringe benefits", "fringe"), "6900": ("Other direct costs", "other_direct"),
            "1400": ("Advances to staff and imprest", "cash_advance")}

# the policy, per category: nothing here names a person or an amount owed
DEFAULTS: dict[str, dict] = {
    "personnel": {"enabled": True, "schedule": "monthly", "pay_day": "last_working_day", "currencies": ["USD", "EUR", "GBP"]},
    "fringe": {"enabled": True, "schedule": "monthly", "pay_day": "1", "currencies": ["USD"],
               "benefits": {b["key"]: {"enabled": b["key"] in ("social_security", "national_health", "workers_comp", "health", "pension_dc"), "frequency": b["frequency"]}
                            for b in BENEFITS}},
    "consultants": {"enabled": True, "schedule": "per_document", "pay_day": "1", "currencies": ["USD", "EUR", "GBP"],
                    "senders": ["consultant"], "procurement_people": [], "finance_people": [], "documents": ["invoice"], "ignore_exceed_claim": False},
    "travel": {"enabled": True, "schedule": "per_document", "pay_day": "1", "currencies": ["USD", "EUR", "GBP"],
               "origin_countries": ["*"], "origin_cities": ["*"], "dest_countries": ["*"], "dest_cities": ["*"],
               "documents": ["travel_request", "receipts"], "channel": "email", "senders": ["traveller"], "finance_people": [], "travel_desk_people": []},
    "office": {"enabled": True, "schedule": "per_document", "pay_day": "1", "currencies": ["USD"], "senders": ["vendor"], "procurement_people": [],
               "operations_people": [], "finance_people": [], "requirements": ["pr_number", "vendor_contact", "amount", "bank_details"]},
    "cash_advance": {"enabled": True, "schedule": "per_document", "pay_day": "1", "currencies": ["USD", "EUR", "GBP"], "requester": None},
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
    out = DEFAULTS[key] | saved | {"_id": key, "category": key}
    if "benefits" in DEFAULTS[key]:  # a benefit added to the catalog later starts from its default
        out["benefits"] = DEFAULTS[key]["benefits"] | {k: v for k, v in (saved.get("benefits") or {}).items() if k in BENEFIT_KEYS}
    return out


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
    if key == "consultants":
        _check_consultants(org, rec)
    if key == "travel":
        _check_travel(org, rec)
    if key == "office":
        _check_office(org, rec)
    if key == "cash_advance" and rec["requester"] is not None and not any(u["user_id"] == rec["requester"] and u.get("active", True) for u in org.list("user")):
        raise DoAError("choose the one person whose email instructs a cash advance, from People and roles")
    if "benefits" in DEFAULTS[key]:
        merged = policy(org, key)["benefits"]
        for k, v in (body.get("benefits") or {}).items():
            if k not in BENEFIT_KEYS:
                raise DoAError(f"{k}: not a benefit in the catalog")
            if not isinstance(v, dict) or v.get("frequency", merged[k]["frequency"]) not in FREQUENCIES:
                raise DoAError(f"{k}: the frequency is one of {', '.join(FREQUENCIES)}")
            merged[k] = {"enabled": bool(v.get("enabled", merged[k]["enabled"])), "frequency": v.get("frequency", merged[k]["frequency"])}
        rec["benefits"] = merged
    rec["updated_by"] = by
    org._put("doa_policy", rec, by)
    return rec


def _check_consultants(org, rec: dict) -> None:
    if not rec["senders"] or any(x not in CONSULTANT_SENDERS for x in rec["senders"]):
        raise DoAError("choose who may send a consultant's payment request: " + ", ".join(CONSULTANT_SENDERS.values()))
    users = {u["user_id"]: u for u in org.list("user") if u.get("active", True)}
    for team, field in (("procurement", "procurement_people"), ("finance", "finance_people")):
        people = rec.get(field) or []
        if any(p not in users for p in people):
            raise DoAError(f"{CONSULTANT_SENDERS[team]}: choose people from People and roles")
        if team in rec["senders"] and not people:
            raise DoAError(f"{CONSULTANT_SENDERS[team]}: choose at least one person whose email the agent accepts")
        if team not in rec["senders"]:
            rec[field] = []
    if not rec["documents"] or any(d not in CONSULTANT_DOCUMENTS for d in rec["documents"]):
        raise DoAError("choose at least one document a consultant's payment request must carry")
    rec["ignore_exceed_claim"] = bool(rec["ignore_exceed_claim"])


OFFICE_SENDERS = {"vendor": "The vendor, from its registered mailbox", "procurement": "Procurement team", "operations": "Admin or operations team",
                  "finance": "Finance team"}
OFFICE_REQUIREMENTS = {
    "pr_number": "A purchase request number",
    "vendor_contact": "The vendor's name, address, phone number and email",
    "amount": "A clear amount to pay",
    "bank_details": "The vendor's bank account number, bank and country",
}
_PR = re.compile(r"\b(?:PR|purchase request)(?:[\s:#-]*(?:no\.?|number|nr\.?))?[\s:#-]*\d[A-Z0-9/-]{1,}|\bpurchase request[\s:#-]+[A-Z0-9][A-Z0-9/-]{2,}", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?:tel|phone|mobile|hp|telp)?[.:\s]*\+?\(?\d[\d\s().-]{7,}\d", re.IGNORECASE)
_ADDRESS = re.compile(r"\b(?:address|street|st\.|road|rd\.|avenue|ave\.?|jalan|jl\.|blvd|boulevard|suite|floor|lane|strasse|straße)\b", re.IGNORECASE)
_ACCOUNT = re.compile(r"\b(?:account(?: number| no\.?)?|acct|a/c|iban|rekening|no\.? rek)\b[^\n]{0,30}?[A-Z]{0,2}\d[\d\s-]{5,}", re.IGNORECASE)
_BANK = re.compile(r"\bbank\b", re.IGNORECASE)
_COUNTRY = re.compile(r"\b(?:country|united states|usa|united kingdom|england|germany|indonesia|singapore|australia|kenya|france|netherlands|japan|india|canada)\b", re.IGNORECASE)


def office_missing(text: str, vendor_name: str | None, amount, requirements: list[str]) -> list[str]:
    """What a bill lacks of the office and operations requirements (empty: it may be paid on the agent's own authority)."""
    have = {
        "pr_number": bool(_PR.search(text)),
        "vendor_contact": bool(vendor_name and vendor_name.lower() in text.lower() and _ADDRESS.search(text) and _PHONE.search(text) and _EMAIL.search(text)),
        "amount": amount is not None,
        "bank_details": bool(_ACCOUNT.search(text) and _BANK.search(text) and _COUNTRY.search(text)),
    }
    return [OFFICE_REQUIREMENTS[r] for r in requirements if not have[r]]


def office_mandate(org) -> dict | None:
    """The intake's rules for an office and operations bill; None when the delegation is switched off."""
    if org is None:
        return None
    pol = policy(org, "office")
    if not pol["enabled"]:
        return None
    users = {u["user_id"]: u for u in org.list("user")}
    fields = ("procurement_people", "operations_people", "finance_people")
    forwarders = {str(users[p]["email"]).lower(): users[p]["name"] for f in fields for p in pol[f] if p in users and users[p].get("email")}
    return pol | {"forwarders": forwarders}


def _check_office(org, rec: dict) -> None:
    if not rec["senders"] or any(x not in OFFICE_SENDERS for x in rec["senders"]):
        raise DoAError("tick who may send an office and operations bill")
    users = {u["user_id"] for u in org.list("user") if u.get("active", True)}
    for team, field in (("procurement", "procurement_people"), ("operations", "operations_people"), ("finance", "finance_people")):
        people = rec.get(field) or []
        if any(p not in users for p in people):
            raise DoAError(f"{OFFICE_SENDERS[team]}: choose people from People and roles")
        if team in rec["senders"] and not people:
            raise DoAError(f"{OFFICE_SENDERS[team]}: choose at least one person whose email the agent accepts")
        if team not in rec["senders"]:
            rec[field] = []
    if any(r not in OFFICE_REQUIREMENTS for r in rec["requirements"]):
        raise DoAError("a requirement is not in the list")


def cash_mandate(org) -> dict | None:
    """The intake's rules for a cash advance; None when off or nobody is chosen to instruct one."""
    if org is None:
        return None
    pol = policy(org, "cash_advance")
    who = org.get("user", pol["requester"]) if pol["requester"] else None
    if not pol["enabled"] or who is None or not who.get("active", True):
        return None
    return pol | {"requester_email": who["email"].lower(), "requester_name": who["name"]}


# -- categories the FCO adds: an item, its charge code, who may ask for it, and what the request must carry ----------
CUSTOM_REQUIREMENTS = {
    "pr_number": "A purchase request number",
    "vendor_name": "The vendor's name",
    "account_number": "The vendor's bank account number",
    "bank_name": "The bank's name",
    "country": "The bank's country",
    "amount": "The amount",
}


def custom_categories(org) -> list[dict]:
    return sorted(org.list("doa_custom"), key=lambda r: r["code"])


def save_custom(org, body: dict, by: str) -> dict:
    """Add or change a category the FCO defines. Paid in real time when a request comes from a person chosen (by
    email, or signed in on the dashboard) and carries everything ticked."""
    title = " ".join(str(body.get("title", "")).split())
    if not title:
        raise DoAError("name the item")
    org.account_code(body.get("charge_code") or "")  # OrgError when not a valid charge code
    users = {u["user_id"] for u in org.list("user") if u.get("active", True)}
    email_people, login_people = list(body.get("email_people") or []), list(body.get("login_people") or [])
    if any(p not in users for p in email_people + login_people):
        raise DoAError("choose people from People and roles")
    if not email_people and not login_people:
        raise DoAError("choose whose email, or whose dashboard sign-in, may ask for this item")
    reqs = list(body.get("requirements") or [])
    if any(r not in CUSTOM_REQUIREMENTS for r in reqs):
        raise DoAError("a requirement is not in the list")
    currencies = list(body.get("currencies") or ["USD"])
    if any(c not in CURRENCIES for c in currencies):
        raise DoAError("a payout currency is not one Airwallex pays")
    existing = custom_categories(org)
    key = body.get("key") or ("c-" + _slug(title))
    old = next((c for c in existing if c["key"] == key), None)
    if old is None and any(c["key"] == key for c in existing):
        raise DoAError(f"a category named {title} exists already")
    code = old["code"] if old else f"{8 + len(existing):02d}"
    rec = {"_id": key, "key": key, "code": code, "title": title, "charge_code": org.account_code(body["charge_code"])["code"],
           "enabled": bool(body.get("enabled", True)), "email_people": email_people, "login_people": login_people, "requirements": reqs,
           "currencies": [c for c in CURRENCIES if c in currencies], "updated_by": by}
    org._put("doa_custom", rec, by)
    return rec


def custom_missing(text: str, vendor_name: str | None, amount, requirements: list[str]) -> list[str]:
    have = {"pr_number": bool(_PR.search(text)), "vendor_name": bool(vendor_name and vendor_name.lower() in text.lower()),
            "account_number": bool(_ACCOUNT.search(text)), "bank_name": bool(_BANK.search(text)), "country": bool(_COUNTRY.search(text)),
            "amount": amount is not None}
    return [CUSTOM_REQUIREMENTS[r] for r in requirements if not have[r]]


def custom_for(org, sender: str, text: str, *, login: bool = False) -> dict | None:
    """The category a request asks for: one the sender may use whose item or charge code the request names."""
    if org is None:
        return None
    u = next((x for x in org.list("user") if x["email"].lower() == sender.lower()), None)
    if u is None:
        return None
    low = text.lower()
    for c in custom_categories(org):
        people = c["login_people"] if login else c["email_people"]
        if c["enabled"] and u["user_id"] in people and (c["title"].lower() in low or c["charge_code"].lower() in low):
            return c
    return None


TRAVEL_SENDERS = {"traveller": "The traveller, from their own mailbox", "travel_desk": "Travel desk or admin team", "finance": "Finance team"}
TRAVEL_DOCUMENTS = {  # keys are document types the document validation recognises
    "travel_request": "Travel request, approved by the supervisor",
    "receipts": "Original receipts, as PDF or image",
    "travel_report": "Travel report",
}
TRAVEL_CHANNELS = {"email": "Auto-pay a complete request received by email", "dashboard": "Dashboard request only"}


def travel_places(org) -> dict[str, list[dict]]:
    """Country -> its cities with their rates, from the Travel rates dataset."""
    out: dict[str, list[dict]] = {}
    for r in sorted(org.list("travel_rate"), key=lambda x: (x["country"], x["city"])):
        out.setdefault(r["country"], []).append({"city": r["city"], "currency": r["currency"], "lodging_minor": r["lodging_minor"],
                                                 "per_diem_minor": r["per_diem_minor"], "valid_from": r.get("valid_from")})
    return out


def _check_places(org, rec: dict) -> None:
    places = travel_places(org)
    for side in ("origin", "dest"):
        countries, cities = rec[f"{side}_countries"], rec[f"{side}_cities"]
        label = "origin" if side == "origin" else "destination"
        if not countries or not cities:
            raise DoAError(f"choose the {label} countries and cities, or all of them")
        if countries != ["*"] and any(c not in places for c in countries):
            raise DoAError(f"{label}: a country with no travel rate on file; upload the travel rates first")
        if cities != ["*"]:
            allowed = {f"{c}/{x['city']}" for c in (places if countries == ["*"] else countries) for x in places.get(c, [])}
            if any(c not in allowed for c in cities):
                raise DoAError(f"{label}: choose cities of the countries chosen")


def _check_travel(org, rec: dict) -> None:
    _check_places(org, rec)
    if any(d not in TRAVEL_DOCUMENTS for d in rec["documents"]):
        raise DoAError("a required document is not in the checklist")
    if rec["channel"] not in TRAVEL_CHANNELS:
        raise DoAError("the request arrives by email or by the dashboard")
    users = {u["user_id"] for u in org.list("user") if u.get("active", True)}
    if rec["channel"] == "email" and (not rec["senders"] or any(x not in TRAVEL_SENDERS for x in rec["senders"])):
        raise DoAError("choose whose email the agent accepts a travel request from")
    for team, field in (("travel_desk", "travel_desk_people"), ("finance", "finance_people")):
        people = rec.get(field) or []
        if any(p not in users for p in people):
            raise DoAError(f"{TRAVEL_SENDERS[team]}: choose people from People and roles")
        if rec["channel"] == "email" and team in rec["senders"] and not people:
            raise DoAError(f"{TRAVEL_SENDERS[team]}: choose at least one person whose email the agent accepts")
        if rec["channel"] != "email" or team not in rec["senders"]:
            rec[field] = []


TRAVEL_DOC_TYPES = {"travel_request": "travel_request", "receipts": "receipt", "travel_report": "travel_report"}  # -> validation types
TRAVEL_WORDS = re.compile(r"\b(?:travel|trip|per diem|lodging|hotel|flight|mission)\b", re.IGNORECASE)


def travel_mandate(org) -> dict | None:
    """The intake's rules for a travel claim; None when the delegation is switched off or no travel rate is on file
    (without rates there is nothing to pay a trip against: a person decides, as for any expense claim)."""
    if org is None:
        return None
    pol = policy(org, "travel")
    if not pol["enabled"] or not org.list("travel_rate"):
        return None
    users = {u["user_id"]: u for u in org.list("user")}
    forwarders = {str(users[p]["email"]).lower(): users[p]["name"] for f in ("travel_desk_people", "finance_people") for p in pol[f]
                  if p in users and users[p].get("email")}
    return pol | {"forwarders": forwarders, "places": travel_places(org)}


def _allowed(pol: dict, side: str, country: str, city: str) -> bool:
    cs, ci = pol[f"{side}_countries"], pol[f"{side}_cities"]
    return (cs == ["*"] or country in cs) and (ci == ["*"] or f"{country}/{city}" in ci)


def judge_travel(text: str, mand: dict, amount_minor: int | None, currency: str | None, dates: list[dt.date]) -> dict:
    """A travel claim against the delegation: where from and to (cities named in the claim, from the travel rates),
    how long (nights or days named, else the first and last date), what the rates allow (lodging per night plus per
    diem per day), and the verdict: auto (paid on the agent's own authority), approve (a person decides), never."""
    low = text.lower()
    hits = []
    for country, rows in mand["places"].items():
        for r in rows:
            for m in re.finditer(rf"\b{re.escape(r['city'].lower())}\b", low):
                hits.append((m.start(), country, r))
    hits.sort(key=lambda h: h[0])
    def before(pos: int, word: str) -> bool:
        return bool(re.search(rf"\b{word}\s+(?:the\s+)?$", low[max(0, pos - 14):pos]))
    origin = next((h for h in hits if before(h[0], "from")), None)
    dest = next((h for h in hits if before(h[0], "to")), None)
    if dest is None and hits:
        dest = hits[-1] if (origin is None or hits[-1] is not origin) else None
    if origin is None and len(hits) > 1 and dest is not None:
        origin = next((h for h in hits if h is not dest), None)
    nights = days = None
    if (m := re.search(r"\b(\d{1,2})\s*nights?\b", low)):
        nights = int(m.group(1))
    if (m := re.search(r"\b(\d{1,2})\s*days?\b", low)):
        days = int(m.group(1))
    if nights is None and days is None and len(dates) >= 2:
        nights = (max(dates) - min(dates)).days
    if days is None and nights is not None:
        days = nights + 1
    if nights is None and days is not None:
        nights = max(days - 1, 0)
    out = {"origin": f"{origin[2]['city']}, {origin[1]}" if origin else None, "destination": f"{dest[2]['city']}, {dest[1]}" if dest else None,
           "nights": nights, "days": days, "allowed_minor": None, "currency": dest[2]["currency"] if dest else None}
    never, approve = [], []
    if dest is None:
        approve.append("the claim names no destination city with a travel rate on file")
    elif not _allowed(mand, "dest", dest[1], dest[2]["city"]):
        never.append(f"{dest[2]['city']}, {dest[1]} is not a destination the delegation allows")
    if origin is not None and not _allowed(mand, "origin", origin[1], origin[2]["city"]):
        never.append(f"{origin[2]['city']}, {origin[1]} is not an origin the delegation allows")
    elif origin is None and (mand["origin_countries"] != ["*"] or mand["origin_cities"] != ["*"]):
        approve.append("the claim names no origin the delegation allows")
    if dest is not None and nights is not None:
        out["allowed_minor"] = dest[2]["lodging_minor"] * nights + dest[2]["per_diem_minor"] * days
        if amount_minor is None:
            approve.append("the claim states no amount")
        elif currency != dest[2]["currency"]:
            approve.append(f"claimed in {currency}, the rate for {dest[2]['city']} is in {dest[2]['currency']}")
        elif amount_minor > out["allowed_minor"]:
            approve.append(f"{currency} {amount_minor / 100:,.2f} is above what the rates allow: {nights} night(s) x {dest[2]['lodging_minor'] / 100:,.2f} "
                           f"+ {days} day(s) x {dest[2]['per_diem_minor'] / 100:,.2f} = {out['allowed_minor'] / 100:,.2f}")
    elif dest is not None:
        approve.append("the claim states neither the nights nor the dates of the trip")
    out["verdict"] = "never" if never else ("approve" if approve else "auto")
    out["reasons"] = never or approve
    return out


def consultant_mandate(org) -> dict | None:
    """The intake's rules for a consultant's payment request; None when the delegation is switched off."""
    if org is None:
        return None
    pol = policy(org, "consultants")
    if not pol["enabled"]:
        return None
    users = {u["user_id"]: u for u in org.list("user")}
    forwarders = {str(users[p]["email"]).lower(): users[p]["name"] for f in ("procurement_people", "finance_people") for p in pol[f] if p in users and users[p].get("email")}
    return pol | {"forwarders": forwarders}


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
        elif c["key"] == "travel":
            n = sum(len(v) for v in travel_places(org).values())
            docs = ", ".join(TRAVEL_DOCUMENTS[d].split(",")[0].lower() for d in pol["documents"]) or "no document"
            row["summary"] = "no_document" if not n else ("via_intake" if pol["channel"] == "email" else "dashboard_only")
            row["reasons"] = (["no travel rates on file: upload the organisation's lodging and per diem rates per destination city in Dataset upload"] if not n else
                              [f"{n} destination cit{'y' if n == 1 else 'ies'} with rates on file; required: {docs}; "
                               + ("paid as complete requests arrive by email" if pol["channel"] == "email" else "requests come from the dashboard only")])
        elif c["key"] == "cash_advance":
            who = org.get("user", pol["requester"]) if pol["requester"] else None
            row["summary"] = "via_intake" if who else "no_document"
            row["reasons"] = ([f"paid in real time on an instruction by email from {who['name']}, naming the person to pay and the amount"] if who
                              else ["choose the one person whose email instructs a cash advance"])
        elif c["key"] == "office":
            row["summary"] = "via_intake"
            row["reasons"] = ["paid in real time as bills without a purchase order arrive, from " + ", ".join(OFFICE_SENDERS[x].lower() for x in pol["senders"])
                              + "; required: " + (", ".join(OFFICE_REQUIREMENTS[r].lower() for r in pol["requirements"]) or "nothing more")]
        elif c["key"] == "consultants":
            senders = [CONSULTANT_SENDERS[x] for x in pol["senders"]]
            row["summary"] = "via_intake"
            row["reasons"] = ["paid as requests arrive in the intake queue, from " + ", ".join(senders).lower() + ", with "
                              + ", ".join(CONSULTANT_DOCUMENTS[d].split(",")[0].lower() for d in pol["documents"]) + " attached"
                              + ("; claims above the contract value are not checked" if pol["ignore_exceed_claim"] else "; the claim is checked against the contract value")]
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
        elif ln["currency"] not in ENGINE_CURRENCIES:
            approve.append(f"the agent converts and pays {', '.join(ENGINE_CURRENCIES)} today; a payment in {ln['currency']} is made by a person")
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
