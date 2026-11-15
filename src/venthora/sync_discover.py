# ruff: noqa: E501
"""Find where a finance application keeps each item and propose how its fields map to VENTHORA's.

One line connects the application (sync_catalog.parse_connection); this module does the rest a person would
otherwise type, and writes nothing: a person reviews the proposal and applies it (PUT the connector).

1. Where: the application's OpenAPI document (openapi.json, swagger.json, ...) names its paths; without one,
   VENTHORA tries the usual names with a GET (never a write). The first list of records found is the item.
2. Which fields: the leaf names of a sample record are matched against VENTHORA's fields and their usual synonyms
   ("CompanyName" -> name, "Bank.IBAN" -> bank.iban), with the transform the field needs (amounts to minor units,
   dates to YYYY-MM-DD, codes upper or lower case).
3. The AI model (Settings -> AI model), when one is set, fills or corrects what the names left open. It sees field
   names and masked example values only (no account numbers, no emails), and an answer that names a field the
   sample does not have is dropped.

Every proposed field says who proposed it (name match or AI) and shows the value it would produce from the
first sample record, so the person checks real data before applying.
"""
from __future__ import annotations

import json
import re

from .connectors import headers_for, map_item
from .sync_catalog import PULL, PUSH

OPENAPI_PATHS = ("openapi.json", "swagger.json", "v3/api-docs", "api-docs", "openapi.yaml")
LIST_KEYS = ("items", "data", "results", "value", "records", "rows", "list")

# where each item usually lives: path names to try (matched without case or separators)
PATH_WORDS: dict[str, tuple[str, ...]] = {
    "unit": ("units", "offices", "branches", "cost-centres", "cost-centers", "departments", "locations"),
    "user": ("users", "people", "employees", "staff", "members"),
    "project": ("projects", "jobs"),
    "account": ("accounts", "chart-of-accounts", "gl-accounts", "ledger-accounts"),
    "price": ("prices", "price-list", "products", "catalog"),
    "vendor": ("vendors", "suppliers", "creditors", "payees", "contacts"),
    "customer": ("customers", "clients", "debtors"),
    "po": ("purchase-orders", "purchase_orders", "purchaseorders", "pos"),
    "bill": ("bills", "open-bills", "open_bills", "payables", "vendor-bills", "supplier-invoices", "invoices"),
    "receivable": ("receivables", "sales-invoices", "customer-invoices", "ar-invoices"),
    "budget_line": ("budget", "budgets", "budget-lines", "budget_lines"),
    "tenant_book": ("books", "tenants", "companies", "ledgers"),
    "pr": ("purchase-requests", "purchase_requests", "purchaserequests", "requisitions", "prs"),
    "engagement": ("engagements", "speakers", "resource-persons", "specialists"),
    "fund_request": ("fund-requests", "fund_requests", "cash-advances", "advances", "petty-cash", "imprest"),
    "ta": ("travel-authorisations", "travel_authorisations", "travel-authorizations", "travel-requests", "trips"),
}
PUSH_WORDS: dict[str, tuple[str, ...]] = {
    "payment": ("payments", "payment", "bill-payments", "vendor-payments"),
    "journal": ("journal-entries", "journals", "journal", "manual-journals"),
    "po_balance": ("po_balance", "purchase-order-balances", "balances"),
    "payee_status": ("payee_status", "vendor-status", "status"),
    "request_status": ("request_status", "bill-status", "status"),
    "budget_usage": ("budget_usage", "budget-usage", "usage"),
}

