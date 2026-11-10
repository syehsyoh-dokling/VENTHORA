# ruff: noqa: E501
"""One step for every outside source (Sync API, uploaded files): validate, write, and apply what writing implies.

  vendor        master data; a new vendor is registered NEW, a vendor whose bank details changed becomes CHANGED
                (payees.py): no payment until a call-back on the registered number, whatever the source says
  po            the organisation's purchase orders (the same checks as the PO form: supplier, currency, account code)
  bill          master data, then the intake queue through the same checks as an emailed invoice (vendor, PO, duplicate)
  customer, receivable, budget_line, tenant_book   master data (read by the registry, the plan, budget and tenancy)
  unit, user, project, account, price, allocation   the organisation itself (offices and branches, people and their approval
                limits, projects, the chart of accounts, unit prices, budget allocations), through the same checks as
                the Settings forms; a record is matched by its natural key (cost centre, email, project or account
                code, item, allocation scope and period) so a second
                sync updates it instead of adding a copy
"""
from __future__ import annotations

import contextlib
import re

from .masterdata import MasterStore, beneficiary_payload
from .payees import PayeeError, bank_fingerprint_of


def apply(entity: str, rows: list[dict], ext_ids: list[str | None], *, source: str, master: MasterStore, org=None,
          intake=None, payees=None, dry_run: bool = False) -> dict:
    """Write the rows; the report says what was created, updated, unchanged, rejected (with reasons), and what followed."""
    pre_rejected = [{"row": i + 1, "external_id": ext_ids[i] if i < len(ext_ids) else None, "error": r["_error"]}
                    for i, r in enumerate(rows) if "_error" in r]
    keep = [(r, ext_ids[i] if i < len(ext_ids) else None) for i, r in enumerate(rows) if "_error" not in r]
    if entity == "po":
        return _purchase_orders(keep, pre_rejected, source=source, org=org, dry_run=dry_run)
    if entity in ORG_ENTITIES:
        return _organisation(entity, keep, pre_rejected, source=source, org=org, dry_run=dry_run)
    before = {v["vendor_id"]: v for v in master.list("vendor")} if entity == "vendor" else {}
    notes: list[str] = []
    if entity == "vendor":  # no vendor code: the payee already on file with the same e-mail or the same name
        keep = [(_known_vendor(r, before.values()), e) for r, e in keep]
    if entity == "bill":  # the supplier by its code, or by its name as a person writes it
        payees = _payees()
        keep = [(r | {"vendor_id": _supplier(str(r["vendor_id"]), payees)} if r.get("vendor_id") else r, e) for r, e in keep]
    if entity == "budget_line":
        keep, notes = _budget_months(keep)
    report = master.upsert(entity, [r for r, _ in keep], source=source, external_ids=[e for _, e in keep], dry_run=dry_run)
    report["rejected"] = pre_rejected + report["rejected"]
    report["followed"] = notes
    records = report.pop("records")
    if dry_run:
        return report
    if entity == "vendor" and payees is not None:
        for key in report["created"] + report["updated"]:
            report["followed"] += _vendor_lifecycle(records[key], before.get(key), payees)
    if entity == "vendor" and org is not None:  # a staff member's own account (same e-mail): it becomes that person's bank account
        keys = report["created"] + report["updated"] + report.get("unchanged", [])
        report["followed"] += _staff_accounts([v for v in (records.get(k) or master.get("vendor", k) for k in keys) if v], org, source)
    if entity == "bill" and intake is not None:
        for key in report["created"] + report["updated"]:
            req = intake.ingest_record(records[key], source=source)
            report["followed"].append(f"bill {records[key]['invoice_no']}: {req.status}"
                                      + (f" ({', '.join(req.flags)})" if req.flags else "")
                                      + (f" - {req.reasons[-1]}" if req.status != "VERIFIED" and req.reasons else ""))
    return report


# legal forms a supplier's name carries in one file and not in another ("PT Sumber Kertas", "Lumen Office Supplies Pte. Ltd.")
LEGAL_FORMS = {"pt", "pte", "ltd", "limited", "private", "sdn", "bhd", "tbk", "inc", "llc", "gmbh", "ag", "co", "corp", "cv", "plc", "the"}


