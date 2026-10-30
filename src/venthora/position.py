# ruff: noqa: E501
"""The treasury position on one screen: what is in the Airwallex wallet now, and what is about to change it.

  available now        Airwallex available_amount per currency: old balance + settled deposits + received payments
  (processing out)     transfers Airwallex has not finished (not PAID / FAILED / CANCELLED). Airwallex takes them out of
                       available when the transfer is created (checked on the sandbox: moving one to PAID left available
                       unchanged), so they are shown, not subtracted again
  bills to pay         invoices in the queue not yet paid: approved (VERIFIED) or waiting for a person (NEEDS_REVIEW)
  pending deposits     deposits Airwallex holds as PENDING from anyone who is not a registered customer (top-ups)
  incoming, not clear  PENDING deposits from registered customers, and customer remittances announced but not arrived
  after all of these   available − bills + pending deposits + incoming (an outlook, never spendable)

Everything is also given in the base currency at Airwallex's indicative rates. Only Airwallex's own records count as
money; the queue and announcements are shown as what they are: claims that have not moved money yet.
"""
from __future__ import annotations

from decimal import Decimal

from .gateways.airwallex import TERMINAL_TRANSFER_STATUSES, to_minor

TRACKED = ("USD", "EUR", "GBP")
MAJORS = ("EUR", "GBP", "JPY", "SGD", "CNY", "HKD", "AUD")  # the balance is also shown in these (and the reporting currency)


def _when(v) -> float:
    """A timestamp as epoch seconds: ISO strings (with +0000 or Z), epoch milliseconds, or nothing (= the beginning)."""
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return v / 1000 if v > 10**11 else float(v)
    from datetime import datetime

    s = str(v).replace("Z", "+00:00")
    if len(s) > 5 and s[-5] in "+-" and s[-3] != ":":
        s = s[:-2] + ":" + s[-2:]
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return 0.0


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

    # A remittance has landed only when a settled deposit from that payer, of that amount, arrived AFTER it was
    # announced; each deposit answers one announcement (seen live: an older USD 6,500 from the same customer hid a
    # new announcement of the same amount).
    settled = sorted(((dep(d)["payer"].lower(), dep(d)["amount_minor"], _when(d.get("created_at")), d.get("id"))
                      for d in deposits if d.get("status") == "SETTLED"), key=lambda x: x[2])
    used: set = set()
    announced = []
    for r in sorted(requests, key=lambda r: getattr(r, "received_at", "") or ""):
        if r.kind == "remittance_notice" and r.status == "VERIFIED" and r.amount_minor and r.currency:
            told = _when(getattr(r, "received_at", None))
            hit = next((s for s in settled if s[3] not in used and s[0] == (r.counterparty_name or "").lower()
                        and s[1] == r.amount_minor and s[2] >= told), None)
            if hit:
                used.add(hit[3])
                continue  # it arrived and settled: already in the balance
            announced.append({"currency": r.currency, "amount_minor": r.amount_minor, "payer": r.counterparty_name or r.sender,
                              "expected": r.due_date, "intake_id": r.intake_id})
    incoming_base, incoming_ccy, m4 = _sum(from_customers + announced, rates, base)
    # money that arrived and settled: from registered customers (cash in), or anyone else (top-ups of the main account)
    settled_in = [dep(d) | {"customer": dep(d)["payer"].lower() in known, "base_minor": _base(dep(d)["amount_minor"], dep(d)["currency"], rates, base)}
                  for d in deposits if d.get("status") == "SETTLED"]

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
            "after_all_base_minor": available_base - bills_base + deposits_base + incoming_base,  # processing is already out
            "deposits_settled": sorted(settled_in, key=lambda x: _when(x["created_at"]), reverse=True)[:500],
            "missing_rates": sorted(set(m1 + m2 + m3 + m4 + [w["currency"] for w in wallet if w["available_base_minor"] is None]))}


async def fetch(bank, *, intake, store, customers: set[str], base: str, rate_cache: dict, extra: tuple[str, ...] = ()) -> dict:
    """Read the wallet, its transfers and deposits from Airwallex, and the queue from the intake."""
    balances = await bank.balances_detail()
    transfers = await bank.list_transfers()
    deposits = await bank.list_deposits()
    needed = {c for c, b in balances.items() if (c in TRACKED or any(b.values())) and c != base}
    needed |= {r.currency for r in intake.store.list(1000) if r.currency and r.currency != base}
    needed |= {c for c in (*MAJORS, *extra) if c != base}
    rates = {}
    for c in sorted(needed):
        if c not in rate_cache:
            try:
                rate_cache[c] = await bank.rate_to_base(c, base)
            except Exception:  # noqa: BLE001 - a currency without a rate is listed as missing, not guessed
                continue
        rates[c] = rate_cache[c]
    # paid, or already leaving as a transfer: either way no longer a bill (in flight it is counted as processing)
    paid = store.paid_fingerprints() | store.live_payout_fingerprints()
    return compose(balances=balances, rates=rates, base=base, transfers=transfers, deposits=deposits,
                   requests=intake.store.list(1000), paid_fingerprints=paid, customers=customers)
