# ruff: noqa: E501
"""Payment reports: every PAID transfer in one statement, for a running window or accumulated by period, and every
purchase order reconciled against what was paid on it.

* running      today, this week (Monday to the date), month to date, fiscal quarter to date; by the local date
               the money was paid (reporting time zone).
* accumulated  monthly, quarterly, half-yearly or annual buckets of the fiscal year (annual: the last three fiscal
               years), each with its own total and the running total; by reporting period, so the cut-off decides
               which month a payment counts in, as in the period close.
* PO           total, paid, in flight, waiting invoices, what is left; each term's state; OVERPAID if the money paid
               ever passed the total, and UNDERPAID when a PO was closed before it was paid in full.

Amounts stay in their own currency (one total per currency) with the base-currency cost beside them; a PO is
reconciled in its own currency.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
from zoneinfo import ZoneInfo

from .budget_allocation import months_of, period_of

RUNNING = {"day": "Today", "week": "This week", "month": "Month to date", "quarter": "Quarter to date", "year": "Year to date"}
ACCUMULATED = {"month": "Monthly", "quarter": "Quarterly", "half": "Half-yearly (semester)", "year": "Annual"}
_PTYPE = {"month": "monthly", "quarter": "quarterly", "half": "half_year", "year": "annual"}


def _local_day(paid_at: str, tz: str) -> dt.date:
    t = dt.datetime.fromisoformat(paid_at.replace("Z", "+00:00").replace("+0000", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.UTC)
    return t.astimezone(ZoneInfo(tz)).date()


def enrich(rows: list[dict], tz: str, names: dict[str, str]) -> list[dict]:
    termin: dict[str, int] = {}
    out = []
    for r in sorted(rows, key=lambda x: x["paid_at"]):  # every payment on a PO is its next termin, in paid order
        if r.get("po_number"):
            termin[r["po_number"]] = termin.get(r["po_number"], 0) + 1
        out.append({"paid_on": _local_day(r["paid_at"], tz).isoformat(), "paid_at": r["paid_at"], "reporting_period": r["reporting_period"],
                    "payee_id": r.get("beneficiary_id"), "payee": names.get(r.get("beneficiary_id") or "", r.get("beneficiary_id") or "-"),
                    "obligation_id": r["obligation_id"], "po_number": r.get("po_number"), "installment": r.get("installment"),
                    "termin": termin[r["po_number"]] if r.get("po_number") else None,
                    "account_code": r.get("account_code"), "currency": r["currency"], "amount_minor": r["amount_minor"],
                    "base_cost_minor": r["base_cost_minor"], "transfer_id": r["transfer_id"], "book_id": r.get("book_id")})
    return out


def summarise(rows: list[dict]) -> dict:
    by_ccy: dict[str, int] = {}
    vendors: dict[str, dict] = {}
    pos: dict[str, dict] = {}
    for r in rows:
        by_ccy[r["currency"]] = by_ccy.get(r["currency"], 0) + r["amount_minor"]
        v = vendors.setdefault(r["payee"], {"payee": r["payee"], "payments": 0, "by_currency": {}, "base_cost_minor": 0})
        v["payments"] += 1
        v["by_currency"][r["currency"]] = v["by_currency"].get(r["currency"], 0) + r["amount_minor"]
        v["base_cost_minor"] += r["base_cost_minor"]
        if r["po_number"]:
            p = pos.setdefault(r["po_number"], {"po_number": r["po_number"], "payments": 0, "currency": r["currency"], "amount_minor": 0, "terms": []})
            p["payments"] += 1
            p["amount_minor"] += r["amount_minor"]
            p["terms"].append(f"termin-{r['termin']}")
    return {"payments": len(rows), "by_currency": by_ccy, "base_cost_minor": sum(r["base_cost_minor"] for r in rows),
            "by_vendor": sorted(vendors.values(), key=lambda v: -v["base_cost_minor"]), "by_po": sorted(pos.values(), key=lambda p: p["po_number"])}


def running(rows: list[dict], span: str, day: dt.date, fy_start: int) -> dict:
    if span == "day":
        start = day
    elif span == "week":
        start = day - dt.timedelta(days=day.weekday())
    elif span == "month":
        start = day.replace(day=1)
    elif span == "year":
        fy = int(period_of(f"{day.year:04d}-{day.month:02d}", "annual", fy_start))
        start = dt.date(fy, fy_start, 1)
    else:
        q = period_of(f"{day.year:04d}-{day.month:02d}", "quarterly", fy_start)
        first = months_of(q, fy_start)[0]
        start = dt.date(int(first[:4]), int(first[5:]), 1)
    picked = [r for r in rows if start.isoformat() <= r["paid_on"] <= day.isoformat()]
    return {"view": "running", "span": span, "label": RUNNING[span], "from": start.isoformat(), "to": day.isoformat(),
            "summary": summarise(picked), "rows": picked}


def accumulated(rows: list[dict], span: str, day: dt.date, fy_start: int) -> dict:
    month = f"{day.year:04d}-{day.month:02d}"
    fy = int(period_of(month, "annual", fy_start))
    if span == "year":
        periods = [str(fy - 2), str(fy - 1), str(fy)]
    else:
        ptype = _PTYPE[span]
        periods = []
        for m in months_of(str(fy), fy_start):
            p = period_of(m, ptype, fy_start)
            if p not in periods:
                periods.append(p)
    buckets, running_total, cum_ccy = [], 0, {}
    for p in periods:
        months = set(months_of(p, fy_start))
        picked = [r for r in rows if r["reporting_period"] in months]
        s = summarise(picked)
        running_total += s["base_cost_minor"]
        for c, v in s["by_currency"].items():
            cum_ccy[c] = cum_ccy.get(c, 0) + v
        buckets.append({"period": p, "months": sorted(months), "payments": s["payments"], "by_currency": s["by_currency"],
                        "base_cost_minor": s["base_cost_minor"], "cumulative_base_cost_minor": running_total, "cumulative_by_currency": dict(cum_ccy),
                        "current": month in months})
    covered = {m for p in periods for m in months_of(p, fy_start)}
    picked = [r for r in rows if r["reporting_period"] in covered]
    return {"view": "accumulated", "span": span, "label": ACCUMULATED[span], "fiscal_year": fy if span != "year" else None,
            "buckets": buckets, "summary": summarise(picked), "rows": picked}


def po_reconciliation(org, store, waiting: dict[str, list[dict]] | None = None) -> list[dict]:
    """Every PO against the money paid on it, in flight and still invoiced."""
    from . import procurement

    out = []
    for po in org.list("po"):
        st = procurement.status(org, store, po["po_id"])
        flight = [f for f in store.po_in_flight(po["po_id"]) if f["currency"] == po["currency"]]
        in_flight = sum(f["amount_minor"] for f in flight)
        inv = [w for w in (waiting or {}).get(po["po_id"], [])]
        invoiced = sum(w["amount_minor"] for w in inv)
        left = po["total_minor"] - st["paid_minor"] - in_flight
        if st["paid_minor"] > po["total_minor"] + procurement.TOLERANCE_MINOR:
            state = "OVERPAID"
        elif left <= procurement.TOLERANCE_MINOR:
            state = "FULLY PAID"
        elif po["status"] == "CLOSED":
            state = "UNDERPAID"
        elif po["status"] == "CANCELLED":
            state = "CANCELLED"
        else:
            state = "OPEN"
        terms = []
        for t in st["terms"]:
            ts = "PAID" if t["paid_minor"] >= t["amount_minor"] - procurement.TOLERANCE_MINOR else "PARTLY PAID" if t["paid_minor"] else \
                "IN FLIGHT" if any(f["installment"] == t["term_id"] for f in flight) else "OPEN"
            if t["paid_minor"] > t["amount_minor"] + procurement.TOLERANCE_MINOR:
                ts = "OVERPAID"
            terms.append({"term_id": t["term_id"], "label": t["label"], "amount_minor": t["amount_minor"], "paid_minor": t["paid_minor"], "state": ts})
        termins = [{"termin": p["termin"], "paid_at": p["paid_at"], "amount_minor": p["amount_minor"], "scheduled_term": p["installment"]
                    if p["installment"] not in (None, "partial") else None} for p in st["payments"]]
        out.append({"po_id": po["po_id"], "supplier_id": po["supplier_id"], "currency": po["currency"], "status": po["status"], "termins": termins,
                    "next_termin": st["next_termin"],
                    "account_code": po["account_code"], "allow_partial": po["allow_partial"], "total_minor": po["total_minor"],
                    "paid_minor": st["paid_minor"], "payments": len(st["payments"]), "in_flight_minor": in_flight,
                    "invoiced_waiting_minor": invoiced, "waiting_invoices": [w["invoice_no"] for w in inv], "left_minor": left,
                    "state": state, "terms": terms})
    return out


def to_csv(report: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["paid_on", "reporting_period", "payee", "po_number", "termin", "account_code", "currency", "amount", "base_cost", "obligation_id", "transfer_id"])
    for r in report["rows"]:
        w.writerow([r["paid_on"], r["reporting_period"], r["payee"], r["po_number"] or "", f"termin-{r['termin']}" if r["termin"] else "", r["account_code"] or "",
                    r["currency"], f"{r['amount_minor'] / 100:.2f}", f"{r['base_cost_minor'] / 100:.2f}", r["obligation_id"], r["transfer_id"]])
    for c, v in report["summary"]["by_currency"].items():
        w.writerow(["TOTAL", "", "", "", "", "", c, f"{v / 100:.2f}", "", "", ""])
    return buf.getvalue()
