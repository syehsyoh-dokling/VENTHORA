"""The 72-hour discrete-event reserve simulation (probabilistic proof)."""
import datetime as dt
from pathlib import Path

import numpy as np

from conftest import decide, scenario
from venthora.domain.models import TreasuryState
from venthora.engines.horizon import horizon
from venthora.engines.horizon_m0 import fx_path_factors
from venthora.proofcore.simulate import simulate


def run(state_dict, n=2000, seed=1):
    import asyncio
    state = TreasuryState.model_validate(state_dict)
    h = asyncio.run(horizon(state))
    plan = decide(state_dict)
    return simulate(state, plan.obligations, h.stress_minor, h.receivables, n_paths=n, seed=seed), plan


def test_chosen_plan_has_low_breach_and_unfunded_matches_receipt_risk():
    sim, plan = run(scenario("kit01_initial"))
    # The deterministic plan keeps USD 971 above floor+stress; only a shock about 2x the budgeted
    # stress, landing before the receipt, breaches: a few percent, inside the 5% tolerance.
    assert 0 < sim.breach_probability_bp <= 500
    # SaaS (deferred) goes unfunded when the receipt is missing AND a shock ate the headroom.
    assert 300 <= sim.unfunded_probability_bp["SAAS-USD"] <= 1300
    assert plan.simulation["paths"] == 2000 and plan.simulation["path_events_per_second"] > 0


def test_paying_everything_is_risky_because_stress_can_land_first():
    _, plan = run(scenario("kit01_initial"))
    cf = {c.name: c.breach_probability_bp for c in plan.counterfactuals}
    assert cf["chosen"] <= 500
    assert cf["pay_everything"] >= 2000 and cf["pay_everything"] > cf["chosen"] + 1000
    assert cf["also_pay_SUPPLIER-EU-RESENT"] == 10000  # negative cash at t=0: certain breach


def test_delayed_receipt_moves_risk_out_of_the_window():
    _, plan = run(scenario("kit01_receipt_delayed"))
    cf = {c.name: c.breach_probability_bp for c in plan.counterfactuals}
    assert cf["pay_everything"] >= 6000  # the receipt lands after 72h: most shocks breach


def test_breach_tolerance_is_a_policy_gate():
    s = scenario("kit01_initial")
    s["scenarios"] = [{"name": "chargeback_wave", "unexpected_outflow_minor": 250_000}]
    s["policy"]["reserve_floor_minor"] = 850_000  # thin headroom: stress before the receipt breaches
    r = decide(s)
    assert r.simulation["breach_probability_bp"] > 500
    policy = next(st for st in r.stages if st.engine == "Policy")
    assert "reserve_breach_risk_above_tolerance" in policy.details["local"]["requires_approval"]
    assert r.decision == "REQUIRES_APPROVAL"


def test_simulation_is_seeded_and_reproducible():
    a, _ = run(scenario("kit01_initial"), seed=7)
    b, _ = run(scenario("kit01_initial"), seed=7)
    assert a.breach_probability_bp == b.breach_probability_bp and a.unfunded_probability_bp == b.unfunded_probability_bp


def ecb_fixture(tmp_path) -> Path:
    path = tmp_path / "ecb.csv"
    rows = ["Date,USD,GBP,"]
    day = dt.date(2025, 1, 1)
    for i in range(200):
        if day.weekday() < 5:
            rows.append(f"{day},{1.08 + 0.002 * ((i * 7) % 11 - 5):.4f},{0.85 + 0.001 * ((i * 3) % 7 - 3):.4f},")
        day += dt.timedelta(days=1)
    path.write_text("\n".join(rows), encoding="utf-8")
    return path


def test_horizon_joint_paths_feed_the_simulation(tmp_path, monkeypatch):
    path = ecb_fixture(tmp_path)
    paths = fx_path_factors(("EUR", "GBP"), "USD", path=path)
    assert paths["available"] and paths["factors"].shape == (2000, 2, 3)
    assert np.isfinite(paths["factors"]).all() and (paths["factors"] > 0).all()
    monkeypatch.setenv("VENTHORA_ECB_CSV", str(path))
    s = scenario("kit01_initial")
    s["use_ecb_stress"] = True
    r = decide(s)
    assert r.simulation["fx_model"].startswith("Horizon M0 joint paths")


def test_honest_throughput_accounting():
    sim, _ = run(scenario("kit01_initial"), n=5000)
    assert sim.path_events <= sim.events * sim.paths
    assert sim.loop_ms <= sim.elapsed_ms
    assert abs(sim.path_events_per_second - sim.path_events / (sim.loop_ms / 1000)) < 1_000
