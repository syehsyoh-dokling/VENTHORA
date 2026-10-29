# ruff: noqa: E501
"""The treasury position on one screen: what is in the Airwallex wallet now, and what is about to change it.

  available now        Airwallex available_amount per currency: old balance + settled deposits + received payments
  (processing out)     transfers Airwallex has not finished (not PAID / FAILED / CANCELLED): money already leaving
  bills to pay         invoices in the queue not yet paid: approved (VERIFIED) or waiting for a person (NEEDS_REVIEW)
  pending deposits     deposits Airwallex holds as PENDING from anyone who is not a registered customer (top-ups)
  incoming, not clear  PENDING deposits from registered customers, and customer remittances announced but not arrived
  after all of these   available − processing − bills + pending deposits + incoming (an outlook, never spendable)

Everything is also given in the base currency at Airwallex's indicative rates. Only Airwallex's own records count as
money; the queue and announcements are shown as what they are: claims that have not moved money yet.
"""
from __future__ import annotations

from decimal import Decimal

from .gateways.airwallex import TERMINAL_TRANSFER_STATUSES, to_minor

TRACKED = ("USD", "EUR", "GBP")


def _base(minor: int, ccy: str, rates: dict[str, Decimal], base: str) -> int | None:
    if ccy == base:
        return minor
    r = rates.get(ccy)
    return None if r is None else int((Decimal(minor) * r).to_integral_value())


def _sum(items: list[dict], rates, base) -> tuple[int, dict[str, int], list[str]]:
    total, by_ccy, missing = 0, {}, []
    for it in items:
        by_ccy[it["currency"]] = by_ccy.get(it["currency"], 0) + it["amount_minor"]
    for ccy, amt in by_ccy.items():
        b = _base(amt, ccy, rates, base)
        if b is None:
            missing.append(ccy)
        else:
            total += b
    return total, by_ccy, missing


def compose(*, balances: dict[str, dict[str, int]], rates: dict[str, Decimal], base: str, transfers: list[dict],
            deposits: list[dict], requests: list, paid_fingerprints: set[str], customers: set[str]) -> dict:
    """Pure: the position from what Airwallex reported and what the intake queue holds."""
    held = {c: b for c, b in balances.items() if c in TRACKED or any(b.values())}
    wallet = [{"currency": c, **b, "available_base_minor": _base(b["available"], c, rates, base)} for c, b in sorted(held.items())]
    available_base = sum(w["available_base_minor"] or 0 for w in wallet)

    moving = [{"currency": t.get("transfer_currency") or t.get("source_currency") or "", "amount_minor": to_minor(t.get("transfer_amount", 0)),
               "status": t.get("status"), "id": t.get("id"), "beneficiary": (t.get("beneficiary") or {}).get("bank_details", {}).get("account_name")
               or t.get("reference") or ""} for t in transfers if t.get("status") not in TERMINAL_TRANSFER_STATUSES]
    processing_base, processing_ccy, m1 = _sum(moving, rates, base)

    approved, waiting = [], []
    for r in requests:
        if r.kind not in ("supplier_invoice", "contractor_claim") or not r.amount_minor or not r.currency:
            continue
        if r.invoice_fingerprint and r.invoice_fingerprint in paid_fingerprints:
            continue
        row = {"currency": r.currency, "amount_minor": r.amount_minor, "who": r.counterparty_name or r.sender,
               "invoice_no": r.invoice_no, "po_number": r.po_number, "due_date": r.due_date, "intake_id": r.intake_id}
        if r.status == "VERIFIED":
            approved.append(row)
        elif r.status == "NEEDS_REVIEW":
            waiting.append(row)
    bills_base, bills_ccy, m2 = _sum(approved + waiting, rates, base)

    pend = [d for d in deposits if d.get("status") == "PENDING"]
    known = {c.lower() for c in customers}

    def dep(d):
        return {"currency": d.get("currency", ""), "amount_minor": to_minor(d.get("amount", 0)), "payer": (d.get("payer") or {}).get("name") or "",
                "id": d.get("id"), "created_at": d.get("created_at")}
    from_customers = [dep(d) for d in pend if ((d.get("payer") or {}).get("name") or "").lower() in known]
    top_ups = [dep(d) for d in pend if ((d.get("payer") or {}).get("name") or "").lower() not in known]
    deposits_base, deposits_ccy, m3 = _sum(top_ups, rates, base)

    landed_customers = {(dep(d)["payer"].lower(), dep(d)["amount_minor"]) for d in deposits if d.get("status") == "SETTLED"}
    announced = []
    for r in requests:
        if r.kind == "remittance_notice" and r.status == "VERIFIED" and r.amount_minor and r.currency:
            if ((r.counterparty_name or "").lower(), r.amount_minor) in landed_customers:
                continue  # it arrived and settled: already in the balance
            announced.append({"currency": r.currency, "amount_minor": r.amount_minor, "payer": r.counterparty_name or r.sender,
                              "expected": r.due_date, "intake_id": r.intake_id})
    incoming_base, incoming_ccy, m4 = _sum(from_customers + announced, rates, base)

    return {"base_currency": base, "rates": {c: str(r) for c, r in rates.items()},
            "available": {"base_minor": available_base, "wallet": wallet,
                          "pending_minor_by_currency": {w["currency"]: w["pending"] for w in wallet if w["pending"]},
                          "reserved_minor_by_currency": {w["currency"]: w["reserved"] for w in wallet if w["reserved"]}},
            "processing_out": {"base_minor": processing_base, "by_currency": processing_ccy, "transfers": moving[:20], "count": len(moving)},
            "bills_unpaid": {"base_minor": bills_base, "by_currency": bills_ccy, "approved": approved[:20], "waiting": waiting[:20],
                             "approved_count": len(approved), "waiting_count": len(waiting)},
            "deposits_pending": {"base_minor": deposits_base, "by_currency": deposits_ccy, "items": top_ups[:20], "count": len(top_ups)},
            "incoming_uncleared": {"base_minor": incoming_base, "by_currency": incoming_ccy, "arrived_pending": from_customers[:20],
                                   "announced": announced[:20], "arrived_count": len(from_customers), "announced_count": len(announced)},
            "after_all_base_minor": available_base - processing_base - bills_base + deposits_base + incoming_base,
            "missing_rates": sorted(set(m1 + m2 + m3 + m4 + [w["currency"] for w in wallet if w["available_base_minor"] is None]))}


async def fetch(bank, *, intake, store, customers: set[str], base: str, rate_cache: dict) -> dict:
    """Read the wallet, its transfers and deposits from Airwallex, and the queue from the intake."""
    balances = await bank.balances_detail()
    transfers = await bank.list_transfers()
    deposits = await bank.list_deposits()
    needed = {c for c, b in balances.items() if (c in TRACKED or any(b.values())) and c != base}
    needed |= {r.currency for r in intake.store.list(1000) if r.currency and r.currency != base}
    rates = {}
    for c in sorted(needed):
        if c not in rate_cache:
            try:
                rate_cache[c] = await bank.rate_to_base(c, base)
            except Exception:  # noqa: BLE001 - a currency without a rate is listed as missing, not guessed
                continue
        rates[c] = rate_cache[c]
    paid = store.paid_fingerprints()
    return compose(balances=balances, rates=rates, base=base, transfers=transfers, deposits=deposits,
                   requests=intake.store.list(1000), paid_fingerprints=paid, customers=customers)
