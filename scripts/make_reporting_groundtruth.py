# ruff: noqa: E501
"""Write data/groundtruth/reporting.json: reporting periods with hand-derived answers (src/venthora/reporting.py).

Rules: cut-off = admin day + time + timezone, inclusive to the last microsecond of that minute; a posting at or before
the cut-off instant of month M reports in M, after it in M+1; the cut-off day is clamped to the month's last day;
payments are booked to the period of their payment instant; an invoice is RECONCILED only when its instalments add up
to the contract total, and its completion is reported in the month of the last instalment; consolidation multiplies each
currency's total by the period's closing rate (reporting units per 1 unit), base-currency cost at the base closing rate
beside it, the FX difference being the gap.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "groundtruth" / "reporting.json"
CFG = {"timezone": "Asia/Jakarta", "cutoff_day": 25, "cutoff_time": "23:59", "reporting_currency": "IDR", "base_currency": "USD",
       "closing_rates": {"2026-11": {"USD": "15800", "EUR": "17900", "GBP": "21400"}, "2026-12": {"USD": "16000", "EUR": "18000", "GBP": "21000"}}}
C = []


def add(id_, title, inp, expect, derivation, pain="period cut-off and reporting"):
    C.append({"id": id_, "kind": "reporting", "level": 4, "pain": pain, "title": title, "input": {"config": CFG} | inp, "expect": expect, "derivation": derivation})


def pay(tid, fp, inst, amount, ccy, paid_at, contract=None, base=None, line="suppliers"):
    return {"transfer_id": tid, "book_id": "b", "obligation_id": tid, "invoice_fingerprint": fp, "installment": inst, "contract_total_minor": contract,
            "line": line, "currency": ccy, "amount_minor": amount, "base_cost_minor": base if base is not None else amount, "paid_at": paid_at}


# cut-off instant
add("R01", "posted at the cut-off minute belongs to the month", {"posted": "2026-11-25T23:59:30+07:00"}, {"reporting_period": "2026-11"}, "25 Nov 23:59:30 <= cut-off 25 Nov 23:59:59.999 -> November")
add("R02", "posted one minute after the cut-off belongs to next month", {"posted": "2026-11-26T00:00:00+07:00"}, {"reporting_period": "2026-12"}, "after the cut-off -> December")
add("R03", "posted on the 1st belongs to the running month", {"posted": "2026-11-01T09:00:00+07:00"}, {"reporting_period": "2026-11"}, "1 Nov is after October's cut-off (25 Oct) and before November's -> November")
add("R04", "timezone matters: 25 Nov 17:30 UTC is 26 Nov 00:30 Jakarta", {"posted": "2026-11-25T17:30:00+00:00"}, {"reporting_period": "2026-12"}, "converted to Asia/Jakarta it is past the cut-off -> December")
add("R05", "a naive timestamp is read as local time", {"posted": "2026-11-25T22:00:00"}, {"reporting_period": "2026-11"}, "no offset -> treated as Jakarta time, before the cut-off -> November")
add("R06", "December posting after the cut-off rolls into January", {"posted": "2026-12-28T10:00:00+07:00"}, {"reporting_period": "2027-01"}, "year roll-over")
add("R07", "cut-off day 31 is clamped in a 30-day month", {"config": CFG | {"cutoff_day": 31}, "posted": "2026-11-30T23:00:00+07:00"}, {"reporting_period": "2026-11"}, "day 31 -> 30 Nov; 23:00 <= 23:59 -> November")
add("R08", "cut-off day 31 clamped: 1 Dec is December", {"config": CFG | {"cutoff_day": 31}, "posted": "2026-12-01T00:30:00+07:00"}, {"reporting_period": "2026-12"}, "after 30 Nov 23:59 -> December")
add("R09", "cut-off 28 at noon: 28th 12:01 is next month", {"config": CFG | {"cutoff_day": 28, "cutoff_time": "12:00"}, "posted": "2026-11-28T12:01:00+07:00"}, {"reporting_period": "2026-12"}, "12:01 > 12:00:59 -> December")

# payments booked by payment instant; two-layer invoice reporting
L = [pay("t1", "inv-A", "deposit", 30_000, "USD", "2026-11-10T10:00:00+07:00", contract=100_000),
     pay("t2", "inv-A", "balance", 70_000, "USD", "2026-12-03T10:00:00+07:00", contract=100_000),
     pay("t3", "inv-B", None, 40_000, "EUR", "2026-11-20T10:00:00+07:00", contract=40_000, base=43_400),
     pay("t4", "inv-C", "deposit", 20_000, "GBP", "2026-11-24T10:00:00+07:00", contract=50_000, base=25_400)]
add("R10", "November report: three payments, the deposit of A among them", {"ledger": L, "period": "2026-11"},
    {"payments": 3, "by_currency": {"USD": 30_000, "EUR": 40_000, "GBP": 20_000}, "invoices_completed": ["inv-B"], "invoices_open": ["inv-A", "inv-C"]},
    "t1, t3, t4 paid before 25 Nov 23:59 -> November; inv-B fully paid in November -> completed; A and C still open", "two-layer reporting")
add("R11", "December report: the balance of A completes the invoice", {"ledger": L, "period": "2026-12"},
    {"payments": 1, "by_currency": {"USD": 70_000}, "invoices_completed": ["inv-A"], "invoices_open": ["inv-C"]},
    "t2 paid 3 Dec -> December; deposit 30,000 + balance 70,000 = contract 100,000 -> RECONCILED in December; C still open", "two-layer reporting")
add("R12", "invoice statement across months", {"ledger": L, "invoice": "inv-A"},
    {"invoice_status": "RECONCILED", "invoice_paid_minor": 100_000, "invoice_outstanding_minor": 0, "invoice_completed_in": "2026-12", "instalment_periods": ["2026-11", "2026-12"]},
    "each instalment stays in the month it was paid; the invoice is reconciled when they add up", "two-layer reporting")
add("R13", "an open invoice shows what is outstanding", {"ledger": L, "invoice": "inv-C"},
    {"invoice_status": "OPEN", "invoice_paid_minor": 20_000, "invoice_outstanding_minor": 30_000, "invoice_completed_in": None},
    "deposit 20,000 of 50,000 -> 30,000 outstanding, not reconciled", "two-layer reporting")

# consolidation
add("R14", "November consolidated to IDR at closing rates", {"ledger": L, "period": "2026-11"},
    {"reporting_total_minor": 300 * 15800 + 400 * 17900 + 200 * 21400, "missing_rates": []},
    "USD 300 x 15,800 + EUR 400 x 17,900 + GBP 200 x 21,400 = 4,740,000 + 7,160,000 + 4,280,000 = IDR 16,180,000 (IDR has no minor unit)", "one reporting currency")
add("R15", "FX difference: base cost at the base closing rate vs each currency at its own", {"ledger": L, "period": "2026-11"},
    {"fx_difference_minor": (300 * 15800 + 400 * 17900 + 200 * 21400) - (300_00 + 434_00 + 254_00) * 15800 // 100},
    "base cost USD 300 + 434 + 254 = 988 x 15,800 = 15,610,400; consolidated 16,180,000; difference +569,600 IDR (the agent's realised USD cost was cheaper than closing rates imply)", "one reporting currency")
add("R16", "a missing closing rate is reported, not guessed", {"ledger": L, "period": "2026-11", "rates": {"USD": "15800", "EUR": "17900"}},
    {"missing_rates": ["GBP"], "fx_difference_minor": None}, "no GBP rate -> consolidation flags GBP and gives no FX difference", "one reporting currency")
add("R17", "December uses December's closing rates", {"ledger": L, "period": "2026-12"}, {"reporting_total_minor": 700 * 16000},
    "USD 700 x 16,000 = IDR 11,200,000", "one reporting currency")
add("R18", "a 14-day cut-off puts a 15th posting into next month", {"config": CFG | {"cutoff_day": 14}, "posted": "2026-11-15T08:00:00+07:00"}, {"reporting_period": "2026-12"}, "admin chooses the day; 15 Nov is after 14 Nov 23:59 -> December")

OUT.write_text(json.dumps({"_note": "Reporting-period ground truth, hand-derived; see scripts/make_reporting_groundtruth.py.", "cases": C}, indent=1), encoding="utf-8")
print(f"{len(C)} reporting cases -> {OUT}")
