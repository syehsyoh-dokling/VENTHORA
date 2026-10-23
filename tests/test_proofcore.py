"""ProofCore refuses plans regardless of who proposed them; Theorem's frame and second candidate."""
import asyncio
import random

import pytest

from conftest import decide, scenario
from venthora.domain.models import TreasuryState
from venthora.domain.money import cost_in_base, rate_ppm, value_in_base
from venthora.proofcore import frame


def test_remote_theorem_cannot_pay_rejected_invoice(monkeypatch):
    from venthora.engines import adapters

    async def fake_call(url, payload):
        return {"selected_ids": ["PAYROLL-NOV", "SUPPLIER-EU-RESENT"]}

    monkeypatch.setattr(adapters, "call", fake_call)
    monkeypatch.setenv("VENTHORA_THEOREM_URL", "http://127.0.0.1:9/theorem")
    r = decide(scenario("kit01_initial"))
    assert r.decision == "BLOCKED"
    assert not any(d.action in ("PAY", "CONVERT_AND_PAY") for d in r.obligations)


def test_remote_verity_can_only_narrow(monkeypatch):
    from venthora.engines import adapters

    async def fake_call(url, payload):
        return {"eligible_ids": ["PAYROLL-NOV", "SUPPLIER-EU-RESENT", "CONTRACTOR-US"]}

    monkeypatch.setattr(adapters, "call", fake_call)
    monkeypatch.setenv("VENTHORA_VERITY_URL", "http://127.0.0.1:9/verity")
    r = decide(scenario("kit01_initial"))
    paid = {d.obligation_id for d in r.obligations if d.action in ("PAY", "CONVERT_AND_PAY")}
    assert paid == {"PAYROLL-NOV"}


def test_unreachable_remote_engine_blocks(monkeypatch):
    monkeypatch.setenv("VENTHORA_HORIZON_URL", "http://127.0.0.1:9/horizon")
    assert decide(scenario("kit01_initial")).decision == "BLOCKED"


def test_requested_fx_stress_without_data_blocks():
    s = scenario("kit01_initial")
    s["use_ecb_stress"] = True
    assert decide(s).decision == "BLOCKED"


@pytest.mark.parametrize("seed", range(40))
def test_random_states_never_breach_reserve(seed):
    rng = random.Random(seed)
    s = scenario("kit01_initial")
    s["balances"] = {"USD": rng.randint(0, 3_000_000), "EUR": rng.randint(0, 300_000), "GBP": rng.randint(0, 300_000)}
    for o in s["obligations"]:
        o["amount_minor"] = rng.randint(1, 900_000)
        o["priority"] = rng.randint(1, 10)
    s["scenarios"][0]["unexpected_outflow_minor"] = rng.randint(0, 500_000)
    r = decide(s)
    assert r.decision != "BLOCKED"
    if r.total_base_cost_minor > 0:
        assert r.projected_base_balance_minor >= r.required_base_floor_minor
    else:
        assert r.projected_base_balance_minor == s["balances"]["USD"]
    for engine in ("ProofCore · invariants", "Theorem · decision frame"):
        assert next(st for st in r.stages if st.engine == engine).status == "PASS"


def test_money_rounding_is_conservative():
    up, down = rate_ppm("1.0850001", round_up=True), rate_ppm("1.0850001", round_up=False)
    assert (up, down) == (1_085_001, 1_085_000)
    assert cost_in_base(1, 1_085_000) == 2
    assert value_in_base(1, 1_085_000) == 1
    with pytest.raises(ValueError):
        rate_ppm("-1", round_up=True)


def test_solvers_agree_when_ortools_installed():
    pytest.importorskip("ortools")
    from venthora.engines.theorem import _solve_cp_sat, _solve_exact

    state = TreasuryState.model_validate(scenario("kit01_initial"))
    items = sorted(state.obligations, key=lambda o: o.obligation_id)
    for budget in (0, 500_000, 900_000, 1_400_000, 3_000_000):
        assert sorted(_solve_exact(state, items, budget)[0]) == sorted(_solve_cp_sat(state, items, budget)[0])


def test_second_candidate_disagreement_blocks(monkeypatch):
    pytest.importorskip("ortools")
    from venthora.engines import theorem

    monkeypatch.setattr(theorem, "_solve_exact", lambda state, items, budget, locked=frozenset(): ([], "exact"))
    r = decide(scenario("kit01_initial"))
    stage = next(s for s in r.stages if s.engine == "Theorem · optimizer")
    assert stage.status == "FAIL" and r.decision == "BLOCKED"


def test_frame_checker_rejects_a_tampered_answer():
    r = decide(scenario("kit01_initial"))
    state = TreasuryState.model_validate(scenario("kit01_initial"))
    assert frame.check(state, r.frame, r.obligations) == []
    bad = {**r.frame, "answer": {**r.frame["answer"], "conversions": [
        {**r.frame["answer"]["conversions"][0], "buy_amount_minor": 999_999}, *r.frame["answer"]["conversions"][1:]]}}
    assert any("minimum needed" in p for p in frame.check(state, bad, r.obligations))
    incomplete = {k: v for k, v in r.frame.items() if k != "formula"}
    assert "frame section 'formula' is missing" in frame.check(state, incomplete, r.obligations)


def test_policy_exception_cannot_be_paid_even_if_proposed():
    from venthora.orchestrator import Orchestrator

    async def go():
        agent = Orchestrator()
        try:
            return await agent.challenge(TreasuryState.model_validate(scenario("kit01_initial")),
                                         ["PAYROLL-NOV", "CONTRACTOR-US"], "LLM")
        finally:
            await agent.close()

    assert asyncio.run(go()).decision == "BLOCKED"
