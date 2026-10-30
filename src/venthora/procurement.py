# ruff: noqa: E501
"""Purchase orders: what was agreed, what has been paid, and whether an invoice may draw on it.

A PO names one supplier, one currency, a total and an account code (office cost centre . project . account). It may be
paid any number of times while it has room: every payment is the PO's next termin (termin-1, termin-2, ... in the
order the money was paid), of any amount, as long as all of them together never pass the total. Scheduled terms
(e.g. T1 30% deposit, T2 70% on delivery) are the payment plan shown beside the termins; an invoice that equals an
open scheduled term is labelled with it, any other amount is simply the next termin. Its balance comes from the money
that really left: the spend ledger rows the executor booked for transfers Airwallex reported PAID. Invoices already
accepted but not yet paid count as pending, so two invoices cannot each take the same remaining amount.

An invoice matches when every check passes; each failure is a flag a person sees:
  po_unknown / po_not_open          the quoted PO does not exist, or is closed or cancelled
  po_supplier_mismatch              the PO was placed with someone else
  po_currency_mismatch              the invoice is in another currency
  po_exceeded                       paid + pending + this invoice would pass the PO total
"""
from __future__ import annotations

import re

PO_RE = re.compile(r"\b(?:P\.?\s?O\.?|purchase\s+order)\s*(?:no\.?|number|#|ref\.?)?\s*[:#]?\s*(?P<po>[A-Z0-9]+(?:[/-][A-Z0-9]+)+|\d{3,})",
                   re.IGNORECASE)
PO_BARE_RE = re.compile(r"\b(?P<po>PO-[A-Z0-9]+(?:-[A-Z0-9]+)*)\b", re.IGNORECASE)
TOLERANCE_MINOR = 1


def find_po(text: str, subject: str) -> tuple[str, str, int, int] | None:
    """(po number, quoted words, start, end) - start/end -1 when it is read from the subject."""
    for src, in_text in ((subject, False), (text, True)):
        m = PO_RE.search(src) or PO_BARE_RE.search(src)
        if m:
            return m.group("po").upper(), m.group(0), m.start() if in_text else -1, m.end() if in_text else -1
    return None


PO_FAILS = {"po_unknown", "po_not_open", "po_supplier_mismatch", "po_currency_mismatch", "po_exceeded", "po_term_mismatch"}


def holds(r, paid_fingerprints: set[str]) -> bool:
    """Whether a queued invoice holds part of its PO: accepted or waiting, not yet paid, and a valid draw on the PO.
    An invoice that failed the PO match (seen in the demo cases: GBP 12,000 against a PO with 10,000 left) holds
    nothing, or it would block the honest invoices behind it."""
    return (r.status in ("VERIFIED", "NEEDS_REVIEW") and bool(r.amount_minor) and r.invoice_fingerprint not in paid_fingerprints
            and not (PO_FAILS & set(r.flags)))


def status(org, payments, po_id: str, *, pending: dict[str, int] | None = None) -> dict | None:
    """The PO with what is paid (by term), pending and still open."""
    po = org.get("po", po_id)
    if po is None:
        return None
    rows = sorted(payments.po_payments(po_id) if payments is not None else [], key=lambda r: r["paid_at"])
    for n, r in enumerate(rows, 1):
        r["termin"] = n  # every payment on a PO is its next termin, in the order the money was paid
    flying = payments.po_in_flight(po_id) if payments is not None and hasattr(payments, "po_in_flight") else []
    paid = sum(r["amount_minor"] for r in rows if r["currency"] == po["currency"])
    by_term: dict[str, int] = {}
    for r in rows:
        by_term[r["installment"] or "partial"] = by_term.get(r["installment"] or "partial", 0) + r["amount_minor"]
    waiting = sum((pending or {}).values())
    terms = [t | {"paid_minor": by_term.get(t["term_id"], 0),
                  "state": "PAID" if by_term.get(t["term_id"], 0) >= t["amount_minor"] else
                           "PENDING" if t["term_id"] in (pending or {}) else "OPEN"} for t in po["terms"]]
    return {"po": po, "paid_minor": paid, "pending_minor": waiting, "remaining_minor": po["total_minor"] - paid - waiting,
            "terms": terms, "payments": rows, "next_termin": len(rows) + len(flying) + 1}


