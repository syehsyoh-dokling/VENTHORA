# ruff: noqa: E501
"""Reporting -> what was paid, per spending category, with what is left on each line.

    payroll          the payroll register lines and the DoA runs that paid them
    fringe           fringe benefits paid by the DoA
    goods_services   purchase requests, purchase orders, consultant contracts and one-off engagements; supplier
                     invoices without a PO
    operation        office and operations bills
    cash_advance     cash advances to staff, petty cash floats and slush funds
    travel           travel authorisations and travel claims
    odc              other direct costs

Each row: the name (vendor, consultant, supplier or staff), the day it was asked for, its number (PO, PR, request,
invoice), the amount, what is paid and when, what is left, and Done / Partial / New. The office, branch and unit come
from the charge code (or the person's own unit), so every row can be filtered by the organisation tree and by person.
"""
from __future__ import annotations

from . import doa, procurement
from .organisation import ENGAGEMENT_ROLES, FUND_KINDS

CATEGORIES = {"payroll": "Payroll", "fringe": "Fringe benefits", "goods_services": "Goods and services", "operation": "Operation",
              "cash_advance": "Cash advance, petty cash and slush fund", "travel": "Travel and transport", "odc": "Other direct cost"}
OFFICE_ACCOUNTS = {"6300", "6700"}  # software and subscriptions, office


def _status(amount: int, paid: int) -> str:
    return "Done" if amount and paid >= amount else "Partial" if paid > 0 else "New"


def _account(code: str | None) -> str:
    parts = str(code or "").split(".")
    return parts[2] if len(parts) == 3 else ""


class _Tree:
    """Where a unit sits: its country office (or head office), its branch, itself."""

    def __init__(self, org):
        self.org = org
        self.units = {u["unit_id"]: u for u in org.list("unit")}

    def place(self, unit_id: str | None) -> dict:
        u = self.units.get(unit_id or "")
        country = branch = None
        cur, seen = u, set()
        while cur and cur["unit_id"] not in seen:
            seen.add(cur["unit_id"])
            if cur["kind"] == "BRANCH_OFFICE" and branch is None:
                branch = cur["unit_id"]
            if cur["kind"] in ("COUNTRY_OFFICE", "HQ") and country is None:
                country = cur["unit_id"]
            cur = self.units.get(cur.get("parent_id") or "")
        return {"unit_id": unit_id if u else None, "country_office_id": country, "branch_id": branch}

    def of_code(self, code: str | None) -> dict:
        try:
            unit = procurement.unit_of(self.org, code) if code else None
        except Exception:  # noqa: BLE001 - a code that no longer resolves has no place
            unit = None
        return self.place(unit["unit_id"] if unit else None)


def _row(**kw) -> dict:
    amount, paid = kw.get("amount_minor") or 0, kw.get("paid_minor") or 0
    kw.setdefault("remaining_minor", max(amount - paid, 0))
    kw.setdefault("status", _status(amount, paid))
    return kw


