"""The organizers' failure modes, smallest to largest, with hand-derived answers (data/groundtruth/anomalies.json)."""
import asyncio

import pytest

from venthora.intake.pipeline import Intake, IntakeStore
from venthora.orchestrator import Orchestrator
from venthora.testing.groundtruth import load_cases, run_case

CASES = load_cases("anomalies")


def test_every_case_is_an_anomaly_with_a_pain_and_a_level():
    assert len(CASES) >= 40
    assert all(c["pain"] and c["derivation"] and 1 <= c["level"] <= 6 for c in CASES)
    assert {c["kind"] for c in CASES} == {"plan", "intake", "incident", "execution"}
    assert [c["level"] for c in CASES] == sorted(c["level"] for c in CASES)  # smallest to largest


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_anomaly_matches_ground_truth(case, tmp_path):
    async def go():
        agent = Orchestrator()
        try:
            return await run_case(case, agent, Intake(IntakeStore(str(tmp_path / "intake.db"))), tmp_path)
        finally:
            await agent.close()

    r = asyncio.run(go())
    assert r["match"], ({k: v for k, v in r["fields"].items() if not v["ok"]}, r["view"].get("errors"))


def test_incident_decide_endpoint():
    from fastapi.testclient import TestClient

    from venthora.api import app
    with TestClient(app) as client:
        r = client.post("/v1/incidents/decide", json={"status": "CANCELLED", "failure_type": "BENEFICIARY_BANK_RETURNED"})
        assert r.status_code == 200 and r.json()["action"] == "REPLACE"
        r = client.post("/v1/incidents/decide", json={"status": "SENT", "hours_in_flight": 60})
        assert r.json()["action"] == "ESCALATE"
        assert client.get("/api/groundtruth?suite=anomalies").status_code == 200
        assert client.get("/api/groundtruth?suite=../x").status_code == 404
        assert client.post("/api/groundtruth/N04/check").status_code == 409  # execution cases never run via the service
