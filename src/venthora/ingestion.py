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
    report = master.upsert(entity, [r for r, _ in keep], source=source, external_ids=[e for _, e in keep], dry_run=dry_run)
    report["rejected"] = pre_rejected + report["rejected"]
    report["followed"] = []
    records = report.pop("records")
    if dry_run:
        return report
    if entity == "vendor" and payees is not None:
        for key in report["created"] + report["updated"]:
            report["followed"] += _vendor_lifecycle(records[key], before.get(key), payees)
    if entity == "bill" and intake is not None:
        for key in report["created"] + report["updated"]:
            req = intake.ingest_record(records[key], source=source)
            report["followed"].append(f"bill {records[key]['invoice_no']}: {req.status}"
                                      + (f" ({', '.join(req.flags)})" if req.flags else "")
                                      + (f" - {req.reasons[-1]}" if req.status != "VERIFIED" and req.reasons else ""))
    return report


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
    for i, (row, ext) in enumerate(keep):
        try:
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


ORG_ENTITIES = ("unit", "user", "project", "account", "price", "allocation", "travel_rate", "engagement", "pr", "ta", "fund_request")
ROLE_WORDS = {"keynote": "keynote_speaker", "speaker": "keynote_speaker", "legal": "legal_specialist", "lawyer": "legal_specialist",
              "counsel": "legal_specialist", "resource": "resource_person", "narasumber": "resource_person", "consult": "consultant"}
RECURRING_WORDS = {"month": "monthly", "bulan": "monthly", "quarter": "quarterly", "year": "annual", "annual": "annual", "tahun": "annual"}
KIND_WORDS = {"hq": "HQ", "head": "HQ", "headquarter": "HQ", "headquarters": "HQ", "country": "COUNTRY_OFFICE",
              "branch": "BRANCH_OFFICE", "cabang": "BRANCH_OFFICE", "project": "PROJECT_FINANCE"}


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
        old = org.get("unit", row["unit_id"]) if row.get("unit_id") else None
        old = old or next((u for u in org.list("unit") if row.get("cost_centre") and (u.get("cost_centre") or "").upper() == row["cost_centre"]), None)
        row["unit_id"] = old["unit_id"] if old else row.get("unit_id") or _slug(row.get("name", ""))
    elif entity == "user":
        # a staff list rarely says who may do what in VENTHORA: without a role a person is a requester (the least)
        row["role"] = str(row.get("role") or "requester").strip().lower().replace(" ", "_")
        if row.get("unit_id") and not org.get("unit", row["unit_id"]):  # a cost centre or an office name instead of its id
            u = str(row["unit_id"]).lower()
            hit = next((x for x in org.list("unit") if u in ((x.get("cost_centre") or "").lower(), x["name"].lower())), None)
            row["unit_id"] = hit["unit_id"] if hit else row["unit_id"]
        if row.get("employment_type"):
            row["employment_type"] = str(row["employment_type"]).strip().lower().replace(" ", "_").replace("-", "_")
        if isinstance(row.get("benefits"), list):  # benefit names or keys -> the catalog's keys
            from .doa import BENEFITS

            by = {b["key"]: b["key"] for b in BENEFITS} | {b["label"].lower(): b["key"] for b in BENEFITS}
            row["benefits"] = [by.get(str(x).strip().lower(), next((b["key"] for b in BENEFITS if str(x).strip().lower() in b["label"].lower()), str(x))) for x in row["benefits"]]
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
                x = str(x).strip().lower()
                return next((k for k, label in ENGAGEMENT_DOCUMENTS.items() if x in (k, label.lower()) or x.replace(" ", "_") == k
                             or x in label.lower()), x)
            row["documents"] = [doc(x) for x in row["documents"] if str(x).strip()]
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
        if row.get("basis") not in (None, "regular", "project"):
            row["basis"] = "project" if "project" in str(row["basis"]) else "regular"
        basis, code = row.get("basis", "regular"), (str(row.get("project_code") or "").upper() or None)
        old = next((a for a in org.list("allocation") if a.get("basis", "regular") == basis and a["period"] == str(row.get("period", ""))
                    and (a.get("project_code") or None) == code and (basis == "project" or a.get("unit_id") == row.get("unit_id"))), None)
        row["alloc_id"] = old["alloc_id"] if old else "al-" + _slug(f"{basis}-{row.get('period', '')}-{row.get('unit_id') or ''}-{code or ''}")
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
