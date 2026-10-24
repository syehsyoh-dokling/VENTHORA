# ruff: noqa: E501
"""The live demo session: the builder guide's five elements, step by step, on the stateful fake sandbox."""
import asyncio

import httpx
import pytest

from venthora import live
from venthora.audit import AuditStore
from venthora.execution.executor import Executor
from venthora.execution.store import ExecutionStore
from venthora.gateways.airwallex import Airwallex
from venthora.orchestrator import Orchestrator
from venthora.proofcore.gate import ProofCore
from venthora.testing.fake_airwallex import FakeAirwallex

SANDBOX = "https://api-demo.airwallex.com"


@pytest.fixture(autouse=True)
def sandbox_credentials(monkeypatch):
    monkeypatch.setenv("AIRWALLEX_CLIENT_ID", "sandbox-client")
    monkeypatch.setenv("AIRWALLEX_API_KEY", "sandbox-key")


def run_all(tmp_path):
    fake = FakeAirwallex(balances={"USD": "1000000", "EUR": "1000000", "GBP": "1000000"})

    async def go():
        client = httpx.AsyncClient(base_url=SANDBOX, transport=httpx.MockTransport(fake.handler))
        agent = Orchestrator(ProofCore(airwallex=Airwallex(base_url=SANDBOX, client=client, backoff_s=0)))
        ex = Executor(agent, ExecutionStore(str(tmp_path / "e.db")), AuditStore(str(tmp_path / "a.db")), poll_interval_s=0)
        audit = AuditStore(str(tmp_path / "a2.db"))
        try:
            s = await live.start("demo_live", ex, audit)
            views = [live.view(s)]
            for _ in range(len(live.STEPS) - 1):
                views.append(await live.step(s, ex, audit))
            return s, views
        finally:
            await agent.close()

    return asyncio.run(go())


def test_the_five_elements_end_to_end(tmp_path):
    s, views = run_all(tmp_path)
    V = s.views
    assert V["goal"]["managed"]["USD"] == 3_000_000 and V["goal"]["mandate"]["reserve_floor_minor"] == 1_000_000
    acts = {o["id"]: o["action"] for o in V["plan"]["plan"]["obligations"]}
    assert V["plan"]["plan"]["decision"] == "APPROVED"
    assert acts == {"PAYROLL-NOV": "PAY", "SHIPPING-UK": "CONVERT_AND_PAY", "SUPPLIER-EU": "CONVERT_AND_PAY",
                    "SUPPLIER-EU-RESENT": "REJECT", "CONTRACTOR-US": "ESCALATE", "SAAS-USD": "DEFER"}
    assert [u["new_confidence_bp"] for u in V["news"]["reading"]["updates"]] == [7000]
    assert V["revise"]["plan"]["decision"] == "REQUIRES_APPROVAL"
    assert V["revise"]["plan"]["changes"] == [{"id": "SAAS-USD", "before": "DEFER", "after": "ESCALATE",
                                              "reason": "cannot be funded before due without breaching reserve; needs a person"}]
    assert V["deposit"]["deposit"]["landed"] is True and V["deposit"]["plan"]["decision"] == "APPROVED"
    assert [(c["id"], c["before"], c["after"]) for c in V["deposit"]["plan"]["changes"]] == [("SAAS-USD", "ESCALATE", "PAY")]
    e = V["execute"]
    assert e["status"] == "COMPLETED" and e["discrepancies"] == {} and e["floor_ok"] is True
    assert sorted(e["paid"]) == ["PAYROLL-NOV", "SAAS-USD", "SHIPPING-UK", "SUPPLIER-EU"]
    assert {t["key"]: t["final"] for t in e["steps"] if t["kind"] == "transfer"} == dict.fromkeys(["PAYROLL-NOV", "SHIPPING-UK", "SUPPLIER-EU", "SAAS-USD"], "PAID")
    assert views[-1]["next"] is None and len(s.events) == len(live.STEPS)
    assert all("-" + s.tag not in o["reason"] for o in V["deposit"]["plan"]["obligations"])


def test_a_finished_session_refuses_another_step(tmp_path):
    s, _ = run_all(tmp_path)
    with pytest.raises(live.LiveError):
        asyncio.run(live.step(s, None, None))
