# ruff: noqa: E501
"""Staff wallets: one advance balance per person on payroll, spent by whichever rail is used first.

Every active person on payroll (People & roles: a monthly salary) has one wallet. An approved advance adds to it; it
is spent through three rails that share the same balance, first come first served. At zero nothing spends.

  Visa card   one fixed virtual card per person (on the card-issuing account, e.g. Malaysia). Its limit follows the
              wallet: the whole-life limit is always what the card has spent plus what the wallet still holds, and
              the card is frozen at zero. Each card charge is booked from Airwallex's card transactions.
  Airi        the same card added to Airi (when Airwallex allowlists the account): its charges are card charges.
  Xendit      the person draws to their OVO, DANA, GoPay, ShopeePay, LinkAja or local bank account. The wallet is
              debited and the card limit lowered BEFORE the payout, so the same money is never spent twice; the
              payout comes from the company's Xendit float (xendit_float.py).

The money itself is pooled: the card's share sits on the card-issuing account, the Xendit share in the float. The
wallet is VENTHORA's ledger per person and is the truth of what each person may still spend.

Two books, one event, no double count: a draw lowers the float (cash) and the wallet (the person's advance) under
one reference. Spending becomes expense once: a card charge at once; a draw when the person settles it with
receipts (until then it is money held by the person). An advance is spent oldest first; at its settle-by date what
it still holds goes back to the main account.
"""
from __future__ import annotations

import datetime as dt
import uuid
from math import ceil

KIND = "staff_wallet"
DRAW_KIND = "wallet_draw"
APPROVED_TX = ("APPROVED", "CLEARED", "SETTLED", "COMPLETED", "POSTED")


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def on_payroll(user: dict | None) -> bool:
    return bool(user) and user.get("active", True) and user.get("salary_minor") is not None


def get(org, user_id: str) -> dict | None:
    return org.get(KIND, user_id)


def open_wallet(org, user: dict, currency: str, by: str) -> dict:
    w = get(org, user["user_id"])
    if w:
        return w
    w = {"_id": user["user_id"], "user_id": user["user_id"], "name": user["name"], "currency": currency, "card_id": None,
         "card_masked": None, "entries": [], "advances": [], "opened_at": _now(), "opened_by": by}
    org._put(KIND, w, by)
    return w


def balance(w: dict) -> int:
    return sum(e["amount_minor"] for e in w["entries"])


def card_spent(w: dict) -> int:
    return -sum(e["amount_minor"] for e in w["entries"] if e["kind"] == "CARD_SPEND")


def _entry(w: dict, kind: str, amount_minor: int, ref: str, by: str, allocate: bool = True, **extra) -> dict:
    e = {"at": _now(), "kind": kind, "amount_minor": amount_minor, "ref": ref, "by": by} | extra
    w["entries"].append(e)
    if allocate:
        _allocate(w, e)
    return e


def _allocate(w: dict, e: dict) -> None:
    """Spending is charged to the oldest open advance first; a top-up opens an advance."""
    if e["kind"] == "TOPUP":
        w["advances"].append({"request": e["ref"], "amount_minor": e["amount_minor"], "left_minor": e["amount_minor"], "settle_by": e.get("settle_by"),
                              "card_minor": 0, "drawn_minor": 0, "returned_minor": 0, "status": "OPEN"})
        return
    if e["amount_minor"] >= 0:
        return
    need = -e["amount_minor"]
    for a in w["advances"]:
        if need <= 0:
            break
        if a["status"] != "OPEN" or a["left_minor"] <= 0:
            continue
        take = min(a["left_minor"], need)
        a["left_minor"] -= take
        need -= take
        key = {"CARD_SPEND": "card_minor", "XENDIT_DRAW": "drawn_minor", "RETURN": "returned_minor"}.get(e["kind"])
        if key:
            a[key] += take
        e.setdefault("advances", []).append({"request": a["request"], "minor": take})


