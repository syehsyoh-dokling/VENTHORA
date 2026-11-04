# ruff: noqa: E501
"""Commitments: what the company has agreed to pay or expects to receive, per counterparty.

Vendors      every PO: active and expiry dates, PO amount, paid, remaining, status New / Partial / Multi Payment / Done
Consultants  every contract: LoE bought, rate, LoE used (paid / rate), LoE and budget left, status New / Partial x of y / Done
One-off      consultants for a single task, legal specialists, resource persons, keynote speakers: profile and fee
Customers    open receivables per customer: open, overdue, next due
Staff        people on payroll: monthly net pay, contract dates, the latest payroll register they are on

Paid is money Airwallex reported PAID against the PO; pending is a payment waiting in the queue. Both come from
procurement.status, so this page and the PO match of an invoice always agree.
"""
from __future__ import annotations

from datetime import date

from .organisation import ENGAGEMENT_DOCUMENTS, ENGAGEMENT_ROLES

PERIOD_MONTHS = {"monthly": 1, "quarterly": 3, "annual": 12}
ENGAGEMENT_REQUIRED = {"engagement_letter", "invoice"}  # what a one-off engagement needs on file before it is paid


def _months(start: str, end: str) -> int:
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    return max((b.year - a.year) * 12 + b.month - a.month + 1, 1)


