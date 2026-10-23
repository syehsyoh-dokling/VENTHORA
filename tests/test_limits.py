# ruff: noqa: E501
"""Limits and velocity: how much, to whom, how often — reasons from numbers, mirrored in Rego."""
import asyncio

import pytest

from venthora.intake.pipeline import Intake, IntakeStore
from venthora.orchestrator import Orchestrator
from venthora.proofcore.limits import limit_reasons
from venthora.testing.groundtruth import load_cases, run_case

CASES = load_cases("limits")


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_limit_case_matches_ground_truth(case, tmp_path):
    async def go():
        agent = Orchestrator()
        try:
            return await run_case(case, agent, Intake(IntakeStore(str(tmp_path / "intake.db"))), tmp_path)
        finally:
            await agent.close()

    r = asyncio.run(go())
    assert r["match"], {k: v for k, v in r["fields"].items() if not v["ok"]}


def test_limit_reasons_from_numbers():
    f = {"max_single_payment_minor": 100, "max_per_payee_period_minor": None, "max_outflow_daily_minor": 150,
         "max_outflow_weekly_minor": None, "max_first_payment_minor": 50,
         "payouts": [{"obligation_id": "a", "beneficiary_id": "b", "cost_minor": 101, "new_payee": True}],
         "payees": [{"beneficiary_id": "b", "paid_this_period_minor": 0, "paying_now_minor": 101, "total_minor": 101, "new_payee": True}],
         "paying_now_total_minor": 101, "outflow_24h_minor": 50, "outflow_7d_minor": 50}
    assert limit_reasons(f) == ["daily_outflow_limit_exceeded", "first_payment_to_new_payee_above_limit", "single_payment_above_limit"]
    assert limit_reasons({}) == []


def test_payment_history_from_the_spend_ledger(tmp_path):
    from venthora.execution.store import ExecutionStore
    store = ExecutionStore(str(tmp_path / "e.db"))
    store.book_payment({"transfer_id": "t1", "book_id": "b", "obligation_id": "O1", "currency": "USD", "amount_minor": 100,
                        "base_cost_minor": 103, "paid_at": "2026-10-23T10:00:00+00:00", "reporting_period": "2026-10",
                        "beneficiary_id": "ben-x"})
    store.book_payment({"transfer_id": "t2", "book_id": "b", "obligation_id": "O2", "currency": "USD", "amount_minor": 100,
                        "base_cost_minor": 100, "paid_at": "2026-10-15T10:00:00+00:00", "reporting_period": "2026-10",
                        "beneficiary_id": "ben-y"})
    h = store.payment_history("2026-10-23T12:00:00+00:00", "2026-10")
    assert h["payees_paid_before"] == ["ben-x", "ben-y"]
    assert h["paid_per_payee_period_minor"] == {"ben-x": 103, "ben-y": 100}
    assert h["outflow_24h_minor"] == 103 and h["outflow_7d_minor"] == 103  # t2 is 8 days old
