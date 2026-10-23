"""Budget · mandate: the period's plan is the agent's mandate, line by line."""
import asyncio

import pytest

from conftest import decide, scenario
from venthora.intake.pipeline import Intake, IntakeStore
from venthora.orchestrator import Orchestrator
from venthora.proofcore import policy
from venthora.proofcore.budget import attach_budget, load_budget
from venthora.testing.groundtruth import load_cases, run_case

CASES = load_cases("budget")


def test_budget_file_loads_and_attaches_to_the_november_book():
    b = load_budget("2026-11")
    assert b and {ln.line for ln in b.lines} == {"payroll", "logistics", "suppliers", "contractors", "saas"}
    from venthora.domain.models import TreasuryState
    state = attach_budget(TreasuryState.model_validate(scenario("kit01_initial")))
    assert state.budget and state.budget.period == "2026-11"
    plan = decide(state.model_dump())
    assert plan.budget["attached"] and {r["line"] for r in plan.budget["lines"]} == {ln.line for ln in b.lines}
    assert any(s.engine == "Budget · mandate" for s in plan.stages)
    assert plan.decision == "APPROVED"  # the November book fits the November plan


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_budget_case_matches_ground_truth(case, tmp_path):
    async def go():
        agent = Orchestrator()
        try:
            return await run_case(case, agent, Intake(IntakeStore(str(tmp_path / "intake.db"))), tmp_path)
        finally:
            await agent.close()

    r = asyncio.run(go())
    assert r["match"], {k: v for k, v in r["fields"].items() if not v["ok"]}


def test_budget_reasons_from_numbers_match_the_rego_semantics():
    def line(**kw):
        base = {"line": "l", "mandate_minor": 1000, "used_minor": 0, "paying_now_minor": 0, "auto_share_bp": 8000,
                "alert_share_bp": 7000, "require_approval_on_alert": False, "bypass": False, "bypass_signed": False}
        return {"attached": True, "unbudgeted_paying": 0, "unbudgeted_requires_approval": True, "lines": [base | kw]}

    assert policy.budget_reasons(line(used_minor=800)) == []
    assert policy.budget_reasons(line(used_minor=801)) == ["budget_line_above_auto_share"]
    assert policy.budget_reasons(line(used_minor=1001)) == ["budget_line_above_auto_share", "budget_line_exceeded"]
    assert policy.budget_reasons(line(used_minor=700, require_approval_on_alert=True)) == ["budget_line_alert_requires_approval"]
    assert policy.budget_reasons(line(used_minor=10, paying_now_minor=10, bypass=True)) == ["budget_bypass_unsigned"]
    assert policy.budget_reasons(line(used_minor=5000, bypass=True, bypass_signed=True)) == []
    assert policy.budget_reasons(line() | {"unbudgeted_paying": 1}) == ["unbudgeted_obligation"]
    assert policy.budget_reasons({"attached": False}) == []
