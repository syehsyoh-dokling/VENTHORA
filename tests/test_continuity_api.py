# ruff: noqa: E501
"""Continuity in the running service: safe mode stops what pays by itself, the writer gate refuses a payment on a
server that is not the writer, and on one server the standby endpoints say plainly that none is connected."""
import pytest
from fastapi.testclient import TestClient

from venthora.continuity import guard
from venthora.continuity.lease import WriterLease

FCO = "role=super_admin&by=Rosa%20Lindqvist"
FM = "role=finance_manager&by=Dana%20Finch"


@pytest.fixture
def client(monkeypatch):
    from venthora.api import app

    for name in ("VENTHORA_CONTINUITY_URL", "VENTHORA_REPLICA_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(guard, "_lease", None)
    with TestClient(app) as c:
        yield c


def test_one_server_says_no_standby_is_connected(client):
    s = client.get("/v1/continuity").json()
    assert s["may_pay"] is True and s["shared_lease"] is False and s["safe_mode"]["on"] is False
    assert s["standby"]["connected"] is False and "No standby is connected in this environment" in s["standby"]["notice"]
    assert s["replica"] == {"configured": False, "last_copy": None} and s["design"] == "docs/CONTINUITY.md"
    for path in ("/v1/continuity/takeover", "/v1/continuity/replicate"):
        r = client.post(f"{path}?{FCO}")
        assert r.status_code == 409 and "VENTHORA_CONTINUITY_URL" in r.json()["detail"]  # what is missing, and how it is switched on
        assert client.post(f"{path}?{FM}").status_code == 403  # the FCO's alone


def test_the_page_is_served_and_linked(client):
    page = client.get("/continuity")
    assert page.status_code == 200 and "Take over as the writer" in page.text and "/v1/continuity/safe-mode" in page.text
    assert 'href="/continuity"' in client.get("/").text


def test_safe_mode_is_the_fcos_and_stops_the_payment_run(client):
    assert client.put(f"/v1/continuity/safe-mode?{FM}", json={"on": True, "reason": "x"}).status_code == 403
    assert client.put(f"/v1/continuity/safe-mode?{FCO}", json={"on": True, "reason": ""}).status_code == 422  # a reason is required
    on = client.put(f"/v1/continuity/safe-mode?{FCO}", json={"on": True, "reason": "a payment went to the wrong account"})
    assert on.status_code == 200 and on.json()["on"] is True and on.json()["by"] == "Rosa Lindqvist"
    assert client.get("/v1/continuity").json()["safe_mode"]["reason"] == "a payment went to the wrong account"
    assert guard.may_pay_automatically(client.app.state.org)[0] is False and guard.may_pay()[0] is True
    assert any(e["event_type"] == "safe_mode_on" for e in client.app.state.audit.list(10))
    off = client.put(f"/v1/continuity/safe-mode?{FCO}", json={"on": False})
    assert off.json()["on"] is False and guard.may_pay_automatically(client.app.state.org)[0] is True


def test_the_executor_refuses_to_pay_on_a_server_that_is_not_the_writer(client, tmp_path, monkeypatch):
    import asyncio

    from conftest import scenario
    from venthora.domain.models import TreasuryState
    from venthora.execution.models import ExecutionRequest

    executor = client.app.state.executor
    assert executor.writer_gate is guard.may_pay  # wired when the service starts
    url = str(tmp_path / "lease.db")
    WriterLease(url, "app-2").acquire()  # the standby took over; this server came back
    monkeypatch.setenv("VENTHORA_CONTINUITY_URL", url)
    monkeypatch.setenv("VENTHORA_NODE", "app-1")
    monkeypatch.setattr(guard, "_lease", None)
    state = TreasuryState.model_validate(scenario("kit01_initial"))
    res = asyncio.run(executor.execute(ExecutionRequest(state=state, plan_hash="0" * 64)))
    assert res.status == "REFUSED" and "app-2 holds the lease" in res.message and res.execution_id is None  # before anything is planned or moved
    assert res.steps[0].key == "writer" and res.steps[0].status == "REFUSED"
    s = client.get("/v1/continuity").json()
    assert s["may_pay"] is False and s["standby"]["connected"] is True and s["lease"]["holder"] == "app-2"


def test_a_takeover_through_the_api_locks_the_banks_payments_and_turns_safe_mode_on(client, tmp_path, monkeypatch):
    url = str(tmp_path / "lease.db")
    monkeypatch.setenv("VENTHORA_CONTINUITY_URL", url)
    monkeypatch.setenv("VENTHORA_NODE", "app-2")
    monkeypatch.setattr(guard, "_lease", None)

    class Bank:
        async def list_transfers(self, pages=2, page_size=100):
            return [{"id": "tr-9", "status": "PAID", "reference": "REQ-SLS-88412 straits-line-88412"}]

    monkeypatch.setattr(client.app.state.executor, "bank", Bank())
    r = client.post(f"/v1/continuity/takeover?{FCO}")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["took_over"] is True and out["node"] == "app-2" and out["safe_mode"]["on"] is True
    assert [x["transfer_id"] for x in out["payments"]["locked"]] == ["tr-9"]
    assert client.app.state.executor.store.payout("invoice:straits-line-88412")["status"] == "PAID"
    assert "the mailbox was not read again here" in out["look_at"][0]  # said, not hidden
    assert client.get("/v1/continuity").json()["safe_mode"]["on"] is True
    client.put(f"/v1/continuity/safe-mode?{FCO}", json={"on": False})