def po_line(st: dict, today: str) -> dict:
    """One PO or contract with its money, dates and status."""
    po = st["po"]
    total, paid, pending = po["total_minor"], st["paid_minor"], st["pending_minor"]
    remaining = total - paid - pending
    start, exp = po.get("start_date") or po.get("issued_date"), po.get("expiry_date")
    rec = po.get("recurring") or "none"
    if po.get("status") == "CANCELLED":
        status = "Cancelled"
    elif paid >= total or po.get("status") == "CLOSED":
        status = "Done"
    elif rec != "none":
        status = "Multi Payment"
    elif paid > 0:
        status = "Partial"
    else:
        status = "New"
    line = {"po_id": po["po_id"], "description": po.get("description", ""), "currency": po["currency"], "account_code": po["account_code"],
            "active_date": start, "expiry_date": exp, "total_minor": total, "paid_minor": paid, "pending_minor": pending,
            "remaining_minor": remaining, "status": status, "payments": len(st.get("payments") or []),
            "expired": bool(exp and exp < today and status not in ("Done", "Cancelled")),
            "days_left": (date.fromisoformat(exp) - date.fromisoformat(today)).days if exp else None,
            "recurring": rec, "terms": [{"label": t["label"], "amount_minor": t["amount_minor"], "state": t["state"]} for t in st.get("terms") or []]}
    if rec != "none" and exp:
        n = max(_months(start, exp) // PERIOD_MONTHS[rec], 1)
        per = round(total / n)
        line |= {"periods": n, "per_period_minor": per, "periods_paid": min(round(paid / per), n) if per else 0}
    if po.get("loe") is not None and po.get("rate_minor"):
        used = paid / po["rate_minor"]
        line |= {"loe": po["loe"], "loe_unit": po.get("loe_unit", "day"), "rate_minor": po["rate_minor"], "loe_used": round(used, 2),
                 "loe_left": round(po["loe"] - used - pending / po["rate_minor"], 2)}
        if status == "Partial":
            line["status_label"] = f"Partial {used:g} of {po['loe']:g} LoE"
    line.setdefault("status_label", status)
    return line


def _party(p: dict, lines: list[dict]) -> dict:
    ccy = {ln["currency"] for ln in lines}
    one = len(ccy) == 1
    order = ("New", "Partial", "Multi Payment", "Done", "Cancelled")
    return {"id": p["id"], "name": p["name"], "currency": p.get("currency"), "status": p.get("status"), "engagement": p.get("engagement"),
            "contacts": p.get("contacts", []), "payout_method": p.get("payout_method"), "bank": p.get("bank"), "registered_phone": p.get("registered_phone"),
            "tax_id": p.get("tax_id", ""), "budget_line": p.get("budget_line"),
            "contracts": sorted(lines, key=lambda x: (x["expiry_date"] or "9999", x["po_id"])),
            "statuses": sorted({ln["status"] for ln in lines}, key=order.index),
            "total_minor": sum(ln["total_minor"] for ln in lines) if one else None,
            "paid_minor": sum(ln["paid_minor"] for ln in lines) if one else None,
            "remaining_minor": sum(ln["remaining_minor"] for ln in lines) if one else None,
            "totals_currency": next(iter(ccy)) if one else None,
            "next_expiry": min((ln["expiry_date"] for ln in lines if ln["expiry_date"] and ln["status"] not in ("Done", "Cancelled")), default=None),
            "expired": any(ln["expired"] for ln in lines)}


def engagement_line(e: dict, st: dict | None, payees: dict) -> dict:
    paid = st["paid_minor"] if st else 0
    pending = st["pending_minor"] if st else 0
    missing = sorted(ENGAGEMENT_REQUIRED - set(e.get("documents") or []))
    if e.get("status") == "CANCELLED":
        status = "Cancelled"
    elif paid >= e["fee_minor"]:
        status = "Done"
    elif paid > 0:
        status = "Partial"
    elif not e.get("payee_id"):
        status = "Needs payee"
    elif missing:
        status = "Documents missing"
    else:
        status = "Ready to pay"
    return e | {"role_label": ENGAGEMENT_ROLES[e["role"]], "paid_minor": paid, "pending_minor": pending,
                "remaining_minor": e["fee_minor"] - paid - pending, "status_label": status,
                "payee_name": (payees.get(e.get("payee_id") or "") or {}).get("name"),
                "documents_missing": [ENGAGEMENT_DOCUMENTS[k] for k in missing]}


def build(*, org, po_statuses: list[dict], vendors: list[dict], consultants: list[dict], customers: list[dict],
          receivables: list[dict], payees: dict, today: str | None = None) -> dict:
    today = today or date.today().isoformat()
    eng = {e["engagement_id"]: e for e in org.list("engagement")}
    by_po = {s["po"]["po_id"]: s for s in po_statuses}
    lines: dict[str, list[dict]] = {}
    for s in po_statuses:
        if s["po"]["po_id"] in eng:
            continue  # paid against an engagement: shown under One-off engagements
        lines.setdefault(s["po"]["supplier_id"], []).append(po_line(s, today))
    out_v = [_party(v, lines.get(v["id"], [])) for v in vendors]
    out_c = [_party(c, lines.get(c["id"], [])) for c in consultants]
    out_e = sorted((engagement_line(e, by_po.get(k), payees) for k, e in eng.items()), key=lambda x: (x["event_date"], x["engagement_id"]))
    out_r = []
    for c in customers:
        mine = sorted((r for r in receivables if r["customer_id"] == c["id"]), key=lambda r: r["due_date"])
        ccy = {r["currency"] for r in mine}
        one = len(ccy) <= 1
        overdue = [r for r in mine if r["due_date"] < today]
        out_r.append({"id": c["id"], "name": c["name"], "currency": c.get("currency"), "contacts": c.get("contacts", []),
                      "history": c.get("history"), "invoices": mine, "open_count": len(mine),
                      "open_minor": sum(r["amount_minor"] for r in mine) if one else None,
                      "overdue_minor": sum(r["amount_minor"] for r in overdue) if one else None,
                      "totals_currency": next(iter(ccy)) if len(ccy) == 1 else c.get("currency"),
                      "next_due": next((r["due_date"] for r in mine if r["due_date"] >= today), None),
                      "status": "Overdue" if overdue else "Open" if mine else "Clear"})
    regs = sorted(org.list("payroll_register"), key=lambda r: r["period"])
    units = {u["unit_id"]: u for u in org.list("unit")}
    out_s = []
    for u in org.list("user"):
        if not u.get("active", True) or (u.get("salary_minor") is None and not u.get("employee_no")):
            continue
        on = [r for r in regs if any(str(ln.get("employee", "")).lower() == u["name"].lower() for ln in r.get("lines", []))]
        end = u.get("contract_end")
        days = (date.fromisoformat(end) - date.fromisoformat(today)).days if end else None
        status = ("Ended" if days is not None and days < 0 else "Ends soon" if days is not None and days <= 60 else
                  "No net pay on file" if u.get("salary_minor") is None else "No bank account" if not u.get("payee_id") else "Active")
        out_s.append({"user_id": u["user_id"], "name": u["name"], "office": (units.get(u["unit_id"]) or {}).get("name", u["unit_id"]),
                      "job_title": u.get("job_title", ""), "employee_no": u.get("employee_no", ""), "employment_type": u.get("employment_type"),
                      "contract_start": u.get("contract_start"), "contract_end": end, "days_left": days, "salary_minor": u.get("salary_minor"),
                      "salary_currency": u.get("salary_currency"), "charge_code": u.get("charge_code"), "payee_id": u.get("payee_id"),
                      "last_register": on[-1]["period"] if on else None, "last_register_status": on[-1]["status"] if on else None,
                      "status": status})
    return {"today": today, "vendors": out_v, "consultants": out_c, "engagements": out_e, "customers": out_r, "staff": out_s,
            "roles": ENGAGEMENT_ROLES, "documents": ENGAGEMENT_DOCUMENTS, "required_documents": sorted(ENGAGEMENT_REQUIRED)}
