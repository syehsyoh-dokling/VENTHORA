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


def run_all(tmp_path, mutate=None, approver=None, role="finance_manager"):
    fake = FakeAirwallex(balances={"USD": "1000000", "EUR": "1000000", "GBP": "1000000"})

    async def go():
        client = httpx.AsyncClient(base_url=SANDBOX, transport=httpx.MockTransport(fake.handler))
        agent = Orchestrator(ProofCore(airwallex=Airwallex(base_url=SANDBOX, client=client, backoff_s=0)))
        ex = Executor(agent, ExecutionStore(str(tmp_path / "e.db")), AuditStore(str(tmp_path / "a.db")), poll_interval_s=0)
        audit = AuditStore(str(tmp_path / "a2.db"))
        try:
            cfg = live.defaults("demo_live", ex)
            if mutate:
                mutate(cfg)
            s = await live.start("demo_live", ex, audit, cfg)
            views = [live.view(s)]
            for _ in range(len(live.STEPS) - 1):
                views.append(await live.step(s, ex, audit, approver=approver, role=role))
            return s, views
        finally:
            await agent.close()

    return asyncio.run(go())


def _set(cfg, key, enabled=None, value=None):
    m = next(x for x in cfg["mandates"] if x["key"] == key)
    if enabled is not None:
        m["enabled"] = enabled
    if value is not None:
        m["value"] = value


def test_the_form_shows_every_setting(tmp_path):
    from venthora.execution.store import ExecutionStore as Store
    cfg = live.defaults("demo_live", type("E", (), {"store": Store(str(tmp_path / "s.db"))})())
    keys = {m["key"] for m in cfg["mandates"]}
    assert {"pay_single", "pay_first", "outflow_daily", "convert_fx", "rely_receipts", "budget_auto", "fx_open", "hedge", "overdue", "dunning", "cooling", "duplicates"} <= keys
    assert {r["line"] for r in cfg["budget"]} == {"payroll", "logistics", "suppliers", "contractors", "saas"}
    assert cfg["approvers"] and cfg["approval_matrix"] and cfg["payees"] and cfg["reporting"]["cutoff_day"]
    channels = {r["channel"] for r in cfg["incoming"]}
    assert {"Accounts payable system", "Payroll system", "Email"} <= channels
    assert all(r["date"] is None or r["date"] <= "2026-10-22" for r in cfg["incoming"])


def test_revoking_fx_conversion_sends_the_conversion_to_a_person(tmp_path):
    s, _ = run_all(tmp_path, mutate=lambda c: _set(c, "convert_fx", enabled=False), approver="Dana Finch")
    assert s.views["plan"]["plan"]["decision"] == "REQUIRES_APPROVAL"
    assert "conversion_above_autonomous_limit" in s.views["plan"]["plan"]["requires"]
    assert s.views["goal"]["granted"]["revoked"]


def test_an_unchecked_payee_is_never_paid(tmp_path):
    def mutate(c):
        next(p for p in c["payees"] if p["id"] == "ben-saas-vendor")["allowed"] = False
    s, _ = run_all(tmp_path, mutate=mutate)
    assert {o["id"]: o["action"] for o in s.views["deposit"]["plan"]["obligations"]}["SAAS-USD"] == "ESCALATE"
    assert "SAAS-USD" not in s.views["execute"]["paid"]


def test_only_the_books_named_approvers_may_sign(tmp_path):
    revoke = lambda c: _set(c, "pay_single", enabled=False)  # noqa: E731  every payout now waits for an approver
    s, _ = run_all(tmp_path, mutate=revoke, approver="Pat Lee")
    assert s.views["execute"]["status"] == "REFUSED" and "not an approver of this book" in s.views["execute"]["message"]
    s2, _ = run_all(tmp_path / "b", mutate=revoke, approver="Dana Finch")
    assert s2.views["execute"]["status"] == "COMPLETED"


def test_the_five_elements_end_to_end(tmp_path):
    s, views = run_all(tmp_path)
    V = s.views
    assert V["goal"]["managed"]["USD"] == 3_000_000 and V["goal"]["mandate"]["reserve_floor_minor"] == 1_000_000
    acts = {o["id"]: o["action"] for o in V["plan"]["plan"]["obligations"]}
    assert V["plan"]["plan"]["decision"] == "APPROVED"
    assert acts == {"PAYROLL": "PAY", "SHIPPING-UK": "CONVERT_AND_PAY", "SUPPLIER-EU": "CONVERT_AND_PAY",
                    "SUPPLIER-EU-RESENT": "REJECT", "CONTRACTOR-US": "ESCALATE", "SAAS-USD": "DEFER"}
    assert [u["new_confidence_bp"] for u in V["news"]["reading"]["updates"]] == [7000]
    assert V["revise"]["plan"]["decision"] == "REQUIRES_APPROVAL"
    assert V["revise"]["plan"]["changes"] == [{"id": "SAAS-USD", "before": "DEFER", "after": "ESCALATE",
                                              "reason": "cannot be funded before due without breaching reserve; needs a person"}]
    assert V["deposit"]["deposit"]["landed"] is True and V["deposit"]["plan"]["decision"] == "APPROVED"
    assert [(c["id"], c["before"], c["after"]) for c in V["deposit"]["plan"]["changes"]] == [("SAAS-USD", "ESCALATE", "PAY")]
    e = V["execute"]
    assert e["status"] == "COMPLETED" and e["discrepancies"] == {} and e["floor_ok"] is True
    assert sorted(e["paid"]) == ["PAYROLL", "SAAS-USD", "SHIPPING-UK", "SUPPLIER-EU"]
    assert {t["key"]: t["final"] for t in e["steps"] if t["kind"] == "transfer"} == dict.fromkeys(["PAYROLL", "SHIPPING-UK", "SUPPLIER-EU", "SAAS-USD"], "PAID")
    assert views[-1]["next"] is None and len(s.events) == len(live.STEPS)
    assert all("-" + s.tag not in o["reason"] for o in V["deposit"]["plan"]["obligations"])


def test_a_finished_session_refuses_another_step(tmp_path):
    s, _ = run_all(tmp_path)
    with pytest.raises(live.LiveError):
        asyncio.run(live.step(s, None, None))
