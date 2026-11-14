"""Agent plan line-up: every payment of the horizon, High first then earliest due, against the money above the reserve
floor. Green while it fits, the last that fits is the boundary, and everything after waits, even a High one: the line
is never crossed to pay a cheaper, lower one."""
import datetime as dt

from venthora import agent_today, priority_sop


class State:
    receivables = []
    base_currency = "USD"


def _row(oid, prio, base, day):
    d = (dt.date.today() + dt.timedelta(days=day)).isoformat()
    return {"obligation_id": oid, "payee": oid, "priority": prio, "base_minor": base, "amount_minor": base, "currency": "USD",
            "due_date": d, "category_label": "x", "action": "PAY", "ready": True}


def test_lineup_colours_and_the_line_is_never_crossed():
    rows = [_row("low-cheap", 2, 1_000, 0), _row("wages", 10, 60_000, 1), _row("tax", 10, 30_000, 0),
            _row("insurance", 8, 20_000, 1), _row("consultant", 6, 5_000, 0)]
    c = agent_today.coverage(State(), rows, available=150_000, floor=50_000, hours=72)  # 100,000 spendable
    L = c["lineup"]
    assert [x["obligation_id"] for x in L] == ["tax", "wages", "insurance", "consultant", "low-cheap"]
    assert [x["status"] for x in L] == ["paid", "boundary", "waits", "waits", "waits"]  # 1,000 would fit, still waits
    assert [x["level"] for x in L] == ["high", "high", "high", "medium", "low"]
    assert L[1]["left_minor"] == 10_000


def test_a_next_round_payment_is_shown_last_and_never_green():
    rows = [_row("wages", 10, 60_000, 0), _row("overflow-high", 10, 1_000, 0) | {"next_round": True}, _row("low", 2, 1_000, 1)]
    L = agent_today.coverage(State(), rows, available=150_000, floor=50_000, hours=72)["lineup"]
    assert [x["obligation_id"] for x in L] == ["wages", "low", "overflow-high"]
    assert [x["status"] for x in L] == ["paid", "boundary", "waits"] and L[-1]["next_round"]


def test_cash_advance_slush_fund_and_team_gathering_are_low():
    assert priority_sop.ITEMS["cash_advance"][3] == "low"
    for line in ("cash_advance", "slush_fund", "petty_cash"):
        assert priority_sop.LINE_KEY[line] == "cash_advance"
    assert priority_sop.ITEMS[priority_sop.LINE_KEY["team_gathering"]][3] == "low"