def _bare(name: str) -> str:
    return " ".join(w for w in re.sub(r"[^a-z0-9]+", " ", str(name).lower()).split() if w not in LEGAL_FORMS)


def _payees() -> dict[str, dict]:
    from .intake.pipeline import Registry

    try:
        return Registry().payees
    except Exception:  # noqa: BLE001 - no registry: the reference stays as written and the record's own check refuses it
        return {}


def _supplier(ref: str, payees: dict[str, dict]) -> str:
    """The registered payee a file means: its id (any case), else the one whose name matches without legal forms."""
    if ref in payees:
        return ref
    low, bare = ref.strip().lower(), _bare(ref)
    hit = next((k for k in payees if k.lower() == low), None)
    hit = hit or (next((k for k, p in payees.items() if bare and _bare(p.get("name", "")) == bare), None))
    if not hit and "," in ref:  # "CV Maju Jaya Furniture, Jakarta": the name before the city or address
        head = _supplier(ref.split(",")[0], payees)
        hit = head if head in payees else None
    return hit or ref


def _line_key(line) -> str:
    """A budget line written as its category's title ("Goods and services") is that category's key (goods_services)."""
    from .charge_codes import CATEGORIES

    s = " ".join(str(line or "").split())
    low = s.lower()
    return next((c["key"] for c in CATEGORIES if low in (c["key"], c["title"].lower(), c["key"].replace("_", " "))), s)


def _known_vendor(row: dict, vendors) -> dict:
    """A row without a vendor code is the payee already on file with one of its e-mails, else with its name (legal
    forms and case aside); otherwise a new payee whose code is made from its name."""
    if row.get("vendor_id"):
        return row
    mails = {str(c).strip().lower() for c in row.get("contacts") or [] if c}
    bare = _bare(row.get("name", ""))
    hit = next((v for v in vendors if mails & {str(c).lower() for c in v.get("contacts") or []}), None) \
        or next((v for v in vendors if bare and _bare(v.get("name", "")) == bare), None)
    return row | {"vendor_id": hit["vendor_id"], "name": row.get("name") or hit["name"]} if hit else row


def _budget_months(keep) -> tuple[list, list[str]]:
    """Budget lines are kept per month: a year, half-year or quarter in a file is spread evenly over its months
    (the remainder on the last month), and the report says so."""
    from .budget_allocation import months_of

    out, notes = [], []
    for row, ext in keep:
        if row.get("line"):
            row = row | {"line": _line_key(row["line"])}
        p = str(row.get("period") or "").strip().upper().replace(" ", "")
        if re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", p) or not re.fullmatch(r"\d{4}(-Q[1-4]|-H[12])?", p):
            out.append((row | {"period": p} if p else row, ext))
            continue
        months = months_of(p) or []
        total = int(row.get("amount_minor") or 0)
        each = total // len(months)
        for i, m in enumerate(months):
            out.append((row | {"period": m, "amount_minor": each if i < len(months) - 1 else total - each * (len(months) - 1)}, ext))
        notes.append(f"{row.get('line')} {p}: spread over {len(months)} months ({months[0]} .. {months[-1]})")
    return out, notes


def _staff_accounts(vendors: list[dict], org, source: str) -> list[str]:
    from .organisation import User

    users = {u["email"].lower(): u for u in org.list("user") if u.get("email") and u.get("active", True)}
    out = []
    for v in vendors:
        mail = next((c.lower() for c in (v.get("contacts") or []) if c and c.lower() in users), None)
        if mail is None:
            continue
        u = users[mail]
        if u.get("payee_id") != v["vendor_id"]:
            org.save("user", User.model_validate(u | {"payee_id": v["vendor_id"]}), source)
            out.append(f"{u['name']}: bank account {v['vendor_id']} linked to the person")
    return out


def _why(exc) -> str:
    """A rejection a person can act on: which field, and what is wrong with it."""
    if hasattr(exc, "errors"):
        e = exc.errors()[0]
        field = ".".join(str(x) for x in e.get("loc", ()) if x != "__root__")
        return f"{field}: {e['msg']}" if field else e["msg"]
    return str(exc)