def controls(w: dict, mandate_ceiling: int | None) -> dict:
    """The card's limits for what the wallet holds now: the whole-life limit is what the card has spent plus the
    balance, so the card can never spend more than the wallet; one charge is capped by the mandate's single limit."""
    bal = max(balance(w), 0)
    per_tx = min(bal, mandate_ceiling) if mandate_ceiling else bal
    limits = [{"amount": max(per_tx, 1) / 100, "interval": "PER_TRANSACTION"},
              {"amount": (card_spent(w) + bal) / 100 or 0.01, "interval": "ALL_TIME"}]
    return {"allowed_transaction_count": "MULTIPLE", "transaction_limits": {"currency": w["currency"], "limits": limits}}


def book_card_charges(org, w: dict, txs: list[dict], to_wallet_ccy, by: str) -> list[dict]:
    """New approved card charges become CARD_SPEND entries (each once, by its transaction id) and expense at once."""
    seen = {e.get("tx_id") for e in w["entries"] if e["kind"] == "CARD_SPEND"}
    new = []
    for t in txs:
        tid = t.get("transaction_id") or t.get("id")
        if not tid or tid in seen or str(t.get("status", "")).upper() not in APPROVED_TX:
            continue
        amt = to_wallet_ccy(t)
        if amt <= 0:
            continue
        m = t.get("merchant") or {}
        new.append(_entry(w, "CARD_SPEND", -amt, tid, by, tx_id=tid, merchant=m.get("name") if isinstance(m, dict) else m,
                          mcc=(m.get("category_code") if isinstance(m, dict) else None), billed=t.get("billing_amount"), billed_ccy=t.get("billing_currency"),
                          tx_amount=t.get("transaction_amount"), tx_ccy=t.get("transaction_currency"), expense=True))
    if new:
        org._put(KIND, w, by)
    return new


def topup(org, w: dict, fr: dict, by: str) -> dict:
    if fr["currency"] != w["currency"]:
        raise ValueError(f"{w['name']}'s wallet is in {w['currency']}: an advance in {fr['currency']} goes on its own card")
    if any(e["kind"] == "TOPUP" and e["ref"] == fr["request_number"] for e in w["entries"]):
        raise ValueError(f"{fr['request_number']} is already in the wallet")
    _entry(w, "TOPUP", fr["amount_minor"], fr["request_number"], by, purpose=fr.get("purpose"), settle_by=fr.get("settle_by"))
    org._put(KIND, w, by)
    return w


def draw_amount(idr_minor: int, wallet_ccy: str, rate_wallet_per_idr) -> int:
    """What a rupiah draw costs the wallet: at today's indicative rate, rounded up (the wallet never pays short)."""
    if wallet_ccy == "IDR":
        return idr_minor
    from .reporting import MINOR

    return ceil(idr_minor / MINOR.get("IDR", 100) * float(rate_wallet_per_idr) * MINOR.get(wallet_ccy, 100))


def reserve_draw(org, w: dict, *, idr_minor: int, cost_minor: int, rate, dest: dict, by: str) -> dict:
    """Debit the wallet for a draw BEFORE the payout (so a card charge cannot spend the same money)."""
    if cost_minor > balance(w):
        raise ValueError(f"the wallet holds {w['currency']} {balance(w) / 100:,.2f}: a draw of IDR {idr_minor:,} costs {w['currency']} {cost_minor / 100:,.2f}")
    ref = "WD-" + uuid.uuid4().hex[:10].upper()
    e = _entry(w, "XENDIT_DRAW", -cost_minor, ref, by, idr_minor=idr_minor, rate=str(rate), channel=dest["channel_code"],
               account_masked="•••• " + dest["account_number"][-4:], holder=dest["account_holder_name"], status="RESERVED", expense=False, receipted_minor=0)
    org._put(KIND, w, by)
    return e