def build(category: str, *, org, store, intake_rows: list, payees: dict) -> list[dict]:
    """Every line of the category, newest request first."""
    tree = _Tree(org)
    users = {u["user_id"]: u for u in org.list("user")}
    by_name = {u["name"].lower(): u for u in users.values()}
    funds = {f["request_number"]: f for f in org.list("fund_request")}
    paid_by_ref: dict[str, dict] = {}
    for r in store.paid_rows():
        if r.get("invoice_fingerprint"):
            p = paid_by_ref.setdefault(r["invoice_fingerprint"], {"amount_minor": 0, "paid_at": r["paid_at"]})
            p["amount_minor"] += r["amount_minor"]
            p["paid_at"] = max(p["paid_at"], r["paid_at"])
    live = [q for q in intake_rows if q.status not in ("REJECTED", "DUPLICATE")]
    intake_by_id = {q.intake_id: q for q in live}

    def person_place(user_id: str | None, code: str | None = None) -> dict:
        u = users.get(user_id or "")
        place = tree.of_code(code) if code else {"unit_id": None, "country_office_id": None, "branch_id": None}
        if place["unit_id"] is None and u:
            place = tree.place(u["unit_id"])
        return place | {"person": u["name"] if u else None}

    def q_paid(q) -> tuple[int, str | None]:
        p = q.payment or paid_by_ref.get(q.invoice_fingerprint or "")
        return ((p or {}).get("amount_minor", 0), ((p or {}).get("paid_at") or "")[:10] or None)

    def q_category(q) -> str | None:
        flags, acct = set(q.flags), _account(q.account_code)
        if (q.invoice_no or "") in funds:
            return None  # shown with its request
        if "doa_travel" in flags or acct == "6400" or (q.category or "").lower() in ("travel", "transport"):
            return "travel"
        if "doa_cash_advance" in flags or acct == "1400":
            return "cash_advance"
        if acct == "6900":
            return "odc"
        if acct in ("6010",):
            return "payroll"
        if acct in ("6020",):
            return "fringe"
        if acct in OFFICE_ACCOUNTS or q.kind == "expense_claim":
            return "operation"
        if q.kind in ("supplier_invoice", "contractor_claim"):
            return None if q.po_number else "goods_services"  # an invoice against a PO is counted on its PO
        return None

    def q_row(q, cat: str, typ: str) -> dict:
        paid, day = q_paid(q)
        who = by_name.get((q.counterparty_name or q.display_name or "").lower())
        place = tree.of_code(q.account_code)
        if place["unit_id"] is None and (q.unit_id or who):
            place = tree.place(q.unit_id or who["unit_id"])
        return _row(category=cat, type=typ, name=q.counterparty_name or q.display_name, request_date=q.received_at[:10],
                    reference=q.invoice_no or q.intake_id[:8], reference_kind="invoice", amount_minor=q.amount_minor or 0,
                    currency=q.currency, paid_minor=paid, paid_date=day, source="pending payment" if not paid else "paid",
                    person=(who or {}).get("name") or (q.display_name if q.kind == "expense_claim" else None), **place)

    rows: list[dict] = []
    if category == "payroll":
        runs = {r["_id"]: r for r in org.list("doa_run")}
        for reg in org.list("payroll_register"):
            for ln in reg.get("lines", []):
                ref = f"doa:personnel:{reg['period']}:{doa._slug(str(ln['employee']))}"
                run = runs.get(ref) or next((r for r in runs.values() if r["category"] == "personnel" and r["period"] == reg["period"]
                                             and r["employee"].lower() == str(ln["employee"]).lower()), None)
                paid = ln["amount_minor"] if run and run["status"] in ("PAID", "IN_FLIGHT") else 0
                u = by_name.get(str(ln["employee"]).lower())
                rows.append(_row(category=category, type="net pay", name=ln["employee"], request_date=f"{reg['period']}-01",
                                 reference=f"Payroll {reg['period']}", reference_kind="payroll register", amount_minor=ln["amount_minor"],
                                 currency=ln["currency"], paid_minor=paid, paid_date=(run or {}).get("at", "")[:10] or None,
                                 **person_place((u or {}).get("user_id"), ln.get("charge_code"))))
    elif category == "fringe":
        for r in org.list("doa_run"):
            if r["category"] != "fringe":
                continue
            u = by_name.get(str(r["employee"]).lower())
            paid = r["amount_minor"] if r["status"] in ("PAID", "IN_FLIGHT") else 0
            rows.append(_row(category=category, type="fringe benefit", name=r["employee"], request_date=f"{r['period']}-01",
                             reference=f"Fringe {r['period']}", reference_kind="DoA run", amount_minor=r["amount_minor"], currency=r["currency"],
                             paid_minor=paid, paid_date=(r.get("at") or "")[:10] or None, **person_place((u or {}).get("user_id"))))
    elif category == "goods_services":
        eng = {e["engagement_id"]: e for e in org.list("engagement")}
        prs = {p["pr_number"]: p for p in org.list("pr")}
        for po in org.list("po"):
            st = procurement.status(org, store, po["po_id"])
            pays = st["payments"]
            sup = payees.get(po["supplier_id"]) or {}
            e = eng.get(po["po_id"])
            services = po.get("loe") is not None or e is not None or sup.get("kind") == "consultant" or sup.get("budget_line") == "contractors"
            typ = (ENGAGEMENT_ROLES[e["role"]].lower() if e else "consultant contract" if po.get("loe") is not None else "services" if services else "goods")
            pr = prs.get(po.get("pr_number") or "")
            rows.append(_row(category=category, type=typ, kind="services" if services else "goods", name=e["name"] if e else sup.get("name", po["supplier_id"]),
                             request_date=(pr or {}).get("requested_date") or po.get("start_date") or po.get("issued_date"),
                             reference=po["po_id"] + (f" · {pr['pr_number']}" if pr else ""), reference_kind="engagement" if e else "PO",
                             amount_minor=po["total_minor"], currency=po["currency"], paid_minor=st["paid_minor"],
                             paid_date=(max(p["paid_at"] for p in pays)[:10] if pays else None),
                             status="Cancelled" if po.get("status") == "CANCELLED" else None,
                             person=(e or {}).get("name"), **tree.of_code(po["account_code"])))
        for e in eng.values():
            if e["engagement_id"] in {p["po_id"] for p in org.list("po")} or e.get("status") == "CANCELLED":
                continue
            rows.append(_row(category=category, type=ENGAGEMENT_ROLES[e["role"]].lower(), kind="services", name=e["name"], request_date=e["event_date"],
                             reference=e["engagement_id"], reference_kind="engagement", amount_minor=e["fee_minor"], currency=e["currency"],
                             paid_minor=0, paid_date=None, person=e["name"], **tree.of_code(e["charge_code"])))
        for p in prs.values():
            if p["status"] in ("SUBMITTED", "APPROVED", "DRAFT"):
                rows.append(_row(category=category, type="purchase request", kind="request", name=(payees.get(p.get("supplier_id") or "") or {}).get("name") or p["title"],
                                 request_date=p.get("requested_date"), reference=p["pr_number"], reference_kind="PR", amount_minor=p["estimated_minor"],
                                 currency=p["currency"], paid_minor=0, paid_date=None, note=p["title"],
                                 **person_place((by_name.get(p["requested_by"].lower()) or {}).get("user_id"), p["charge_code"])))
        rows = [r | {"status": r["status"] or _status(r["amount_minor"], r["paid_minor"])} for r in rows]
    elif category in ("cash_advance", "odc"):
        kinds = ("cash_advance", "petty_cash") if category == "cash_advance" else ("odc",)
        for f in funds.values():
            if f["kind"] not in kinds or f["status"] in ("REJECTED", "CANCELLED", "DRAFT"):
                continue
            q = intake_by_id.get(f.get("intake_id") or "")
            paid, day = q_paid(q) if q else (0, None)
            u = users.get(f["requested_by"])
            rows.append(_row(category=category, type=FUND_KINDS[f["kind"]][1].lower(), kind=f["kind"], name=(u or {}).get("name", f["requested_by"]) if f["kind"] != "odc"
                             else (payees.get(f.get("payee_id") or "") or {}).get("name") or (u or {}).get("name"), request_date=f.get("request_date"),
                             reference=f["request_number"], reference_kind="request", amount_minor=f["amount_minor"], currency=f["currency"],
                             paid_minor=paid, paid_date=day, settled_minor=f.get("settled_minor"),
                             status="Done" if f["status"] == "SETTLED" else None, note=f["purpose"],
                             **person_place(f["requested_by"], f["charge_code"])))
        rows = [r | {"status": r["status"] or _status(r["amount_minor"], r["paid_minor"])} for r in rows]
    elif category == "travel":
        for t in org.list("ta"):
            if t["status"] == "CANCELLED":
                continue
            u = users.get(t["traveller"])
            rows.append(_row(category=category, type="travel authorisation", kind="authorisation", name=(u or {}).get("name", t["traveller"]),
                             request_date=t["start"], reference=t["ta_number"], reference_kind="TA", amount_minor=t.get("estimated_minor") or 0,
                             currency=t["currency"], paid_minor=0, paid_date=None, note=f"{t['origin']} → {t['destination']}",
                             status="Done" if t["status"] == "CLAIMED" else "New", **person_place(t["traveller"], t.get("charge_code"))))
    for q in live:
        if q_category(q) == category and category not in ("payroll", "fringe"):
            typ = {"travel": "travel claim", "cash_advance": "cash advance (instructed)", "odc": "other direct cost",
                   "operation": "office and operations" if q.kind != "expense_claim" else "expense claim", "goods_services": "invoice without a PO"}[category]
            rows.append(q_row(q, category, typ) | ({"kind": "cash_advance"} if category == "cash_advance" else {"kind": "goods"} if category == "goods_services" else {}))
    rows.sort(key=lambda r: (r.get("request_date") or ""), reverse=True)
    for i, r in enumerate(rows, 1):
        r["no"] = i
    return rows
