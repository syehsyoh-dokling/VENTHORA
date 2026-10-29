# ruff: noqa: E501
"""Upload dataset: master data from a file when the old application has no API (CSV, Excel, or a PDF report).

  parse      CSV (delimiter sniffed), Excel (first sheet, first non-empty row is the header), or PDF (its text layer)
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
    },
    "customer": {
        "customer_id": ("text", ("customer id", "customer code", "debtor id", "client id", "code")),
        "name": ("text", ("customer name", "customer", "client", "company", "name")),
        "currency": ("ccy", ("currency", "ccy")),
        "contacts": ("list", ("email", "emails", "e-mail", "ap email")),
        "promises_kept": ("int", ("kept", "paid on time", "on time")),
        "promises_missed": ("int", ("missed", "late", "paid late")),
    },
    "po": {
        "po_id": ("upper", ("po number", "po no", "po", "purchase order", "order number")),
        "supplier_id": ("lower", ("vendor id", "vendor code", "supplier id", "supplier", "vendor")),
        "currency": ("ccy", ("currency", "ccy")),
        "total_minor": ("amount", ("total", "po total", "amount", "value", "po value")),
        "account_code": ("upper", ("account code", "cost code", "gl code", "charge code", "cost centre", "cost center")),
        "description": ("text", ("description", "memo", "item", "details")),
        "allow_partial": ("bool", ("partial", "allow partial", "partial allowed")),
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
    },
    "tenant_book": {
        "tenant": ("lower", ("tenant", "entity", "company")), "tenant_name": ("text", ("tenant name",)),
        "book": ("text", ("book", "book id")), "allowed_payees": ("list", ("allowed payees", "payees", "vendors")),
    },
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def parse(data: bytes, filename: str) -> dict:
    """{'kind', 'headers', 'rows', 'text'}: rows are dicts keyed by header."""
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm")):
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        grid = [[("" if c is None else (c.isoformat()[:10] if hasattr(c, "isoformat") else str(c))).strip() for c in r]
                for r in ws.iter_rows(values_only=True)]
        grid = [r for r in grid if any(r)]
        return _table("xlsx", grid)
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
    raw = data.decode("utf-8-sig", errors="replace") if b"\x00" not in data[:200] else data.decode("utf-16", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(raw[:4000], delimiters=",;\t|")
        delim = dialect.delimiter
    except csv.Error:
        delim = ","
    grid = [[c.strip() for c in r] for r in csv.reader(io.StringIO(raw), delimiter=delim)]
    return _table("csv", [r for r in grid if any(r)])


def _table(kind: str, grid: list[list[str]]) -> dict:
    if not grid:
        return {"kind": kind, "headers": [], "rows": []}
    headers = [h or f"column {i + 1}" for i, h in enumerate(grid[0])]
    rows = [{headers[i]: (r[i] if i < len(r) else "") for i in range(len(headers))} for r in grid[1:]]
    return {"kind": kind, "headers": headers, "rows": rows[:5000], "truncated": len(rows) > 5000}


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
    """Rows a model reads from a PDF report; a value survives only when it occurs in the PDF's text."""
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