def draw_result(org, w: dict, ref: str, *, ok: bool, payout: dict | None, why: str, by: str) -> dict:
    e = next(x for x in w["entries"] if x["ref"] == ref and x["kind"] == "XENDIT_DRAW")
    e["payout"] = payout
    if ok:
        e["status"] = "SENT"
    else:  # the payout did not go: the wallet gets the money back
        e["status"] = "FAILED"
        e["why"] = why
        _entry(w, "DRAW_REVERSED", -e["amount_minor"], ref, by, allocate=False, why=why)
        for part in e.get("advances", []):  # undo the allocation on the same advances
            a = next(a for a in w["advances"] if a["request"] == part["request"])
            a["left_minor"] += part["minor"]
            a["drawn_minor"] -= part["minor"]
    org._put(KIND, w, by)
    return e


def receipt(org, w: dict, ref: str, amount_minor: int, note: str, by: str) -> dict:
    """The person proves what a draw paid for (in rupiah): that part becomes expense."""
    e = next((x for x in w["entries"] if x["ref"] == ref and x["kind"] == "XENDIT_DRAW"), None)
    if e is None:
        raise LookupError(f"no draw {ref}")
    if e.get("status") not in ("SENT", "PAID"):
        raise ValueError(f"a {str(e.get('status')).lower()} draw takes no receipts")
    if e["receipted_minor"] + amount_minor > e["idr_minor"]:
        raise ValueError(f"receipts cannot exceed the IDR {e['idr_minor']:,} drawn")
    e["receipted_minor"] += amount_minor
    e.setdefault("receipts", []).append({"at": _now(), "idr_minor": amount_minor, "note": note[:200], "by": by})
    e["expense"] = e["receipted_minor"] >= e["idr_minor"]
    org._put(KIND, w, by)
    return e


def due(w: dict, today: dt.date | None = None) -> list[dict]:
    t = (today or dt.date.today()).isoformat()
    return [a for a in w["advances"] if a["status"] == "OPEN" and (not a.get("settle_by") or a["settle_by"] <= t)]


def settle(org, w: dict, request: str, by: str) -> dict:
    """Close one advance: what it still holds leaves the wallet (RETURN) and goes back to the main account; its card
    charges are expense; its draws are expense as far as receipts prove, the rest is owed by the person."""
    a = next((a for a in w["advances"] if a["request"] == request), None)
    if a is None or a["status"] != "OPEN":
        raise ValueError(f"{request} is not an open advance in this wallet")
    left = a["left_minor"]
    if left:  # this advance's own remainder, not the oldest one's
        _entry(w, "RETURN", -left, request, by, allocate=False, advances=[{"request": request, "minor": left}])
        a["returned_minor"] += left
        a["left_minor"] = 0
    draws = [e for e in w["entries"] if e["kind"] == "XENDIT_DRAW" and e.get("status") in ("SENT", "PAID") and any(p["request"] == request for p in e.get("advances", []))]
    unreceipted_idr = sum(e["idr_minor"] - e["receipted_minor"] for e in draws)
    a |= {"status": "SETTLED", "settled_at": _now(), "settled_by": by, "unreceipted_idr_minor": unreceipted_idr}
    org._put(KIND, w, by)
    return a | {"returned_minor": left}


def public(w: dict) -> dict:
    return {k: w[k] for k in ("user_id", "name", "currency", "card_id", "card_masked", "opened_at")} | {
        "balance_minor": balance(w), "card_spent_minor": card_spent(w),
        "drawn_minor": -sum(e["amount_minor"] for e in w["entries"] if e["kind"] == "XENDIT_DRAW" and e.get("status") != "FAILED"),
        "expense_minor": -sum(e["amount_minor"] for e in w["entries"] if e["kind"] == "CARD_SPEND"),
        "advances": w["advances"], "entries": w["entries"][-50:]}


def listing(org) -> list[dict]:
    return sorted(org.list(KIND), key=lambda w: w["name"])
