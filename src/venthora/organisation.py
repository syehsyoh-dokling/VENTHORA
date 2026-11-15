# ruff: noqa: E501
"""The organisation's master data: units, people, budget allocations, unit prices and finance standards.

Units form one tree: a single Head Quarter, Country Offices under it, Branch Offices under a Country Office,
and Project Finance units under any of them. People belong to a unit and carry a role. These records are the
finance team's reference data; they do not loosen the agent's policy. Thresholds the agent obeys (reserve
floor, payout and FX limits, approval matrix) stay in policy code and the mandate, and are shown read-only.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from .config import data_path, sample_dir

UnitKind = Literal["HQ", "COUNTRY_OFFICE", "BRANCH_OFFICE", "PROJECT_FINANCE"]
Role = Literal["super_admin", "developer", "hq_admin", "country_admin", "branch_admin", "project_admin", "admin", "cfo", "finance_manager",
               "treasury_analyst", "country_finance_lead", "project_accountant", "procurement", "requester", "auditor"]
# company-wide writers; the scoped admins below write only inside their own unit and the units under it
WRITE_ROLES = {"super_admin", "hq_admin", "admin", "cfo", "finance_manager"}
# the admin team: who runs each level of the organisation (the super admin is the FCO at HQ, who sees and may do everything)
ADMIN_LEVELS = {"super_admin": "HQ", "hq_admin": "HQ", "country_admin": "COUNTRY_OFFICE", "branch_admin": "BRANCH_OFFICE",
                "project_admin": "PROJECT_FINANCE"}
SCOPED_ADMINS = {"country_admin", "branch_admin", "project_admin"}
# roles a scoped admin may give: never one at or above its own level, never a company-wide one
GRANTABLE = {"country_admin": {"branch_admin", "project_admin", "country_finance_lead", "project_accountant", "treasury_analyst", "procurement", "requester", "auditor"},
             "branch_admin": {"project_admin", "project_accountant", "treasury_analyst", "procurement", "requester", "auditor"},
             "project_admin": {"project_accountant", "procurement", "requester", "auditor"}}
# a branch office sits under a country office or straight under the head office
PARENTS: dict[str, set[str]] = {"HQ": set(), "COUNTRY_OFFICE": {"HQ"}, "BRANCH_OFFICE": {"HQ", "COUNTRY_OFFICE"},
                                "PROJECT_FINANCE": {"HQ", "COUNTRY_OFFICE", "BRANCH_OFFICE"}}
CCY = re.compile(r"^[A-Z]{3}$")


class OrgError(ValueError):
    pass


def _ccy(v: str) -> str:
    v = v.upper()
    if not CCY.match(v):
        raise ValueError("a currency is a three-letter ISO code")
    return v


class Unit(BaseModel):
    unit_id: str | None = Field(default=None, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    kind: UnitKind
    parent_id: str | None = None
    country: str = Field(default="", max_length=2)
    currency: str = "USD"
    timezone: str = Field(default="UTC", max_length=60)
    cost_centre: str = Field(default="", max_length=40)
    # The agent may pay alone, per payment charged to this unit, up to this (base currency); above it a person in the
    # unit chain whose approval limit covers the amount must approve. None = the parent unit's limit applies.
    payment_limit_minor: int | None = Field(default=None, ge=0, le=10**13)
    active: bool = True

    @field_validator("currency")
    @classmethod
    def check_currency(cls, v: str) -> str:
        return _ccy(v)


class Project(BaseModel):
    """A project spending is charged to; unit_id None = a company-wide code (e.g. general operations)."""
    project_id: str | None = Field(default=None, max_length=40)
    code: str = Field(pattern=r"^[A-Z0-9]{2,12}$")
    name: str = Field(min_length=1, max_length=120)
    unit_id: str | None = None
    active: bool = True


class Account(BaseModel):
    """One line of the chart of accounts (the cost account)."""
    account_id: str | None = Field(default=None, max_length=40)
    code: str = Field(pattern=r"^\d{4,6}$")
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=60, description="the finance standards' expense category it belongs to")
    active: bool = True


class POTerm(BaseModel):
    term_id: str = Field(pattern=r"^[A-Z0-9-]{1,12}$")
    label: str = Field(min_length=1, max_length=80)
    amount_minor: int = Field(gt=0, le=10**13)


class PurchaseOrder(BaseModel):
    """What the company agreed to buy: one supplier, one currency, one account code, and how it will be paid.
    Paid any number of times while it has room: every payment is its next termin (termin-1, termin-2 ...), of any amount,
    never past the total. Scheduled terms (e.g. 30% deposit, 70% on delivery) are the payment plan shown beside them.
    allow_partial is kept for stored records and imports; it no longer limits payments."""
    po_id: str | None = Field(default=None, pattern=r"^[A-Z0-9][A-Z0-9/-]{2,30}$", description="the PO number")
    supplier_id: str = Field(min_length=1, max_length=80, description="the payee (beneficiary) the PO is placed with")
    currency: str = "USD"
    total_minor: int = Field(gt=0, le=10**13)
    account_code: str = Field(min_length=5, max_length=60, description="office cost centre . project . account, e.g. DE-400.PRJ900.6500")
    description: str = Field(default="", max_length=300)
    terms: list[POTerm] = Field(default_factory=list)
    allow_partial: bool = True
    status: Literal["OPEN", "CLOSED", "CANCELLED"] = "OPEN"
    issued_date: str = Field(default="2026-10-01", pattern=r"^\d{4}-\d{2}-\d{2}$")
    start_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="active from (the issue date when blank)")
    expiry_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="the last day it may be paid against")
    recurring: Literal["none", "monthly", "quarterly", "annual"] = Field(
        "none", description="Multi Payment: a service billed every period against one PO, e.g. internet or a subscription")
    loe: float | None = Field(default=None, gt=0, le=100_000, description="a consultant contract: the level of effort bought")
    loe_unit: Literal["day", "hour", "month"] = "day"
    rate_minor: int | None = Field(default=None, gt=0, le=10**12, description="a consultant contract: the fee per LoE unit")
    pr_number: str | None = Field(default=None, max_length=40, description="the approved purchase request it was raised from")

    @field_validator("currency")
    @classmethod
    def check_currency(cls, v: str) -> str:
        return _ccy(v)


class PurchaseRequest(BaseModel):
    """What someone in the organisation asks to buy, before there is a PO: approved by a person other than the
    requester, then raised as a purchase order that may not exceed the approved estimate."""
    pr_number: str | None = Field(default=None, pattern=r"^[A-Z0-9][A-Z0-9/-]{2,30}$", description="the PR number")
    title: str = Field(min_length=2, max_length=200, description="what is to be bought")
    requested_by: str = Field(min_length=1, max_length=120, description="the person asking, from People and roles")
    supplier_id: str | None = Field(default=None, max_length=80, description="the suggested supplier, a registered payee")
    currency: str = "USD"
    estimated_minor: int = Field(gt=0, le=10**13)
    charge_code: str = Field(min_length=5, max_length=60)
    needed_by: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    requested_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="the day it was asked (today when blank)")
    justification: str = Field(default="", max_length=500)
    quantity: float | None = Field(default=None, gt=0, le=10**7, description="how many are to be bought")
    unit: str = Field(default="", max_length=20, description="the unit of the quantity: pcs, box, licence ...")
    brand: str = Field(default="", max_length=80, description="a required brand; quotations of another brand are not recommended")
    specs: list[str] = Field(default_factory=list, max_length=30, description="the specifications a quotation must meet, one per line")
    status: Literal["DRAFT", "SUBMITTED", "APPROVED", "REJECTED", "ORDERED"] = "SUBMITTED"
    decided_by: str | None = Field(default=None, max_length=120)
    decided_at: str | None = None
    decision_note: str = Field(default="", max_length=300)
    po_id: str | None = None

    @field_validator("currency")
    @classmethod
    def check_currency(cls, v: str) -> str:
        return _ccy(v)

    @field_validator("specs", mode="before")
    @classmethod
    def split_specs(cls, v):
        if isinstance(v, str):  # a form or a file gives them as one text: one per line, or separated by ;
            v = re.split(r"[\n;]+", v)
        return [str(x).strip()[:200] for x in (v or []) if str(x).strip()]


class TravelAuthorisation(BaseModel):
    """A trip approved before it is made: who travels, from where to where, when, why, and the estimated cost. The
    claim after the trip is checked against the travel rates and policy in the Delegation of Authority."""
    ta_number: str | None = Field(default=None, pattern=r"^[A-Z0-9][A-Z0-9/-]{2,30}$", description="the travel authorisation number")
    traveller: str = Field(min_length=1, max_length=40, description="user_id of the person travelling (People and roles)")
    origin: str = Field(min_length=2, max_length=120, description="Country/City")
    destination: str = Field(min_length=2, max_length=120, description="Country/City")
    start: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    purpose: str = Field(default="", max_length=500)
    estimated_minor: int | None = Field(default=None, gt=0, le=10**12)
    currency: str = "USD"
    charge_code: str | None = Field(default=None, max_length=60)
    status: Literal["APPROVED", "CANCELLED", "CLAIMED"] = "APPROVED"
    approved_by: str = Field(default="", max_length=120)

    @field_validator("currency")
    @classmethod
    def check_currency(cls, v: str) -> str:
        return _ccy(v)


def same_person(org, by: str, requested_by: str) -> bool:
    """Whether the person deciding is the person who asked. A request names its requester by user id (raised by
    email), by name or by email (typed on the dashboard); the person deciding is known by name or email. One rule
    for every decision, so nobody approves or awards their own request whichever way it was raised."""
    me, asked = (by or "").strip().lower(), (requested_by or "").strip().lower()
    if not me or not asked:
        return False
    if me == asked:
        return True
    names = lambda u: {u["user_id"].lower(), u["name"].lower(), u["email"].lower()}  # noqa: E731
    return any(me in names(u) and asked in names(u) for u in org.list("user"))


FUND_KINDS = {"cash_advance": ("CA", "Cash advance"), "petty_cash": ("PC", "Petty cash / slush fund"), "odc": ("ODC", "Other direct cost")}


class FundRequest(BaseModel):
    """A cash advance to a person, a petty cash (slush fund) float or its top-up for a custodian, or an other direct
    cost of a programme activity: asked by one person, decided by another, then sent to Pending payment. An advance
    or a float is settled later against receipts."""
    request_number: str | None = Field(default=None, pattern=r"^[A-Z0-9][A-Z0-9/-]{2,30}$")
    kind: Literal["cash_advance", "petty_cash", "odc"]
    requested_by: str = Field(min_length=1, max_length=40, description="user_id of the person asking (the custodian for petty cash)")
    payee_id: str | None = Field(default=None, max_length=80, description="who is paid: the person's own account, or the supplier for an ODC")
    amount_minor: int = Field(gt=0, le=10**12)
    currency: str = "USD"
    charge_code: str = Field(min_length=5, max_length=60)
    purpose: str = Field(min_length=2, max_length=300)
    activity: str = Field(default="", max_length=160, description="the programme activity (ODC) or the float's use")
    request_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    needed_by: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    settle_by: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="an advance or a float: when receipts are due")
    status: Literal["DRAFT", "SUBMITTED", "APPROVED", "REJECTED", "IN_PAYMENT", "SETTLED", "CANCELLED"] = "SUBMITTED"
    decided_by: str | None = Field(default=None, max_length=120)
    decided_at: str | None = None
    decision_note: str = Field(default="", max_length=300)
    intake_id: str | None = None
    settled_minor: int | None = Field(default=None, ge=0, le=10**12, description="receipts accounted for at settlement")
    settlement_note: str = Field(default="", max_length=300)
    ta_number: str | None = Field(default=None, pattern=r"^[A-Z0-9][A-Z0-9/-]{2,30}$", description="a travel advance: its travel authorisation (travel clearance) number")

    @field_validator("currency")
    @classmethod
    def check_currency(cls, v: str) -> str:
        return _ccy(v)


class Receipt(BaseModel):
    """Money another party paid into the organisation's Airwallex account: who paid, how much, when, into which
    receiving account, and the customer invoice it settles. Confirmed once Airwallex reports the deposit."""
    receipt_id: str | None = Field(default=None, pattern=r"^[A-Z0-9][A-Z0-9/-]{2,30}$")
    payer_name: str = Field(min_length=1, max_length=160, description="who paid, as on the bank statement")
    customer_id: str | None = Field(default=None, max_length=80, description="the customer, when the payer is one")
    amount_minor: int = Field(gt=0, le=10**13)
    currency: str = "USD"
    received_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    account: str = Field(default="", max_length=120, description="the receiving account (Global Account) it was paid into")
    reference: str = Field(default="", max_length=120, description="the payer's reference, e.g. the invoice number")
    receivable_id: str | None = Field(default=None, max_length=80, description="the open customer invoice it settles")
    category: Literal["customer_payment", "refund", "grant_or_donation", "loan", "capital", "other"] = "customer_payment"
    note: str = Field(default="", max_length=300)

    @field_validator("currency")
    @classmethod
    def check_currency(cls, v: str) -> str:
        return _ccy(v)


ENGAGEMENT_ROLES = {"consultant": "Consultant", "legal_specialist": "Legal specialist", "resource_person": "Resource person",
                    "keynote_speaker": "Keynote speaker"}
ENGAGEMENT_DOCUMENTS = {"engagement_letter": "Engagement letter or signed terms of reference", "invoice": "Invoice or fee note",
                        "tax_form": "Tax form (e.g. W-8BEN / W-9, NPWP)", "attendance": "Proof of delivery (agenda, attendance list, report)",
                        "id_document": "Identity document", "bank_letter": "Bank letter for the payout account"}


class Engagement(BaseModel):
    """A one-off or incidental engagement: a consultant for a single task, a legal specialist, a resource person or a
    keynote speaker. The profile and the payment in one record; once it has a registered payee it is paid against its
    own number, like a PO (the engagement number is the PO number of its payment order)."""
    engagement_id: str | None = Field(default=None, pattern=r"^[A-Z0-9][A-Z0-9/-]{2,30}$", description="the engagement number")
    name: str = Field(min_length=1, max_length=120)
    role: Literal["consultant", "legal_specialist", "resource_person", "keynote_speaker"]
    organisation: str = Field(default="", max_length=120)
    email: str = Field(default="", max_length=200, pattern=r"^$|^[^@\s]+@[^@\s]+\.[^@\s]+$")
    phone: str = Field(default="", max_length=40)
    tax_id: str = Field(default="", max_length=40)
    payee_id: str | None = Field(default=None, max_length=80, description="the registered payee holding the bank account")
    event: str = Field(min_length=1, max_length=200, description="the event, task or matter")
    event_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    fee_minor: int = Field(gt=0, le=10**12)
    currency: str = "USD"
    charge_code: str = Field(min_length=5, max_length=60)
    documents: list[str] = Field(default_factory=list, description="the documents on file, keys of ENGAGEMENT_DOCUMENTS")
    note: str = Field(default="", max_length=300)
    status: Literal["ACTIVE", "CANCELLED"] = "ACTIVE"

    @field_validator("currency")
    @classmethod
    def check_currency(cls, v: str) -> str:
        return _ccy(v)

    @field_validator("documents")
    @classmethod
    def check_documents(cls, v: list[str]) -> list[str]:
        bad = [x for x in v if x not in ENGAGEMENT_DOCUMENTS]
        if bad:
            raise ValueError(f"unknown documents {bad}; choose from {sorted(ENGAGEMENT_DOCUMENTS)}")
        return sorted(set(v))


def engagement_po(e: dict, old: dict | None = None) -> dict:
    """The PO an engagement is paid against: its number, payee, fee and charge code."""
    return PurchaseOrder.model_validate({
        "po_id": e["engagement_id"], "supplier_id": e["payee_id"], "currency": e["currency"], "total_minor": e["fee_minor"],
        "account_code": e["charge_code"], "description": f"{ENGAGEMENT_ROLES[e['role']]}: {e['event']}"[:300], "terms": [],
        "allow_partial": True, "status": "CANCELLED" if e.get("status") == "CANCELLED" else ((old or {}).get("status") or "OPEN"),
        "issued_date": (old or {}).get("issued_date") or e["event_date"], "start_date": e["event_date"],
        "expiry_date": (old or {}).get("expiry_date")}).model_dump()


class Mailbox(BaseModel):
    """An inbound address the mail service posts to /webhooks/email, and what may arrive there."""
    mailbox_id: str | None = Field(default=None, max_length=40)
    address: str = Field(min_length=3, max_length=200, pattern=r"^[^@\s]+@[^@\s]+$")
    purpose: Literal["invoices", "claims", "remittances", "payment_orders", "any"] = "any"
    provider: Literal["postmark", "mailgun", "sendgrid", "cloudflare", "raw", "other"] = "postmark"
    note: str = Field(default="", max_length=200)
    active: bool = True


PURPOSE_KINDS = {"invoices": {"supplier_invoice", "contractor_claim", "bank_change"}, "claims": {"expense_claim"},
                 "remittances": {"remittance_notice"}, "payment_orders": {"supplier_invoice", "contractor_claim"}}


class User(BaseModel):
    user_id: str | None = Field(default=None, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    role: Role
    unit_id: str
    approval_limit_minor: int = Field(default=0, ge=0, le=10**13)
    may_order_by_email: bool = Field(default=False, description="may ask VENTHORA to pay a vendor by email (Settings → Email)")
    payee_id: str | None = Field(default=None, max_length=60, description="the person's own registered bank account (a payee), for salary, travel claims and reimbursements")
    contract_end: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="the last day of the person's contract; no salary is paid after it")
    # employment: what the payroll register and the delegation of authority read for this person
    employee_no: str | None = Field(default=None, max_length=40, description="the staff or employee number")
    job_title: str = Field(default="", max_length=120)
    employment_type: Literal["permanent", "fixed_term", "expatriate", "part_time", "intern"] = "permanent"
    contract_start: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    salary_minor: int | None = Field(default=None, ge=0, le=10**12, description="monthly net pay, in salary_currency")
    salary_currency: str | None = Field(default=None, description="the currency the person is paid in")
    charge_code: str | None = Field(default=None, max_length=60, description="the charge code their salary is booked to")
    benefits: list[str] = Field(default_factory=list, description="the fringe benefits the person is enrolled in (Delegation of Authority → 02)")

    @field_validator("salary_currency")
    @classmethod
    def check_salary_currency(cls, v: str | None) -> str | None:
        return _ccy(v) if v else None
    active: bool = True


class Allocation(BaseModel):
    """Budget given to a charge-code scope for one budget period (src/venthora/budget_allocation.py enforces it).

    * regular  every payment carries a charge code OFFICE.PROJECT.ACCOUNT. The allocation names a unit (a country
               office, a branch or a project-finance unit); include_children also covers the units under it, and
               project_code narrows it to one project of that unit.
    * project  the allocation follows one project wherever it is charged.
    period: YYYY-MM (monthly), YYYY-Qn (quarterly), YYYY-Hn (half-year), YYYY (annual: the fiscal year starting in
    that year) or ALL (no period: until it is used). no_limit: the scope may spend whatever the balance allows."""
    alloc_id: str | None = Field(default=None, max_length=40)
    basis: Literal["regular", "project"] = "regular"
    period: str = Field(pattern=r"^(\d{4}(-(0[1-9]|1[0-2])|-Q[1-4]|-H[12])?|ALL)$")
    unit_id: str | None = None
    include_children: bool = True
    project_code: str | None = Field(default=None, max_length=12)
    line: str = Field(default="Operations", min_length=1, max_length=60)
    amount_minor: int = Field(default=0, ge=0, le=10**14)
    no_limit: bool = False
    currency: str = "USD"
    # a project-based allocation runs for the project's duration (period ALL): it covers the months from start to end
    start_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("currency")
    @classmethod
    def check_currency(cls, v: str) -> str:
        return _ccy(v)


class TravelRate(BaseModel):
    """The organisation's lodging and per diem (meals and incidentals) for one destination city, per night and day."""
    rate_id: str | None = Field(default=None, max_length=60)
    country: str = Field(min_length=2, max_length=60)
    city: str = Field(min_length=1, max_length=80)
    currency: str = "USD"
    lodging_minor: int = Field(default=0, ge=0, le=10**10)
    per_diem_minor: int = Field(default=0, ge=0, le=10**10)
    valid_from: str = Field(default="2026-01-01", pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("currency")
    @classmethod
    def check_currency(cls, v: str) -> str:
        return _ccy(v)

    @field_validator("country", "city")
    @classmethod
    def tidy(cls, v: str) -> str:
        return " ".join(v.split())


class Price(BaseModel):
    price_id: str | None = Field(default=None, max_length=40)
    item: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=60)
    unit: str = Field(min_length=1, max_length=30)
    price_minor: int = Field(ge=0, le=10**12)
    currency: str = "USD"
    unit_id: str | None = None
    valid_from: str = Field(default="2026-01-01", pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("currency")
    @classmethod
    def check_currency(cls, v: str) -> str:
        return _ccy(v)


class Standards(BaseModel):
    company_name: str = Field(default="Acme Group", min_length=1, max_length=120)
    base_currency: str = "USD"
    fiscal_year_start_month: int = Field(default=1, ge=1, le=12)
    default_payment_terms_days: int = Field(default=30, ge=0, le=365)
    invoice_matching: Literal["two_way", "three_way"] = "three_way"
    petty_cash_limit_minor: int = Field(default=50_000, ge=0, le=10**10)
    expense_claim_deadline_days: int = Field(default=30, ge=1, le=365)
    document_retention_years: int = Field(default=7, ge=1, le=30)
    fx_rate_source: Literal["airwallex", "ecb", "central_bank", "other"] = "airwallex"
    expense_categories: list[str] = Field(default_factory=lambda: ["Payroll", "Logistics", "Suppliers", "Contractors",
                                                                   "Software", "Travel", "Office", "Marketing"])
    require_po: bool = Field(default=True, description="a supplier invoice is payable only against an open purchase order it quotes")
    # every other choice (framework, controls, tax, audit, sector rules) by its key in finance_standards.FIELDS
    policies: dict[str, Any] = Field(default_factory=dict)

    @field_validator("base_currency")
    @classmethod
    def check_currency(cls, v: str) -> str:
        return _ccy(v)

    @field_validator("policies")
    @classmethod
    def check_policies(cls, v: dict) -> dict:
        from .finance_standards import check

        return check(v)


SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    kind TEXT NOT NULL, record_id TEXT NOT NULL, record_json TEXT NOT NULL, updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL, PRIMARY KEY (kind, record_id));
"""
MODELS = {"unit": Unit, "user": User, "allocation": Allocation, "price": Price, "project": Project, "account": Account, "mailbox": Mailbox,
          "po": PurchaseOrder, "travel_rate": TravelRate, "engagement": Engagement, "pr": PurchaseRequest, "receipt": Receipt,
          "ta": TravelAuthorisation, "fund_request": FundRequest}
ID_FIELD = {"unit": "unit_id", "user": "user_id", "allocation": "alloc_id", "price": "price_id", "project": "project_id",
            "account": "account_id", "po": "po_id", "mailbox": "mailbox_id", "travel_rate": "rate_id", "engagement": "engagement_id",
            "pr": "pr_number", "receipt": "receipt_id", "ta": "ta_number",
            "fund_request": "request_number"}
SEED_VERSION = 7
STAFF_ACCOUNTS = {"u-dana": "emp-dana-finch", "u-lee": "emp-lee-park", "u-omar": "emp-omar-reyes", "u-amira": "emp-amira-shah", "u-jonas": "emp-jonas-weber"}
ACCOUNTS_V6 = [{"account_id": "ac-2200", "code": "2200", "name": "Taxes and social security payable", "category": "Statutory"},
               {"account_id": "ac-2300", "code": "2300", "name": "Loan and debt service", "category": "Debt service"}]
# what v3 adds to the seeded POs: active and expiry dates, a consultant's LoE and rate, and a Multi Payment subscription
PO_V3 = {"PO-2026-0101": {"start_date": "2026-10-01", "expiry_date": "2027-03-31"},
         "PO-2026-0102": {"start_date": "2026-10-01", "expiry_date": "2027-01-31"},
         "PO-2026-0103": {"start_date": "2026-10-01", "expiry_date": "2026-12-31"},
         "PO-2026-0104": {"start_date": "2026-10-01", "expiry_date": "2026-12-31", "loe": 192.0, "loe_unit": "hour", "rate_minor": 5_000}}
PO_SUBSCRIPTION = {"po_id": "PO-2026-0105", "supplier_id": "ben-saas-vendor", "currency": "USD", "total_minor": 1_440_000,
                   "account_code": "US-200.OPS.6300", "description": "Cloud workspace subscription, billed monthly (Jan-Dec 2026)",
                   "terms": [], "allow_partial": True, "issued_date": "2026-01-01", "start_date": "2026-01-01",
                   "expiry_date": "2026-12-31", "recurring": "monthly"}
# the seeded people's employee records (People and roles), so payroll and Commitments -> Staff have something real to read
STAFF_V3 = {
    "u-omar": {"employee_no": "E-1001", "job_title": "Chief Financial Officer", "employment_type": "permanent", "contract_start": "2022-03-01",
               "salary_minor": 1_250_000, "salary_currency": "USD", "charge_code": "HQ-100.OPS.6010", "benefits": ["health", "pension_dc"]},
    "u-dana": {"employee_no": "E-1014", "job_title": "Finance manager", "employment_type": "permanent", "contract_start": "2023-01-09",
               "salary_minor": 780_000, "salary_currency": "USD", "charge_code": "US-200.OPS.6010", "benefits": ["health", "pension_dc"]},
    "u-lee": {"employee_no": "E-1022", "job_title": "Treasury analyst", "employment_type": "permanent", "contract_start": "2024-06-03",
              "salary_minor": 540_000, "salary_currency": "USD", "charge_code": "HQ-100.OPS.6010", "benefits": ["health"]},
    "u-amira": {"employee_no": "E-2003", "job_title": "Country finance lead", "employment_type": "fixed_term", "contract_start": "2025-01-06",
                "contract_end": "2026-12-31", "salary_minor": 520_000, "salary_currency": "GBP", "charge_code": "UK-300.OPS.6010", "benefits": ["health"]},
    "u-jonas": {"employee_no": "E-3008", "job_title": "Project accountant", "employment_type": "fixed_term", "contract_start": "2026-02-02",
                "contract_end": "2027-06-30", "salary_minor": 460_000, "salary_currency": "EUR", "charge_code": "PRJ-900.PRJ900.6010", "benefits": []},
}
ENGAGEMENTS = [
    {"engagement_id": "ENG-2026-001", "name": "Dr. Lena Hartmann", "role": "keynote_speaker", "organisation": "Hamburg School of Logistics",
     "email": "l.hartmann@hsl.example", "event": "EU partner summit, opening keynote", "event_date": "2026-11-20", "fee_minor": 250_000,
     "currency": "EUR", "charge_code": "DE-400.PRJ900.6600", "documents": ["engagement_letter"]},
    {"engagement_id": "ENG-2026-002", "name": "Mia Okafor", "role": "consultant", "payee_id": "ben-contractor-okafor",
     "email": "mia@okafor-design.example", "event": "One-day brand workshop facilitation", "event_date": "2026-11-12", "fee_minor": 120_000,
     "currency": "USD", "charge_code": "US-200.OPS.6200", "documents": ["engagement_letter", "invoice"]},
]
UNIT_LIMITS = {"hq": 2_500_000, "co-us": 1_000_000, "co-uk": 800_000, "co-de": 800_000, "br-us-west": 300_000,
               "br-uk-manchester": 300_000, "pf-eu-rollout": 500_000}


def _seed() -> dict[str, list[dict]]:
    units = [
        {"unit_id": "hq", "name": "Acme Group Head Quarter", "kind": "HQ", "country": "SG", "currency": "USD", "timezone": "Asia/Singapore", "cost_centre": "HQ-100"},
        {"unit_id": "co-us", "name": "US Country Office", "kind": "COUNTRY_OFFICE", "parent_id": "hq", "country": "US", "currency": "USD", "timezone": "America/New_York", "cost_centre": "US-200"},
        {"unit_id": "co-uk", "name": "UK Country Office", "kind": "COUNTRY_OFFICE", "parent_id": "hq", "country": "GB", "currency": "GBP", "timezone": "Europe/London", "cost_centre": "UK-300"},
        {"unit_id": "co-de", "name": "Germany Country Office", "kind": "COUNTRY_OFFICE", "parent_id": "hq", "country": "DE", "currency": "EUR", "timezone": "Europe/Berlin", "cost_centre": "DE-400"},
        {"unit_id": "br-us-west", "name": "US West Branch", "kind": "BRANCH_OFFICE", "parent_id": "co-us", "country": "US", "currency": "USD", "timezone": "America/Los_Angeles", "cost_centre": "US-210"},
        {"unit_id": "br-uk-manchester", "name": "Manchester Branch", "kind": "BRANCH_OFFICE", "parent_id": "co-uk", "country": "GB", "currency": "GBP", "timezone": "Europe/London", "cost_centre": "UK-310"},
        {"unit_id": "pf-eu-rollout", "name": "EU Warehouse Roll-out", "kind": "PROJECT_FINANCE", "parent_id": "co-de", "country": "DE", "currency": "EUR", "timezone": "Europe/Berlin", "cost_centre": "PRJ-900"},
    ]
    users = [
        {"user_id": "u-fco", "name": "Rosa Lindqvist", "email": "rosa.lindqvist@acme.example", "role": "super_admin", "unit_id": "hq"},
        {"user_id": "u-omar", "name": "Omar Reyes", "email": "omar.reyes@acme.example", "role": "cfo", "unit_id": "hq", "approval_limit_minor": 100_000_000},
        {"user_id": "u-dana", "name": "Dana Finch", "email": "dana.finch@acme.example", "role": "finance_manager", "unit_id": "co-us", "approval_limit_minor": 5_000_000},
        {"user_id": "u-lee", "name": "Lee Park", "email": "lee.park@acme.example", "role": "treasury_analyst", "unit_id": "hq"},
        {"user_id": "u-amira", "name": "Amira Shah", "email": "amira.shah@acme.example", "role": "country_finance_lead", "unit_id": "co-uk", "approval_limit_minor": 2_500_000},
        {"user_id": "u-jonas", "name": "Jonas Weber", "email": "jonas.weber@acme.example", "role": "project_accountant", "unit_id": "pf-eu-rollout", "approval_limit_minor": 500_000},
        {"user_id": "u-ines", "name": "Ines Duarte", "email": "ines.duarte@acme.example", "role": "auditor", "unit_id": "hq"},
    ]
    allocations = [
        {"alloc_id": "a-hq", "period": "2026", "unit_id": "hq", "line": "Group operating budget", "amount_minor": 600_000_000},
        {"alloc_id": "a-us", "period": "2026", "unit_id": "co-us", "line": "Operations", "amount_minor": 240_000_000},
        {"alloc_id": "a-uk", "period": "2026", "unit_id": "co-uk", "line": "Operations", "amount_minor": 150_000_000},
        {"alloc_id": "a-de", "period": "2026", "unit_id": "co-de", "line": "Operations", "amount_minor": 120_000_000},
        {"alloc_id": "a-usw", "period": "2026", "unit_id": "br-us-west", "line": "Operations", "amount_minor": 60_000_000},
        {"alloc_id": "a-man", "period": "2026", "unit_id": "br-uk-manchester", "line": "Operations", "amount_minor": 40_000_000},
        {"alloc_id": "a-prj", "period": "2026", "unit_id": "pf-eu-rollout", "line": "Capital project", "amount_minor": 80_000_000},
        {"alloc_id": "a-brand26", "basis": "project", "period": "2026", "project_code": "BRAND26", "unit_id": "co-us",
         "line": "Brand refresh", "amount_minor": 600_000},
    ]
    prices = [
        {"price_id": "p-freight", "item": "Freight container UK to US", "category": "Logistics", "unit": "container", "price_minor": 250_000, "currency": "GBP"},
        {"price_id": "p-contractor", "item": "Contractor engineering day", "category": "Contractors", "unit": "day", "price_minor": 60_000, "currency": "USD"},
        {"price_id": "p-saas", "item": "SaaS seat", "category": "Software", "unit": "seat / month", "price_minor": 4_500, "currency": "USD"},
        {"price_id": "p-perdiem-de", "item": "Per diem Germany", "category": "Travel", "unit": "day", "price_minor": 2_800, "currency": "EUR", "unit_id": "co-de"},
        {"price_id": "p-perdiem-us", "item": "Per diem United States", "category": "Travel", "unit": "day", "price_minor": 7_500, "currency": "USD", "unit_id": "co-us"},
        {"price_id": "p-pallet", "item": "Warehouse pallet slot", "category": "Logistics", "unit": "pallet / month", "price_minor": 1_900, "currency": "EUR", "unit_id": "pf-eu-rollout"},
    ]
    for u in units:
        u["payment_limit_minor"] = UNIT_LIMITS.get(u["unit_id"])
    return {"unit": units, "user": users, "allocation": allocations, "price": prices} | _seed_v2()


def _seed_v2() -> dict[str, list[dict]]:
    """Added with purchase orders: projects, the chart of accounts and one open PO per registered supplier."""
    projects = [
        {"project_id": "pj-ops", "code": "OPS", "name": "General operations", "unit_id": None},
        {"project_id": "pj-prj900", "code": "PRJ900", "name": "EU Warehouse Roll-out", "unit_id": "pf-eu-rollout"},
        {"project_id": "pj-brand26", "code": "BRAND26", "name": "Brand refresh 2026", "unit_id": "co-us"},
    ]
    accounts = [
        {"account_id": "ac-6100", "code": "6100", "name": "Freight and logistics", "category": "Logistics"},
        {"account_id": "ac-6200", "code": "6200", "name": "Contractors and professional services", "category": "Contractors"},
        {"account_id": "ac-6300", "code": "6300", "name": "Software and subscriptions", "category": "Software"},
        {"account_id": "ac-6400", "code": "6400", "name": "Travel", "category": "Travel"},
        {"account_id": "ac-6500", "code": "6500", "name": "Materials and components", "category": "Suppliers"},
        {"account_id": "ac-6600", "code": "6600", "name": "Marketing and design", "category": "Marketing"},
        {"account_id": "ac-6700", "code": "6700", "name": "Office", "category": "Office"},
        {"account_id": "ac-7100", "code": "7100", "name": "Capital equipment", "category": "Suppliers"},
    ]
    pos = [
        {"po_id": "PO-2026-0101", "supplier_id": "ben-lumen-design", "currency": "USD", "total_minor": 500_000,
         "account_code": "US-200.BRAND26.6600", "description": "Brand refresh, design phases 1-2",
         "terms": [{"term_id": "T1", "label": "Phase 1 on approval", "amount_minor": 180_000},
                   {"term_id": "T2", "label": "Phase 2 on delivery", "amount_minor": 320_000}], "allow_partial": True},
        {"po_id": "PO-2026-0102", "supplier_id": "ben-eu-components", "currency": "EUR", "total_minor": 1_200_000,
         "account_code": "DE-400.PRJ900.6500", "description": "Racking components for the EU warehouse",
         "terms": [{"term_id": "T1", "label": "30% deposit", "amount_minor": 360_000},
                   {"term_id": "T2", "label": "70% on delivery", "amount_minor": 840_000}], "allow_partial": False},
        {"po_id": "PO-2026-0103", "supplier_id": "ben-uk-logistics", "currency": "GBP", "total_minor": 1_000_000,
         "account_code": "UK-300.OPS.6100", "description": "Q4 freight UK to US, call-off", "terms": [], "allow_partial": True},
        {"po_id": "PO-2026-0104", "supplier_id": "ben-contractor-okafor", "currency": "USD", "total_minor": 960_000,
         "account_code": "US-200.OPS.6200", "description": "Contract design support, Oct-Dec (monthly timesheets)",
         "terms": [], "allow_partial": True},
    ]
    return {"project": projects, "account": accounts, "po": pos}


BLANK_SEED = [
    ("unit", {"unit_id": "hq", "name": "Head office", "kind": "HQ", "country": "SG", "currency": "USD", "timezone": "Asia/Singapore", "cost_centre": "HQ-100"}),
    ("user", {"user_id": "u-admin", "name": "Super Admin", "email": "admin@venthora.example", "role": "super_admin", "unit_id": "hq"}),
]


class OrgStore:
    def __init__(self, path: str | None = None, *, seed: bool = True):
        self.path = Path(path) if path else data_path("VENTHORA_ORG_DB", "data/venthora_org.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as c:
            c.executescript(SCHEMA)
            empty = c.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        if seed and empty and sample_dir() is not None:
            # the sample company (VENTHORA_SAMPLE_DATA, the test suite): offices, people, budgets, prices, projects, POs
            for kind, rows in _seed().items():
                for row in rows:
                    self._put(kind, MODELS[kind].model_validate(row).model_dump(), "seed")
            self._seed_v3()
            self._put("standards", Standards().model_dump() | {"_id": "standards"}, "seed")
            self._put("meta", {"_id": "seed", "version": SEED_VERSION}, "seed")
        elif seed and empty:
            # VENTHORA starts blank: one head office to rename and one Super admin, so a person can sign in and
            # enter everything else from the UI (no offices, people, budgets, prices, POs, accounts or engagements)
            for kind, row in BLANK_SEED:
                self._put(kind, MODELS[kind].model_validate(row).model_dump(), "seed")
            self._put("standards", Standards().model_dump() | {"_id": "standards"}, "seed")
            self._put("meta", {"_id": "seed", "version": SEED_VERSION}, "seed")

    def _seed_v3(self) -> None:
        """Contract dates, LoE and rate on the seeded POs, a Multi Payment subscription, two one-off engagements; a field
        a person already set is kept."""
        for po_id, extra in PO_V3.items():
            po = self.get("po", po_id)
            if po and not po.get("start_date") and not po.get("expiry_date") and po.get("loe") is None:
                self._put("po", PurchaseOrder.model_validate(po | extra).model_dump(), "seed")
        if self.get("po", PO_SUBSCRIPTION["po_id"]) is None:
            self._put("po", PurchaseOrder.model_validate(PO_SUBSCRIPTION).model_dump(), "seed")
        from .doa import ensure_accounts

        ensure_accounts(self)  # the salary account the staff charge codes name
        have = {a["code"] for a in self.list("account")}
        for a in ACCOUNTS_V6:  # taxes and social security, debt service: their own categories of the Standard Payment Priority Policy
            if a["code"] not in have:
                self._put("account", Account.model_validate(a).model_dump(), "seed")
        for uid, extra in STAFF_V3.items():
            u = self.get("user", uid)
            if u and u.get("salary_minor") is None and not u.get("employee_no"):
                self._put("user", User.model_validate(u | extra).model_dump(), "seed")
        for uid, pid in STAFF_ACCOUNTS.items():  # the demo staff's own sandbox accounts (data/payees.json)
            u = self.get("user", uid)
            if u and not u.get("payee_id"):
                self._put("user", u | {"payee_id": pid}, "seed")
        if not self.list("engagement"):
            for e in ENGAGEMENTS:
                rec = Engagement.model_validate(e).model_dump()
                self._put("engagement", rec, "seed")
                if rec["payee_id"]:
                    self._put("po", PurchaseOrder.model_validate(engagement_po(rec)).model_dump(), "seed")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _put(self, kind: str, record: dict, by: str) -> dict:
        rid = record.get(ID_FIELD.get(kind, "_id"))
        with self._lock, self._connect() as c:
            c.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?,?)",
                      (kind, rid, json.dumps(record, sort_keys=True), datetime.now(UTC).isoformat(timespec="seconds"), by))
        return record

    def list(self, kind: str) -> list[dict]:
        with self._connect() as c:
            rows = c.execute("SELECT record_json FROM records WHERE kind = ? ORDER BY record_id", (kind,)).fetchall()
        return [json.loads(r["record_json"]) for r in rows]

    def source(self, kind: str, rid: str) -> str | None:
        """Who or what wrote the record last: seed, dashboard:<person>, upload:<file>, sync:<connector>."""
        with self._connect() as c:
            r = c.execute("SELECT updated_by FROM records WHERE kind = ? AND record_id = ?", (kind, rid)).fetchone()
        return r["updated_by"] if r else None

    def get(self, kind: str, rid: str) -> dict | None:
        with self._connect() as c:
            r = c.execute("SELECT record_json FROM records WHERE kind = ? AND record_id = ?", (kind, rid)).fetchone()
        return json.loads(r["record_json"]) if r else None

    # -- writes, each checked against the rest of the organisation ---------------------------------
    def save(self, kind: str, record: BaseModel, by: str) -> dict:
        data = record.model_dump()
        idf = ID_FIELD[kind]
        if kind == "engagement" and not data.get(idf):
            year = (data.get("event_date") or "2026")[:4]
            taken = {e["engagement_id"] for e in self.list("engagement")} | {p["po_id"] for p in self.list("po")}
            data[idf] = next(f"ENG-{year}-{n:03d}" for n in range(1, 10_000) if f"ENG-{year}-{n:03d}" not in taken)
        if kind == "user":  # who changed a salary or an employee's account: a payroll line that changed waits for someone else
            old = self.get("user", data[idf]) or {}
            if old and any(old.get(k) != data.get(k) for k in ("salary_minor", "salary_currency", "payee_id")):
                at = datetime.now(UTC).isoformat(timespec="seconds")
                self._put("salary_change", {"_id": f"{data[idf]}:{at}", "user_id": data[idf], "at": at, "by": by,
                                            "before": {k: old.get(k) for k in ("salary_minor", "salary_currency", "payee_id")},
                                            "after": {k: data.get(k) for k in ("salary_minor", "salary_currency", "payee_id")}}, by)
        if kind == "pr" and not data.get("requested_date"):
            data["requested_date"] = datetime.now(UTC).strftime("%Y-%m-%d")
        if kind == "fund_request":
            data["request_date"] = data.get("request_date") or datetime.now(UTC).strftime("%Y-%m-%d")
            if not data.get(idf):
                prefix, year = FUND_KINDS[data["kind"]][0], datetime.now(UTC).strftime("%Y")
                taken = {r[idf] for r in self.list(kind)}
                data[idf] = next(f"{prefix}-{year}-{n:04d}" for n in range(1, 100_000) if f"{prefix}-{year}-{n:04d}" not in taken)
            if not data.get("payee_id") and data["kind"] != "odc":
                # an advance or a float is paid to the person's own account when that account is in the advance's
                # currency; in another currency it has no account of its own and is paid on a card
                from .intake.pipeline import Registry

                own = (self.get("user", data["requested_by"]) or {}).get("payee_id")
                ccy = (Registry().payees.get(own or "") or {}).get("currency")
                data["payee_id"] = own if own and (not ccy or ccy == data["currency"]) else None
        if kind in ("pr", "receipt", "ta") and not data.get(idf):
            prefix, year = {"pr": "PR", "receipt": "RC", "ta": "TA"}[kind], datetime.now(UTC).strftime("%Y")
            taken = {r[idf] for r in self.list(kind)}
            data[idf] = next(f"{prefix}-{year}-{n:04d}" for n in range(1, 100_000) if f"{prefix}-{year}-{n:04d}" not in taken)
        data[idf] = data.get(idf) or f"{kind[:2]}-{uuid4().hex[:8]}"
        getattr(self, f"_check_{kind}")(data)
        if kind == "po" and data.get("pr_number"):  # the PR it was raised from is now ordered
            pr = self.get("pr", data["pr_number"])
            if pr and pr["status"] == "APPROVED":
                self._put("pr", pr | {"status": "ORDERED", "po_id": data[idf]}, by)
        if kind == "engagement" and data["payee_id"]:  # paid against its own number, like a PO
            po = engagement_po(data, self.get("po", data[idf]))
            self._check_po(po)
            self._put("po", po, by)
        return self._put(kind, data, by)

    def _check_pr(self, d: dict) -> None:
        from .intake.pipeline import Registry

        self.account_code(d["charge_code"])
        if d.get("supplier_id"):
            payees = Registry().payees
            if d["supplier_id"] not in payees:
                raise OrgError(f"unknown supplier {d['supplier_id']!r}: suggest a registered payee, or leave it empty")
            ccy = payees[d["supplier_id"]].get("currency")
            if ccy and ccy != d["currency"]:
                raise OrgError(f"{payees[d['supplier_id']]['name']} is paid in {ccy}, not {d['currency']}")

    def _check_fund_request(self, d: dict) -> None:
        from .intake.pipeline import Registry

        u = self.get("user", d["requested_by"])
        if u is None or not u.get("active", True):
            raise OrgError(f"{d['requested_by']!r} is not an active person in People and roles")
        self.account_code(d["charge_code"])
        if d.get("payee_id"):
            payees = Registry().payees
            if d["payee_id"] not in payees:
                raise OrgError(f"unknown payee {d['payee_id']!r}: register the bank account in Users & Beneficiaries first")
            ccy = payees[d["payee_id"]].get("currency")
            if ccy and ccy != d["currency"]:
                raise OrgError(f"{payees[d['payee_id']]['name']} is paid in {ccy}, not {d['currency']}")
        if d.get("settle_by") and d.get("request_date") and d["settle_by"] < d["request_date"]:
            raise OrgError("the settlement date is before the request date")

    def _check_ta(self, d: dict) -> None:
        u = self.get("user", d["traveller"])
        if u is None or not u.get("active", True):
            raise OrgError(f"the traveller {d['traveller']!r} is not an active person in People and roles")
        if d["end"] < d["start"]:
            raise OrgError("the trip ends before it starts")
        if d.get("charge_code"):
            self.account_code(d["charge_code"])

    def _check_receipt(self, d: dict) -> None:
        if d.get("receivable_id"):
            from .masterdata import MasterStore

            rv = MasterStore().get("receivable", d["receivable_id"])
            if rv is None:
                raise OrgError(f"no open customer invoice {d['receivable_id']!r}")
            if rv["currency"] != d["currency"]:
                raise OrgError(f"invoice {rv['invoice_no']} is in {rv['currency']}, the receipt in {d['currency']}")
            if d.get("customer_id") and rv["customer_id"] != d["customer_id"]:
                raise OrgError(f"invoice {rv['invoice_no']} belongs to another customer")

    def _check_engagement(self, d: dict) -> None:
        from .intake.pipeline import Registry

        self.account_code(d["charge_code"])
        other = self.get("po", d["engagement_id"])
        if other and other.get("supplier_id") != d.get("payee_id") and not (self.get("engagement", d["engagement_id"])):
            raise OrgError(f"{d['engagement_id']} is already the number of a purchase order")
        if d.get("payee_id"):
            payees = Registry().payees
            if d["payee_id"] not in payees:
                raise OrgError(f"unknown payee {d['payee_id']!r}: register the bank account in Users & Beneficiaries first")
            ccy = payees[d["payee_id"]].get("currency")
            if ccy and ccy != d["currency"]:
                raise OrgError(f"{payees[d['payee_id']]['name']} is paid in {ccy}, not {d['currency']}")

    def delete(self, kind: str, rid: str, by: str) -> None:
        if self.get(kind, rid) is None:
            raise KeyError(rid)
        if kind == "unit":
            if any(u.get("parent_id") == rid for u in self.list("unit")):
                raise OrgError("the unit still has units under it")
            if any(u["unit_id"] == rid for u in self.list("user")):
                raise OrgError("people still belong to the unit; move them first")
            if any(a["unit_id"] == rid for a in self.list("allocation")):
                raise OrgError("the unit still has budget allocations")
        with self._lock, self._connect() as c:
            c.execute("DELETE FROM records WHERE kind = ? AND record_id = ?", (kind, rid))

    def _unit(self, uid: str | None) -> dict:
        unit = self.get("unit", uid) if uid else None
        if unit is None:
            raise OrgError(f"unknown unit {uid!r}")
        return unit

    def _check_unit(self, d: dict) -> None:
        if d["kind"] == "HQ":
            if d.get("parent_id"):
                raise OrgError("the Head Quarter has no parent unit")
            other = [u for u in self.list("unit") if u["kind"] == "HQ" and u["unit_id"] != d["unit_id"]]
            if other:
                raise OrgError("there is already a Head Quarter")
            return
        parent = self._unit(d.get("parent_id"))
        if parent["kind"] not in PARENTS[d["kind"]]:
            raise OrgError(f"a {d['kind'].replace('_', ' ').lower()} cannot sit under a {parent['kind'].replace('_', ' ').lower()}")
        seen, cur = {d["unit_id"]}, parent
        while cur:  # no unit may become its own ancestor
            if cur["unit_id"] in seen:
                raise OrgError("the change would make a loop in the unit tree")
            seen.add(cur["unit_id"])
            cur = self.get("unit", cur["parent_id"]) if cur.get("parent_id") else None

    def _check_user(self, d: dict) -> None:
        unit = self._unit(d["unit_id"])
        level = ADMIN_LEVELS.get(d["role"])
        if level and unit["kind"] != level:
            raise OrgError(f"a {d['role'].replace('_', ' ')} belongs to a {level.replace('_', ' ').lower()} unit, not to {unit['name']}")
        if d["role"] == "super_admin" and d.get("active", True) and any(
                u["role"] == "super_admin" and u.get("active", True) and u["user_id"] != d["user_id"] for u in self.list("user")):
            raise OrgError("there is already an active super admin (the FCO at HQ); deactivate them first")
        if any(u["email"].lower() == d["email"].lower() and u["user_id"] != d["user_id"] for u in self.list("user")):
            raise OrgError("another person already uses that email")
        if d.get("charge_code"):
            d["charge_code"] = self.account_code(d["charge_code"])["code"]
        if d.get("salary_minor") and not d.get("salary_currency"):
            d["salary_currency"] = unit.get("currency") or "USD"
        if d.get("contract_start") and d.get("contract_end") and d["contract_end"] < d["contract_start"]:
            raise OrgError("the contract ends before it starts")
        if d.get("employee_no") and any((u.get("employee_no") or "") == d["employee_no"] and u["user_id"] != d["user_id"] for u in self.list("user")):
            raise OrgError(f"employee number {d['employee_no']} is already used")

    def _check_allocation(self, d: dict) -> None:
        d["project_code"] = (d.get("project_code") or "").strip().upper() or None
        project = None
        if d["project_code"]:
            project = next((x for x in self.list("project") if x["code"] == d["project_code"]), None)
            if project is None:
                raise OrgError(f"unknown project {d['project_code']}")
        if d["basis"] == "project":
            if project is None:
                raise OrgError("a project-based allocation names its project")
            d["unit_id"] = d.get("unit_id") or project.get("unit_id")
            if d["unit_id"]:
                self._unit(d["unit_id"])
        else:
            if not d.get("unit_id"):
                raise OrgError("a regular allocation names its unit (country office, branch or project-finance unit)")
            self._unit(d["unit_id"])
            if project is not None and project.get("unit_id"):
                ids = {u["unit_id"] for u in self.chain(project["unit_id"])}
                below = {u["unit_id"] for u in self.list("unit") if d["unit_id"] in {c["unit_id"] for c in self.chain(u["unit_id"])}}
                if d["unit_id"] not in ids and project["unit_id"] not in below:
                    raise OrgError(f"project {project['code']} is not run by {self._unit(d['unit_id'])['name']} or a unit under it")
        if not d["no_limit"] and d["amount_minor"] <= 0:
            raise OrgError("give an amount, or choose No limit")
        if d["no_limit"]:
            d["amount_minor"] = 0
        if any(a["alloc_id"] != d["alloc_id"] and a.get("basis", "regular") == d["basis"] and a["period"] == d["period"]
               and (d["basis"] == "project" or a.get("unit_id") == d.get("unit_id")) and a.get("project_code") == d["project_code"]
               for a in self.list("allocation")):
            raise OrgError("this scope already has an allocation for that period; edit it instead (one allocation governs a scope)")

    def _check_price(self, d: dict) -> None:
        if d.get("unit_id"):
            self._unit(d["unit_id"])

    def _check_travel_rate(self, d: dict) -> None:
        if not d["lodging_minor"] and not d["per_diem_minor"]:
            raise OrgError(f"{d['city']}: give the lodging rate, the per diem, or both")

    def _check_project(self, d: dict) -> None:
        if d.get("unit_id"):
            self._unit(d["unit_id"])
        if any(x["code"] == d["code"] and x["project_id"] != d["project_id"] for x in self.list("project")):
            raise OrgError(f"project code {d['code']} is already used")

    def _check_mailbox(self, d: dict) -> None:
        d["address"] = d["address"].strip().lower()
        if any(m["address"] == d["address"] and m["mailbox_id"] != d["mailbox_id"] for m in self.list("mailbox")):
            raise OrgError(f"{d['address']} is already a mailbox")

    def _check_account(self, d: dict) -> None:
        if any(x["code"] == d["code"] and x["account_id"] != d["account_id"] for x in self.list("account")):
            raise OrgError(f"account {d['code']} is already in the chart of accounts")

    def _check_po(self, d: dict) -> None:
        from .intake.pipeline import Registry

        payees = Registry().payees  # the file's payees and the synced or uploaded vendors
        if d["supplier_id"] not in payees:
            raise OrgError(f"unknown supplier {d['supplier_id']!r}: a PO is placed with a registered payee")
        if payees[d["supplier_id"]].get("currency") and payees[d["supplier_id"]]["currency"] != d["currency"]:
            raise OrgError(f"{payees[d['supplier_id']]['name']} is paid in {payees[d['supplier_id']]['currency']}, not {d['currency']}")
        self.account_code(d["account_code"])
        ids = [t["term_id"] for t in d["terms"]]
        if len(ids) != len(set(ids)):
            raise OrgError("each payment term needs its own id")
        start = d.get("start_date") or d.get("issued_date")
        if d.get("expiry_date") and start and d["expiry_date"] < start:
            raise OrgError(f"the expiry date {d['expiry_date']} is before the active date {start}")
        if (d.get("loe") is None) != (d.get("rate_minor") is None):
            raise OrgError("a consultant contract needs both the LoE and the rate (or neither)")
        if d.get("loe") is not None and abs(round(d["loe"] * d["rate_minor"]) - d["total_minor"]) > 1:
            raise OrgError(f"the contract value is LoE x rate: {d['loe']:g} {d['loe_unit']}s x {d['currency']} {d['rate_minor'] / 100:,.2f} = "
                           f"{d['currency']} {d['loe'] * d['rate_minor'] / 100:,.2f}, not {d['total_minor'] / 100:,.2f}")
        if d.get("recurring", "none") != "none" and not d.get("expiry_date"):
            raise OrgError("a Multi Payment PO needs an expiry date (the last period it covers)")
        if d.get("pr_number"):
            pr = self.get("pr", d["pr_number"])
            if pr is None:
                raise OrgError(f"no purchase request {d['pr_number']}")
            if not (pr["status"] == "APPROVED" or (pr["status"] == "ORDERED" and pr.get("po_id") == d["po_id"])):
                raise OrgError(f"{d['pr_number']} is {pr['status'].lower()}: a PO is raised only from an approved purchase request")
            if pr["currency"] != d["currency"]:
                raise OrgError(f"{d['pr_number']} was approved in {pr['currency']}, not {d['currency']}")
            if d["total_minor"] > pr["estimated_minor"]:
                raise OrgError(f"the PO total {d['currency']} {d['total_minor'] / 100:,.2f} is above the approved estimate of "
                               f"{d['pr_number']} ({pr['currency']} {pr['estimated_minor'] / 100:,.2f}); raise a new request for the difference")
        if sum(t["amount_minor"] for t in d["terms"]) > d["total_minor"]:
            raise OrgError("the payment terms add up to more than the PO total")

    def account_code(self, code: str) -> dict:
        """'office cost centre . project . account' -> the unit, project and account it names; refused when any part is
        unknown, inactive, or the project does not belong to that office (or an office under it)."""
        parts = (code or "").strip().upper().split(".")
        if len(parts) != 3 or not all(parts):
            raise OrgError(f"account code {code!r} must be OFFICE.PROJECT.ACCOUNT, e.g. DE-400.PRJ900.6500")
        cc, pcode, acode = parts
        unit = next((u for u in self.list("unit") if (u.get("cost_centre") or "").upper() == cc and u.get("active", True)), None)
        if unit is None:
            raise OrgError(f"no active unit has cost centre {cc}")
        project = next((x for x in self.list("project") if x["code"] == pcode and x.get("active", True)), None)
        if project is None:
            raise OrgError(f"unknown or inactive project {pcode}")
        account = next((x for x in self.list("account") if x["code"] == acode and x.get("active", True)), None)
        if account is None:
            raise OrgError(f"account {acode} is not in the chart of accounts")
        if project.get("unit_id"):
            chain = [u["unit_id"] for u in self.chain(project["unit_id"])]
            if unit["unit_id"] not in chain:
                raise OrgError(f"project {pcode} belongs to {self._unit(project['unit_id'])['name']}, not to {unit['name']}")
        return {"code": ".".join(parts), "unit": unit, "project": project, "account": account}

    def subtree(self, unit_id: str) -> set[str]:
        """The unit and every unit under it."""
        units = self.list("unit")
        out, todo = {unit_id}, [unit_id]
        while todo:
            cur = todo.pop()
            for u in units:
                if u.get("parent_id") == cur and u["unit_id"] not in out:
                    out.add(u["unit_id"])
                    todo.append(u["unit_id"])
        return out

    def unit_of_record(self, kind: str, rec: dict) -> str | None:
        """The unit a record belongs to, for the scoped admins' reach (None: company-wide)."""
        if kind == "unit":
            return rec.get("parent_id") if not self.get("unit", rec.get("unit_id") or "") else rec.get("unit_id")
        if kind == "po":
            try:
                return self.account_code(rec.get("account_code", ""))["unit"]["unit_id"]
            except OrgError:
                return None
        return rec.get("unit_id")

    def chain(self, unit_id: str | None) -> list[dict]:
        """The unit and its ancestors, nearest first."""
        out, seen = [], set()
        while unit_id and unit_id not in seen:
            seen.add(unit_id)
            u = self.get("unit", unit_id)
            if u is None:
                break
            out.append(u)
            unit_id = u.get("parent_id")
        return out

    def payment_limit(self, unit_id: str) -> tuple[int | None, dict | None]:
        """The agent's per-payment limit for spending charged to this unit: the nearest unit in the chain that sets one."""
        for u in self.chain(unit_id):
            if u.get("payment_limit_minor") is not None:
                return u["payment_limit_minor"], u
        return None, None

    def approvers_for(self, unit_id: str, amount_minor: int, roles=("country_finance_lead", "finance_manager", "cfo")) -> list[dict]:
        """Who may approve a payment above the unit's limit: active finance roles in the unit chain whose own approval
        limit covers the amount, nearest unit first."""
        order = {u["unit_id"]: i for i, u in enumerate(self.chain(unit_id))}
        people = [u for u in self.list("user") if u.get("active", True) and u["role"] in roles and u["unit_id"] in order
                  and u.get("approval_limit_minor", 0) >= amount_minor]
        return sorted(people, key=lambda u: (order[u["unit_id"]], roles.index(u["role"])))

    def standards(self) -> dict:
        from .finance_standards import BY_KEY, defaults

        rec = Standards().model_dump() | (self.get("standards", "standards") or {})  # a field added later takes its default
        saved = {k: v for k, v in (rec.get("policies") or {}).items() if k in BY_KEY}  # a key the catalog dropped is ignored
        return {k: v for k, v in rec.items() if k != "_id"} | {"policies": defaults() | saved}

    def budget_policy(self) -> dict:
        from .budget_allocation import BudgetPolicy

        rec = self.get("budget_policy", "budget_policy") or BudgetPolicy().model_dump()
        return {k: v for k, v in rec.items() if k != "_id"}

    def save_budget_policy(self, p, by: str) -> dict:
        self._put("budget_policy", p.model_dump() | {"_id": "budget_policy"}, by)
        return self.budget_policy()

    def doc_rules(self) -> dict:
        from .intake.validation import DocRules

        rec = self.get("doc_rules", "doc_rules")
        return DocRules.model_validate({k: v for k, v in (rec or {}).items() if k != "_id"}).model_dump()

    def save_doc_rules(self, r, by: str) -> dict:
        self._put("doc_rules", r.model_dump() | {"_id": "doc_rules"}, by)
        return self.doc_rules()

    def save_standards(self, s: Standards, by: str) -> dict:
        self._put("standards", s.model_dump() | {"_id": "standards"}, by)
        return self.standards()

    # -- the allocation tree: what each unit gave out against what it received --------------------
    def allocation_tree(self, period: str) -> list[dict]:
        units = {u["unit_id"]: u for u in self.list("unit")}
        own: dict[str, dict[str, int]] = {}
        for a in self.list("allocation"):
            if a["period"] == period and a.get("basis", "regular") == "regular" and a.get("unit_id") and not a.get("no_limit"):
                own.setdefault(a["unit_id"], {}).setdefault(a["currency"], 0)
                own[a["unit_id"]][a["currency"]] += a["amount_minor"]
        rows = []
        for uid, u in units.items():
            children = [c for c in units.values() if c.get("parent_id") == uid]
            given: dict[str, int] = {}
            for c in children:
                for ccy, amt in own.get(c["unit_id"], {}).items():
                    given[ccy] = given.get(ccy, 0) + amt
            mine = own.get(uid, {})
            over = {c: given[c] - mine.get(c, 0) for c in given if given[c] > mine.get(c, 0)}
            rows.append({"unit_id": uid, "name": u["name"], "kind": u["kind"], "parent_id": u.get("parent_id"),
                         "allocated": mine, "passed_down": given, "over_allocated": over})
        return rows