def _vendor_lifecycle(rec: dict, before: dict | None, payees) -> list[str]:
    """Bank details from outside start untrusted: NEW for a new payee, CHANGED for a changed account."""
    payload = beneficiary_payload(rec)
    if payload is None:
        return [f"{rec['vendor_id']}: no bank details yet; it cannot be paid"]
    fp = bank_fingerprint_of(payload["payload"]["beneficiary"]["bank_details"])
    current = payees.payee(rec["vendor_id"])
    try:
        if current is None:
            payees.payee_event(rec["vendor_id"], {"kind": "register", "bank_fingerprint": fp, "registered_phone": rec.get("registered_phone") or None})
            return [f"{rec['vendor_id']}: registered NEW; a call-back on {rec.get('registered_phone') or 'a registered number'} verifies it"]
        if current.get("bank_fingerprint") != fp:
            out = payees.payee_event(rec["vendor_id"], {"kind": "bank_change", "bank_fingerprint": fp, "source": "sync", "note": "bank details from the company's system"})
            return [f"{rec['vendor_id']}: bank details changed, now {out['status']}; no payment until a call-back on the registered number"]
    except PayeeError as exc:
        return [f"{rec['vendor_id']}: {exc}"]
    return []


def _purchase_orders(keep, rejected, *, source: str, org, dry_run: bool) -> dict:
    from .organisation import OrgError, PurchaseOrder

    report = {"entity": "po", "source": source, "created": [], "updated": [], "unchanged": [], "rejected": list(rejected),
              "changes": [], "followed": []}
    payees = _payees()
    for i, (row, ext) in enumerate(keep):
        try:
            if row.get("supplier_id"):  # the supplier by its vendor code, or by its name as a person writes it
                row = row | {"supplier_id": _supplier(str(row["supplier_id"]), payees)}
            if row.get("recurring"):
                r = str(row["recurring"]).strip().lower()
                row = row | {"recurring": r if r in ("none", "monthly", "quarterly", "annual") else
                             next((v for w, v in RECURRING_WORDS.items() if w in r), "none")}
            if row.get("loe_unit"):
                u = str(row["loe_unit"]).strip().lower().rstrip("s")
                row = row | {"loe_unit": {"hari": "day", "jam": "hour", "bulan": "month"}.get(u, u)}
            po = PurchaseOrder.model_validate(row)
            old = org.get("po", po.po_id) if po.po_id else None
            data = po.model_dump()
            if old and not source.startswith("sync:"):
                changed = [k for k in ("supplier_id", "currency", "total_minor", "loe", "rate_minor") if data.get(k) != old.get(k)]
                if changed:
                    raise OrgError(f"{po.po_id} exists and is locked on its number and value ({', '.join(changed)} differ); "
                                   "upload it as a new PO number, or register an amendment in Intake")
            if old and {k: old.get(k) for k in data} == data:
                report["unchanged"].append(po.po_id)
                continue
            if not dry_run:
                org.save("po", po, source)
            else:
                org._check_po(data)
            (report["updated"] if old else report["created"]).append(po.po_id)
        except (OrgError, ValueError) as exc:
            msg = _why(exc)
            report["rejected"].append({"row": i + 1, "external_id": ext, "error": msg})
    return report


ORG_ENTITIES = ("unit", "user", "project", "account", "price", "allocation", "travel_rate", "engagement", "pr", "ta", "fund_request", "mailbox")
# how a staff list names a benefit (Indonesian and English) -> the catalog's key (doa.BENEFITS)
BENEFIT_WORDS = (("bpjs kesehatan", "national_health"), ("jkn", "national_health"), ("bpjs ketenagakerjaan", "social_security"),
                 ("jamsostek", "social_security"), ("jht", "social_security"), ("thr", "thirteenth"), ("hari raya", "thirteenth"),
                 ("13th", "thirteenth"), ("cuti", "paid_leave"), ("annual leave", "paid_leave"), ("sick leave", "paid_leave"),
                 ("dental", "dental_vision"), ("life insurance", "life"), ("asuransi jiwa", "life"), ("health insurance", "health"),
                 ("asuransi kesehatan", "health"), ("bonus", "bonus"), ("pesangon", "severance"), ("perumahan", "housing"),
                 ("transport", "vehicle"), ("pelatihan", "training"), ("pensiun", "pension_dc"))
