# ruff: noqa: E501
"""After a payment, the stores are copied to the replica; a transfer counts as paid when the bank's status says so."""
import asyncio
import sqlite3

from fastapi.testclient import TestClient

from venthora.execution.models import ExecutionResult, Step


def result(status="COMPLETED"):
    return ExecutionResult(execution_id="ex-1", plan_hash="0" * 64, status=status, message="ok", steps=[
        Step(kind="transfer", key="REQ-1", status="OK", detail={"status": "SCHEDULED"}),
        Step(kind="simulation", key="REQ-1", status="OK", detail={"status": "SENT"}),
        Step(kind="simulation", key="REQ-1", status="OK", detail={"status": "PAID"})])


def test_a_payment_is_counted_and_triggers_the_copy(monkeypatch, tmp_path):
    import venthora.api as api

    replica = tmp_path / "replica.sqlite"
    monkeypatch.setenv("VENTHORA_REPLICA_URL", str(replica))
    with TestClient(api.app) as c:
        events = []
        monkeypatch.setattr(c.app.state.hub, "publish", lambda kind, data: events.append((kind, data)))

        async def go():
            api._execution_done(result())
            for _ in range(50):
                await asyncio.sleep(0.1)
                if replica.exists() and any(e["event_type"] == "stores_replicated" for e in c.app.state.audit.list(20)):
                    return
        asyncio.run(go())
        assert events[0][1]["paid"] == 1  # the SCHEDULED transfer that the bank then reports PAID
        ev = next(e for e in c.app.state.audit.list(20) if e["event_type"] == "stores_replicated")
        assert ev["payload"]["ok"] and ev["payload"]["after"] == "payment ex-1"
        with sqlite3.connect(replica) as r:
            assert r.execute("SELECT COUNT(*) FROM replication_log").fetchone()[0] >= 1
