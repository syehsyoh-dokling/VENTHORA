# ruff: noqa: E501
"""The Agent plan sets the balance against everything due in the horizon by priority level: High first, then Medium,
then Low; what the balance above the reserve floor cannot cover is marked as waiting."""
from types import SimpleNamespace

from venthora import agent_today


def test_the_horizon_is_grouped_high_medium_low_and_the_balance_covers_from_the_top():
    state = SimpleNamespace(receivables=[], base_currency="USD")
    rows = [
        {"obligation_id": "pay", "priority": 10, "base_minor": 400_000, "due_in_hours": 10, "category_label": "Wages and salaries"},
        {"obligation_id": "ship", "priority": 8, "base_minor": 200_000, "due_in_hours": 20, "category_label": "Operations-critical suppliers"},
        {"obligation_id": "cons", "priority": 6, "base_minor": 300_000, "due_in_hours": 30, "category_label": "Consultants and contracted services"},
        {"obligation_id": "saas", "priority": 2, "base_minor": 90_000, "due_in_hours": 40, "category_label": "Subscriptions and deferrable costs"},
        {"obligation_id": "late", "priority": 2, "base_minor": 50_000, "due_in_hours": 200, "category_label": "Subscriptions and deferrable costs"},
    ]
    c = agent_today.coverage(state, rows, available=1_000_000, floor=100_000, hours=72)
    lv = {x["level"]: x for x in c["levels"]}
    assert [x["level"] for x in c["levels"]] == ["high", "medium", "low"]
    assert lv["high"]["due_minor"] == 600_000 and lv["high"]["count"] == 2 and lv["high"]["covered"]
    assert lv["medium"]["cumulative_minor"] == 900_000 and lv["medium"]["left_minor"] == 0 and lv["medium"]["covered"]
    assert lv["low"]["due_minor"] == 90_000 and not lv["low"]["covered"] and lv["low"]["left_minor"] == -90_000  # beyond the horizon is left out
    assert "Wages and salaries" in lv["high"]["categories"]
