# ruff: noqa: E501
"""A customer's payment that has settled tops up the book, by itself.

The book ledger is what the plan may spend (execution/facts.py). It was only ever reduced: payments left it, and a
receipt that landed in the wallet did not come back into it until a person adjusted the book by hand. So the plan
after a deposit was the same as before it.

`credit_settled_deposits` posts one `deposit` entry per settled deposit **from a registered customer**:

  once          the entry's reference is the deposit's Airwallex id; a deposit already in the journal is skipped
  after opening a deposit that settled before the book's first entry is part of the opening allocation, not new money;
                nor is one that settled before VENTHORA began watching the book (a person already dealt with it)
  customers only a top-up from the company's own bank (any payer that is not a registered customer) is not credited:
                how much of the wallet a book may use is the FCO's decision (the allocation, or an adjustment)
  a ceiling     the plan still takes the least of the book's cash, the allocation and the live wallet, so a receipt
                refills the book up to the allocation the FCO ring-fenced, never beyond it

Nothing is posted for a book that has no journal yet: its balance is still its allocation.
"""
from __future__ import annotations

from . import ledger
from .position import _when

KIND = "book_funding"


def watching_since(org, book_id: str) -> float:
    """When VENTHORA began crediting deposits to this book (kept on first use). A deposit that settled before then was
    already handled by a person, in the allocation or by an adjustment, and is never credited again."""
    from datetime import UTC, datetime

    rec = org.get(KIND, book_id)
    if rec is None:
        rec = {"_id": book_id, "since": datetime.now(UTC).isoformat(timespec="seconds")}
        org._put(KIND, rec, "VENTHORA (book funding)")
    return _when(rec["since"])


def credit_settled_deposits(store, book_id: str, deposits: list[dict], since: float = 0.0) -> list[dict]:
    """Post the settled customer deposits not yet in the book. Returns what was posted."""
    if not deposits or not store.has_book(book_id):
        return []
    journal = store.journal(book_id)
    seen = {r["ref"] for r in journal if r["kind"] == "deposit"}
    opened = max(since, min(_when(r["at"]) for r in journal))
    posted = []
    for d in sorted(deposits, key=lambda d: _when(d.get("created_at"))):
        ref = f"deposit:{d.get('id')}"
        if not d.get("customer") or not d.get("id") or ref in seen:
            continue
        if not d.get("currency") or int(d.get("amount_minor") or 0) <= 0 or _when(d.get("created_at")) <= opened:
            continue
        store.post(book_id, ledger.deposit(d["currency"], int(d["amount_minor"]), ref))
        seen.add(ref)
        posted.append({"book_id": book_id, "deposit_id": d["id"], "currency": d["currency"], "amount_minor": int(d["amount_minor"]),
                       "payer": d.get("payer")})
    return posted
