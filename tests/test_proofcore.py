"""ProofCore refuses plans regardless of who proposed them."""
import random

import pytest

from conftest import decide, scenario
from venthora.domain.money import cost_in_base, rate_ppm, value_in_base


def test_remote_theorem_cannot_pay_rejected_invoice(monkeypatch):
    from venthora.engines import adapters

    async def fake_call(url, payload):
        return {"selected_ids": ["PAYROLL-USD", "SUPPLIER-EU-RESENT"]}

    monkeypatch.setattr(adapters, "call", fake_call)
    monkeypatch.setenv("VENTHORA_THEOREM_URL", "http://127.0.0.1:9/theorem")
    r = decide(scenario("kit01_baseline"))
    assert r.decision == "BLOCKED"
    assert not any(d.action in ("PAY", "CONVERT_AND_PAY") for d in r.obligations)


def test_remote_verity_can_only_narrow(monkeypatch):
    from venthora.engines import adapters

    async def fake_call(url, payload):
        return {"eligible_ids": ["PAYROLL-USD", "SUPPLIER-EU-RESENT"]}

    monkeypatch.setattr(adapters, "call", fake_call)
    monkeypatch.setenv("VENTHORA_VERITY_URL", "http://127.0.0.1:9/verity")
    r = decide(scenario("kit01_baseline"))
    paid = {d.obligation_id for d in r.obligations if d.action in ("PAY", "CONVERT_AND_PAY")}
    assert paid == {"PAYROLL-USD"}


def test_unreachable_remote_engine_blocks(monkeypatch):
    monkeypatch.setenv("VENTHORA_HORIZON_URL", "http://127.0.0.1:9/horizon")
    assert decide(scenario("kit01_baseline")).decision == "BLOCKED"


def test_requested_fx_stress_without_data_blocks():
    s = scenario("kit01_baseline")
    s["use_ecb_stress"] = True
    assert decide(s).decision == "BLOCKED"


@pytest.mark.parametrize("seed", range(40))
def test_random_states_never_breach_reserve(seed):
    rng = random.Random(seed)
    s = scenario("kit01_baseline")
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
    gate = next(st for st in r.stages if st.engine == "ProofCore · invariants")
    assert gate.status == "PASS"
    assert all(v >= 0 for v in gate.details["balances_after"].values())


def test_money_rounding_is_conservative():
    up, down = rate_ppm("1.0850001", round_up=True), rate_ppm("1.0850001", round_up=False)
    assert (up, down) == (1_085_001, 1_085_000)
    assert cost_in_base(1, 1_085_000) == 2  # 1.085 cents costs 2 cents
    assert value_in_base(1, 1_085_000) == 1
    with pytest.raises(ValueError):
        rate_ppm("-1", round_up=True)


def test_solvers_agree_when_ortools_installed():
    pytest.importorskip("ortools")
    from venthora.domain.models import TreasuryState
    from venthora.engines.theorem import _solve_cp_sat, _solve_exact

    state = TreasuryState.model_validate(scenario("kit01_baseline"))
    items = sorted(state.obligations, key=lambda o: o.obligation_id)
    for budget in (0, 500_000, 900_000, 1_400_000, 3_000_000):
        assert sorted(_solve_exact(state, items, budget)[0]) == sorted(_solve_cp_sat(state, items, budget)[0])