# VENTHORA field -> the usual names of the same thing (normalized); entity-specific entries win
SYN: dict[str, tuple[str, ...]] = {
    "name": ("name", "companyname", "displayname", "fullname", "legalname", "title"),
    "currency": ("currency", "ccy", "currencycode"),
    "country": ("country", "countrycode"),
    "email": ("email", "emailaddress", "mail"),
    "contacts": ("emails", "email", "contacts", "contactemails"),
    "registered_phone": ("phone", "telephone", "mobile", "phonenumber"),
    "payout_method": ("paymethod", "paymentmethod", "payoutmethod", "method"),
    "budget_line": ("category", "budgetline", "expensecategory"),
    "default_priority": ("priority",),
    "kind": ("type", "kind", "unittype"),
    "parent_id": ("parent", "parentid", "parentcode"),
    "timezone": ("timezone", "tz"),
    "cost_centre": ("costcentre", "costcenter"),
    "payment_limit_minor": ("paylimit", "paymentlimit"),
    "role": ("role", "jobrole"),
    "approval_limit_minor": ("approvallimit", "limit"),
    "unit_id": ("unit", "office", "unitid", "officeid", "department"),
    "code": ("code", "gl", "glcode", "accountcode", "number"),
    "category": ("category", "type", "class"),
    "item": ("item", "product", "description"),
    "unit": ("per", "unit", "uom"),
    "price_minor": ("price", "unitprice", "rate"),
    "promises_kept": ("kept", "promiseskept"),
    "promises_missed": ("missed", "promisesmissed"),
    "supplier_id": ("vendor", "supplier", "vendorid", "supplierid", "vendorcode"),
    "vendor_id": ("vendor", "vendorid", "vendorcode", "supplier", "supplierid"),
    "customer_id": ("customer", "customerid", "customercode", "client"),
    "total_minor": ("total", "amount", "totalamount"),
    "amount_minor": ("amount", "total", "amountdue", "balance"),
    "account_code": ("costcode", "accountcode", "chargecode", "glcode"),
    "description": ("memo", "description", "notes", "narration"),
    "allow_partial": ("partialallowed", "allowpartial", "partial"),
    "terms": ("terms", "milestones", "paymentterms"),
    "issued_date": ("issued", "issueddate", "orderdate", "podate"),
    "start_date": ("start", "startdate", "activedate", "validfrom"),
    "expiry_date": ("expiry", "expirydate", "enddate", "validto", "expires"),
    "loe": ("loe", "levelofeffort", "effort"),
    "loe_unit": ("loeunit", "effortunit"),
    "rate_minor": ("rate", "dailyrate", "hourlyrate", "feerate"),
    "recurring": ("billing", "recurring", "frequency"),
    "invoice_no": ("invoiceno", "invoicenumber", "invoice", "reference"),
    "due_date": ("duedate", "due", "dueon"),
    "po_number": ("po", "ponumber", "purchaseorder"),
    "period": ("period", "month"),
    "line": ("line", "budgetline", "category"),
    "auto_share_bp": ("auto", "autoshare"),
    "alert_share_bp": ("alert", "alertshare"),
    "tenant": ("tenant", "tenantid", "company"),
    "tenant_name": ("tenantname", "companyname"),
    "book": ("book", "bookid", "ledger"),
    "allowed_payees": ("payees", "allowedpayees"),
    "approvers": ("approvers",),
    "requested_by": ("requestedby", "requester", "requestor"),
    "estimated_minor": ("estimate", "estimated", "estimatedamount", "estimatedcost"),
    "needed_by": ("neededby", "requiredby"),
    "requested_date": ("requested", "requestdate", "requesteddate"),
    "justification": ("reason", "justification"),
    "status": ("status", "state"),
    "decided_by": ("approvedby", "decidedby", "approver"),
    "approved_by": ("approvedby", "approver"),
    "organisation": ("organisation", "organization", "affiliation"),
    "event": ("event", "session", "activity"),
    "event_date": ("date", "eventdate"),
    "fee_minor": ("fee", "honorarium"),
    "payee_id": ("payee", "payeeid"),
    "traveller": ("traveller", "traveler", "employee"),
    "origin": ("from", "origin", "departure"),
    "destination": ("to", "destination", "arrival"),
    "start": ("start", "startdate", "departuredate"),
    "end": ("end", "enddate", "returndate"),
    "purpose": ("purpose", "reason"),
    "activity": ("activity", "programmeactivity"),
    "request_date": ("requested", "requestdate", "date"),
    "settle_by": ("settleby", "settlementdate", "liquidateby"),
    # nested: matched only under a bank / address object
    "bank.account_name": ("holder", "accountname", "accountholder", "beneficiaryname", "name"),
    "bank.account_number": ("number", "accountnumber", "accountno"),
    "bank.iban": ("iban",),
    "bank.swift_code": ("swift", "swiftcode", "bic"),
    "bank.routing_type": ("routingtype",),
    "bank.routing_value": ("routing", "routingnumber", "sortcode", "bsb", "routingvalue"),
    "bank.bank_account_category": ("accounttype", "accountcategory", "checkingorsavings"),
    "bank.bank_name": ("bankname", "bank", "institution"),
    "bank.bank_country_code": ("country", "bankcountry", "countrycode"),
    "bank.account_currency": ("currency", "accountcurrency"),
    "address.street_address": ("street", "address1", "addressline1", "line1", "streetaddress"),
    "address.city": ("city", "town"),
    "address.state": ("state", "province", "region"),
    "address.postcode": ("zip", "postcode", "postalcode", "zipcode"),
    "address.country_code": ("country", "countrycode"),
}
ENTITY_SYN: dict[str, dict[str, tuple[str, ...]]] = {
    "unit": {"unit_id": ("code", "unitid", "id")},
    "vendor": {"vendor_id": ("code", "vendorid", "vendorcode", "supplierid", "id")},
    "customer": {"customer_id": ("code", "customerid", "customercode", "id")},
    "po": {"po_id": ("number", "ponumber", "poid", "id")},
    "bill": {"bill_id": ("id", "billid")},
    "receivable": {"receivable_id": ("id", "receivableid")},
    "project": {"code": ("code", "projectcode", "number")},
    "account": {"code": ("gl", "code", "glcode", "accountcode", "number")},
    "user": {"name": ("fullname", "name", "displayname")},
    "pr": {"pr_number": ("number", "prnumber", "requestnumber", "id"), "title": ("title", "subject", "item", "description"),
           "charge_code": ("costcode", "chargecode", "accountcode")},
    "engagement": {"engagement_id": ("number", "engagementnumber", "id"), "role": ("type", "role", "capacity"),
                   "charge_code": ("costcode", "chargecode", "accountcode")},
    "ta": {"ta_number": ("number", "tanumber", "authorisationnumber", "id"), "charge_code": ("costcode", "chargecode", "accountcode")},
    "fund_request": {"request_number": ("number", "requestnumber", "voucher", "id"), "charge_code": ("costcode", "chargecode", "accountcode"),
                     "kind": ("kind", "type", "category"), "requested_by": ("requestedby", "employee", "custodian")},
}
LOWER = {"vendor_id", "customer_id", "unit_id", "parent_id", "supplier_id", "tenant", "budget_line", "line", "email", "payee_id", "traveller", "role", "loe_unit", "requested_by"}
UPPER = {"currency", "country", "po_id", "po_number", "pr_number", "engagement_id", "ta_number", "request_number", "charge_code", "status", "invoice_no", "cost_centre", "bank.bank_country_code", "bank.account_currency",
         "address.country_code", "payout_method"}
