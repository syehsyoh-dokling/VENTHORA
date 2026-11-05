# ruff: noqa: E501
"""Reporting -> FX & transfer costs: what it cost to move the organisation's money across currencies and rails in a
reporting period, every cost in the base currency.

Two costs per payment, never mixed:
* the exchange spread: what was sold minus what the bought amount is worth at the mid-market rate of the quote, for a
  booked conversion (EUR, GBP, SGD, ...) and for a payout Airwallex converted inside the transfer (IDR, INR, ...);
* the payout fee Airwallex charged for the transfer (SWIFT, LOCAL), valued in the base currency.
Both are set against what the period converted and what it paid, so the cost reads as a rate, not just an amount.
"""
from __future__ import annotations

KINDS = {"conversion": "Conversion", "pay_through": "Paid through (converted in the transfer)", "payout_fee": "Payout fee"}


def backfill(store) -> int:
    """Payments made before costs were recorded: their fee is known from the transfer Airwallex returned, a conversion's
    amounts from its record. The spread of those is NOT measured (their quotes kept no mid-market rate) and says so."""
    import json
    from decimal import Decimal

    from .domain.money import cost_in_base, rate_ppm
    from .reporting import load_config, reporting_period

    cfg = load_config()
    with store._lock, store._connect() as c:
        known = {r[0] for r in c.execute("SELECT ref FROM fx_costs")}
        ops = c.execute("SELECT o.kind, o.airwallex_id, o.response_json, e.request_json, e.created_at FROM operations o"
                        " JOIN executions e ON e.execution_id = o.execution_id WHERE o.status = 'SUCCEEDED'"
                        " AND o.kind IN ('conversion', 'transfer') AND o.airwallex_id IS NOT NULL").fetchall()
    added = 0
    for kind, ref, resp, req, created in ops:
        if ref in known:
            continue
        try:
            body, state = json.loads(resp or "{}"), (json.loads(req or "{}").get("request") or {}).get("state") or {}
        except ValueError:
            continue
        base, rates = state.get("base_currency", "USD"), state.get("fx_to_base") or {}

        def in_base(ccy, minor, base=base, rates=rates):
            if not minor or ccy == base:
                return minor or 0
            return cost_in_base(minor, rate_ppm(rates[ccy], round_up=True)) if ccy in rates else None

        if kind == "conversion":
            conv = body.get("conversion") or {}
            if not conv:
                continue
            at = created
            ex = {"sell_currency": conv["sell_currency"], "sell_minor": conv["sell_amount_minor"], "buy_currency": conv["buy_currency"],
                  "buy_minor": conv["buy_amount_minor"], "currency_pair": None, "mid_rate": None, "client_rate": conv.get("client_rate"),
                  "at_mid_minor": None, "spread_minor": None, "spread_bp": None}
            row = {"kind": "conversion", "exchange": ex, "spread_base_minor": None, "fee_base_minor": 0, "fee_currency": None, "fee_minor": 0}
        else:
            src, dst = body.get("source_currency"), body.get("transfer_currency")
            fee = int((Decimal(str(body.get("fee_amount") or 0)) * 100).to_integral_value())
            fee_ccy = body.get("fee_currency") or src
            at = (body.get("created_at") or created or "").replace("+0000", "+00:00")
            fee_base = in_base(fee_ccy, fee)
            if src and dst and src != dst:
                ex = {"sell_currency": src, "sell_minor": int(Decimal(str(body.get("source_amount") or 0)) * 100), "buy_currency": dst,
                      "buy_minor": int(Decimal(str(body.get("transfer_amount") or 0)) * 100), "currency_pair": None, "mid_rate": None,
                      "client_rate": (body.get("conversion") or {}).get("rate"), "at_mid_minor": None, "spread_minor": None, "spread_bp": None}
                row = {"kind": "pay_through", "exchange": ex, "spread_base_minor": None, "fee_base_minor": fee_base or 0,
                       "fee_currency": fee_ccy, "fee_minor": fee}
            elif fee:
                row = {"kind": "payout_fee", "exchange": None, "spread_base_minor": None, "fee_base_minor": fee_base or 0,
                       "fee_currency": fee_ccy, "fee_minor": fee}
            else:
                continue
            row["obligation_id"] = (body.get("reference") or "").split(" ")[0] or None
        try:
            period = reporting_period(at, cfg)
        except (ValueError, TypeError):
            continue
        store.record_fx_cost(row | {"ref": ref, "book_id": state.get("book_id"), "at": at, "reporting_period": period,
                                    "base_currency": base, "backfilled": True})
        added += 1
    return added


