"""Hand-derived ground truth (data/groundtruth/cases.json) must be reproduced by the live loop."""
import asyncio

import pytest

from venthora.intake.pipeline import Intake, IntakeStore
from venthora.orchestrator import Orchestrator
from venthora.testing.groundtruth import compare, load_cases, run_case

CASES = load_cases()


def test_cases_are_many_and_hand_derived():
    assert len(CASES) >= 40
    assert all(c["derivation"] for c in CASES)
    assert len({c["id"] for c in CASES}) == len(CASES)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_case_matches_ground_truth(case, tmp_path):
    async def go():
        agent = Orchestrator()
        try:
            return await run_case(case, agent, Intake(IntakeStore(str(tmp_path / "intake.db"))))
        finally:
            await agent.close()

    r = asyncio.run(go())
    assert r["match"], {k: v for k, v in r["fields"].items() if not v["ok"]}


def test_comparator_semantics():
    assert compare({"actions": {"O1": "PAY"}}, {"actions": {"O1": "PAY", "O2": "DEFER"}})["match"]
    assert not compare({"actions": {"O1": "PAY"}}, {"actions": {"O1": "DEFER"}})["match"]
    assert compare({"requires": ["a"]}, {"requires": ["a", "b"]})["match"]
    assert not compare({"paid": ["O1"]}, {"paid": ["O1", "O2"]})["match"]
    assert compare({"conversions": {"GBP": 60_000}}, {"conversions": {"GBP": 60_000, "EUR": 1}})["match"]
