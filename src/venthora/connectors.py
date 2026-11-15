# ruff: noqa: E501
"""Sync API: read master data from the company's old application over a generic REST/JSON API.

A connector names the base URL, how to authenticate (none, bearer token, basic, or an API-key header; secrets are
write-only like the AI keys) and, per entity, the path, where the list sits in the reply, and a field mapping from the
old system's names to VENTHORA's. A mapping value is a source path with optional transforms, separated by "|":

    "vendor_id": "VendorCode|lower"         "bank.iban": "Bank.IBAN|nospace|upper"
    "amount_minor": "Amount|major"           "due_date": "DueDate|date"           "contacts": "Emails|split"
    "currency": "=USD"  (a constant)

Transforms: major (a decimal amount to minor units), int, upper, lower, nospace, alnum (letters and digits only),
default:<value> (when the source field is empty), date (to YYYY-MM-DD), split (a
comma- or semicolon-separated text to a list). Every synced row then goes through the same validation as an upload
(masterdata.py); a row that does not fit is rejected with its reason, never half-written.

The other way, ``push`` names what VENTHORA writes back (sync_catalog.PUSH: payments made, journal entries, PO
balances, payee verification, request decisions, budget usage). Each record is POSTed (or PUT) as
``{"type", "id", "data"}`` with an Idempotency-Key of its id and content, and only when it is new or changed since
the last successful send (``connector_push``), so a retry or a second run never books anything twice.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import re
import sqlite3
import threading
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from .config import data_path
from .sync_catalog import PUSH

ENTITY_NAMES = ("unit", "user", "project", "account", "price", "vendor", "customer", "po", "bill", "receivable", "budget_line", "tenant_book",
                "pr", "engagement", "ta", "fund_request")


class EntityMap(BaseModel):
    enabled: bool = True
    path: str = Field(min_length=1, max_length=600)
    items_path: str = Field(default="", max_length=100, description="where the list sits in the reply, e.g. data.items")
    id_path: str = Field(default="", max_length=60, description="the record's own id in the other system (ERPNext: name); blank = id/Id/ID")
    mapping: dict[str, str] = Field(default_factory=dict)


class PushMap(BaseModel):
    enabled: bool = True
    path: str = Field(min_length=1, max_length=200)
    method: Literal["POST", "PUT"] = "POST"


class Auth(BaseModel):
    type: Literal["none", "bearer", "basic", "header"] = "none"
    username: str = Field(default="", max_length=120)
    header_name: str = Field(default="X-API-Key", max_length=60)
    secret: str | None = Field(default=None, max_length=500, description="token or password; write-only, None keeps it")
    clear_secret: bool = False


class Connector(BaseModel):
    connector_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,40}$")
    name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=8, max_length=300)
    auth: Auth = Field(default_factory=Auth)
    entities: dict[str, EntityMap] = Field(default_factory=dict)
    push: dict[str, PushMap] = Field(default_factory=dict, description="what VENTHORA writes back (sync_catalog.PUSH)")
    schedule_minutes: int = Field(default=0, ge=0, le=1440, description="0 = only when a person presses Sync; 1 = every minute")
    enabled: bool = Field(default=True, description="false = disconnected: kept with its key so Connect needs nothing typed again, never read or written")

    @field_validator("base_url")
    @classmethod
    def url(cls, v):
        v = v.strip().rstrip("/")
        u = urlparse(v)
        if u.scheme not in ("http", "https") or not u.netloc:
            raise ValueError("the base URL must start with http:// or https://")
        return v

    @field_validator("entities")
    @classmethod
    def known(cls, v):
        bad = [k for k in v if k not in ENTITY_NAMES]
        if bad:
            raise ValueError(f"unknown entity {bad[0]}; one of {', '.join(ENTITY_NAMES)}")
        return v

    @field_validator("push")
    @classmethod
    def known_push(cls, v):
        bad = [k for k in v if k not in PUSH]
        if bad:
            raise ValueError(f"unknown item to send back {bad[0]}; one of {', '.join(PUSH)}")
        return v


DEMO_KEY = "demo-legacy-key"  # the stand-in ERP's fake API key: demo only, it guards fictional data


def _erp_list(doctype: str, fields: list[str], filters: list | None = None) -> str:
    """An ERPNext / Frappe list call: the fields to return, the filters, and every row (no paging limit)."""
    from urllib.parse import quote

    q = f"fields={quote(json.dumps(fields))}&limit_page_length=0"
    if filters:
        q += f"&filters={quote(json.dumps(filters))}"
    return f"api/resource/{quote(doctype)}?{q}"


def erpnext_connector(site_url: str, base_currency: str = "", default_unit: str = "", default_charge_code: str = "") -> Connector:
    """ERPNext (Frappe) as the company's ERP, over its REST API: https://<site>/api/resource/<DocType>, the list under
    "data", the record's id in "name", and the key sent as "Authorization: token <api key>:<api secret>" (User ->
    API Access -> Generate Keys). Only submitted documents (docstatus 1) are read; open bills and receivables only.
    base_currency: ERPNext leaves a customer's currency empty when it is the company's; default_unit: the VENTHORA office
    employees land in until someone assigns them; default_charge_code: the charge code of POs (ERPNext keeps none)."""
    def em(doctype, fields, mapping, filters=None):
        return EntityMap(path=_erp_list(doctype, fields, filters), items_path="data", id_path="name", mapping=mapping)

    submitted = [["docstatus", "=", 1]]
    return Connector(connector_id="erpnext", name="ERPNext", base_url=site_url, schedule_minutes=0,
                     auth=Auth(type="header", header_name="Authorization"), entities={
        "vendor": em("Supplier", ["name", "supplier_name", "default_currency", "email_id", "mobile_no", "tax_id"], {
            # no vendor_id: the payee already on file with the same e-mail or name is matched (ingestion._known_vendor),
            # else one is made from the name; POs and bills name the supplier, resolved the same way (ingestion._supplier)
            "name": "supplier_name", "currency": "default_currency|upper", "contacts": "email_id|split",
            "registered_phone": "mobile_no", "tax_id": "tax_id"}, [["disabled", "=", 0]]),
        "customer": em("Customer", ["name", "customer_name", "default_currency", "email_id"], {
            "customer_id": "name|lower", "name": "customer_name", "currency": "default_currency|upper" + (f"|default:{base_currency}" if base_currency else ""),
            "contacts": "email_id|split"}, [["disabled", "=", 0]]),
        "user": em("Employee", ["name", "employee_name", "company_email", "personal_email", "designation", "date_of_joining"], {
            "name": "employee_name", "email": "company_email|lower", "employee_no": "name", "job_title": "designation",
            "contract_start": "date_of_joining|date"} | ({"unit_id": f"={default_unit}"} if default_unit else {}), [["status", "=", "Active"]]),
        "project": em("Project", ["name", "project_name"], {"code": "name|alnum|upper", "name": "project_name"}),
        "po": em("Purchase Order", ["name", "supplier", "currency", "grand_total", "transaction_date", "schedule_date"], {
            "po_id": "name|upper", "supplier_id": "supplier|lower", "currency": "currency|upper", "total_minor": "grand_total|major",
            "issued_date": "transaction_date|date", "start_date": "transaction_date|date", "expiry_date": "schedule_date|date"}
            | ({"account_code": f"={default_charge_code}"} if default_charge_code else {}), submitted),
        "bill": em("Purchase Invoice", ["name", "supplier", "bill_no", "currency", "outstanding_amount", "due_date"], {
            "bill_id": "name", "vendor_id": "supplier|lower", "invoice_no": "bill_no|upper", "amount_minor": "outstanding_amount|major",
            "currency": "currency|upper", "due_date": "due_date|date"}, [*submitted, ["outstanding_amount", ">", 0]]),
        "receivable": em("Sales Invoice", ["name", "customer", "currency", "outstanding_amount", "due_date"], {
            "receivable_id": "name", "customer_id": "customer|lower", "invoice_no": "name|upper", "amount_minor": "outstanding_amount|major",
            "currency": "currency|upper", "due_date": "due_date|date"}, [*submitted, ["outstanding_amount", ">", 0]]),
    })


def demo_connector(base_url: str) -> Connector:
    """The connector to the stand-in ERP (data/demo_legacy_erp.json at /demo/legacy-erp): every entity mapped from the
    old system's field names, so a sync fills VENTHORA's tables the way a real one would."""
    return Connector(connector_id="legacy-erp-demo", name="Legacy ERP (demo)", base_url=f"{base_url}/demo/legacy-erp",
                     auth=Auth(type="header", header_name="X-Legacy-Key", secret=DEMO_KEY), entities={
        "vendor": EntityMap(path="vendors", items_path="items", mapping={
            "vendor_id": "Code|lower", "name": "CompanyName", "currency": "Currency|upper", "payout_method": "PayMethod|upper",
            "contacts": "Emails|split", "registered_phone": "Phone", "budget_line": "Category|lower", "default_priority": "Priority|int",
            "bank.account_name": "Bank.Holder", "bank.account_number": "Bank.Number", "bank.iban": "Bank.IBAN|nospace|upper",
            "bank.swift_code": "Bank.SWIFT", "bank.routing_type": "Bank.RoutingType", "bank.routing_value": "Bank.Routing",
            "bank.bank_name": "Bank.BankName", "bank.bank_country_code": "Bank.Country|upper", "bank.account_currency": "Bank.Currency|upper",
            "bank.bank_account_category": "Bank.AccountType",
            "address.street_address": "Address.Street", "address.city": "Address.City", "address.state": "Address.State",
            "address.postcode": "Address.Zip", "address.country_code": "Address.Country|upper"}),
        "unit": EntityMap(path="units", items_path="items", mapping={
            "unit_id": "Code|lower", "name": "Name", "kind": "Type", "parent_id": "Parent|lower", "country": "Country|upper",
            "currency": "Currency|upper", "timezone": "TimeZone", "cost_centre": "CostCentre|upper", "payment_limit_minor": "PayLimit|major"}),
        "user": EntityMap(path="people", items_path="items", mapping={
            "name": "FullName", "email": "Email|lower", "role": "Role", "unit_id": "Unit|lower", "approval_limit_minor": "ApprovalLimit|major"}),
        "project": EntityMap(path="projects", items_path="items", mapping={
            "code": "Code|upper", "name": "Name", "unit_id": "Unit|lower"}),
        "account": EntityMap(path="accounts", items_path="items", mapping={
            "code": "GL", "name": "Name", "category": "Category"}),
        "price": EntityMap(path="prices", items_path="items", mapping={
            "item": "Item", "category": "Category", "unit": "Per", "price_minor": "Price|major", "currency": "Currency|upper",
            "unit_id": "Office|lower"}),
        "customer": EntityMap(path="customers", items_path="items", mapping={
            "customer_id": "Code|lower", "name": "CompanyName", "currency": "Currency|upper", "contacts": "Emails|split",
            "promises_kept": "Kept|int", "promises_missed": "Missed|int"}),
        "po": EntityMap(path="purchase_orders", items_path="items", mapping={
            "po_id": "Number|upper", "supplier_id": "Vendor|lower", "currency": "Currency|upper", "total_minor": "Total|major",
            "account_code": "CostCode|upper", "description": "Memo", "allow_partial": "PartialAllowed", "terms": "Terms",
            "issued_date": "Issued|date", "start_date": "Start|date", "expiry_date": "Expiry|date", "loe": "LoE", "loe_unit": "LoEUnit|lower",
            "rate_minor": "Rate|major"}),
        "pr": EntityMap(path="purchase_requests", items_path="items", mapping={
            "pr_number": "Number|upper", "title": "Title", "requested_by": "RequestedBy", "supplier_id": "Vendor|lower", "currency": "Currency|upper",
            "estimated_minor": "Estimate|major", "charge_code": "CostCode|upper", "needed_by": "NeededBy|date", "requested_date": "Requested|date",
            "justification": "Reason", "status": "Status|upper", "decided_by": "ApprovedBy"}),
        "engagement": EntityMap(path="engagements", items_path="items", mapping={
            "engagement_id": "Number|upper", "name": "Name", "role": "Type|lower", "organisation": "Organisation", "email": "Email|lower",
            "event": "Event", "event_date": "Date|date", "fee_minor": "Fee|major", "currency": "Currency|upper", "charge_code": "CostCode|upper",
            "payee_id": "Payee|lower"}),
        "fund_request": EntityMap(path="fund_requests", items_path="items", mapping={
            "request_number": "Number|upper", "kind": "Kind", "requested_by": "RequestedBy|lower", "amount_minor": "Amount|major",
            "currency": "Currency|upper", "charge_code": "CostCode|upper", "purpose": "Purpose", "activity": "Activity", "request_date": "Requested|date",
            "needed_by": "NeededBy|date", "settle_by": "SettleBy|date", "status": "Status|upper", "decided_by": "ApprovedBy"}),
        "ta": EntityMap(path="travel_authorisations", items_path="items", mapping={
            "ta_number": "Number|upper", "traveller": "Traveller|lower", "origin": "From", "destination": "To", "start": "Start|date",
            "end": "End|date", "purpose": "Purpose", "estimated_minor": "Estimate|major", "currency": "Currency|upper",
            "charge_code": "CostCode|upper", "status": "Status|upper", "approved_by": "ApprovedBy"}),
        "bill": EntityMap(path="open_bills", items_path="items", mapping={
            "bill_id": "Id", "vendor_id": "Vendor|lower", "invoice_no": "InvoiceNo|upper", "amount_minor": "Amount|major",
            "currency": "Currency|upper", "due_date": "DueDate|date", "po_number": "PO|upper", "description": "Memo"}),
        "receivable": EntityMap(path="receivables", items_path="items", mapping={
            "receivable_id": "Id", "customer_id": "Customer|lower", "invoice_no": "InvoiceNo|upper", "amount_minor": "Amount|major",
            "currency": "Currency|upper", "due_date": "DueDate|date"}),
        "budget_line": EntityMap(path="budget", items_path="items", mapping={
            "period": "Period", "line": "Line|lower", "amount_minor": "Amount|major", "auto_share_bp": "Auto|int", "alert_share_bp": "Alert|int"}),
        "tenant_book": EntityMap(path="books", items_path="items", mapping={
            "tenant": "Tenant|lower", "tenant_name": "TenantName", "book": "Book", "allowed_payees": "Payees|split", "approvers": "Approvers"}),
    }, push={k: PushMap(path=f"inbox/{k}") for k in PUSH})


ORDER = ("unit", "user", "project", "account", "price", "vendor", "customer", "pr", "po", "engagement", "ta", "fund_request", "bill", "receivable",
         "budget_line", "tenant_book")  # who must exist before whom: offices before their people, accounts and vendors before POs


class ConnectorStore:
    def __init__(self, path: str | None = None):
        self.path = Path(path) if path else data_path("VENTHORA_MASTER_DB", "data/venthora_master.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connect() as c:
            c.execute("CREATE TABLE IF NOT EXISTS connectors (connector_id TEXT PRIMARY KEY, record_json TEXT NOT NULL, secret TEXT,"
                      " updated_at TEXT NOT NULL, last_sync_at TEXT, last_report_json TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS connector_push (connector_id TEXT NOT NULL, item TEXT NOT NULL, record_id TEXT NOT NULL,"
                      " hash TEXT, sent_at TEXT, status INTEGER, error TEXT, PRIMARY KEY (connector_id, item, record_id))")
            cols = {r[1] for r in c.execute("PRAGMA table_info(connectors)")}
            if "last_push_json" not in cols:
                c.execute("ALTER TABLE connectors ADD COLUMN last_push_json TEXT")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, c: Connector) -> dict:
        old = self.get_raw(c.connector_id)
        secret = "" if c.auth.clear_secret else (c.auth.secret if c.auth.secret else (old or {}).get("_secret", ""))
        rec = c.model_dump()
        rec["auth"].pop("secret", None)
        rec["auth"].pop("clear_secret", None)
        with self._lock, self._connect() as con:
            con.execute("INSERT INTO connectors (connector_id, record_json, secret, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(connector_id)"
                        " DO UPDATE SET record_json = excluded.record_json, secret = excluded.secret, updated_at = excluded.updated_at",
                        (c.connector_id, json.dumps(rec), secret, dt.datetime.now(dt.UTC).isoformat(timespec="seconds")))
        return self.public(c.connector_id)

    def get_raw(self, cid: str) -> dict | None:
        with self._connect() as con:
            r = con.execute("SELECT * FROM connectors WHERE connector_id = ?", (cid,)).fetchone()
        if r is None:
            return None
        return json.loads(r["record_json"]) | {"_secret": r["secret"] or "", "_last_sync_at": r["last_sync_at"],
                                                "_last_report": json.loads(r["last_report_json"]) if r["last_report_json"] else None,
                                                "_last_push": json.loads(r["last_push_json"]) if r["last_push_json"] else None}

    def public(self, cid: str) -> dict | None:
        raw = self.get_raw(cid)
        if raw is None:
            return None
        s = raw.pop("_secret")
        raw["auth"]["secret_set"] = bool(s)
        raw["auth"]["secret_hint"] = f"…{s[-4:]}" if len(s) >= 8 else ("set" if s else None)
        raw["last_sync_at"], raw["last_report"], raw["last_push"] = raw.pop("_last_sync_at"), raw.pop("_last_report"), raw.pop("_last_push")
        raw.setdefault("push", {})
        return raw

    def list(self) -> list[dict]:
        with self._connect() as con:
            ids = [r[0] for r in con.execute("SELECT connector_id FROM connectors ORDER BY connector_id")]
        return [self.public(i) for i in ids]

    def delete(self, cid: str) -> bool:
        with self._lock, self._connect() as con:
            con.execute("DELETE FROM connector_push WHERE connector_id = ?", (cid,))
            return con.execute("DELETE FROM connectors WHERE connector_id = ?", (cid,)).rowcount == 1

    # -- what was sent back ----------------------------------------------------------------------------------------
    def sent_hashes(self, cid: str, item: str) -> dict[str, str]:
        with self._connect() as con:
            return {r["record_id"]: r["hash"] for r in con.execute(
                "SELECT record_id, hash FROM connector_push WHERE connector_id = ? AND item = ? AND error IS NULL", (cid, item))}

    def record_push(self, cid: str, item: str, record_id: str, digest: str | None, status: int | None, error: str | None) -> None:
        """A success stores the content hash; a failure keeps the last good hash, so the record is sent again."""
        now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        with self._lock, self._connect() as con:
            if error is None:
                con.execute("INSERT INTO connector_push VALUES (?,?,?,?,?,?,NULL) ON CONFLICT(connector_id, item, record_id) DO UPDATE SET"
                            " hash = excluded.hash, sent_at = excluded.sent_at, status = excluded.status, error = NULL",
                            (cid, item, record_id, digest, now, status))
            else:
                con.execute("INSERT INTO connector_push VALUES (?,?,?,NULL,?,?,?) ON CONFLICT(connector_id, item, record_id) DO UPDATE SET"
                            " sent_at = excluded.sent_at, status = excluded.status, error = excluded.error",
                            (cid, item, record_id, now, status, error[:300]))

    def record_push_report(self, cid: str, report: dict) -> None:
        with self._lock, self._connect() as con:
            con.execute("UPDATE connectors SET last_push_json = ? WHERE connector_id = ?", (json.dumps(report, default=str), cid))

    def record_sync(self, cid: str, report: dict) -> None:
        with self._lock, self._connect() as con:
            con.execute("UPDATE connectors SET last_sync_at = ?, last_report_json = ? WHERE connector_id = ?",
                        (dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), json.dumps(report, default=str), cid))


# -- mapping ------------------------------------------------------------------------------------------------------------
def dig(obj, path: str):
    if not path:
        return obj
    for part in path.split("."):
        if isinstance(obj, list) and part.isdigit():
            obj = obj[int(part)] if int(part) < len(obj) else None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
        if obj is None:
            return None
    return obj


def _transform(v, t: str):
    if t.startswith("default:"):  # "default:IDR": the value when the source leaves the field empty
        return t[8:] if v is None or (isinstance(v, str) and not v.strip()) else v
    if v is None or (isinstance(v, str) and not v.strip()):  # an empty field is no value, not a bad one
        return None
    if t == "alnum":  # "PROJ-0001" -> "PROJ0001": a code VENTHORA keeps as letters and digits only
        return re.sub(r"[^A-Za-z0-9]+", "", str(v))
    if t == "major":
        try:
            return int((Decimal(str(v).replace(",", "").strip()) * 100).to_integral_value())
        except InvalidOperation as exc:
            raise ValueError(f"not an amount: {v!r}") from exc
    if t == "int":
        return int(str(v).strip())
    if t == "upper":
        return str(v).upper()
    if t == "lower":
        return str(v).lower()
    if t == "nospace":
        return re.sub(r"\s+", "", str(v))
    if t == "date":
        s = str(v).strip()[:10]
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%d-%m-%Y"):
            try:
                return dt.datetime.strptime(s, fmt).date().isoformat()
            except ValueError:
                continue
        raise ValueError(f"not a date: {v!r}")
    if t == "split":
        return [x.strip() for x in re.split(r"[;,]", v) if x.strip()] if isinstance(v, str) else list(v)
    raise ValueError(f"unknown transform {t!r}")


def _set(out: dict, path: str, value) -> None:
    parts = path.split(".")
    cur = out
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = value


def map_item(item: dict, mapping: dict[str, str]) -> dict:
    out: dict = {}
    for target, spec in mapping.items():
        if spec.startswith("="):
            value = spec[1:]
        else:
            src, *ts = spec.split("|")
            value = dig(item, src.strip())
            for t in ts:
                value = _transform(value, t.strip())
        if value is not None and value != "":
            _set(out, target, value)
    return out


def headers_for(raw: dict) -> dict[str, str]:
    a, secret = raw["auth"], raw.get("_secret", "")
    if a["type"] == "bearer" and secret:
        return {"Authorization": f"Bearer {secret}"}
    if a["type"] == "basic":
        return {"Authorization": "Basic " + base64.b64encode(f"{a.get('username', '')}:{secret}".encode()).decode()}
    if a["type"] == "header" and secret:
        return {a.get("header_name") or "X-API-Key": secret}
    return {}


async def fetch(raw: dict, entity: str, client=None) -> tuple[list[dict], list[str | None]]:
    """The entity's rows from the old system, mapped to VENTHORA's fields, with the source ids."""
    import httpx

    from . import net_guard

    em = raw["entities"][entity]
    own = client is None
    if own:
        net_guard.check_url(raw["base_url"])
    client = client or httpx.AsyncClient(timeout=30)
    try:
        res = await client.get(raw["base_url"] + "/" + em["path"].lstrip("/"), headers=headers_for(raw))
        res.raise_for_status()
        body = res.json()
    finally:
        if own:
            await client.aclose()
    items = dig(body, em.get("items_path") or "")
    if not isinstance(items, list):
        raise ValueError(f"{entity}: the reply has no list at {em.get('items_path') or 'the top level'}")
    rows, ids = [], []
    for it in items:
        try:
            rows.append(map_item(it, em["mapping"]))
        except ValueError as exc:
            rows.append({"_error": str(exc)})
        own_id = dig(it, em["id_path"]) if em.get("id_path") else None
        ids.append(str(own_id or it.get("id") or it.get("Id") or it.get("ID") or "") or None)
    return rows, ids


def digest(record: dict) -> str:
    return hashlib.sha256(json.dumps(record, sort_keys=True, default=str).encode()).hexdigest()


async def send(raw: dict, item: str, records: list[tuple[str, dict]], store: ConnectorStore, dry_run: bool = False,
               client=None, limit: int = 200) -> dict:
    """Write the new or changed records of one item to the other application; the unchanged ones are skipped."""
    import httpx

    pm = raw["push"][item]
    sent = store.sent_hashes(raw["connector_id"], item)
    todo = [(rid, rec, digest(rec)) for rid, rec in records if sent.get(rid) != digest(rec)]
    report = {"records": len(records), "unchanged": len(records) - len(todo), "sent": 0, "failed": 0, "errors": []}
    if dry_run:
        return report | {"would_send": len(todo), "sample": [rec for _, rec, _ in todo[:2]]}
    from . import net_guard

    own = client is None
    if own:
        net_guard.check_url(raw["base_url"])
    client = client or httpx.AsyncClient(timeout=30)
    url = raw["base_url"] + "/" + pm["path"].lstrip("/")
    try:
        for rid, rec, h in todo[:limit]:
            headers = headers_for(raw) | {"Idempotency-Key": f"venthora-{item}-{rid}-{h[:16]}", "content-type": "application/json"}
            try:
                res = await client.request(pm.get("method", "POST"), url, headers=headers, json={"type": item, "id": rid, "data": rec})
                if res.status_code < 300:
                    store.record_push(raw["connector_id"], item, rid, h, res.status_code, None)
                    report["sent"] += 1
                    continue
                error, status = f"HTTP {res.status_code}: {res.text[:160]}", res.status_code
            except httpx.HTTPError as exc:
                error, status = f"{type(exc).__name__}: {str(exc)[:160]}", None
            store.record_push(raw["connector_id"], item, rid, None, status, error)
            report["failed"] += 1
            if len(report["errors"]) < 3:
                report["errors"].append(f"{rid}: {error}")
    finally:
        if own:
            await client.aclose()
    report["left"] = max(0, len(todo) - limit)
    return report
