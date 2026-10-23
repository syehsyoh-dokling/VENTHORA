"""Reporting periods: admin cut-off, payments booked by payment instant, invoice statements, consolidation."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from venthora.intake.pipeline import Intake, IntakeStore
from venthora.orchestrator import Orchestrator
from venthora.reporting import ReportingConfig, cutoff_instant, reporting_period
from venthora.testing.groundtruth import load_cases, run_case

CASES = load_cases("reporting")


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_reporting_case_matches_ground_truth(case, tmp_path):
    async def go():
        agent = Orchestrator()
        try:
            return await run_case(case, agent, Intake(IntakeStore(str(tmp_path / "intake.db"))), tmp_path)
        finally:
            await agent.close()

    r = asyncio.run(go())
    assert r["match"], {k: v for k, v in r["fields"].items() if not v["ok"]}


def test_cutoff_is_inclusive_to_the_minute_and_clamped():
    cfg = ReportingConfig(cutoff_day=31, cutoff_time="23:59", timezone="Asia/Jakarta")
    assert cutoff_instant(2026, 2, cfg).day == 28
    assert reporting_period("2026-02-28T23:59:59+07:00", cfg) == "2026-02"
    assert reporting_period("2026-03-01T00:00:00+07:00", cfg) == "2026-03"


def test_reporting_endpoints(tmp_path, monkeypatch):
    from venthora.api import app
    with TestClient(app) as client:
        cfg = client.get("/v1/reporting/config").json()
        assert cfg["reporting_currency"] and cfg["cutoff_day"]
        res = client.post("/v1/reporting/eval", json={"config": cfg, "posted": "2026-11-26T00:00:00+07:00"})
        assert res.status_code == 200 and res.json()["reporting_period"] == "2026-12"
        rep = client.get("/v1/reporting/periods/2026-11").json()
        assert rep["period"] == "2026-11" and "spend" in rep and rep["window"]["timezone"] == cfg["timezone"]
        assert client.put("/v1/reporting/config?role=viewer", json=cfg).status_code == 403
        assert client.post("/v1/reporting/periods/2026-11/rates?role=viewer", json={"USD": "1"}).status_code == 403
