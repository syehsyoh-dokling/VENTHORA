"""Finance controls: instalments, three-way match, period cut-off, reconciliation cases."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from venthora.intake.pipeline import Intake, IntakeStore
from venthora.orchestrator import Orchestrator
from venthora.testing.groundtruth import load_cases, run_case

CASES = load_cases("controls")


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_control_case_matches_ground_truth(case, tmp_path):
    async def go():
        agent = Orchestrator()
        try:
            return await run_case(case, agent, Intake(IntakeStore(str(tmp_path / "intake.db"))), tmp_path)
        finally:
            await agent.close()

    r = asyncio.run(go())
    assert r["match"], ({k: v for k, v in r["fields"].items() if not v["ok"]}, r["view"].get("errors"), r["view"].get("message"))


def test_period_close_and_recon_case_endpoints(monkeypatch, tmp_path):
    from venthora.api import app
    with TestClient(app) as client:
        # period cut-off statement for the November book, closed by a finance manager
        bad = client.post("/v1/budgets/2026-11/close", json={"scenario_id": "kit01_initial", "as_of": "2026-11-28",
                                                            "reviewer": "x", "role": "viewer"})
        assert bad.status_code == 403
        res = client.post("/v1/budgets/2026-11/close", json={"scenario_id": "kit01_initial", "as_of": "2026-11-28",
                                                            "reviewer": "dana", "role": "finance_manager", "note": "month end"})
        assert res.status_code == 200
        report = res.json()
        assert report["period"] == "2026-11" and {r["line"] for r in report["lines"]} >= {"payroll", "suppliers"}
        assert client.get("/v1/budgets/2026-11/cutoff").status_code == 200
        # a plan against the closed period now needs a person
        plan = client.post("/api/scenarios/kit01_initial/plan").json()
        policy = next(s for s in plan["stages"] if s["engine"] == "Policy")
        assert "period_closed" in policy["details"]["local"]["requires_approval"]
        # reconciliation cases: closing needs the role and a note
        assert client.get("/v1/recon-cases").status_code == 200
        gone = client.post("/v1/recon-cases/nope/close", json={"reviewer": "dana", "role": "finance_manager", "note": "x"})
        assert gone.status_code == 404
        assert client.post("/v1/recon-cases/nope/close", json={"reviewer": "dana", "role": "finance_manager"}).status_code == 422