INTS = {"default_priority", "promises_kept", "promises_missed", "auto_share_bp", "alert_share_bp"}
LISTS = {"contacts", "allowed_payees"}


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def flatten(obj, prefix: str = "", depth: int = 0) -> dict[str, object]:
    """Every leaf of a record as dotted path -> value (lists of scalars are leaves)."""
    out: dict[str, object] = {}
    if isinstance(obj, dict) and depth < 3:
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                out |= flatten(v, p, depth + 1)
            else:  # a list, even of records (approvers, payment terms), is one field
                out[p] = v
    return out


def find_list(body) -> tuple[str, list] | None:
    """Where the records sit in a reply: the top level, a usual key, or the first key holding a list of records."""
    if isinstance(body, list):
        return ("", body) if all(isinstance(x, dict) for x in body[:5]) else None
    if not isinstance(body, dict):
        return None
    for k in (*LIST_KEYS, *body.keys()):
        v = body.get(k)
        if isinstance(v, list) and (not v or isinstance(v[0], dict)):
            return k, v
        if isinstance(v, dict):  # one level down: {"data": {"items": [...]}}
            inner = find_list(v)
            if inner and inner[0]:
                return f"{k}.{inner[0]}", inner[1]
    return None


def _transform(target: str, sample) -> str:
    if target.endswith("_minor"):
        return "|major"
    if target == "due_date":
        return "|date"
    if target == "bank.iban":
        return "|nospace|upper"
    if target in INTS:
        return "|int"
    if target in LISTS and isinstance(sample, str):
        return "|split"
    if target in LOWER:
        return "|lower"
    if target in UPPER:
        return "|upper"
    return ""


