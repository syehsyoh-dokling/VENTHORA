# ruff: noqa: E501
"""One step for every outside source (Sync API, uploaded files): validate, write, and apply what writing implies.

  vendor        master data; a new vendor is registered NEW, a vendor whose bank details changed becomes CHANGED
                (payees.py): no payment until a call-back on the registered number, whatever the source says
  po            the organisation's purchase orders (the same checks as the PO form: supplier, currency, account code)
  bill          master data, then the intake queue through the same checks as an emailed invoice (vendor, PO, duplicate)
  customer, receivable, budget_line, tenant_book   master data (read by the registry, the plan, budget and tenancy)
"""
from __future__ import annotations

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
            po = PurchaseOrder.model_validate(row)
            old = org.get("po", po.po_id) if po.po_id else None
            data = po.model_dump()
            if old and {k: old.get(k) for k in data} == data:
                report["unchanged"].append(po.po_id)
                continue
            if not dry_run:
                org.save("po", po, source)
            else:
                org._check_po(data)
            (report["updated"] if old else report["created"]).append(po.po_id)
        except (OrgError, ValueError) as exc:
            msg = exc.errors()[0]["msg"] if hasattr(exc, "errors") else str(exc)
            report["rejected"].append({"row": i + 1, "external_id": ext, "error": msg})
    return report
