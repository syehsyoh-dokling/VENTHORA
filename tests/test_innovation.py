"""The three differentiators: incremental recalculation, counterfactual proof, AI challenger."""
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

def test_deposit_recalculates_only_open_decisions():
    async def go(agent):
        s1 = TreasuryState.model_validate(scenario("kit01_receipt_delayed"))
        p1 = await agent.decide(s1)
        s2 = receipt_lands(s1)
        return p1, await agent.decide(s2, previous=(s1, p1))

    p1, p2 = run(go)
    r = p2.recalculation
    assert r.mode == "incremental"
    assert r.locked == ["PAYROLL-USD", "SAAS-USD", "SUPPLIER-EU"]
    assert r.reconsidered == ["VENDOR-UK"]
    assert [(c.obligation_id, c.before, c.after) for c in r.changes] == [("VENDOR-UK", "ESCALATE", "CONVERT_AND_PAY")]
    assert "USD budget +USD 10,000.00" in r.triggers and "RCV-ACME received/settled" in r.triggers


def test_unchanged_information_changes_nothing():
    async def go(agent):
        s = TreasuryState.model_validate(scenario("kit01_baseline"))
        p = await agent.decide(s)
        return await agent.decide(s, previous=(s, p))

    r = run(go).recalculation
    assert r.mode == "incremental" and r.changes == [] and r.triggers == []


def test_obligation_whose_own_inputs_change_is_not_locked():
    async def go(agent):
        s1 = TreasuryState.model_validate(scenario("kit01_baseline"))
        p1 = await agent.decide(s1)
        data = s1.model_dump()
        data["obligations"][4]["amount_minor"] = 5_000_000  # SAAS-USD invoice re-issued at USD 50,000
        return await agent.decide(TreasuryState.model_validate(data), previous=(s1, p1))

    p2 = run(go)
    assert "SAAS-USD" not in p2.recalculation.locked
    assert {d.obligation_id: d.action for d in p2.obligations}["SAAS-USD"] != "PAY"


def test_falls_back_to_full_recalculation_when_cash_drops():
    async def go(agent):
        s1 = TreasuryState.model_validate(scenario("kit01_baseline"))
        p1 = await agent.decide(s1)
        data = s1.model_dump()
        data["balances"]["USD"] = 1_200_000
        return await agent.decide(TreasuryState.model_validate(data), previous=(s1, p1))

    p2 = run(go)
    assert p2.recalculation.mode == "full" and "no longer fits" in p2.recalculation.fallback_reason
    assert p2.projected_base_balance_minor >= p2.required_base_floor_minor or p2.total_base_cost_minor == 0


# -- 2. counterfactual proof --------------------------------------------------------------

def test_counterfactuals_prove_why_not_the_alternatives():
    r = decide(scenario("kit01_baseline"))
    cf = {c.name: c for c in r.counterfactuals}
    assert cf["chosen"].feasible and cf["chosen"].headroom_minor == r.projected_base_balance_minor - r.required_base_floor_minor
    assert not cf["pay_everything"].feasible and cf["pay_everything"].headroom_minor == -336_395
    assert "PAYROLL-USD" in cf["pay_nothing"].consequence
    assert not cf["also_pay_SUPPLIER-EU-RESENT"].feasible and "twice" in cf["also_pay_SUPPLIER-EU-RESENT"].consequence
    assert not cf["receipt_fails_VENDOR-UK"].feasible and "unfunded" in cf["receipt_fails_VENDOR-UK"].consequence


def test_every_unpaid_decision_has_a_counterfactual():
    r = decide(scenario("kit01_receipt_delayed"))
    names = {c.name for c in r.counterfactuals}
    for d in r.obligations:
        if d.action in ("DEFER", "ESCALATE"):
            assert f"also_pay_{d.obligation_id}" in names


# -- 3. AI challenger: a wrong recommendation is blocked ------------------------------------

def test_naive_pay_everything_recommendation_is_blocked_with_reasons():
    async def go(agent):
        s = TreasuryState.model_validate(scenario("kit01_baseline"))
        return await agent.challenge(s, naive_proposal(s), "naive assistant")

    r = run(go)
    assert r.decision == "BLOCKED"
    failure = next(s for s in r.stages if s.engine == "ProofCore · invariants")
    for reason in ("unverified obligation selected", "share an invoice fingerprint", "below floor"):
        assert reason in failure.summary


def test_safe_recommendation_passes_the_same_gate():
    async def go(agent):
        s = TreasuryState.model_validate(scenario("kit01_baseline"))
        return await agent.challenge(s, ["PAYROLL-USD", "SAAS-USD"], "LLM")

    assert run(go).decision == "APPROVED"  # the gate refuses what is unsafe, not what comes from an AI


def test_unknown_obligation_in_recommendation_is_blocked():
    async def go(agent):
        s = TreasuryState.model_validate(scenario("kit01_baseline"))
        return await agent.challenge(s, ["PAYROLL-USD", "GHOST-INVOICE"], "LLM")

    assert run(go).decision == "BLOCKED"


def test_llm_planner_falls_back_to_naive_policy_without_model():
    s = TreasuryState.model_validate(scenario("kit01_baseline"))
    ids, source = asyncio.run(llm_proposal(s, ChatModel(endpoint="http://127.0.0.1:9", model="x", timeout_s=0.5)))
    assert ids == naive_proposal(s) and "naive" in source


def test_api_replan_and_challenge():
    with TestClient(app) as client:
        state = client.get("/api/scenarios/kit01_receipt_delayed").json()["state"]
        p1 = client.post("/v1/plans", json=state).json()
        landed = dict(state, balances=dict(state["balances"], USD=state["balances"]["USD"] + 1_000_000), receivables=[])
        p2 = client.post("/v1/plans/replan", json={"state": landed, "previous_state": state, "previous_plan": p1}).json()
        assert p2["recalculation"]["mode"] == "incremental" and p2["recalculation"]["changes"]
        c = client.post("/v1/plans/challenge", json={"state": state, "proposal": [o["obligation_id"] for o in
                                                                                  state["obligations"]]}).json()
        assert c["decision"] == "BLOCKED" and c["counterfactuals"]