def by_name(entity: str, fields: dict[str, object]) -> dict[str, str]:
    """VENTHORA field -> "source|transform" from names alone; each source field is used once."""
    out, used = {}, set()
    for target in PULL[entity][3]:
        words = ENTITY_SYN.get(entity, {}).get(target) or SYN.get(target) or (norm(target.split(".")[-1]),)
        if "." in target:  # bank.x / address.x: only under an object of that kind
            group = target.split(".")[0]
            pool = [p for p in fields if "." in p and group in norm(p.rsplit(".", 1)[0])]
        else:
            pool = [p for p in fields if "." not in p]
        best = None
        for w in words:  # the order of synonyms is the order of preference
            best = next((p for p in pool if p not in used and norm(p.rsplit(".", 1)[-1]) == w), None)
            if best:
                break
        if best:
            used.add(best)
            out[target] = best + _transform(target, fields[best])
    return out


def mask(value) -> object:
    """What the AI model may see of a value: its shape, never an account number or an address."""
    if isinstance(value, str):
        if "@" in value:
            return "name@example.com"
        if sum(c.isdigit() for c in value) >= 5:
            return re.sub(r"\d", "9", value)
        return value[:40]
    return value


async def by_ai(model, entity: str, fields: dict[str, object], have: dict[str, str], notes: list | None = None) -> dict[str, str]:
    """The model's mapping for the fields the names left open (or got wrong); only real source fields survive."""
    if model is None or not getattr(model, "configured", False):
        return {}
    from .evidence.llm import LLMUnavailable, first_json_object

    sample = {k: mask(v) for k, v in list(fields.items())[:60]}
    targets = list(PULL[entity][3])
    prompt = ("Map the fields of a record from a company's finance application to the target fields of a treasury system.\n"
              f"Item: {PULL[entity][0]}.\nTarget fields: {json.dumps(targets)}.\n"
              f"Source record (field path: example value, values masked): {json.dumps(sample, default=str)}.\n"
              f"Already mapped by name: {json.dumps(have)}.\n"
              'Answer with one JSON object {"target field": "source field path"} for the targets you can map with confidence; '
              "use only source field paths from the record above; leave out any target you are unsure of.")
    try:
        reply = await model.complete([{"role": "user", "content": prompt}], max_tokens=600)
        got = first_json_object(reply)
    except (LLMUnavailable, ValueError) as exc:
        if notes is not None:
            notes.append(f"{type(exc).__name__}: {str(exc)[:200]}")
        return {}
    out = {}
    for t, src in got.items():
        if t in targets and isinstance(src, str) and src.split("|")[0] in fields:
            out[t] = src.split("|")[0] + _transform(t, fields[src.split("|")[0]])
    return out


async def _get_json(client, url: str, headers: dict):
    try:
        r = await client.get(url, headers=headers)
    except Exception:  # noqa: BLE001 - an unreachable path is simply not the one
        return None, None
    if r.status_code != 200:
        return r.status_code, None
    try:
        return 200, r.json()
    except ValueError:
        return 200, None


def _openapi_paths(doc: dict) -> tuple[list[str], list[tuple[str, str]]]:
    gets, writes = [], []
    for path, ops in (doc.get("paths") or {}).items():
        if "{" in path:
            continue
        for m in ops:
            if m.lower() == "get":
                gets.append(path)
            elif m.lower() in ("post", "put"):
                writes.append((path, m.upper()))
    return gets, writes


def _pick(paths: list[str], words: tuple[str, ...]) -> str | None:
    for w in words:
        for p in paths:
            if norm(p.rstrip("/").rsplit("/", 1)[-1]) == norm(w):
                return p
    return None


AI_BUDGET_S = 45.0  # the model's time for the whole discovery; past it, the names stand alone


def needs_ai(entity: str, fields: dict, mapping: dict) -> bool:
    """Worth asking only when a field is still open and the record has a field nothing uses yet."""
    used = {m.split("|")[0] for m in mapping.values()}
    return any(t not in mapping for t in PULL[entity][3]) and any(f not in used for f in fields)