def match(org, payments, po_id: str, *, supplier_id: str | None, currency: str | None, amount_minor: int | None,
          pending: dict[str, int] | None = None) -> dict:
    """Whether this invoice may draw on the PO, and as which term."""
    st = status(org, payments, po_id, pending=pending)
    if st is None:
        return {"ok": False, "flags": ["po_unknown"], "reasons": [f"PO {po_id} does not exist"], "po": None}
    po, flags, reasons = st["po"], [], []
    if po["status"] != "OPEN":
        flags.append("po_not_open")
        reasons.append(f"PO {po_id} is {po['status'].lower()}")
    if supplier_id and po["supplier_id"] != supplier_id:
        flags.append("po_supplier_mismatch")
        reasons.append(f"PO {po_id} was placed with another supplier ({po['supplier_id']})")
    if currency and po["currency"] != currency:
        flags.append("po_currency_mismatch")
        reasons.append(f"PO {po_id} is in {po['currency']}, the invoice in {currency}")
    installment = None
    if amount_minor:
        if amount_minor > st["remaining_minor"] + TOLERANCE_MINOR:
            flags.append("po_exceeded")
            reasons.append(f"{po['currency']} {amount_minor / 100:,.2f} asked, PO {po_id} has {po['currency']} "
                           f"{max(st['remaining_minor'], 0) / 100:,.2f} left (total {po['total_minor'] / 100:,.2f}, paid "
                           f"{st['paid_minor'] / 100:,.2f}, pending {st['pending_minor'] / 100:,.2f})")
        term = next((t for t in st["terms"] if t["state"] == "OPEN" and abs(t["amount_minor"] - amount_minor) <= TOLERANCE_MINOR), None)
        installment = term["term_id"] if term else "partial"  # any amount is the next termin; a scheduled term is only a label
    ok = not flags
    if ok:
        sched = next((t["label"] for t in st["terms"] if t["term_id"] == installment), None)
        reasons.append(f"PO {po_id} matched as its next payment (termin-{st['next_termin']} if paid next"
                       + (f"; scheduled term {installment}, {sched}" if sched else "") + f"); {po['currency']} "
                       f"{(st['remaining_minor'] - (amount_minor or 0)) / 100:,.2f} left after it; charged to {po['account_code']}")
    return {"ok": ok, "flags": flags, "reasons": reasons, "po": po, "installment": installment,
            "account_code": po["account_code"], "paid_minor": st["paid_minor"], "remaining_minor": st["remaining_minor"]}


def unit_of(org, account_code: str | None) -> dict | None:
    if not account_code or org is None:
        return None
    try:
        return org.account_code(account_code)["unit"]
    except Exception:  # noqa: BLE001 - an unknown code simply has no unit limit; the PO check already refused it
        return None


def spending(org, payments, period_prefix: str | None = None) -> dict:
    """Paid spend rolled up four ways: account code, project, account and office (each office's own and the offices
    under it), with each open PO's remaining commitment beside it. Amounts in base currency (the executor's cost)."""
    rows = payments.spend_by_account(period_prefix)
    by_code: dict[str, dict] = {}
    for r in rows:
        d = by_code.setdefault(r["account_code"], {"account_code": r["account_code"], "base_cost_minor": 0, "payments": 0, "by_currency": {}})
        d["base_cost_minor"] += r["base_cost_minor"]
        d["payments"] += r["payments"]
        d["by_currency"][r["currency"]] = d["by_currency"].get(r["currency"], 0) + r["amount_minor"]
    units = {u["unit_id"]: u for u in org.list("unit")}
    own: dict[str, int] = {}
    projects: dict[str, int] = {}
    accounts: dict[str, int] = {}
    for code, d in by_code.items():
        try:
            parts = org.account_code(code)
        except Exception:  # noqa: BLE001 - a code retired after payment still shows under its own row
            continue
        d |= {"unit_id": parts["unit"]["unit_id"], "project": parts["project"]["code"], "account": parts["account"]["code"],
              "account_name": parts["account"]["name"]}
        own[d["unit_id"]] = own.get(d["unit_id"], 0) + d["base_cost_minor"]
        projects[d["project"]] = projects.get(d["project"], 0) + d["base_cost_minor"]
        accounts[d["account"]] = accounts.get(d["account"], 0) + d["base_cost_minor"]
    rolled = dict.fromkeys(units, 0)
    for uid, amt in own.items():
        for u in org.chain(uid):
            rolled[u["unit_id"]] = rolled.get(u["unit_id"], 0) + amt
    committed = []
    for po in org.list("po"):
        if po["status"] != "OPEN":
            continue
        st = status(org, payments, po["po_id"])
        committed.append({"po_id": po["po_id"], "account_code": po["account_code"], "currency": po["currency"],
                          "total_minor": po["total_minor"], "paid_minor": st["paid_minor"], "open_minor": po["total_minor"] - st["paid_minor"]})
    return {"by_account_code": sorted(by_code.values(), key=lambda d: d["account_code"]),
            "by_unit": [{"unit_id": uid, "name": units[uid]["name"], "kind": units[uid]["kind"], "parent_id": units[uid].get("parent_id"),
                         "own_minor": own.get(uid, 0), "with_units_below_minor": rolled.get(uid, 0)} for uid in units],
            "by_project": [{"project": k, "base_cost_minor": v} for k, v in sorted(projects.items())],
            "by_account": [{"account": k, "base_cost_minor": v} for k, v in sorted(accounts.items())],
            "open_commitments": committed}
