# ruff: noqa: E501
"""The book ledger: double-entry, per treasury book, in integer minor units.

A wallet is shared; a book is not. Every book keeps its own accounts per currency:
  cash        money of the book that is in the wallet (available)
  committed   money that has left the wallet for a payout not yet PAID (a transfer in flight)
  expense     money paid out for good
  fees        rail fees the book has paid
  fx          the two legs of a conversion (sell leg +, buy leg −; nets to the rate difference)
  funding     where the book's money came from (allocation, deposits, grants); credit-side
Every entry is balanced per currency: the signed amounts of its lines sum to zero. The wallet moves exactly by the
sum of the `cash` lines of ALL books, which is what reconciliation checks: wallet delta − Σ books' cash delta = 0.
A claim that would take a book's cash below zero is refused: a book can never spend another book's money.

TigerBeetle, when configured, mirrors the same entries (see gateways/tigerbeetle.py); this ledger is the one the
agent proves against even without it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

ACCOUNTS = ("cash", "committed", "expense", "fees", "fx", "funding")


class LedgerError(ValueError):
    pass


@dataclass
class Line:
    account: str
    currency: str
    amount: int  # signed minor units


@dataclass
class Entry:
    kind: str
    ref: str
    lines: list[Line] = field(default_factory=list)

    def check(self) -> None:
        for ln in self.lines:
            if ln.account not in ACCOUNTS:
                raise LedgerError(f"unknown account {ln.account}")
            if not isinstance(ln.amount, int):
                raise LedgerError("amounts are integer minor units")
        per_ccy: dict[str, int] = {}
        for ln in self.lines:
            per_ccy[ln.currency] = per_ccy.get(ln.currency, 0) + ln.amount
        unbalanced = {c: v for c, v in per_ccy.items() if v != 0}
        if unbalanced:
            raise LedgerError(f"entry {self.kind} {self.ref} is not balanced: {unbalanced}")

    def as_rows(self) -> list[dict]:
        return [{"account": ln.account, "currency": ln.currency, "amount": ln.amount} for ln in self.lines]


# -- postings (pure constructors) ----------------------------------------------------------------

def open_book(balances: dict[str, int], ref: str = "allocation") -> Entry:
    e = Entry("open", ref)
    for ccy, amt in balances.items():
        if amt:
            e.lines += [Line("cash", ccy, amt), Line("funding", ccy, -amt)]
    return e


def deposit(currency: str, amount: int, ref: str) -> Entry:
    return Entry("deposit", ref, [Line("cash", currency, amount), Line("funding", currency, -amount)])


def grant(currency: str, amount: int, ref: str) -> Entry:
    return Entry("grant", ref, [Line("cash", currency, amount), Line("funding", currency, -amount)])


def convert(sell_currency: str, sell_amount: int, buy_currency: str, buy_amount: int, ref: str) -> Entry:
    return Entry("convert", ref, [Line("cash", sell_currency, -sell_amount), Line("fx", sell_currency, sell_amount),
                                  Line("cash", buy_currency, buy_amount), Line("fx", buy_currency, -buy_amount)])


def commit(currency: str, amount: int, fee: int, ref: str) -> Entry:
    """A transfer was created: the wallet is debited amount + fee; the amount is in flight, the fee is gone."""
    lines = [Line("cash", currency, -amount), Line("committed", currency, amount)]
    if fee:
        lines += [Line("cash", currency, -fee), Line("fees", currency, fee)]
    return Entry("commit", ref, lines)


def settle(currency: str, amount: int, ref: str) -> Entry:
    """The transfer is PAID: in-flight money becomes expense."""
    return Entry("settle", ref, [Line("committed", currency, -amount), Line("expense", currency, amount)])


def refund(currency: str, amount: int, ref: str) -> Entry:
    """The transfer came back: in-flight money is back in the wallet (the fee stays paid)."""
    return Entry("refund", ref, [Line("committed", currency, -amount), Line("cash", currency, amount)])


def adjust(currency: str, amount: int, ref: str) -> Entry:
    """A person's correction (e.g. an unreported bank charge), against funding."""
    return Entry("adjust", ref, [Line("cash", currency, amount), Line("funding", currency, -amount)])