async def discover(raw: dict, model=None, client=None) -> dict:
    """The proposal for every item: where it is, how its fields map, and a preview; nothing is written."""
    import asyncio

    import httpx

    from . import net_guard

    own = client is None
    if own:
        net_guard.check_url(raw["base_url"])
    client = client or httpx.AsyncClient(timeout=20)  # no redirects: the address that was checked is the address reached
    base, headers = raw["base_url"].rstrip("/"), headers_for(raw)
    result: dict = {"openapi": None, "entities": {}, "push": {}, "ai_errors": []}
    try:
        gets: list[str] = []
        writes: list[tuple[str, str]] = []
        for cand in OPENAPI_PATHS:
            status, doc = await _get_json(client, f"{base}/{cand}", headers)
            if isinstance(doc, dict) and doc.get("paths"):
                result["openapi"] = f"{base}/{cand}"
                gets, writes = _openapi_paths(doc)
                break
        for ent in PULL:
            prop: dict = {"found": False}
            if gets:
                path = _pick(gets, PATH_WORDS[ent])
                candidates = [path] if path else []
            else:
                candidates = [f"/{w}" for w in PATH_WORDS[ent]] + [f"/{PULL[ent][2]}"]
            for path in dict.fromkeys(candidates):
                full = base + path if path.startswith("/") else f"{base}/{path}"
                status, body = await _get_json(client, full, headers)
                found = find_list(body) if body is not None else None
                if found and found[1]:
                    items_path, items = found
                    fields: dict[str, object] = {}  # every field any of the first records has, with a filled example
                    for it in items[:20]:
                        for k, v in flatten(it).items():
                            if fields.get(k) in (None, "", []):
                                fields[k] = v
                    mapping = by_name(ent, fields)
                    prop = {"found": True, "path": path.lstrip("/"), "items_path": items_path, "records": len(items),
                            "fields": list(fields), "mapping": mapping, "by": dict.fromkeys(mapping, "name match"),
                            "_first": items[0], "_examples": fields}
                    break
                if status is not None and not prop.get("tried"):
                    prop["tried"] = f"{path.lstrip('/')}: HTTP {status}"
            result["entities"][ent] = prop
        # the AI model, for every item at once and within one time budget, only where the names left a gap
        ask = [e for e, pr in result["entities"].items() if pr.get("found") and needs_ai(e, pr["_examples"], pr["mapping"])]
        if model is not None and getattr(model, "configured", False) and ask:
            async def one(e):
                pr = result["entities"][e]
                return e, await by_ai(model, e, pr["_examples"], pr["mapping"], result["ai_errors"])
            try:
                answers = await asyncio.wait_for(asyncio.gather(*(one(e) for e in ask)), AI_BUDGET_S)
            except TimeoutError:
                answers = []
                result["ai_errors"].append(f"the model took longer than {int(AI_BUDGET_S)} s; names only")
            for e, got in answers:
                pr = result["entities"][e]
                for t, m in got.items():
                    if t not in pr["mapping"]:
                        pr["mapping"][t], pr["by"][t] = m, "AI"
        result["ai_asked"] = ask if model is not None and getattr(model, "configured", False) else []
        for ent, pr in result["entities"].items():
            if pr.get("found"):
                pr.pop("_examples")
                try:
                    preview = map_item(pr.pop("_first"), pr["mapping"])
                except ValueError as exc:
                    preview = {"_error": str(exc)}
                pr["preview"] = {k: _shown(k, v) for k, v in flat(preview).items()}
                pr["missing"] = [t for t in PULL[ent][3] if t not in pr["mapping"]]
        for item, (_, _, default, _) in PUSH.items():
            hit = _pick([p for p, _ in writes], PUSH_WORDS[item]) if writes else None
            method = next((m for p, m in writes if p == hit), "POST")
            result["push"][item] = {"path": (hit or default).lstrip("/"), "method": method, "found": bool(hit)}
    finally:
        if own:
            await client.aclose()
    return result




def flat(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        p = f"{prefix}.{k}" if prefix else k
        out |= flat(v, p) if isinstance(v, dict) else {p: v}
    return out


def _shown(field: str, value):
    """Preview values as a person sees them on the page: bank numbers masked."""
    if field in ("bank.account_number", "bank.iban") and isinstance(value, str) and len(value) > 4:
        return "•••• " + value[-4:]
    return value
