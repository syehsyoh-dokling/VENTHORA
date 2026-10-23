# ruff: noqa: E501
"""Write data/groundtruth/ledger.json: the book ledger with hand-derived answers (src/venthora/ledger.py).

Rules: every entry is balanced per currency (signed lines sum to zero); open/deposit/grant fund cash against funding;
convert moves cash between currencies through fx; commit moves amount from cash to committed and the fee from cash to
fees; settle moves committed to expense; refund moves committed back to cash (the fee stays); a commit or convert that
would take cash below zero is REFUSED (a book never spends another book's money); the wallet moves by exactly the sum of
cash lines.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "groundtruth" / "ledger.json"
C = []


def add(id_, title, events, expect, derivation, pain="a book never spends another book's money"):
    C.append({"id": id_, "kind": "ledger", "level": 4, "pain": pain, "title": title, "input": {"events": events}, "expect": expect, "derivation": derivation})


OPEN = {"kind": "open", "balances": {"USD": 2_250_000, "EUR": 0, "GBP": 0}}
add("L01", "opening the book", [OPEN], {"cash": {"USD": 2_250_000, "EUR": 0, "GBP": 0}, "posted": 1, "all_balanced": True},
    "cash +22,500 / funding −22,500 per currency; balanced")
add("L02", "a conversion moves cash across currencies", [OPEN, {"kind": "convert", "sell_currency": "USD", "sell_amount": 435_394, "buy_currency": "EUR", "buy_amount": 401_285}],
    {"cash": {"USD": 1_814_606, "EUR": 401_285, "GBP": 0}, "all_balanced": True}, "USD −4,353.94 via fx; EUR +4,012.85 via fx; each currency balanced on its own")
add("L03", "a commit takes amount + fee from cash", [OPEN, {"kind": "commit", "currency": "USD", "amount": 800_000, "fee": 300}],
    {"cash": {"USD": 1_449_700, "EUR": 0, "GBP": 0}, "committed": {"USD": 800_000, "EUR": 0, "GBP": 0}, "fees": {"USD": 300, "EUR": 0, "GBP": 0}},
    "cash −8,003; committed +8,000; fees +3")
add("L04", "settle turns in-flight money into expense", [OPEN, {"kind": "commit", "currency": "USD", "amount": 800_000, "fee": 300}, {"kind": "settle", "currency": "USD", "amount": 800_000}],
    {"committed": {"USD": 0, "EUR": 0, "GBP": 0}, "expense": {"USD": 800_000, "EUR": 0, "GBP": 0}, "cash": {"USD": 1_449_700, "EUR": 0, "GBP": 0}}, "committed −8,000 / expense +8,000; cash unchanged")
add("L05", "a refund brings the amount back, not the fee", [OPEN, {"kind": "commit", "currency": "USD", "amount": 800_000, "fee": 300}, {"kind": "refund", "currency": "USD", "amount": 800_000}],
    {"cash": {"USD": 2_249_700, "EUR": 0, "GBP": 0}, "committed": {"USD": 0, "EUR": 0, "GBP": 0}, "fees": {"USD": 300, "EUR": 0, "GBP": 0}}, "cash back to 22,497: the fee is gone for good")
add("L06", "a commit beyond the book's cash is refused", [OPEN, {"kind": "commit", "currency": "USD", "amount": 2_300_000, "fee": 0}],
    {"posted": 1, "cash": {"USD": 2_250_000, "EUR": 0, "GBP": 0}}, "23,000 > 22,500 -> refused; nothing posted; whatever the shared wallet holds")
add("L07", "a commit in a currency the book does not hold is refused", [OPEN, {"kind": "commit", "currency": "EUR", "amount": 100, "fee": 0}],
    {"posted": 1}, "EUR cash 0 -> refused until a conversion funds it")
add("L08", "convert then commit in the bought currency", [OPEN, {"kind": "convert", "sell_currency": "USD", "sell_amount": 435_394, "buy_currency": "EUR", "buy_amount": 401_285},
                                                            {"kind": "commit", "currency": "EUR", "amount": 400_000, "fee": 1285}],
    {"posted": 3, "cash": {"USD": 1_814_606, "EUR": 0, "GBP": 0}, "committed": {"USD": 0, "EUR": 400_000, "GBP": 0}, "fees": {"USD": 0, "EUR": 1285, "GBP": 0}},
    "EUR 4,012.85 bought covers 4,000 + 12.85 fee exactly -> EUR cash 0")
add("L09", "the real fee is larger than the conversion bought: refused before money moves", [OPEN, {"kind": "convert", "sell_currency": "USD", "sell_amount": 435_394, "buy_currency": "EUR", "buy_amount": 401_285},
                                                                                              {"kind": "commit", "currency": "EUR", "amount": 400_000, "fee": 1337}],
    {"posted": 2}, "4,013.37 > 4,012.85 -> the pre-flight refuses by 0.52 (on the rail the executor would record it as an overdraft to report)")
add("L10", "a deposit funds the book", [OPEN, {"kind": "deposit", "currency": "USD", "amount": 1_000_000}], {"cash": {"USD": 3_250_000, "EUR": 0, "GBP": 0}}, "cash +10,000 / funding −10,000")
add("L11", "a grant funds the book", [OPEN, {"kind": "grant", "currency": "USD", "amount": 110_000}], {"cash": {"USD": 2_360_000, "EUR": 0, "GBP": 0}}, "cash +1,100")
add("L12", "an adjustment can go either way", [OPEN, {"kind": "adjust", "currency": "USD", "amount": -63}], {"cash": {"USD": 2_249_937, "EUR": 0, "GBP": 0}}, "an unreported bank charge of 0.63 recorded by a person")
add("L13", "an unbalanced entry is refused", [OPEN, {"kind": "nonsense"}], {"posted": 1}, "unknown kind -> refused; the journal stays balanced")
add("L14", "the wallet moves by exactly the cash lines", [OPEN, {"kind": "convert", "sell_currency": "USD", "sell_amount": 317_500, "buy_currency": "GBP", "buy_amount": 250_000},
                                                            {"kind": "commit", "currency": "GBP", "amount": 250_000, "fee": 0}, {"kind": "commit", "currency": "USD", "amount": 800_000, "fee": 300},
                                                            {"kind": "settle", "currency": "GBP", "amount": 250_000}, {"kind": "settle", "currency": "USD", "amount": 800_000}],
    {"cash_delta": {"USD": 2_250_000 - 317_500 - 800_300, "GBP": 0, "EUR": 0}, "committed": {"USD": 0, "EUR": 0, "GBP": 0}},
    "cash lines: USD +22,500 −3,175 −8,003 = +11,322; GBP +2,500 −2,500 = 0: reconciliation compares the wallet's movement to exactly this")
add("L15", "two books on one wallet: each is its own ledger", [{"kind": "open", "balances": {"USD": 500_000}}, {"kind": "commit", "currency": "USD", "amount": 400_000, "fee": 0}, {"kind": "commit", "currency": "USD", "amount": 200_000, "fee": 0}],
    {"posted": 2, "cash": {"USD": 100_000}, "committed": {"USD": 400_000}}, "book A has 5,000: 4,000 fits, another 2,000 does not — even if book B's money is in the same wallet")

OUT.write_text(json.dumps({"_note": "Book-ledger ground truth, hand-derived; see scripts/make_ledger_groundtruth.py.", "cases": C}, indent=1), encoding="utf-8")
print(f"{len(C)} ledger cases -> {OUT}")