# -- balances and the rules --------------------------------------------------------------------------

def balances(entries: list[dict]) -> dict[str, dict[str, int]]:
    """{currency: {account: balance}} from journal rows ({'lines': [...]})."""
    out: dict[str, dict[str, int]] = {}
    for e in entries:
        for ln in e["lines"]:
            acc = out.setdefault(ln["currency"], dict.fromkeys(ACCOUNTS, 0))
            acc[ln["account"]] += ln["amount"]
    return out


def would_overdraw(entries: list[dict], entry: Entry) -> list[str]:
    """Currencies whose cash would go below zero if the entry were posted (a book never spends another's money)."""
    bal = balances(entries)
    deltas: dict[str, int] = {}
    for ln in entry.lines:
        if ln.account == "cash":
            deltas[ln.currency] = deltas.get(ln.currency, 0) + ln.amount
    return sorted(c for c, d in deltas.items() if d < 0 and bal.get(c, {}).get("cash", 0) + d < 0)


def cash_delta(entries: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in entries:
        for ln in e["lines"]:
            if ln["account"] == "cash":
                out[ln["currency"]] = out.get(ln["currency"], 0) + ln["amount"]
    return out


def evaluate(inp: dict) -> dict:
    """Pure: {'events': [...]} -> balances, what was refused, per-entry balance check. Used by ground truth and the form.
    events: {kind: open|deposit|grant|convert|commit|settle|refund|adjust, ...fields}"""
    journal: list[dict] = []
    refused: list[dict] = []
    builders = {
        "open": lambda ev: open_book(ev["balances"], ev.get("ref", "allocation")),
        "deposit": lambda ev: deposit(ev["currency"], ev["amount"], ev.get("ref", "deposit")),
        "grant": lambda ev: grant(ev["currency"], ev["amount"], ev.get("ref", "grant")),
        "convert": lambda ev: convert(ev["sell_currency"], ev["sell_amount"], ev["buy_currency"], ev["buy_amount"], ev.get("ref", "fx")),
        "commit": lambda ev: commit(ev["currency"], ev["amount"], ev.get("fee", 0), ev.get("ref", "transfer")),
        "settle": lambda ev: settle(ev["currency"], ev["amount"], ev.get("ref", "transfer")),
        "refund": lambda ev: refund(ev["currency"], ev["amount"], ev.get("ref", "transfer")),
        "adjust": lambda ev: adjust(ev["currency"], ev["amount"], ev.get("ref", "adjustment")),
    }
    for i, ev in enumerate(inp.get("events", [])):
        try:
            entry = builders[ev["kind"]](ev)
            entry.check()
        except (KeyError, LedgerError) as exc:
            refused.append({"index": i, "kind": ev.get("kind"), "reason": str(exc)})
            continue
        over = would_overdraw(journal, entry)
        if over:
            refused.append({"index": i, "kind": ev["kind"], "reason": f"would overdraw cash in {', '.join(over)}"})
            continue
        journal.append({"kind": entry.kind, "ref": entry.ref, "lines": entry.as_rows()})
    bal = balances(journal)
    # every currency the events mention has an account, even when nothing touched it (a zero is an answer too)
    for ev in inp.get("events", []):
        for c in [*list((ev.get("balances") or {}).keys()), ev.get("currency"), ev.get("sell_currency"), ev.get("buy_currency")]:
            if c:
                bal.setdefault(c, dict.fromkeys(ACCOUNTS, 0))
    return {"balances": bal, "cash": {c: a["cash"] for c, a in bal.items()}, "committed": {c: a["committed"] for c, a in bal.items()},
            "expense": {c: a["expense"] for c, a in bal.items()}, "fees": {c: a["fees"] for c, a in bal.items()},
            "posted": len(journal), "refused": refused, "all_balanced": all(sum(ln["amount"] for ln in e["lines"] if ln["currency"] == c) == 0
                                                                            for e in journal for c in {ln["currency"] for ln in e["lines"]}),
            "cash_delta": {c: 0 for c in bal} | cash_delta(journal)}
