# ruff: noqa: E501
"""Upload dataset: any file that holds VENTHORA's data, when the old application has no API.

  parse      CSV / TSV / TXT (delimiter sniffed), Excel (the first sheet with data), JSON (a list of records), Word
             .docx (its first table, else its text), HTML (its first table, else its text), e-mail .eml (its body),
             PDF (its text layer), and any other text file as text
  detect     entity=auto: the columns are scored against every entity's field names and synonyms; when that is not
             clear (or the file is a document without columns) the configured AI model picks the entity
  map        each VENTHORA field is matched to a column by name and synonyms ("Vendor Name", "Supplier", "Kreditor"...);
             the configured AI model may propose the rest, and only a column that exists can be proposed
  clean      by the field's kind: amounts to minor units, dates to YYYY-MM-DD, currencies upper-case, IBANs without
             spaces, e-mail lists split
  PDF rows   a report or statement has no columns: the AI model reads its rows, and a value is kept only when it
             occurs in the PDF's own text (the same rule as the invoice reader); without a model, table-like lines
             (two or more spaces between cells) are split into columns
  preview    the full pipeline as a dry run (ingestion.apply): what would be created, updated, rejected and why
  commit     the same rows written, with the same side effects as a sync (vendors NEW / CHANGED, bills into the queue)
"""
from __future__ import annotations

import csv
import io
import json
import re

from .connectors import _transform

