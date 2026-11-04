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
