# ruff: noqa: E501
"""The approval matrix (four-eyes): tiers by amount and reason, different people, never the requester."""
import asyncio

import pytest

from venthora.intake.pipeline import Intake, IntakeStore
from venthora.orchestrator import Orchestrator
from venthora.proofcore.approvals import check, requirement
from venthora.testing.groundtruth import load_cases, run_case

CASES = load_cases("approvals")


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_approval_case_matches_ground_truth(case, tmp_path):
    async def go():
        agent = Orchestrator()
        try:
            return await run_case(case, agent, Intake(IntakeStore(str(tmp_path / "intake.db"))), tmp_path)
        finally:
            await agent.close()

    r = asyncio.run(go())
    assert r["match"], {k: v for k, v in r["fields"].items() if not v["ok"]}


def test_last_applicable_tier_wins():
    tiers = [{"name": "a", "min_total_minor": 0, "reasons": [], "approvals_required": 1, "roles": ["x"]},
             {"name": "b", "min_total_minor": 100, "reasons": ["r"], "approvals_required": 2, "roles": ["y"]}]
    inp = {"approval_matrix": tiers, "approver_roles": ["fm"]}
    assert requirement(inp | {"paying_total_minor": 50}, ["z"])["tier"] == "a"
    assert requirement(inp | {"paying_total_minor": 50}, ["r"])["tier"] == "b"
    assert requirement(inp | {"paying_total_minor": 100}, ["z"])["tier"] == "b"
    assert requirement(inp | {"paying_total_minor": 100}, []) is None
    assert requirement({"approval_matrix": [], "approver_roles": ["fm"], "paying_total_minor": 1}, ["z"]) == {
        "tier": "default", "approvals_required": 1, "roles": ["fm"], "distinct_people": False}


def test_executor_enforces_two_different_people(tmp_path, monkeypatch):
    """A plan above the two-person threshold: one approval, the same person twice and the requester are refused; two people run."""
    import httpx

    from venthora.audit import AuditStore
    from venthora.domain.models import TreasuryState
    from venthora.execution.executor import Executor, terms_digest
    from venthora.execution.models import Approval, ExecutionRequest
    from venthora.execution.store import ExecutionStore
    from venthora.gateways.airwallex import Airwallex
    from venthora.proofcore.gate import ProofCore
    from venthora.testing.fake_airwallex import FakeAirwallex

    monkeypatch.setenv("AIRWALLEX_CLIENT_ID", "sandbox-client")
    monkeypatch.setenv("AIRWALLEX_API_KEY", "sandbox-key")
    fake = FakeAirwallex(balances={"USD": "1000000", "EUR": "0", "GBP": "0"})
    case = next(c for c in CASES if c["id"] == "Q03")
    state = TreasuryState.model_validate(case["input"])

    async def go():
        base_url = "https://api-demo.airwallex.com"
        client = httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(fake.handler))
        agent = Orchestrator(ProofCore(airwallex=Airwallex(base_url=base_url, client=client, backoff_s=0)))
        store = ExecutionStore(str(tmp_path / "e.db"))
        ex = Executor(agent, store, AuditStore(str(tmp_path / "a.db")), poll_interval_s=0)
        try:
            plan = await agent.decide(state)
            assert plan.decision == "REQUIRES_APPROVAL" and plan.approval_requirement["approvals_required"] == 2
            d = terms_digest(plan)
            fm = Approval(approver="Dana Finch", role="finance_manager", terms_digest=d)
            cfo = Approval(approver="Omar Reyes", role="cfo", terms_digest=d)
            one = await ex.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash, approvals=[fm]))
            twice = await ex.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash, approvals=[fm, fm]))
            own = await ex.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash, approvals=[fm, cfo], requested_by="dana finch"))
            both = await ex.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash, approvals=[fm, cfo], requested_by="Treasury agent"))
            return one, twice, own, both
        finally:
            await agent.close()

    one, twice, own, both = asyncio.run(go())
    assert one.status == "REFUSED" and "2 different people" in one.message
    assert twice.status == "REFUSED" and "approved twice" in twice.message
    assert own.status == "REFUSED" and "four-eyes" in own.message
    assert both.status == "COMPLETED"
    assert both.steps[0].detail["approved_by"] == ["Dana Finch", "Omar Reyes"]


def test_check_without_requirement_is_ok():
    assert check(None, [], None, None)["ok"]