def _obligation(op_key: str) -> str | None:
    for tag in (":transfer:", ":replacement:"):
        if tag in op_key:
            return op_key.split(tag, 1)[1].split(":")[0]
    return None


def lines(report: dict, store, *, intake_rows: list, org) -> dict:
    """Every cost line with what the FCO reads it by: the Airwallex transaction code and its value date, the payment and
    the vendor it belongs to, the invoice and PO, the budget line, the charge code and the office, the payout rail. A
    conversion is tied to the payments it funded (the transfers of the same execution in the currency it bought)."""
    from .paid_reports import _Tree

    tree = _Tree(org)
    unit_name = {u: v["name"] for u, v in tree.units.items()}
    ops = store.operations_by_ref(r["ref"] for r in report["rows"])
    funded: dict[tuple, list] = {}
    for t in store.transfers_in(o["execution_id"] for o in ops.values() if o["kind"] == "conversion"):
        b = t["response"] or {}
        funded.setdefault((t["execution_id"], b.get("transfer_currency")), []).append(
            {"obligation_id": _obligation(t["op_key"]), "txn_code": b.get("short_reference_id") or t["airwallex_id"], "ref": t["airwallex_id"]})
    ledger = {r["transfer_id"]: r for r in store.paid_rows()}
    by_fp = {q.invoice_fingerprint: q for q in intake_rows if q.invoice_fingerprint}
    for row in report["rows"]:
        op = ops.get(row["ref"]) or {}
        body = op.get("response") or {}
        row["execution_id"] = op.get("execution_id")
        row["pair"] = f"{row['sold_currency']}→{row['currency']}" if row.get("sold_currency") and row["sold_currency"] != row["currency"] else None
        row["spread_status"] = "n/a" if row["kind"] == "payout_fee" else "not measured" if row.get("spread_base_minor") is None else "measured"
        refs = [row["ref"]]
        if row["kind"] == "conversion":
            conv = body.get("conversion") or {}
            fund = funded.get((op.get("execution_id"), row["currency"]), [])
            row |= {"txn_type": "Conversion", "txn_code": conv.get("short_reference_id") or row["ref"], "txn_status": conv.get("status"),
                    "value_date": (conv.get("conversion_date") or conv.get("settlement_date") or row["at"])[:10], "rail": None, "funds": fund}
            if not row.get("obligation_id") and fund:
                row["obligation_id"] = ", ".join(f["obligation_id"] for f in fund if f["obligation_id"])
            refs = [f["ref"] for f in fund]
        else:
            bank = (body.get("beneficiary") or {}).get("bank_details") or {}
            row |= {"txn_type": "Transfer", "txn_code": body.get("short_reference_id") or row["ref"], "txn_status": body.get("status"),
                    "value_date": (body.get("transfer_date") or row["at"])[:10], "funds": [],
                    "rail": " · ".join(x for x in (body.get("transfer_method"), bank.get("local_clearing_system")) if x) or None,
                    "swift_charges": body.get("swift_charge_option") if body.get("transfer_method") == "SWIFT" else None,
                    "fee_paid_by": body.get("fee_paid_by"), "payee": bank.get("account_name"), "bank_country": bank.get("bank_country_code"),
                    "paid": {"currency": body.get("transfer_currency"), "amount": body.get("transfer_amount")} if body.get("transfer_currency") else None}
        led = [ledger[r] for r in refs if r in ledger]
        row["paid_base_minor"] = sum(x["base_cost_minor"] for x in led) if led and row["kind"] != "conversion" else None
        if row["paid_base_minor"] is None and row["kind"] != "conversion" and body.get("source_currency") == report["base_currency"] and body.get("source_amount") is not None:
            from decimal import Decimal  # not booked in the ledger yet (still scheduled): what Airwallex debits for it

            row["paid_base_minor"] = int((Decimal(str(body["source_amount"])) * 100).to_integral_value())
        row["paid_at"] = max((x["paid_at"] for x in led), default=None)
        row["budget_line"] = next((x["line"] for x in led if x.get("line")), None)
        q = next((by_fp[x["invoice_fingerprint"]] for x in led if x.get("invoice_fingerprint") in by_fp), None)
        place = tree.of_code(q.account_code) if q and q.account_code else {"unit_id": None, "country_office_id": None, "branch_id": None}
        if q and place["unit_id"] is None and q.unit_id:
            place = tree.place(q.unit_id)
        row |= {"payee": (q.counterparty_name if q else None) or row.get("payee"), "invoice_no": q.invoice_no if q else None,
                "po_number": q.po_number if q else None, "charge_code": q.account_code if q else None,
                "country_office": unit_name.get(place["country_office_id"]), "unit": unit_name.get(place["unit_id"])}
        if row["kind"] == "conversion" and not row.get("payee") and len(row["funds"]) > 1:
            row["payee"] = f"{len(row['funds'])} payments"
    return report