# VENTHORA field -> (kind, synonyms); kind decides the cleaning
FIELDS: dict[str, dict[str, tuple[str, tuple[str, ...]]]] = {
    "vendor": {
        "vendor_id": ("text", ("vendor id", "vendor code", "supplier id", "supplier code", "kreditor", "creditor id", "code")),
        "name": ("text", ("vendor name", "supplier name", "company", "company name", "name", "supplier", "vendor", "payee")),
        "currency": ("ccy", ("currency", "ccy", "cur", "waehrung")),
        "payout_method": ("upper", ("payout method", "payment method", "pay method", "method")),
        "contacts": ("list", ("email", "emails", "e-mail", "invoice email", "billing email", "contact email")),
        "registered_phone": ("text", ("phone", "telephone", "phone number", "registered phone", "tel")),
        "budget_line": ("lower", ("budget line", "category", "expense category")),
        "bank.account_name": ("text", ("account name", "account holder", "beneficiary name", "holder")),
        "bank.account_number": ("nospace", ("account number", "account no", "acct no", "bank account", "account")),
        "bank.iban": ("iban", ("iban",)),
        "bank.swift_code": ("upper", ("swift", "bic", "swift code", "swift/bic")),
        "bank.routing_type": ("lower", ("routing type",)),
        "bank.routing_value": ("nospace", ("routing number", "aba", "sort code", "routing", "bsb")),
        "bank.bank_name": ("text", ("bank name", "bank")),
        "bank.bank_country_code": ("upper", ("bank country", "bank country code")),
        "bank.account_currency": ("ccy", ("account currency",)),
        "address.city": ("text", ("city", "town")), "address.country_code": ("upper", ("country", "country code")),
        "address.street_address": ("text", ("street", "street address", "address line 1", "address")),
        "address.state": ("text", ("state", "province", "region")),
        "address.postcode": ("text", ("postcode", "postal code", "zip", "zip code")),
        "bank.bank_account_category": ("text", ("account type", "account category", "checking or savings")),
        "entity_type": ("upper", ("entity type", "legal type", "company or person")),
        "default_priority": ("int", ("priority", "payment priority")),
        "kind": ("lower", ("payee type", "vendor or consultant")),
        "engagement": ("lower", ("engagement", "engagement type")),
        "tax_id": ("text", ("tax id", "tin", "ein", "vat number", "npwp", "tax number")),
        "transfer_reason": ("lower", ("transfer reason", "payment reason")),
    },
    "customer": {
        "customer_id": ("text", ("customer id", "customer code", "debtor id", "client id", "code")),
        "name": ("text", ("customer name", "customer", "client", "company", "name")),
        "currency": ("ccy", ("currency", "ccy")),
        "contacts": ("list", ("email", "emails", "e-mail", "ap email")),
        "promises_kept": ("int", ("kept", "paid on time", "on time")),
        "promises_missed": ("int", ("missed", "late", "paid late")),
    },
    "unit": {
        "unit_id": ("lower", ("unit id", "office id", "branch id", "unit code", "office code")),
        "name": ("text", ("unit name", "office name", "branch name", "office", "branch", "name")),
        "kind": ("text", ("unit type", "office type", "type", "kind", "level")),
        "parent_id": ("lower", ("parent", "parent id", "parent unit", "reports to", "under")),
        "country": ("upper", ("country", "country code")),
        "currency": ("ccy", ("currency", "ccy")),
        "timezone": ("text", ("timezone", "time zone", "tz")),
        "cost_centre": ("upper", ("cost centre", "cost center", "cc", "office cost centre")),
        "payment_limit_minor": ("amount", ("payment limit", "auto pay limit", "limit")),
    },
    "user": {
        "name": ("text", ("full name", "employee name", "staff name", "name")),
        "email": ("lower", ("email", "e-mail", "mail")),
        "role": ("text", ("system role", "access role", "venthora role", "role")),
        "unit_id": ("lower", ("unit id", "unit", "office", "branch", "department", "cost centre", "cost center")),
        "approval_limit_minor": ("amount", ("approval limit", "authority", "signing limit", "limit")),
        "employee_no": ("text", ("employee number", "employee no", "staff number", "staff id", "employee id", "nik", "personnel number")),
        "job_title": ("text", ("job title", "position", "title", "jabatan")),
        "employment_type": ("lower", ("employment type", "contract type", "employment status")),
        "contract_start": ("date", ("contract start", "start date", "hire date", "joined", "date joined")),
        "contract_end": ("date", ("contract end", "end date", "expiry", "contract expiry")),
        "salary_minor": ("amount", ("net pay", "net salary", "monthly net pay", "salary", "monthly salary", "gaji", "take home pay")),
        "salary_currency": ("ccy", ("salary currency", "pay currency", "currency")),
        "charge_code": ("upper", ("charge code", "cost code", "account code", "salary charge code")),
        "benefits": ("list", ("benefits", "enrolled benefits", "benefit")),
        "payee_id": ("lower", ("payee id", "bank account id", "beneficiary id")),
    },
    "project": {
        "code": ("upper", ("project code", "project no", "project number", "code")),
        "name": ("text", ("project name", "project", "name")),
        "unit_id": ("lower", ("unit id", "unit", "office", "owner")),
        "active": ("bool", ("active", "open", "status active")),
    },
    "account": {
        "code": ("text", ("account code", "gl code", "gl account", "account no", "account number", "coa", "code")),
        "name": ("text", ("account name", "description", "name")),
        "category": ("text", ("expense category", "category", "group", "class")),
    },
    "price": {
        "item": ("text", ("item", "item name", "product", "service", "description")),
        "category": ("text", ("category", "group")),
        "unit": ("text", ("unit of measure", "uom", "per", "unit")),
        "price_minor": ("amount", ("unit price", "price", "rate", "harga satuan", "harga")),
        "currency": ("ccy", ("currency", "ccy")),
        "unit_id": ("lower", ("office", "branch", "unit id", "applies to")),
        "valid_from": ("date", ("valid from", "effective", "effective date", "start date")),
    },
    "travel_rate": {
        "country": ("text", ("destination country", "country", "negara")),
        "city": ("text", ("destination city", "city", "location", "kota", "destination")),
        "currency": ("ccy", ("currency", "ccy")),
        "lodging_minor": ("amount", ("lodging", "lodging rate", "hotel", "accommodation", "max lodging", "penginapan")),
        "per_diem_minor": ("amount", ("per diem", "m&ie", "mie", "meals and incidentals", "daily allowance", "uang harian")),
        "valid_from": ("date", ("valid from", "effective", "effective date", "start date")),
    },
    "po": {
        "po_id": ("upper", ("po number", "po no", "po", "purchase order", "order number")),
        "supplier_id": ("lower", ("vendor id", "vendor code", "supplier id", "supplier", "vendor")),
        "currency": ("ccy", ("currency", "ccy")),
        "total_minor": ("amount", ("total", "po total", "amount", "value", "po value")),
        "account_code": ("upper", ("account code", "cost code", "gl code", "charge code", "cost centre", "cost center")),
        "description": ("text", ("description", "memo", "item", "details")),
        "allow_partial": ("bool", ("partial", "allow partial", "partial allowed")),
        "start_date": ("date", ("active date", "start date", "valid from", "contract start", "effective date")),
        "expiry_date": ("date", ("expiry date", "expiry", "expires", "end date", "valid to", "contract end")),
        "recurring": ("lower", ("recurring", "billing", "billing frequency", "frequency", "multi payment")),
        "loe": ("text", ("loe", "level of effort", "days", "hours", "effort")),
        "loe_unit": ("lower", ("loe unit", "effort unit", "unit of effort")),
        "rate_minor": ("amount", ("rate", "daily rate", "hourly rate", "fee rate", "unit rate")),
    },
    "pr": {
        "pr_number": ("upper", ("pr number", "pr no", "purchase request", "request number", "requisition", "requisition number")),
        "title": ("text", ("title", "item", "description", "what", "subject")),
        "requested_by": ("text", ("requested by", "requester", "requestor", "employee")),
        "supplier_id": ("lower", ("supplier id", "vendor id", "supplier", "vendor")),
        "currency": ("ccy", ("currency", "ccy")),
        "estimated_minor": ("amount", ("estimate", "estimated amount", "estimated", "amount", "total", "budget")),
        "charge_code": ("upper", ("charge code", "account code", "cost code", "gl code")),
        "needed_by": ("date", ("needed by", "required by", "need date", "delivery date")),
        "requested_date": ("date", ("request date", "requested date", "date requested", "date", "created")),
        "justification": ("text", ("justification", "reason", "why", "notes")),
        "status": ("upper", ("status", "state", "approval status")),
        "decided_by": ("text", ("approved by", "approver", "decided by")),
    },
    "fund_request": {
        "request_number": ("upper", ("request number", "advance number", "voucher", "voucher number", "reference", "number")),
        "kind": ("lower", ("type", "kind", "category", "request type")),
        "requested_by": ("text", ("requested by", "employee", "staff", "custodian", "person", "email")),
        "payee_id": ("lower", ("payee id", "payee", "vendor id")),
        "amount_minor": ("amount", ("amount", "advance", "float", "total", "value")),
        "currency": ("ccy", ("currency", "ccy")),
        "charge_code": ("upper", ("charge code", "account code", "cost code", "gl code")),
        "purpose": ("text", ("purpose", "description", "reason", "what for")),
        "activity": ("text", ("activity", "programme activity", "project activity")),
        "request_date": ("date", ("request date", "date requested", "date", "created")),
        "needed_by": ("date", ("needed by", "required by", "need date")),
        "settle_by": ("date", ("settle by", "settlement date", "liquidate by", "due for settlement")),
        "status": ("upper", ("status", "state")),
        "decided_by": ("text", ("approved by", "approver")),
    },
    "ta": {
        "ta_number": ("upper", ("ta number", "travel authorisation", "travel authorization", "authorisation number", "authorization number", "ta no")),
        "traveller": ("text", ("traveller", "traveler", "employee", "staff", "name", "email")),
        "origin": ("text", ("origin", "from", "departure")),
        "destination": ("text", ("destination", "to", "arrival")),
        "start": ("date", ("start", "start date", "departure date", "from date", "travel date")),
        "end": ("date", ("end", "end date", "return date", "to date")),
        "purpose": ("text", ("purpose", "reason", "description")),
        "estimated_minor": ("amount", ("estimate", "estimated cost", "estimated amount", "amount", "budget")),
        "currency": ("ccy", ("currency", "ccy")),
        "charge_code": ("upper", ("charge code", "account code", "cost code")),
        "status": ("upper", ("status", "state")),
        "approved_by": ("text", ("approved by", "approver")),
    },
    "engagement": {
        "engagement_id": ("upper", ("engagement number", "engagement no", "engagement id", "reference")),
        "name": ("text", ("full name", "name", "speaker", "specialist", "person")),
        "role": ("lower", ("role", "type", "engagement type", "capacity")),
        "organisation": ("text", ("organisation", "organization", "affiliation", "company", "firm")),
        "email": ("text", ("email", "e-mail", "email address")),
        "phone": ("text", ("phone", "mobile", "telephone")),
        "tax_id": ("text", ("tax id", "tax number", "npwp", "tin", "vat number")),
        "payee_id": ("lower", ("payee id", "payee", "vendor id", "beneficiary id")),
        "event": ("text", ("event", "task", "matter", "session", "activity")),
        "event_date": ("date", ("event date", "date", "session date", "delivery date")),
        "fee_minor": ("amount", ("fee", "honorarium", "amount", "fee amount", "total")),
        "currency": ("ccy", ("currency", "ccy")),
        "charge_code": ("upper", ("charge code", "account code", "cost code", "gl code")),
        "documents": ("list", ("documents", "documents on file", "attachments")),
        "note": ("text", ("note", "notes", "remarks")),
    },
    "bill": {
        "bill_id": ("text", ("bill id", "id", "document id", "doc no")),
        "vendor_id": ("lower", ("vendor id", "vendor code", "supplier id", "supplier", "vendor")),
        "invoice_no": ("upper", ("invoice number", "invoice no", "invoice", "inv no", "reference")),
        "amount_minor": ("amount", ("amount", "amount due", "total", "balance", "open amount")),
        "currency": ("ccy", ("currency", "ccy")),
        "due_date": ("date", ("due date", "due", "payment due")),
        "po_number": ("upper", ("po number", "po", "purchase order")),
        "description": ("text", ("description", "memo")),
    },
    "receivable": {
        "receivable_id": ("text", ("id", "document id")),
        "customer_id": ("lower", ("customer id", "customer code", "customer", "client")),
        "invoice_no": ("upper", ("invoice number", "invoice no", "invoice", "reference")),
        "amount_minor": ("amount", ("amount", "amount due", "balance", "open amount", "total")),
        "currency": ("ccy", ("currency", "ccy")),
        "due_date": ("date", ("due date", "due")),
    },
    "budget_line": {
        "period": ("text", ("period", "month")),
        "line": ("lower", ("line", "budget line", "category")),
        "amount_minor": ("amount", ("amount", "budget", "plan", "planned")),
        "auto_share_bp": ("int", ("auto share", "autonomous share")),
        "alert_share_bp": ("int", ("alert share", "alert")),
        "require_approval_on_alert": ("bool", ("approver at alert", "approval on alert")),
        "bypass": ("bool", ("bypass", "unlimited")),
        "bypass_approved_by": ("text", ("signed by", "bypass signed by", "signatory")),
    },
    "tenant_book": {
        "tenant": ("lower", ("tenant", "entity", "company")), "tenant_name": ("text", ("tenant name",)),
        "book": ("text", ("book", "book id")), "allowed_payees": ("list", ("allowed payees", "payees", "vendors")),
    },
    "allocation": {
        "basis": ("lower", ("basis", "allocation basis", "allocation type", "budget basis")),
        "period": ("text", ("budget period", "period", "month", "quarter", "fiscal year")),
        "unit_id": ("lower", ("unit id", "unit", "office", "branch", "cost centre", "cost center")),
        "include_children": ("bool", ("include children", "include branches", "include sub units")),
        "project_code": ("upper", ("project code", "project", "project no")),
        "line": ("text", ("budget line", "line", "expense category", "category")),
        "amount_minor": ("amount", ("allocated amount", "allocation", "allocated", "budget amount", "amount", "budget")),
        "no_limit": ("bool", ("no limit", "unlimited")),
        "currency": ("ccy", ("currency", "ccy")),
    },
}

