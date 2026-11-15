# ruff: noqa: E501
"""A settled customer deposit tops up the book once; nothing else does."""
from datetime import UTC, datetime, timedelta

from venthora import book_funding, ledger
from venthora.execution.store import ExecutionStore


def _store(tmp_path):
    store = ExecutionStore(str(tmp_path / "exec.db"))
    store.post("book-1", ledger.open_book({"USD": 500_000}))
    return store


def _dep(i, amount=100_000, *, customer=True, hours=1, currency="USD"):
    at = (datetime.now(UTC) + timedelta(hours=hours)).isoformat()
    return {"id": f"dep-{i}", "currency": currency, "amount_minor": amount, "payer": "Acme Retail", "customer": customer, "created_at": at}


def test_a_customer_deposit_is_credited_once(tmp_path):
    store = _store(tmp_path)
    posted = book_funding.credit_settled_deposits(store, "book-1", [_dep(1)])
    assert [p["deposit_id"] for p in posted] == ["dep-1"] and store.book_balances("book-1")["USD"]["cash"] == 600_000
    assert book_funding.credit_settled_deposits(store, "book-1", [_dep(1)]) == []  # the same deposit, read again
    assert store.book_balances("book-1")["USD"]["cash"] == 600_000
    book_funding.credit_settled_deposits(store, "book-1", [_dep(1), _dep(2, 50_000, currency="EUR")])
    assert store.book_balances("book-1")["USD"]["cash"] == 600_000 and store.book_balances("book-1")["EUR"]["cash"] == 50_000


def test_what_is_not_credited(tmp_path):
    store = _store(tmp_path)
    skipped = [_dep(1, customer=False),          # a top-up from the company's own bank: the FCO decides
               _dep(2, hours=-48),               # settled before the book opened: already in the allocation
               _dep(3, 0), {"id": None, "currency": "USD", "amount_minor": 5, "customer": True, "created_at": _dep(9)["created_at"]}]
    assert book_funding.credit_settled_deposits(store, "book-1", skipped) == []
    assert store.book_balances("book-1")["USD"]["cash"] == 500_000
    assert book_funding.credit_settled_deposits(store, "no-such-book", [_dep(4)]) == []  # no journal yet: still its allocation
    assert not store.has_book("no-such-book")


def test_a_deposit_from_before_the_watch_began_is_left_to_the_person_who_handled_it(tmp_path):
    class Org:
        rows = {}

        def get(self, kind, rid):
            return self.rows.get((kind, rid))

        def _put(self, kind, rec, by):
            self.rows[(kind, rec["_id"])] = rec

    store, org = _store(tmp_path), Org()
    since = book_funding.watching_since(org, "book-1")
    assert book_funding.watching_since(org, "book-1") == since  # kept on first use, never moved
    assert book_funding.credit_settled_deposits(store, "book-1", [_dep(1, hours=-1)], since) == []
    assert [p["deposit_id"] for p in book_funding.credit_settled_deposits(store, "book-1", [_dep(2, hours=1)], since)] == ["dep-2"]


def test_a_currency_the_book_bought_is_planned_with_before_a_new_conversion():
    import asyncio
    import json
    from pathlib import Path

    from venthora.domain.models import TreasuryState
    from venthora.execution.live import with_live_balances_and_rates

    class Bank:
        async def balances_minor(self):
            return {"USD": 900_000, "EUR": 95_397, "GBP": 40_000}

        async def rate_to_base(self, currency, base):
            return {"EUR": 1.1, "GBP": 1.3}.get(currency, 1)

    raw = json.loads((Path(__file__).resolve().parents[1] / "data" / "scenarios" / "kit01_initial.json").read_text(encoding="utf-8"))["state"]
    state = TreasuryState.model_validate(raw | {"balances": {"USD": 500_000, "EUR": 0, "GBP": 0}})

    def fenced(**kw):
        return asyncio.run(with_live_balances_and_rates(state, Bank(), ring_fence=True, **kw)).balances

    assert fenced() == {"USD": 500_000, "EUR": 0, "GBP": 0}  # no ledger: only the allocation
    assert fenced(book_cash={"USD": 100, "EUR": 95_397})["EUR"] == 95_397  # the book's own euros, bought and unspent
    assert fenced(book_cash={"EUR": 200_000})["EUR"] == 95_397  # never more than the wallet holds
    assert fenced(book_cash={"GBP": 0, "JPY": 5_000}) == {"USD": 500_000, "EUR": 0, "GBP": 0}  # not the wallet's other money
    assert fenced(book_cash={"USD": 900_000})["USD"] == 500_000  # the base currency stays inside the allocation


def test_the_plan_spends_euros_on_hand_instead_of_buying_them_again(tmp_path):
    import asyncio

    from test_execution import WALLET, harness

    from conftest import scenario
    from venthora.domain.models import TreasuryState
    from venthora.testing.fake_airwallex import FakeAirwallex

    async def plan(extra_eur):
        agent, _ = harness(FakeAirwallex(balances=WALLET), tmp_path)
        try:
            raw = scenario("kit01_initial")
            raw["balances"] = raw["balances"] | {"EUR": raw["balances"].get("EUR", 0) + extra_eur}
            return await agent.decide(TreasuryState.model_validate(raw))
        finally:
            await agent.close()

    before, after = asyncio.run(plan(0)), asyncio.run(plan(500_000))
    assert any(c.buy_currency == "EUR" for c in before.conversions)  # nothing on hand: the euros are bought
    assert not any(c.buy_currency == "EUR" for c in after.conversions)  # EUR 5,000 on hand covers the EUR 4,000 supplier
    assert next(d for d in after.obligations if d.obligation_id == "SUPPLIER-EU").action in ("PAY", "CONVERT_AND_PAY")  # still paid
    assert after.total_base_cost_minor < before.total_base_cost_minor  # and no base currency is sold for it
