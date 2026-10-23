"""The differentiators: incremental recalculation, counterfactual proof, AI challenger."""
import asyncio

from fastapi.testclient import TestClient

from conftest import decide, scenario
from venthora.api import app
from venthora.domain.models import TreasuryState
from venthora.engines.llm_planner import llm_proposal, naive_proposal
from venthora.evidence.llm import ChatModel
from venthora.orchestrator import Orchestrator


def run(coro_fn):
    async def go():
        agent = Orchestrator()
        try:
            return await coro_fn(agent)
        finally:
            await agent.close()
    return asyncio.run(go())


def receipt_lands(state: TreasuryState, amount=1_000_000) -> TreasuryState:
    data = state.model_dump()
    data["balances"]["USD"] += amount
    data["receivables"] = []
    return TreasuryState.model_validate(data)


# -- 1. recalculate only the affected decisions ----------------------------------------

def test_new_information_then_deposit_recalculate_only_open_decisions():
    async def go(agent):
        s1 = TreasuryState.model_validate(scenario("kit01_initial"))
        p1 = await agent.decide(s1)
        s2 = TreasuryState.model_validate(scenario("kit01_receipt_delayed"))
        p2 = await agent.decide(s2, previous=(s1, p1))
        s3 = receipt_lands(s2)
        return p2, await agent.decide(s3, previous=(s2, p2))

    p2, p3 = run(go)
    for p in (p2, p3):
        assert p.recalculation.mode == "incremental"
        assert p.recalculation.locked == ["PAYROLL-NOV", "SHIPPING-UK", "SUPPLIER-EU"]
    assert [(c.obligation_id, c.before, c.after) for c in p2.recalculation.changes] == [("SAAS-USD", "DEFER", "ESCALATE")]
    assert [(c.obligation_id, c.before, c.after) for c in p3.recalculation.changes] == [("SAAS-USD", "ESCALATE", "PAY")]
    assert "RCV-ACME forecast 24h/90% -> 144h/70%" in p2.recalculation.triggers
    assert "USD budget +USD 10,000.00" in p3.recalculation.triggers and p3.decision == "APPROVED"


def test_unchanged_information_changes_nothing():
    async def go(agent):
        s = TreasuryState.model_validate(scenario("kit01_initial"))
        p = await agent.decide(s)
        return await agent.decide(s, previous=(s, p))

    r = run(go).recalculation
    assert r.mode == "incremental" and r.changes == [] and r.triggers == []


def test_obligation_whose_own_inputs_change_is_not_locked():
    async def go(agent):
        s1 = TreasuryState.model_validate(scenario("kit01_initial"))
        p1 = await agent.decide(s1)
        data = s1.model_dump()
        data["obligations"][0]["amount_minor"] = 1_500_000  # payroll re-issued higher
        return await agent.decide(TreasuryState.model_validate(data), previous=(s1, p1))

    p2 = run(go)
    assert "PAYROLL-NOV" not in p2.recalculation.locked


def test_falls_back_to_full_recalculation_when_cash_drops():
    async def go(agent):
        s1 = TreasuryState.model_validate(scenario("kit01_initial"))
        p1 = await agent.decide(s1)
        data = s1.model_dump()
        data["balances"]["USD"] = 1_400_000
        return await agent.decide(TreasuryState.model_validate(data), previous=(s1, p1))

    p2 = run(go)
    assert p2.recalculation.mode == "full" and "no longer fits" in p2.recalculation.fallback_reason
    assert p2.total_base_cost_minor == 0 or p2.projected_base_balance_minor >= p2.required_base_floor_minor


# -- 2. counterfactual proof --------------------------------------------------------------

def test_counterfactuals_prove_why_not_the_alternatives():
    r = decide(scenario("kit01_initial"))
    cf = {c.name: c for c in r.counterfactuals}
    assert cf["chosen"].feasible and cf["chosen"].headroom_minor == r.projected_base_balance_minor - r.required_base_floor_minor
    assert not cf["pay_everything"].feasible and cf["pay_everything"].headroom_minor == -22_895
    assert "PAYROLL-NOV" in cf["pay_nothing"].consequence and "SHIPPING-UK" in cf["pay_nothing"].consequence
    assert "twice" in cf["also_pay_SUPPLIER-EU-RESENT"].consequence
    assert "policy exception" in cf["also_pay_CONTRACTOR-US"].consequence and not cf["also_pay_CONTRACTOR-US"].feasible
    assert not cf["receipt_fails_SAAS-USD"].feasible and "unfunded" in cf["receipt_fails_SAAS-USD"].consequence


def test_every_unpaid_decision_has_a_counterfactual():
    r = decide(scenario("kit01_receipt_delayed"))
    names = {c.name for c in r.counterfactuals}
    for d in r.obligations:
        if d.action in ("DEFER", "ESCALATE"):
            assert f"also_pay_{d.obligation_id}" in names


# -- 3. AI challenger ----------------------------------------------------------------------

def test_naive_pay_everything_recommendation_is_blocked_with_reasons():
    async def go(agent):
        s = TreasuryState.model_validate(scenario("kit01_initial"))
        return await agent.challenge(s, naive_proposal(s), "naive assistant")

    r = run(go)
    assert r.decision == "BLOCKED"
    failure = next(s for s in r.stages if s.engine == "ProofCore · invariants")
    for reason in ("unverified obligation selected", "share an invoice fingerprint", "below floor"):
        assert reason in failure.summary


def test_safe_recommendation_passes_the_same_gate():
    async def go(agent):
        s = TreasuryState.model_validate(scenario("kit01_initial"))
        return await agent.challenge(s, ["PAYROLL-NOV"], "LLM")

    assert run(go).decision == "APPROVED"  # the gate refuses what is unsafe, not what comes from an AI


def test_unknown_obligation_in_recommendation_is_blocked():
    async def go(agent):
        s = TreasuryState.model_validate(scenario("kit01_initial"))
        return await agent.challenge(s, ["PAYROLL-NOV", "GHOST-INVOICE"], "LLM")

    assert run(go).decision == "BLOCKED"


def test_llm_planner_falls_back_to_naive_policy_without_model():
    s = TreasuryState.model_validate(scenario("kit01_initial"))
    ids, source = asyncio.run(llm_proposal(s, ChatModel(endpoint="http://127.0.0.1:9", model="x", timeout_s=0.5)))
    assert ids == naive_proposal(s) and "naive" in source


def test_api_replan_and_challenge():
    with TestClient(app) as client:
        initial = client.get("/api/scenarios/kit01_initial").json()["state"]
        delayed = client.get("/api/scenarios/kit01_receipt_delayed").json()["state"]
        p1 = client.post("/v1/plans", json=initial).json()
        p2 = client.post("/v1/plans/replan", json={"state": delayed, "previous_state": initial, "previous_plan": p1}).json()
        assert p2["recalculation"]["mode"] == "incremental" and p2["decision"] == "REQUIRES_APPROVAL"
        c = client.post("/v1/plans/challenge", json={"state": initial, "proposal": [o["obligation_id"] for o in
                                                                                    initial["obligations"]]}).json()
        assert c["decision"] == "BLOCKED" and c["counterfactuals"]
