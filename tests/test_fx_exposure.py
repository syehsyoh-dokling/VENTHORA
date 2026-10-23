# ruff: noqa: E501
"""FX exposure limits: open position per currency, daily conversion cap, minimum hedge ratio — mirrored in Rego."""
import asyncio

import pytest

from venthora.intake.pipeline import Intake, IntakeStore
from venthora.orchestrator import Orchestrator
from venthora.proofcore.fx_exposure import fx_reasons
from venthora.testing.groundtruth import load_cases, run_case

CASES = load_cases("fx")


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_fx_case_matches_ground_truth(case, tmp_path):
    async def go():
        agent = Orchestrator()
        try:
            return await run_case(case, agent, Intake(IntakeStore(str(tmp_path / "intake.db"))), tmp_path)
        finally:
            await agent.close()

    r = asyncio.run(go())
    assert r["match"], {k: v for k, v in r["fields"].items() if not v["ok"]}


def test_fx_reasons_from_numbers():
    f = {"rows": [{"currency": "EUR", "position_after_minor": 101, "cap_minor": 100, "coverage_bp": 5000}],
         "min_hedge_ratio_bp": 8000, "max_conversion_daily_minor": 10, "converted_24h_minor": 5, "converting_now_minor": 6}
    assert fx_reasons(f) == ["daily_conversion_cap_exceeded", "hedge_ratio_below_minimum", "open_position_above_cap"]
    assert fx_reasons({"rows": [{"currency": "EUR", "position_after_minor": 10**9, "cap_minor": None, "coverage_bp": None}]}) == []


def test_converted_24h_from_the_journal(tmp_path):
    from venthora import ledger
    from venthora.execution.store import ExecutionStore
    store = ExecutionStore(str(tmp_path / "e.db"))
    store.post("b", ledger.open_book({"USD": 1_000_000}, "allocation"))
    store.post("b", ledger.convert("USD", 435_394, "EUR", 401_285, "fx-1"))
    h = store.payment_history("2099-01-01T00:00:00+00:00", "2099-01")
    assert h["converted_24h_minor"] == 0  # a year later nothing counts
    import datetime as dt
    h = store.payment_history(dt.datetime.now(dt.UTC).isoformat(), "2026-10")
    assert h["converted_24h_minor"] == 435_394
