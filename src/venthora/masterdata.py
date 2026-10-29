# ruff: noqa: E501
"""Master data from the company's own systems: vendors (with bank details), customers, open bills (AP), receivables
(AR), budget lines and tenant books. It replaces the placeholders in data/*.json without changing how VENTHORA decides.

Sources: a sync from the old application's API (connectors.py), an uploaded CSV / Excel / PDF (imports.py), or a
person on the dashboard. Every record keeps its source and the source's own id, so the readiness page can say what is
still a placeholder. One pipeline serves all sources: rows are mapped, cleaned, validated against the schemas below,
and upserted; a row that fails validation is rejected with its reason and never half-written.

Overlay: a record here wins over the file record with the same key; an empty store leaves VENTHORA exactly as the
files describe it (the tests and ground-truth suites rely on that).

Trust: bank details that arrive from outside are never trusted by arriving. A new vendor starts NEW and a changed
account makes the payee CHANGED (payees.py): no payment until a call-back on the registered number, as for any
change by email. A sync cannot be used to redirect a payment.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .config import data_path

CCY = re.compile(r"^[A-Z]{3}$")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,60}$")


def _ccy(v: str) -> str:
    v = (v or "").strip().upper()
    if not CCY.match(v):
        raise ValueError("a currency is a three-letter ISO code")
    return v


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:60] or "x"


class Strict(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class BankDetails(Strict):
    account_name: str = Field(min_length=1, max_length=140)
    account_currency: str
    bank_country_code: str = Field(pattern=r"^[A-Z]{2}$")
    bank_name: str = Field(default="", max_length=140)
    account_number: str = Field(default="", max_length=40)
    iban: str = Field(default="", max_length=40)
    swift_code: str = Field(default="", max_length=11)
    routing_type: str = Field(default="", max_length=30, description="aba, sort_code, bsb, ifsc ... (Airwallex account_routing_type1)")
    routing_value: str = Field(default="", max_length=40)

    @field_validator("account_currency")
    @classmethod
    def c(cls, v):
        return _ccy(v)

    @field_validator("iban")
    @classmethod
    def iban_ok(cls, v):
        v = re.sub(r"\s+", "", v or "").upper()
        if v and not re.match(r"^[A-Z]{2}\d{2}[A-Z0-9]{10,30}$", v):
            raise ValueError("not an IBAN")
        return v

    def check(self) -> None:
        if not (self.account_number or self.iban):
            raise ValueError("bank details need an account number or an IBAN")


class Address(Strict):
    street_address: str = Field(default="", max_length=140)
    city: str = Field(default="", max_length=60)
    state: str = Field(default="", max_length=60)
    postcode: str = Field(default="", max_length=20)
    country_code: str = Field(default="", max_length=2)


class Vendor(Strict):
    vendor_id: str = Field(description="VENTHORA's id (the beneficiary id); derived from the name when the source has none")
    name: str = Field(min_length=1, max_length=200)
    currency: str
    payout_method: Literal["LOCAL", "SWIFT"] = "LOCAL"
    contacts: list[str] = Field(default_factory=list, description="the mailboxes invoices may come from")
    domains: list[str] = Field(default_factory=list)
    invoice_prefix: str = Field(default="", max_length=20)
    budget_line: str = Field(default="", max_length=60)
    default_priority: int = Field(default=5, ge=1, le=10)
    registered_phone: str = Field(default="", max_length=40, description="the number a call-back goes to")
    entity_type: Literal["COMPANY", "PERSONAL"] = "COMPANY"
    transfer_reason: str = Field(default="business_expenses", max_length=60)
    bank: BankDetails | None = None
    address: Address = Field(default_factory=Address)

    @field_validator("currency")
    @classmethod
    def c(cls, v):
        return _ccy(v)

    @field_validator("contacts")
    @classmethod
    def mails(cls, v):
        out = [x.strip().lower() for x in v if x and x.strip()]
        bad = [x for x in out if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", x)]
        if bad:
            raise ValueError(f"not an email address: {bad[0]}")
        return out


class Customer(Strict):
    customer_id: str
    name: str = Field(min_length=1, max_length=200)
    currency: str
    contacts: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    promises_kept: int | None = Field(default=None, ge=0, le=10_000, description="payment promises kept (calibrates receipt confidence)")
    promises_missed: int | None = Field(default=None, ge=0, le=10_000)

    @field_validator("currency")
    @classmethod
    def c(cls, v):
        return _ccy(v)


class Bill(Strict):
    """An open supplier invoice in the old system (accounts payable)."""
    bill_id: str
    vendor_id: str
    invoice_no: str = Field(min_length=1, max_length=60)
    amount_minor: int = Field(gt=0, le=10**13)
    currency: str
    due_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    po_number: str = Field(default="", max_length=40)
    description: str = Field(default="", max_length=300)

    @field_validator("currency")
    @classmethod
    def c(cls, v):
        return _ccy(v)


class Receivable(Strict):
    """An open customer invoice in the old system (accounts receivable)."""
    receivable_id: str
    customer_id: str
    invoice_no: str = Field(min_length=1, max_length=60)
    amount_minor: int = Field(gt=0, le=10**13)
    currency: str
    due_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("currency")
    @classmethod
    def c(cls, v):
        return _ccy(v)


class BudgetLineIn(Strict):
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    line: str = Field(min_length=1, max_length=60)
    amount_minor: int = Field(ge=0, le=10**14)
    auto_share_bp: int = Field(default=10_000, ge=0, le=10_000)
    alert_share_bp: int = Field(default=8_000, ge=0, le=10_000)
    require_approval_on_alert: bool = False


class TenantBook(Strict):
    tenant: str = Field(min_length=1, max_length=60)
    tenant_name: str = Field(default="", max_length=120)
    book: str = Field(min_length=1, max_length=80, description="a book id or a glob pattern")
    allowed_payees: list[str] = Field(default_factory=lambda: ["*"])
    approvers: list[dict] = Field(default_factory=list)


ENTITIES: dict[str, tuple[type[BaseModel], str]] = {
    "vendor": (Vendor, "vendor_id"), "customer": (Customer, "customer_id"), "bill": (Bill, "bill_id"),
    "receivable": (Receivable, "receivable_id"), "budget_line": (BudgetLineIn, "_key"), "tenant_book": (TenantBook, "_key"),
}
LABELS = {"vendor": "Vendors and bank details", "customer": "Customers", "bill": "Open bills (AP)", "receivable": "Receivables (AR)",
          "budget_line": "Budget lines", "tenant_book": "Tenant books", "po": "Purchase orders"}


def key_of(entity: str, rec: dict) -> str:
    if entity == "budget_line":
        return f"{rec['period']}:{rec['line']}"
    if entity == "tenant_book":
        return f"{rec['tenant']}:{rec['book']}"
    return str(rec[ENTITIES[entity][1]])


def validate(entity: str, row: dict) -> dict:
    """The row as a clean record, or ValueError with the reason (pydantic's message, made readable)."""
    model, idf = ENTITIES[entity]
    row = dict(row)
    if entity == "vendor" and not row.get("vendor_id") and row.get("name"):
        row["vendor_id"] = slug(row["name"])
    if entity == "customer" and not row.get("customer_id") and row.get("name"):
        row["customer_id"] = slug(row["name"])
    if entity in ("bill", "receivable") and not row.get(idf) and row.get("invoice_no"):
        owner = row.get("vendor_id") or row.get("customer_id") or ""
        row[idf] = f"{owner}:{row['invoice_no']}"
    try:
        rec = model.model_validate(row)
    except ValidationError as exc:
        e = exc.errors()[0]
        raise ValueError(f"{'.'.join(str(x) for x in e['loc']) or 'row'}: {e['msg']}") from None
    if entity == "vendor" and rec.bank is not None:
        rec.bank.check()
    return rec.model_dump()


SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    entity TEXT NOT NULL, key TEXT NOT NULL, record_json TEXT NOT NULL, source TEXT NOT NULL, external_id TEXT,
    row_hash TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (entity, key));
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, source TEXT NOT NULL, entity TEXT NOT NULL, started_at TEXT NOT NULL, report_json TEXT NOT NULL);
"""


class MasterStore:
    def __init__(self, path: str | None = None):
        self.path = Path(path) if path else data_path("VENTHORA_MASTER_DB", "data/venthora_master.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as c:
            c.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def list(self, entity: str) -> list[dict]:
        with self._connect() as c:
            rows = c.execute("SELECT record_json, source, external_id, updated_at FROM records WHERE entity = ? ORDER BY key", (entity,)).fetchall()
        return [json.loads(r["record_json"]) | {"_source": r["source"], "_external_id": r["external_id"], "_updated_at": r["updated_at"]}
                for r in rows]

    def get(self, entity: str, key: str) -> dict | None:
        with self._connect() as c:
            r = c.execute("SELECT record_json FROM records WHERE entity = ? AND key = ?", (entity, key)).fetchone()
        return json.loads(r["record_json"]) if r else None

    def counts(self) -> dict[str, dict[str, int]]:
        with self._connect() as c:
            out: dict[str, dict[str, int]] = {}
            for r in c.execute("SELECT entity, source, COUNT(*) AS n FROM records GROUP BY entity, source"):
                out.setdefault(r["entity"], {})[r["source"]] = r["n"]
        return out

    def version(self) -> str:
        with self._connect() as c:
            r = c.execute("SELECT MAX(updated_at) AS v, COUNT(*) AS n FROM records").fetchone()
        return f"{r['v']}:{r['n']}"

    def upsert(self, entity: str, rows: list[dict], *, source: str, external_ids: list[str | None] | None = None,
               dry_run: bool = False) -> dict:
        """Validate and write rows; the report says what was created, updated, unchanged or rejected (and why)."""
        report = {"entity": entity, "source": source, "created": [], "updated": [], "unchanged": [], "rejected": [], "changes": []}
        clean: list[tuple[str, dict, str | None, str]] = []
        for i, row in enumerate(rows):
            ext = (external_ids or [None] * len(rows))[i]
            try:
                rec = validate(entity, row)
            except ValueError as exc:
                report["rejected"].append({"row": i + 1, "external_id": ext, "error": str(exc)})
                continue
            key = key_of(entity, rec)
            h = hashlib.sha256(json.dumps(rec, sort_keys=True).encode()).hexdigest()
            old = self.get(entity, key)
            if old is None:
                report["created"].append(key)
            elif hashlib.sha256(json.dumps(old, sort_keys=True).encode()).hexdigest() == h:
                report["unchanged"].append(key)
                continue
            else:
                report["updated"].append(key)
                report["changes"].append({"key": key, "fields": sorted(k for k in rec if rec.get(k) != old.get(k))})
            clean.append((key, rec, ext, h))
        report["records"] = {k: r for k, r, _, _ in clean}
        if dry_run:
            return report
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self._lock, self._connect() as c:
            for key, rec, ext, h in clean:
                c.execute("INSERT OR REPLACE INTO records VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (entity, key, json.dumps(rec, sort_keys=True), source, ext, h, now))
            c.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?)", (uuid4().hex[:16],
                                                                   source, entity, now, json.dumps({k: v for k, v in report.items() if k != "records"})))
        return report

    def runs(self, limit: int = 30) -> list[dict]:
        with self._connect() as c:
            rows = c.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) | {"report": json.loads(r["report_json"])} for r in rows]

    def delete(self, entity: str, key: str) -> bool:
        with self._lock, self._connect() as c:
            return c.execute("DELETE FROM records WHERE entity = ? AND key = ?", (entity, key)).rowcount == 1


# -- overlays: what the rest of VENTHORA reads ----------------------------------------------------------------------------
def registry_entries(store: MasterStore) -> tuple[dict, dict]:
    """Vendors and customers in the shape of data/payees.json (payees, payers)."""
    payees, payers = {}, {}
    for v in store.list("vendor"):
        payees[v["vendor_id"]] = {"name": v["name"], "currency": v["currency"], "payout_method": v["payout_method"],
                                  "contacts": v["contacts"], "domains": v["domains"], "aliases": [], "invoice_prefix": v["invoice_prefix"] or v["vendor_id"],
                                  "budget_line": v["budget_line"] or None, "default_priority": v["default_priority"],
                                  "lifecycle": {"status": "NEW", "registered_phone": v["registered_phone"] or None}, "_source": v["_source"]}
    for c in store.list("customer"):
        hist = None
        if c.get("promises_kept") is not None or c.get("promises_missed") is not None:
            hist = {"kept": c.get("promises_kept") or 0, "missed": c.get("promises_missed") or 0}
        payers[c["customer_id"]] = {"name": c["name"], "currency": c["currency"], "contacts": c["contacts"], "domains": c["domains"],
                                    "aliases": c["aliases"], "history": hist, "_source": c["_source"]}
    return payees, payers


def beneficiary_payload(v: dict) -> dict | None:
    """Airwallex's beneficiary payload for a vendor (the shape data/beneficiaries.json holds)."""
    b = v.get("bank")
    if not b:
        return None
    bank = {"account_currency": b["account_currency"], "account_name": b["account_name"], "bank_country_code": b["bank_country_code"]}
    for k_out, k_in in (("bank_name", "bank_name"), ("account_number", "account_number"), ("iban", "iban"), ("swift_code", "swift_code")):
        if b.get(k_in):
            bank[k_out] = b[k_in]
    if b.get("routing_type") and b.get("routing_value"):
        bank["account_routing_type1"], bank["account_routing_value1"] = b["routing_type"], b["routing_value"]
    addr = {k: x for k, x in (v.get("address") or {}).items() if x}
    who = {"company_name": v["name"]} if v["entity_type"] == "COMPANY" else {"first_name": v["name"].split()[0], "last_name": " ".join(v["name"].split()[1:]) or v["name"]}
    return {"transfer_reason": v.get("transfer_reason") or "business_expenses",
            "payload": {"nickname": v["name"][:50], "transfer_methods": [v["payout_method"]], "payment_methods": [v["payout_method"]],
                        "beneficiary": {"type": "BANK_ACCOUNT", "entity_type": v["entity_type"], **who, "address": addr, "bank_details": bank}}}


def beneficiaries(store: MasterStore) -> dict:
    return {v["vendor_id"]: p for v in store.list("vendor") if (p := beneficiary_payload(v))}


def budget_for(store: MasterStore, period: str) -> dict | None:
    lines = [ln for ln in store.list("budget_line") if ln["period"] == period]
    if not lines:
        return None
    return {"period": period, "unbudgeted_requires_approval": True,
            "lines": [{k: ln[k] for k in ("line", "amount_minor", "auto_share_bp", "alert_share_bp", "require_approval_on_alert")} for ln in lines]}


def tenant_overlay(store: MasterStore, registry: dict) -> dict:
    """data/tenants.json with the synced books added (a synced book replaces the file's book of the same id)."""
    out = json.loads(json.dumps(registry))
    for b in store.list("tenant_book"):
        t = out.setdefault("tenants", {}).setdefault(b["tenant"], {"name": b["tenant_name"] or b["tenant"], "books": {}})
        if b["tenant_name"]:
            t["name"] = b["tenant_name"]
        t.setdefault("books", {})[b["book"]] = {"allowed_payees": b["allowed_payees"], "approvers": b["approvers"], "policy_overrides": {}}
    return out
