# ruff: noqa: E501
"""The book ledger: double-entry per book; a book never spends another book's money; the wallet is explained."""
import asyncio
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import scenario
from venthora import ledger
from venthora.audit import AuditStore
from venthora.domain.models import TreasuryState
from venthora.execution.executor import Executor
from venthora.execution.models import ExecutionRequest
from venthora.execution.store import ExecutionStore
from venthora.gateways.airwallex import SANDBOX_URL, Airwallex
from venthora.intake.pipeline import Intake, IntakeStore
from venthora.orchestrator import Orchestrator
from venthora.proofcore.gate import ProofCore
from venthora.testing.fake_airwallex import REAL_SANDBOX_FEES, FakeAirwallex
from venthora.testing.groundtruth import load_cases, run_case

CASES = load_cases("ledger")


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_ledger_case_matches_ground_truth(case, tmp_path):
    async def go():
        agent = Orchestrator()
        try:
            return await run_case(case, agent, Intake(IntakeStore(str(tmp_path / "intake.db"))), tmp_path)
        finally:
            await agent.close()

    r = asyncio.run(go())
    assert r["match"], {k: v for k, v in r["fields"].items() if not v["ok"]}


def test_entries_must_balance_per_currency():
    bad = ledger.Entry("x", "r", [ledger.Line("cash", "USD", -1), ledger.Line("fees", "EUR", 1)])
    with pytest.raises(ledger.LedgerError):
        bad.check()


def _harness(fake, tmp_path):
    client = httpx.AsyncClient(base_url=SANDBOX_URL, transport=httpx.MockTransport(fake.handler))
    bank = Airwallex(base_url=SANDBOX_URL, client=client, backoff_s=0)
    agent = Orchestrator(ProofCore(airwallex=bank))
    store = ExecutionStore(str(tmp_path / "exec.db"))
    return agent, Executor(agent, store, AuditStore(str(tmp_path / "audit.db")), poll_interval_s=0), store


def test_two_books_on_one_wallet_each_reconcile_and_neither_spends_the_other(tmp_path, monkeypatch):
    """The wallet holds both books' money. Book B runs while A's run is 'in progress' (A's before-read is older):
    A's reconciliation explains the wallet's movement by A's AND B's cash lines; and B cannot spend A's share."""
    monkeypatch.setenv("AIRWALLEX_CLIENT_ID", "sandbox-client")
    monkeypatch.setenv("AIRWALLEX_API_KEY", "sandbox-key")
    fake = FakeAirwallex(balances={"USD": "60000", "EUR": "0", "GBP": "0"})
    fake.fees = dict(REAL_SANDBOX_FEES)
    agent, executor, store = _harness(fake, tmp_path)

    def book(name, cash):
        s = scenario("kit01_initial")
        s["book_id"], s["case_id"] = name, name
        s["balances"]["USD"] = cash
        s["policy"]["payout_fees_minor"] = {"SWIFT:EUR": 1337, "LOCAL:USD": 300, "LOCAL:GBP": 224}
        for o in s["obligations"]:
            o["obligation_id"] += "-" + name
            if o.get("invoice_fingerprint"):
                o["invoice_fingerprint"] += "-" + name
        for e in s["evidence"]:
            if not e["subject_id"].startswith("RCV"):
                e["subject_id"] += "-" + name
        return TreasuryState.model_validate(s)

    async def go():
        try:
            a, b = book("A", 2_250_000), book("B", 2_250_000)
            pa, pb = await agent.decide(a), await agent.decide(b)
            ra = await executor.execute(ExecutionRequest(state=a, plan_hash=pa.plan_hash))
            rb = await executor.execute(ExecutionRequest(state=b, plan_hash=pb.plan_hash))
            return ra, rb, store.book_balances("A"), store.book_balances("B")
        finally:
            await agent.close()

    ra, rb, bal_a, bal_b = asyncio.run(go())
    assert ra.status == "COMPLETED", ra.message
    assert rb.status == "COMPLETED", rb.message
    for bal in (bal_a, bal_b):
        assert bal["USD"]["cash"] >= 0 and bal["USD"]["expense"] == 800_000 and bal["USD"]["committed"] == 0
        for acc in bal.values():
            assert sum(acc.values()) == 0  # every book's accounts net to zero: double entry
    # the wallet moved by exactly the sum of both books' cash lines
    wallet_usd = int(fake.balances["USD"] * 100)
    cash_a, cash_b = bal_a["USD"]["cash"], bal_b["USD"]["cash"]
    assert wallet_usd == 6_000_000 - (2 * 2_250_000) + cash_a + cash_b


def test_book_cannot_spend_the_wallets_other_money(tmp_path, monkeypatch):
    monkeypatch.setenv("AIRWALLEX_CLIENT_ID", "sandbox-client")
    monkeypatch.setenv("AIRWALLEX_API_KEY", "sandbox-key")
    fake = FakeAirwallex(balances={"USD": "1000000", "EUR": "0", "GBP": "0"})  # the wallet is rich...
    fake.fees = dict(REAL_SANDBOX_FEES)
    agent, executor, store = _harness(fake, tmp_path)
    s = scenario("kit01_initial")
    s["policy"]["payout_fees_minor"] = {"SWIFT:EUR": 1337, "LOCAL:USD": 300, "LOCAL:GBP": 224}
    state = TreasuryState.model_validate(s)

    async def go():
        try:
            # ...but the book's ledger says it has only USD 100 left (an earlier, recorded spend)
            store.post(state.book_id, ledger.open_book({"USD": 2_250_000}, "allocation"))
            store.post(state.book_id, ledger.adjust("USD", -2_240_000, "earlier spend on this book"))
            plan = await agent.decide(state)
            return await executor.execute(ExecutionRequest(state=state, plan_hash=plan.plan_hash))
        finally:
            await agent.close()

    r = asyncio.run(go())
    assert r.status == "FAILED" and "cannot fund" in r.message and fake.transfers == {}
    assert Decimal(fake.balances["USD"]) == Decimal("1000000")  # nothing moved on the rail


def test_ledger_api(tmp_path, monkeypatch):
    from venthora.api import app
    with TestClient(app) as client:
        r = client.post("/v1/ledger/eval", json={"events": [{"kind": "open", "balances": {"USD": 100}}, {"kind": "commit", "currency": "USD", "amount": 200, "fee": 0}]})
        assert r.status_code == 200 and r.json()["posted"] == 1 and r.json()["refused"][0]["reason"].startswith("would overdraw")
        assert client.get("/v1/ledger").status_code == 200
        assert client.get("/v1/ledger/no-such-book").status_code == 404
        assert client.post("/v1/ledger/no-such-book/adjust", json={"currency": "USD", "amount_minor": -1, "reviewer": "d", "role": "viewer", "note": "x"}).status_code == 403