def build(store, period: str | None = None) -> dict:
    backfill(store)
    all_rows = store.fx_costs()
    periods = sorted({r["reporting_period"] for r in all_rows}, reverse=True)
    rows = [r for r in all_rows if not period or r["reporting_period"] == period]
    out_rows, by_ccy = [], {}
    tot = {"spread_base_minor": 0, "fee_base_minor": 0, "converted_base_minor": 0, "measured_base_minor": 0, "unmeasured": 0}
    base = rows[0]["base_currency"] if rows else "USD"
    for r in rows:
        ex = r.get("exchange") or {}
        spread = r.get("spread_base_minor")
        fee = r.get("fee_base_minor") or 0
        ccy = ex.get("buy_currency") or r.get("fee_currency") or base
        sold = ex.get("sell_minor") if ex.get("sell_currency") == base else None
        out_rows.append({"at": r["at"], "period": r["reporting_period"], "kind": r["kind"], "kind_label": KINDS.get(r["kind"], r["kind"]),
                         "ref": r["ref"], "obligation_id": r.get("obligation_id"), "currency": ccy,
                         "bought_minor": ex.get("buy_minor"), "sold_minor": ex.get("sell_minor"), "sold_currency": ex.get("sell_currency"),
                         "client_rate": ex.get("client_rate"), "mid_rate": ex.get("mid_rate"), "currency_pair": ex.get("currency_pair"),
                         "spread_base_minor": spread, "spread_bp": ex.get("spread_bp"), "fee_base_minor": fee,
                         "fee": None if not r.get("fee_minor") else {"currency": r.get("fee_currency"), "minor": r.get("fee_minor")},
                         "total_base_minor": (spread or 0) + fee})
        c = by_ccy.setdefault(ccy, {"currency": ccy, "bought_minor": 0, "converted_base_minor": 0, "spread_base_minor": 0,
                                    "fee_base_minor": 0, "exchanges": 0, "payouts": 0, "measured_base_minor": 0})
        if ex:
            c["exchanges"] += 1
            c["bought_minor"] += ex.get("buy_minor") or 0
            if sold is not None:
                c["converted_base_minor"] += sold
                tot["converted_base_minor"] += sold
            if spread is None:
                tot["unmeasured"] += 1
            elif sold is not None:  # a spread rate is only read on the volume whose spread was measured
                c["measured_base_minor"] += sold
                tot["measured_base_minor"] += sold
        if r["kind"] != "conversion":
            c["payouts"] += 1
        c["spread_base_minor"] += spread or 0
        c["fee_base_minor"] += fee
        tot["spread_base_minor"] += spread or 0
        tot["fee_base_minor"] += fee
    for c in by_ccy.values():
        c["total_base_minor"] = c["spread_base_minor"] + c["fee_base_minor"]
        c["spread_bp"] = round(c["spread_base_minor"] * 10_000 / c["measured_base_minor"]) if c["measured_base_minor"] else None
    paid = store.paid_totals(period)
    paid_base = paid["base_cost_minor"]
    total = tot["spread_base_minor"] + tot["fee_base_minor"]
    return {"period": period, "periods": periods, "base_currency": base, "rows": out_rows[::-1],
            "by_currency": sorted(by_ccy.values(), key=lambda c: -c["total_base_minor"]),
            "totals": tot | {"total_base_minor": total, "paid_base_minor": paid_base, "payments": paid["payments"],
                             "spread_bp_of_converted": round(tot["spread_base_minor"] * 10_000 / tot["measured_base_minor"]) if tot["measured_base_minor"] else None,
                             "cost_bp_of_paid": round(total * 10_000 / paid_base) if paid_base else None}}
