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
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from .config import data_path

UnitKind = Literal["HQ", "COUNTRY_OFFICE", "BRANCH_OFFICE", "PROJECT_FINANCE"]
Role = Literal["admin", "cfo", "finance_manager", "treasury_analyst", "country_finance_lead",
               "project_accountant", "auditor"]
WRITE_ROLES = {"admin", "cfo", "finance_manager"}
PARENTS: dict[str, set[str]] = {"HQ": set(), "COUNTRY_OFFICE": {"HQ"}, "BRANCH_OFFICE": {"COUNTRY_OFFICE"},
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
    Scheduled terms (e.g. 30% deposit, 70% on delivery) and, when allow_partial, any partial invoice while the PO has room."""
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

    @field_validator("currency")
    @classmethod
    def check_currency(cls, v: str) -> str:
        return _ccy(v)


class Mailbox(BaseModel):
    """An inbound address the mail service posts to /webhooks/email, and what may arrive there."""
    mailbox_id: str | None = Field(default=None, max_length=40)
    address: str = Field(min_length=3, max_length=200, pattern=r"^[^@\s]+@[^@\s]+$")
    purpose: Literal["invoices", "claims", "remittances", "any"] = "any"
    provider: Literal["postmark", "raw"] = "postmark"
    note: str = Field(default="", max_length=200)
    active: bool = True


PURPOSE_KINDS = {"invoices": {"supplier_invoice", "contractor_claim", "bank_change"}, "claims": {"expense_claim"},
                 "remittances": {"remittance_notice"}}


class User(BaseModel):
    user_id: str | None = Field(default=None, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    role: Role
    unit_id: str
    approval_limit_minor: int = Field(default=0, ge=0, le=10**13)
    active: bool = True


class Allocation(BaseModel):
    alloc_id: str | None = Field(default=None, max_length=40)
    period: str = Field(pattern=r"^\d{4}(-(0[1-9]|1[0-2])|-Q[1-4])?$")
    unit_id: str
    line: str = Field(min_length=1, max_length=60)
    amount_minor: int = Field(ge=0, le=10**14)
    currency: str = "USD"

    @field_validator("currency")
    @classmethod
    def check_currency(cls, v: str) -> str:
        return _ccy(v)


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
    fx_rate_source: Literal["airwallex", "ecb"] = "airwallex"
    expense_categories: list[str] = Field(default_factory=lambda: ["Payroll", "Logistics", "Suppliers", "Contractors",
                                                                   "Software", "Travel", "Office", "Marketing"])
    require_po: bool = Field(default=True, description="a supplier invoice is payable only against an open purchase order it quotes")

    @field_validator("base_currency")
    @classmethod
    def check_currency(cls, v: str) -> str:
        return _ccy(v)


SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    kind TEXT NOT NULL, record_id TEXT NOT NULL, record_json TEXT NOT NULL, updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL, PRIMARY KEY (kind, record_id));
"""
MODELS = {"unit": Unit, "user": User, "allocation": Allocation, "price": Price, "project": Project, "account": Account, "mailbox": Mailbox,
          "po": PurchaseOrder}
ID_FIELD = {"unit": "unit_id", "user": "user_id", "allocation": "alloc_id", "price": "price_id", "project": "project_id",
            "account": "account_id", "po": "po_id", "mailbox": "mailbox_id"}
SEED_VERSION = 2
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


class OrgStore:
    def __init__(self, path: str | None = None, *, seed: bool = True):
        self.path = Path(path) if path else data_path("VENTHORA_ORG_DB", "data/venthora_org.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as c:
            c.executescript(SCHEMA)
            empty = c.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        if seed and empty:
            for kind, rows in _seed().items():
                for row in rows:
                    self._put(kind, MODELS[kind].model_validate(row).model_dump(), "seed")
            self._put("standards", Standards().model_dump() | {"_id": "standards"}, "seed")
            self._put("meta", {"_id": "seed", "version": SEED_VERSION}, "seed")
        elif seed and (self.get("meta", "seed") or {}).get("version", 1) < SEED_VERSION:
            # a store made before purchase orders: add the new reference data, change nothing a person entered
            for kind, rows in _seed_v2().items():
                if not self.list(kind):
                    for row in rows:
                        self._put(kind, MODELS[kind].model_validate(row).model_dump(), "seed")
            for u in self.list("unit"):
                if u.get("payment_limit_minor") is None and u["unit_id"] in UNIT_LIMITS:
                    self._put("unit", u | {"payment_limit_minor": UNIT_LIMITS[u["unit_id"]]}, "seed")
            self._put("meta", {"_id": "seed", "version": SEED_VERSION}, "seed")

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

    def get(self, kind: str, rid: str) -> dict | None:
        with self._connect() as c:
            r = c.execute("SELECT record_json FROM records WHERE kind = ? AND record_id = ?", (kind, rid)).fetchone()
        return json.loads(r["record_json"]) if r else None

    # -- writes, each checked against the rest of the organisation ---------------------------------
    def save(self, kind: str, record: BaseModel, by: str) -> dict:
        data = record.model_dump()
        idf = ID_FIELD[kind]
        data[idf] = data.get(idf) or f"{kind[:2]}-{uuid4().hex[:8]}"
        getattr(self, f"_check_{kind}")(data)
        return self._put(kind, data, by)

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
        self._unit(d["unit_id"])
        if any(u["email"].lower() == d["email"].lower() and u["user_id"] != d["user_id"] for u in self.list("user")):
            raise OrgError("another person already uses that email")

    def _check_allocation(self, d: dict) -> None:
        self._unit(d["unit_id"])

    def _check_price(self, d: dict) -> None:
        if d.get("unit_id"):
            self._unit(d["unit_id"])

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
        if sum(t["amount_minor"] for t in d["terms"]) > d["total_minor"]:
            raise OrgError("the payment terms add up to more than the PO total")
        if not d["terms"] and not d["allow_partial"]:
            raise OrgError("a PO without payment terms must allow partial invoices (otherwise it can never be paid)")

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
        rec = self.get("standards", "standards") or Standards().model_dump()
        return {k: v for k, v in rec.items() if k != "_id"}

    def save_standards(self, s: Standards, by: str) -> dict:
        self._put("standards", s.model_dump() | {"_id": "standards"}, by)
        return self.standards()

    # -- the allocation tree: what each unit gave out against what it received --------------------
    def allocation_tree(self, period: str) -> list[dict]:
        units = {u["unit_id"]: u for u in self.list("unit")}
        own: dict[str, dict[str, int]] = {}
        for a in self.list("allocation"):
            if a["period"] == period:
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