# how a file names an engagement document -> ENGAGEMENT_DOCUMENTS key; anything else is kept in the note
DOCUMENT_WORDS = (("surat penunjukan", "engagement_letter"), ("kontrak", "engagement_letter"), ("contract", "engagement_letter"),
                  ("tor", "engagement_letter"), ("kak", "engagement_letter"), ("faktur", "invoice"), ("kwitansi", "invoice"),
                  ("npwp", "tax_form"), ("w-8", "tax_form"), ("w-9", "tax_form"), ("ktp", "id_document"), ("paspor", "id_document"),
                  ("passport", "id_document"), ("daftar hadir", "attendance"), ("absensi", "attendance"), ("laporan", "attendance"),
                  ("report", "attendance"), ("agenda", "attendance"), ("buku tabungan", "bank_letter"), ("surat bank", "bank_letter"),
                  ("rekening", "bank_letter"))
MAILBOX_PURPOSES = (("invoice", "invoices"), ("tagihan", "invoices"), ("faktur", "invoices"), ("claim", "claims"), ("klaim", "claims"),
                    ("reimburs", "claims"), ("remit", "remittances"), ("transfer", "remittances"), ("payment order", "payment_orders"),
                    ("order", "payment_orders"))
ROLE_WORDS = {"keynote": "keynote_speaker", "speaker": "keynote_speaker", "legal": "legal_specialist", "lawyer": "legal_specialist",
              "counsel": "legal_specialist", "resource": "resource_person", "narasumber": "resource_person", "consult": "consultant"}
RECURRING_WORDS = {"month": "monthly", "bulan": "monthly", "quarter": "quarterly", "year": "annual", "annual": "annual", "tahun": "annual"}
KIND_WORDS = {"hq": "HQ", "head": "HQ", "headquarter": "HQ", "headquarters": "HQ", "country": "COUNTRY_OFFICE",
              "branch": "BRANCH_OFFICE", "cabang": "BRANCH_OFFICE", "project": "PROJECT_FINANCE", "proyek": "PROJECT_FINANCE"}
# how an HR file writes the kind of contract (English and Indonesian: PKWT is fixed-term, PKWTT / tetap is permanent)
EMPLOYMENT_WORDS = (("pkwtt", "permanent"), ("tetap", "permanent"), ("permanent", "permanent"), ("expat", "expatriate"),
                    ("part", "part_time"), ("paruh", "part_time"), ("intern", "intern"), ("magang", "intern"),
                    ("fixed", "fixed_term"), ("pkwt", "fixed_term"), ("kontrak", "fixed_term"), ("contract", "fixed_term"))
# a parent written as a word, not a name: the company's head office
HQ_WORDS = {"hq", "head office", "headquarter", "headquarters", "kantor pusat", "pusat", "head quarter", "main office"}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")[:36] or "x"


