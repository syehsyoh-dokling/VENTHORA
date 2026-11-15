# ruff: noqa: E501
"""Today's plan is kept for a few seconds, and never served from before a change."""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    from venthora import api

    monkeypatch.setenv("VENTHORA_PLAN_TTL", "60")
    made = []

    async def plan():
        made.append(1)
        return {"plan": None, "n": len(made)}

    monkeypatch.setattr(api, "_agent_plan_now", plan)
    with TestClient(api.app) as c:
        api.app.state.plan_cache = None
        c.made = made
        yield c


def test_a_second_read_gets_the_same_plan_at_once(client):
    assert client.get("/v1/agent-plan").json()["n"] == 1
    assert client.get("/v1/agent-plan").json()["n"] == 1 and len(client.made) == 1
    assert client.get("/v1/agent-plan?fresh=true").json()["n"] == 2  # asked to plan again


def test_any_change_discards_it(client):
    from venthora import api

    assert client.get("/v1/agent-plan").json()["n"] == 1
    client.post("/v1/agent-plan/hold?role=super_admin&by=Rosa", json={"obligation_id": "REQ-X", "note": "wait"})  # a write, whatever it answers
    assert client.get("/v1/agent-plan").json()["n"] == 2
    api.app.state.position_cache = {"at": 0.0, "value": {}}  # a webhook or a payment replaced the wallet reading
    assert client.get("/v1/agent-plan").json()["n"] == 3
    assert client.get("/v1/agent-plan").json()["n"] == 3  # nothing changed since: kept again
    client.get("/v1/balances")  # reading something else changes nothing
    assert client.get("/v1/agent-plan").json()["n"] == 3


def test_it_can_be_switched_off(client, monkeypatch):
    monkeypatch.setenv("VENTHORA_PLAN_TTL", "0")
    assert [client.get("/v1/agent-plan").json()["n"] for _ in range(3)] == [1, 2, 3]