# entity -> (label, what it is for, the dashboard page its records show on)
ENTITIES: dict[str, tuple[str, str, str]] = {
    "unit": ("Offices, branches and units", "the organisation tree, local currency, time zone, cost centre and payment limit", "#/offices"),
    "user": ("People and approval limits", "who works where, their role and how much they may approve", "#/users"),
    "project": ("Projects", "the project part of every charge code", "#/codes"),
    "account": ("Chart of accounts", "the GL account every payment is booked to", "#/codes"),
    "price": ("Unit prices", "agreed prices invoice lines are checked against", "#/prices"),
    "travel_rate": ("Travel rates", "lodging and per diem per destination city, for travel and transport", "#/mandate"),
    "vendor": ("Vendors and bank details", "who can be paid, to which account", "#/payees"),
    "customer": ("Customers", "who pays the company, to match money that arrives", "#/sources-sync"),
    "po": ("Purchase orders and contracts", "supplier invoices are paid only against an open PO; active and expiry dates, Multi Payment, a consultant's LoE and rate", "#/intake?po"),
    "pr": ("Purchase requests", "what people ask to buy before there is a PO, with the estimate and the charge code", "#/intake?pr"),
    "fund_request": ("Cash advances, petty cash and ODC", "advances to staff, petty cash floats and other direct costs, with the person and the charge code", "#/intake?ca"),
    "ta": ("Travel authorisations", "approved trips: traveller, route, dates, purpose and estimated cost", "#/intake?travel"),
    "engagement": ("One-off engagements", "consultants for a single task, legal specialists, resource persons and keynote speakers, with the fee and the event", "#/intake?con"),
    "bill": ("Open bills (AP)", "supplier invoices due; they enter the payment queue", "#/pending"),
    "receivable": ("Receivables (AR)", "customer invoices still open, for the cash forecast", "#/sources-sync"),
    "budget_line": ("Budget lines", "the plan per budget line and period", "#/budget"),
    "allocation": ("Budget allocations", "budget given to an office, branch or project for a period", "#/allocation"),
    "tenant_book": ("Tenant books", "which entity a payment belongs to, its payees and approvers", "#/mandate"),
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


IMAGE_ENDINGS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic")


def parse(data: bytes, filename: str) -> dict:
    """{'kind', 'headers', 'rows', 'text'}: rows are dicts keyed by header; a document without columns has only text."""
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm")):
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        for ws in wb.worksheets:  # the first sheet that holds anything
            grid = [[("" if c is None else (c.isoformat()[:10] if hasattr(c, "isoformat") else str(c))).strip() for c in r]
                    for r in ws.iter_rows(values_only=True)]
            grid = [r for r in grid if any(r)]
            if grid:
                return _table("xlsx", grid) | {"sheet": ws.title}
        return _table("xlsx", [])
    if name.endswith((".xls", ".ods", ".numbers")):
        raise ValueError("this spreadsheet format cannot be read here: save it as .xlsx or .csv and upload again")
    if name.endswith(IMAGE_ENDINGS):
        raise ValueError("a photo or scan has no text to read: upload the PDF, Excel or CSV it came from, or a PDF with a text layer")
    if name.endswith(".json"):
        return _json_table(data)
    if name.endswith(".docx"):
        return _docx(data)
    if name.endswith((".html", ".htm")):
        return _html(data.decode("utf-8", errors="replace"))
    if name.endswith(".eml"):
        return _eml(data)
    if name.endswith(".pdf"):
        from .intake.documents import pdf_text

        text, pages, problem = pdf_text(data)
        if problem:
            raise ValueError(f"the PDF could not be read: {problem}")
        # columns survive only in layout mode: the plain text joins cells with single spaces
        try:
            from pypdf import PdfReader

            layout = "\n".join(p.extract_text(extraction_mode="layout") or "" for p in PdfReader(io.BytesIO(data)).pages[:20])
        except Exception:  # noqa: BLE001 - an older pypdf or an odd page: fall back to the plain text
            layout = text
        lines = [ln for ln in layout.splitlines() if ln.strip()]
        grid = [re.split(r"\s{2,}|\t", ln.strip()) for ln in lines]
        table = [r for r in grid if len(r) >= 3]
        out = _table("pdf", table) if len(table) >= 2 else {"kind": "pdf", "headers": [], "rows": []}
        return out | {"text": text, "pages": pages}
    if data[:4] == b"PK\x03\x04":  # a zip that is not .xlsx or .docx
        raise ValueError("this file is not text, a spreadsheet, a PDF or a Word document VENTHORA can read")
    raw = data.decode("utf-8-sig", errors="replace") if b"\x00" not in data[:200] else data.decode("utf-16", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(raw[:4000], delimiters=",;\t|")
        delim = dialect.delimiter
    except csv.Error:
        delim = None
    if delim is None and not name.endswith((".csv", ".tsv")):
        return {"kind": "text", "headers": [], "rows": [], "text": raw}  # prose, a statement or a list: the model reads it
    grid = [[c.strip() for c in r] for r in csv.reader(io.StringIO(raw), delimiter=delim or ",")]
    return _table("csv", [r for r in grid if any(r)]) | {"text": raw[:20000]}


def _json_table(data: bytes) -> dict:
    try:
        doc = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"the JSON could not be read: {exc}") from exc
    if isinstance(doc, dict):  # {"vendors": [...]} or {"data": [...]}: the first list of records
        doc = next((v for v in doc.values() if isinstance(v, list) and v and isinstance(v[0], dict)), [doc])
    recs = [r for r in doc if isinstance(r, dict)] if isinstance(doc, list) else []
    if not recs:
        raise ValueError("the JSON holds no list of records")
    flat = []
    for r in recs[:5000]:
        row: dict[str, str] = {}
        for k, v in r.items():
            if isinstance(v, dict):  # {"bank": {"iban": ...}} -> "bank.iban"
                row |= {f"{k}.{k2}": "" if v2 is None else str(v2) for k2, v2 in v.items() if not isinstance(v2, (dict, list))}
            else:
                row[k] = ", ".join(map(str, v)) if isinstance(v, list) else ("" if v is None else str(v))
        flat.append(row)
    headers = list(dict.fromkeys(k for r in flat for k in r))
    return {"kind": "json", "headers": headers, "rows": [{h: r.get(h, "") for h in headers} for r in flat],
            "truncated": len(recs) > 5000}


def _docx(data: bytes) -> dict:
    import zipfile
    from xml.etree import ElementTree as ET

    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        root = ET.fromstring(zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise ValueError("the Word file could not be read; save it as .docx or PDF") from exc
    cell = lambda el: " ".join("".join(t.text or "" for t in p.iter(f"{w}t")) for p in el.iter(f"{w}p")).strip()  # noqa: E731
    text = "\n".join(cell(p) for p in root.iter(f"{w}p"))
    for tbl in root.iter(f"{w}tbl"):
        grid = [[cell(tc) for tc in tr.iter(f"{w}tc")] for tr in tbl.iter(f"{w}tr")]
        grid = [r for r in grid if any(r)]
        if len(grid) >= 2:
            return _table("docx", grid) | {"text": text}
    return {"kind": "docx", "headers": [], "rows": [], "text": text}


def _html(raw: str) -> dict:
    from html.parser import HTMLParser

    class Tables(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tables, self.row, self.cell, self.text = [], None, None, []

        def handle_starttag(self, tag, attrs):
            if tag == "table":
                self.tables.append([])
            elif tag == "tr" and self.tables:
                self.row = []
            elif tag in ("td", "th") and self.row is not None:
                self.cell = []

        def handle_endtag(self, tag):
            if tag in ("td", "th") and self.cell is not None and self.row is not None:
                self.row.append(" ".join("".join(self.cell).split()))
                self.cell = None
            elif tag == "tr" and self.row is not None and self.tables:
                self.tables[-1].append(self.row)
                self.row = None

        def handle_data(self, d):
            if self.cell is not None:
                self.cell.append(d)
            self.text.append(d)

    t = Tables()
    t.feed(raw)
    text = re.sub(r"\n\s*\n+", "\n", "".join(t.text)).strip()
    grid = next(([r for r in g if any(r)] for g in t.tables if len([r for r in g if any(r)]) >= 2), None)
    return (_table("html", grid) if grid else {"kind": "html", "headers": [], "rows": []}) | {"text": text}


def _eml(data: bytes) -> dict:
    from email import policy
    from email.parser import BytesParser

    msg = BytesParser(policy=policy.default).parsebytes(data)
    body = msg.get_body(preferencelist=("plain", "html"))
    text = body.get_content() if body else ""
    if body is not None and body.get_content_type() == "text/html":
        return _html(text) | {"kind": "eml"}
    return {"kind": "eml", "headers": [], "rows": [], "text": f"Subject: {msg.get('subject', '')}\n{text}"}


def _table(kind: str, grid: list[list[str]]) -> dict:
    if not grid:
        return {"kind": kind, "headers": [], "rows": []}
    headers = [h or f"column {i + 1}" for i, h in enumerate(grid[0])]
    rows = [{headers[i]: (r[i] if i < len(r) else "") for i in range(len(headers))} for r in grid[1:]]
    return {"kind": kind, "headers": headers, "rows": rows[:5000], "truncated": len(rows) > 5000}


def detect(headers: list[str]) -> list[tuple[str, float]]:
    """Every entity scored by how many of its fields the columns name; its key field (the first) counts double."""
    scores = []
    for e, fields in FIELDS.items():
        hit = suggest(e, headers)
        if hit:
            key = next(iter(fields))
            scores.append((e, len(hit) + (1 if key in hit else 0) + len(hit) / len(fields)))
    return sorted(scores, key=lambda x: -x[1])


async def detect_with_model(headers: list[str], sample: list[dict], text: str, model) -> str | None:
    """The entity a model says the file holds, or None; it may answer only one of VENTHORA's entities."""
    from .evidence.llm import LLMUnavailable, first_json_object

    if not model.configured:
        return None
    kinds = {e: f"{label}: {why}" for e, (label, why, _) in ENTITIES.items()}
    prompt = ("Decide which kind of business records a file holds. Reply with one JSON object {\"entity\": key}, where key is "
              "one of the kinds given, or {\"entity\": null} when none fits.")
    doc = json.dumps({"kinds": kinds, "columns": headers[:40], "sample_rows": sample[:3], "text": text[:3000]}, default=str)[:6000]
    try:
        reply = first_json_object(await model.complete([{"role": "system", "content": prompt}, {"role": "user", "content": doc}], max_tokens=60))
    except (LLMUnavailable, ValueError):
        return None
    e = reply.get("entity")
    return e if e in FIELDS else None


def suggest(entity: str, headers: list[str]) -> dict[str, str]:
    """VENTHORA field -> column, by exact name, then synonyms (longest first), each column used once."""
    by_norm = {_norm(h): h for h in headers}
    used: set[str] = set()
    out: dict[str, str] = {}
    cands = {f: [_norm(f.split(".")[-1]), *sorted((_norm(s) for s in syns), key=len, reverse=True)] for f, (_, syns) in FIELDS[entity].items()}
    # exact names first for every field, so a short synonym cannot take a column another field names exactly
    # (seen: "bank" took "Bank Country" before the bank country field was looked at)
    for field, cs in cands.items():
        hit = next((by_norm[c] for c in cs if c in by_norm and by_norm[c] not in used), None)
        if hit:
            out[field] = hit
            used.add(hit)
    for field, cs in cands.items():  # then a synonym contained in a longer header ("Vendor Name (legal)")
        if field in out:
            continue
        hit = next((h for c in cs for n, h in by_norm.items() if len(c) > 3 and c in n and h not in used), None)
        if hit:
            out[field] = hit
            used.add(hit)
    return out


async def suggest_with_model(entity: str, headers: list[str], sample: list[dict], have: dict[str, str], model) -> dict[str, str]:
    """Ask the model for the fields the names did not settle; keep only real columns and real fields."""
    from .evidence.llm import LLMUnavailable, first_json_object

    missing = [f for f in FIELDS[entity] if f not in have]
    free = [h for h in headers if h not in have.values()]
    if not missing or not free or not model.configured:
        return {}
    prompt = ("Map spreadsheet columns to target fields. Reply with one JSON object {target_field: column_name}. "
              "Use only the column names given, each at most once; leave out a field no column fits.")
    doc = json.dumps({"target_fields": missing, "columns": free, "sample_rows": sample[:3]}, default=str)[:4000]
    try:
        reply = first_json_object(await model.complete([{"role": "system", "content": prompt}, {"role": "user", "content": doc}], max_tokens=300))
    except (LLMUnavailable, ValueError):
        return {}
    out, used = {}, set()
    for f, col in reply.items():
        if f in missing and isinstance(col, str) and col in free and col not in used:
            out[f] = col
            used.add(col)
    return out


async def pdf_rows_with_model(entity: str, text: str, model) -> list[dict]:
    """Rows a model reads from a document (a PDF report, a Word file, an e-mail, plain text); a value survives only
    when it occurs in the document's own text."""
    from .evidence.llm import LLMUnavailable, first_json_object

    if not model.configured:
        return []
    fields = list(FIELDS[entity])
    prompt = (f"Extract every {entity.replace('_', ' ')} row from the document. Reply with one JSON object {{\"rows\": [...]}} where each row "
              f"uses these keys when the document states them: {', '.join(fields)}. Copy values exactly as written; do not compute or guess.")
    try:
        reply = first_json_object(await model.complete([{"role": "system", "content": prompt}, {"role": "user", "content": text[:8000]}], max_tokens=1500))
    except (LLMUnavailable, ValueError):
        return []
    flat = re.sub(r"\s+", " ", text).lower()
    rows = []
    for r in reply.get("rows") or []:
        if isinstance(r, dict):
            kept = {k: v for k, v in r.items() if k in fields and v not in (None, "") and re.sub(r"\s+", " ", str(v)).lower() in flat}
            if kept:
                rows.append(kept)
    return rows


def clean(entity: str, rows: list[dict], mapping: dict[str, str]) -> list[dict]:
    """Rows in VENTHORA's fields; a cell that cannot be cleaned makes its row an error row (rejected with the reason)."""
    out = []
    for r in rows:
        rec: dict = {}
        try:
            for field, col in mapping.items():
                v = col[1:] if col.startswith("=") else r.get(col, "") if col in r else r.get(field, "")  # "=EUR": one value for every row
                if v in (None, ""):
                    continue
                kind = FIELDS[entity][field][0] if field in FIELDS[entity] else "text"
                if kind == "amount":
                    v = _transform(re.sub(r"[^\d.,\-]", "", str(v)), "major")
                elif kind == "date":
                    v = _transform(v, "date")
                elif kind in ("ccy", "upper"):
                    v = str(v).strip().upper()
                elif kind == "lower":
                    v = str(v).strip().lower()
                elif kind in ("nospace", "iban"):
                    v = re.sub(r"\s+", "", str(v)).upper() if kind == "iban" else re.sub(r"\s+", "", str(v))
                elif kind == "list":
                    v = _transform(v, "split")
                elif kind == "int":
                    v = int(float(str(v).replace(",", "")))
                elif kind == "bool":
                    v = str(v).strip().lower() in ("1", "true", "yes", "y", "ja", "ya")
                cur = rec
                parts = field.split(".")
                for p in parts[:-1]:
                    cur = cur.setdefault(p, {})
                cur[parts[-1]] = v
        except (ValueError, TypeError) as exc:
            rec = {"_error": str(exc)}
        out.append(rec)
    return out