def _org_row(entity: str, row: dict, org) -> tuple[dict, dict | None]:
    """The row with its id (the existing record's, matched by natural key, or a new one) and that existing record."""
    row = {k: v for k, v in row.items() if v not in (None, "")}
    if entity == "unit":
        if row.get("kind"):
            k = str(row["kind"]).strip().upper().replace(" ", "_")
            row["kind"] = k if k in ("HQ", "COUNTRY_OFFICE", "BRANCH_OFFICE", "PROJECT_FINANCE") else \
                next((v for w, v in KIND_WORDS.items() if w in k.lower()), k)
        units = org.list("unit")
        if row.get("name"):
            row["name"] = " ".join(str(row["name"]).split())
        if row.get("parent_id") and not org.get("unit", row["parent_id"]):  # the parent by its name, cost centre or as "head office"
            p = " ".join(str(row["parent_id"]).split()).lower()
            hit = next((u for u in units if p in (u["name"].lower(), (u.get("cost_centre") or "").lower(), _slug(u["name"]))), None)
            if hit is None and p in HQ_WORDS:
                hit = next((u for u in units if u.get("kind") == "HQ"), None)
            row["parent_id"] = hit["unit_id"] if hit else row["parent_id"]
        parent = org.get("unit", row["parent_id"]) if row.get("parent_id") else None
        if parent and not row.get("timezone") and parent.get("timezone"):  # a branch keeps the time of the office above it
            row["timezone"] = parent["timezone"]
        old = org.get("unit", row["unit_id"]) if row.get("unit_id") else None
        old = old or next((u for u in units if row.get("cost_centre") and (u.get("cost_centre") or "").upper() == str(row["cost_centre"]).upper()), None)
        old = old or next((u for u in units if row.get("name") and u["name"].lower() == row["name"].lower()), None)
        row["unit_id"] = old["unit_id"] if old else row.get("unit_id") or _slug(row.get("name", ""))
    elif entity == "user":
        # a staff list rarely says who may do what in VENTHORA: without a role a person is a requester (the least)
        row["role"] = str(row.get("role") or "requester").strip().lower().replace(" ", "_")
        if row.get("unit_id") and not org.get("unit", row["unit_id"]):  # a cost centre or an office name instead of its id
            u = str(row["unit_id"]).lower()
            hit = next((x for x in org.list("unit") if u in ((x.get("cost_centre") or "").lower(), x["name"].lower())), None)
            row["unit_id"] = hit["unit_id"] if hit else row["unit_id"]
        if row.get("employment_type"):
            raw = str(row["employment_type"]).strip()
            low = raw.lower()
            row["employment_type"] = next((v for w, v in EMPLOYMENT_WORDS if w in low), low.replace(" ", "_").replace("-", "_"))
            until = re.search(r"(?:until|s/d|sd|sampai|hingga|to|thru)\s+(.+)$", raw, re.IGNORECASE)
            if until and not row.get("contract_end"):  # "Fixed-term until 14 Mar 2027": the end date is in the same cell
                from .imports import _date

                with contextlib.suppress(ValueError):
                    row["contract_end"] = _date(until.group(1).strip())
        if isinstance(row.get("benefits"), list):  # benefit names or keys -> the catalog's keys
            from .doa import BENEFITS

            by = {b["key"]: b["key"] for b in BENEFITS} | {b["label"].lower(): b["key"] for b in BENEFITS}

            def ben(x):
                x = " ".join(str(x).split()).lower()
                return by.get(x) or next((b["key"] for b in BENEFITS if x in b["label"].lower()), None) \
                    or next((k for w, k in BENEFIT_WORDS if re.search(rf"\b{re.escape(w)}\b", x)), str(x))
            row["benefits"] = list(dict.fromkeys(ben(x) for x in row["benefits"] if str(x).strip()))
        old = next((u for u in org.list("user") if u["email"].lower() == str(row.get("email", "")).lower()), None)
        row["user_id"] = old["user_id"] if old else "u-" + _slug(row.get("name", row.get("email", "")))
    elif entity == "project":
        old = next((x for x in org.list("project") if x["code"] == str(row.get("code", "")).upper()), None)
        row["project_id"] = old["project_id"] if old else "pj-" + _slug(row.get("code", ""))
    elif entity == "account":
        row["code"] = str(row.get("code", "")).strip()
        old = next((x for x in org.list("account") if x["code"] == row["code"]), None)
        row["account_id"] = old["account_id"] if old else "ac-" + _slug(row["code"])
    elif entity == "travel_rate":
        key = (" ".join(str(row.get("country", "")).split()).lower(), " ".join(str(row.get("city", "")).split()).lower())
        old = next((x for x in org.list("travel_rate") if (x["country"].lower(), x["city"].lower()) == key), None)
        row["rate_id"] = old["rate_id"] if old else "tr-" + _slug(f"{key[0]}-{key[1]}")
    elif entity == "pr":
        row["pr_number"] = str(row.get("pr_number") or "").upper() or None
        if row.get("supplier_id"):
            row["supplier_id"] = _supplier(str(row["supplier_id"]), _payees())
        q = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*([A-Za-z][\w ]{0,19})?\s*", str(row.get("quantity") or ""))
        if q and row.get("quantity") not in (None, ""):  # "10 unit", "3 pcs": the number, and its unit when none is given
            row["quantity"] = float(q.group(1).replace(",", "."))
            if q.group(2) and not row.get("unit"):
                row["unit"] = q.group(2).strip()
        old = org.get("pr", row["pr_number"]) if row["pr_number"] else None
        if row.get("status"):
            st = str(row["status"]).upper()
            row["status"] = next((v for w, v in (("ORDER", "ORDERED"), ("APPROV", "APPROVED"), ("REJECT", "REJECTED"), ("DRAFT", "DRAFT"))
                                  if w in st), "SUBMITTED")
        if old:  # what VENTHORA added stays: the PO raised from it
            row.setdefault("po_id", old.get("po_id"))
    elif entity == "fund_request":
        row["request_number"] = str(row.get("request_number") or "").upper() or None
        k = str(row.get("kind") or "").strip().lower()
        row["kind"] = k if k in ("cash_advance", "petty_cash", "odc") else ("petty_cash" if any(w in k for w in ("petty", "slush", "imprest", "float"))
                                                                           else "odc" if ("odc" in k or "direct" in k) else "cash_advance")
        t = str(row.get("requested_by") or "").strip().lower()
        hit = next((u for u in org.list("user") if t in (u["user_id"].lower(), u["name"].lower(), u["email"].lower())), None)
        if hit:
            row["requested_by"] = hit["user_id"]
        if row.get("status"):
            st = str(row["status"]).upper()
            row["status"] = next((v for w, v in (("SETTLE", "SETTLED"), ("PAID", "IN_PAYMENT"), ("APPROV", "APPROVED"), ("REJECT", "REJECTED"),
                                                 ("CANCEL", "CANCELLED"), ("DRAFT", "DRAFT")) if w in st), "SUBMITTED")
        old = org.get("fund_request", row["request_number"]) if row["request_number"] else None
        if old:
            row.setdefault("intake_id", old.get("intake_id"))
    elif entity == "ta":
        row["ta_number"] = str(row.get("ta_number") or "").upper() or None
        t = str(row.get("traveller") or "").strip().lower()
        hit = next((u for u in org.list("user") if t in (u["user_id"].lower(), u["name"].lower(), u["email"].lower())), None)
        if hit:
            row["traveller"] = hit["user_id"]
        if row.get("status"):
            st = str(row["status"]).upper()
            row["status"] = "CANCELLED" if "CANCEL" in st else "CLAIMED" if "CLAIM" in st else "APPROVED"
        old = org.get("ta", row["ta_number"]) if row["ta_number"] else None
    elif entity == "engagement":
        if row.get("role"):
            r = str(row["role"]).strip().lower().replace(" ", "_")
            row["role"] = r if r in ("consultant", "legal_specialist", "resource_person", "keynote_speaker") else \
                next((v for w, v in ROLE_WORDS.items() if w in r), r)
        if isinstance(row.get("documents"), list):  # document names or keys -> the keys
            from .organisation import ENGAGEMENT_DOCUMENTS

            def doc(x):
                x = " ".join(str(x).split()).lower()
                return next((k for k, label in ENGAGEMENT_DOCUMENTS.items() if x in (k, label.lower()) or x.replace(" ", "_") == k
                             or x in label.lower()), None) or next((k for w, k in DOCUMENT_WORDS if re.search(rf"\b{re.escape(w)}\b", x)), None)
            names = [str(x).strip() for x in row["documents"] if str(x).strip()]
            row["documents"] = list(dict.fromkeys(d for d in (doc(x) for x in names) if d))
            other = [x for x in names if not doc(x)]  # a CV, a portfolio ...: on file, but not a document VENTHORA checks
            if other:
                row["note"] = "; ".join(filter(None, [row.get("note"), "also on file: " + ", ".join(other)]))[:300]
        eid = str(row.get("engagement_id") or "").upper() or None
        old = (org.get("engagement", eid) if eid else None) or next(
            (e for e in org.list("engagement") if e["name"].lower() == str(row.get("name", "")).lower()
             and e["event_date"] == str(row.get("event_date", "")) and e["event"].lower() == str(row.get("event", "")).lower()), None)
        if old:
            row["engagement_id"] = old["engagement_id"]
    elif entity == "allocation":
        if row.get("unit_id") and not org.get("unit", row["unit_id"]):  # a cost centre or an office name instead of its id
            u = str(row["unit_id"]).lower()
            hit = next((x for x in org.list("unit") if u in ((x.get("cost_centre") or "").lower(), x["name"].lower())), None)
            row["unit_id"] = hit["unit_id"] if hit else row["unit_id"]
        if row.get("line"):
            row["line"] = _line_key(row["line"])
        if row.get("basis") not in (None, "regular", "project"):
            row["basis"] = "project" if "project" in str(row["basis"]) else "regular"
        basis, code = row.get("basis", "regular"), (str(row.get("project_code") or "").upper() or None)
        old = next((a for a in org.list("allocation") if a.get("basis", "regular") == basis and a["period"] == str(row.get("period", ""))
                    and (a.get("project_code") or None) == code and (basis == "project" or a.get("unit_id") == row.get("unit_id"))), None)
        row["alloc_id"] = old["alloc_id"] if old else "al-" + _slug(f"{basis}-{row.get('period', '')}-{row.get('unit_id') or ''}-{code or ''}")
    elif entity == "mailbox":
        row["address"] = str(row.get("address") or "").strip().lower()
        p = str(row.get("purpose") or "").strip().lower()
        row["purpose"] = p if p in ("invoices", "claims", "remittances", "payment_orders", "any") else \
            next((v for w, v in MAILBOX_PURPOSES if w in p), "any")
        pv = str(row.get("provider") or "").strip().lower()
        row["provider"] = next((v for v in ("postmark", "mailgun", "sendgrid", "cloudflare") if v in pv), "other" if pv else "postmark")
        old = next((m for m in org.list("mailbox") if m["address"].lower() == row["address"]), None)
        row["mailbox_id"] = old["mailbox_id"] if old else "mb-" + _slug(row["address"])
    else:
        old = next((x for x in org.list("price") if x["item"].lower() == str(row.get("item", "")).lower()
                    and (x.get("unit_id") or None) == (row.get("unit_id") or None)), None)
        row["price_id"] = old["price_id"] if old else "p-" + _slug(f"{row.get('item', '')}-{row.get('unit_id') or 'all'}")
    return row, old


