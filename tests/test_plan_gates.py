# ruff: noqa: E501
"""What the executor would refuse is known before the plan: a payment over its budget allocation or above the transfer
limit the bank showed is left out of the book and carries its reason."""
from types import SimpleNamespace

from venthora import plan_gates


class Org:
    def __init__(self):
        self.rows = {}

    def get(self, kind, rid):
        return self.rows.get((kind, rid))

    def _put(self, kind, rec, by):
        self.rows[(kind, rec["_id"])] = rec


class State:
    base_currency = "USD"

    def __init__(self, obligations):
        self.obligations = obligations

    def ppm_up(self, currency):
        raise KeyError(currency)

    def model_copy(self, update):
        return State(update["obligations"])


def ob(oid, amount, currency="USD", priority=5, code=None, method="LOCAL"):
    return SimpleNamespace(obligation_id=oid, amount_minor=amount, currency=currency, priority=priority, due_in_hours=24,
                           account_code=code, payout_method=method)


LIMIT_ERROR = "The resultant transfer amount is above the maximum limit for this transfer method."


def test_the_smallest_refused_amount_is_kept():
    org = Org()
    assert plan_gates.learn_limit(org, "IDR", "LOCAL", 500, "insufficient Wallet balance") is None  # another failure: nothing learned
    assert plan_gates.learn_limit(None, "IDR", "LOCAL", 500, LIMIT_ERROR) is None  # no organisation store
    plan_gates.learn_limit(org, "IDR", "LOCAL", 1_600, LIMIT_ERROR)
    plan_gates.learn_limit(org, "IDR", "LOCAL", 2_000, LIMIT_ERROR)  # a larger refusal says nothing new
    assert org.get(plan_gates.KIND, "IDR:LOCAL")["refused_minor"] == 1_600
    plan_gates.learn_limit(org, "IDR", "LOCAL", 900, LIMIT_ERROR)
    assert org.get(plan_gates.KIND, "IDR:LOCAL")["refused_minor"] == 900


def test_a_payment_at_the_refused_amount_waits_for_a_split():
    org = Org()
    plan_gates.learn_limit(org, "IDR", "LOCAL", 1_600, LIMIT_ERROR)
    state = State([ob("big", 1_600, "IDR"), ob("small", 400, "IDR"), ob("swift", 1_600, "IDR", method="SWIFT"), ob("usd", 9_999)])
    why = plan_gates.blockers(org, None, state)
    assert list(why) == ["big"] and "split this payment" in why["big"]
    left, refused = plan_gates.apply(org, None, state)
    assert [o.obligation_id for o in left.obligations] == ["small", "swift", "usd"] and refused[0][0].obligation_id == "big"


def test_budget_is_used_most_urgent_first(monkeypatch):
    seen = []

    class Budget:
        def __init__(self, org, store):
            pass

        def check(self, items, month):
            seen.extend(i["obligation_id"] for i in items)
            left, out = 100, []
            for i in items:  # one allocation of 100 for every charge code
                ok = i["base_cost_minor"] <= left
                left -= i["base_cost_minor"] if ok else 0
                out.append({"obligation_id": i["obligation_id"], "ok": ok, "reason": "within" if ok else f"{left} left"})
            return out

    monkeypatch.setattr("venthora.budget_allocation.Budget", Budget)
    state = State([ob("low", 80, priority=3, code="ID.OPS.6700"), ob("high", 60, priority=9, code="ID.OPS.6700"),
                   ob("no-code", 10_000), ob("no-rate", 50, "EUR", code="ID.OPS.6700")])
    why = plan_gates.blockers(Org(), None, state)
    assert seen == ["high", "low"]  # no charge code: not held to a budget; no rate: the plan itself reports it
    assert why == {"low": "budget allocation: 40 left"}
    left, refused = plan_gates.apply(Org(), None, state)
    assert [o.obligation_id for o in left.obligations] == ["high", "no-code", "no-rate"] and len(refused) == 1


def test_nothing_refused_keeps_the_book_and_a_broken_check_never_loses_the_plan(monkeypatch):
    state = State([ob("a", 10)])
    assert plan_gates.apply(Org(), None, state) == (state, [])
    assert plan_gates.apply(None, None, state) == (state, [])
    monkeypatch.setattr(plan_gates, "blockers", lambda *a: 1 / 0)
    assert plan_gates.apply(Org(), None, state) == (state, [])


def test_every_payment_refused_leaves_no_book():
    org = Org()
    plan_gates.learn_limit(org, "IDR", "LOCAL", 100, LIMIT_ERROR)
    left, refused = plan_gates.apply(org, None, State([ob("x", 100, "IDR")]))
    assert left is None and len(refused) == 1