def _organisation(entity: str, keep, rejected, *, source: str, org, dry_run: bool) -> dict:
    from .organisation import ID_FIELD, MODELS, OrgError

    report = {"entity": entity, "source": source, "created": [], "updated": [], "unchanged": [], "rejected": list(rejected),
              "changes": [], "followed": []}
    todo = list(enumerate(keep))
    for _ in range(6):  # units: a parent may come later in the file; retry until nothing more can be placed
        later = []
        for i, (raw, ext) in todo:
            try:
                row, old = _org_row(entity, raw, org)
                if old:  # a source updates the fields it sends; what it does not send (net pay, contract dates ...) stays
                    row = {k: v for k, v in old.items() if not str(k).startswith("_")} | row
                if entity in ("pr", "fund_request") and not source.startswith("sync:"):
                    if old and old["status"] not in ("DRAFT", "SUBMITTED"):
                        raise OrgError(f"{old[ID_FIELD[entity]]} is already {old['status'].lower()}; it no longer changes")
                    row = row | {"status": "SUBMITTED", "decided_by": None, "decided_at": None}
                rec = MODELS[entity].model_validate(row)
                data = rec.model_dump()
                rid = data[ID_FIELD[entity]]
                if old and {k: old.get(k) for k in data} == data:
                    report["unchanged"].append(rid)
                    continue
                if dry_run:
                    getattr(org, f"_check_{entity}")(dict(data))
                else:
                    org.save(entity, rec, source)
                (report["updated"] if old else report["created"]).append(rid)
            except (OrgError, ValueError, KeyError) as exc:
                msg = _why(exc)
                if entity == "unit" and "unit" in msg.lower() and "unknown" in msg.lower():
                    later.append((i, (raw, ext), msg))
                    continue
                report["rejected"].append({"row": i + 1, "external_id": ext, "error": msg})
        if not later or len(later) == len(todo):
            report["rejected"] += [{"row": i + 1, "external_id": ext, "error": msg} for i, (raw, ext), msg in later]
            break
        todo = [(i, item) for i, item, _ in later]
    return report
